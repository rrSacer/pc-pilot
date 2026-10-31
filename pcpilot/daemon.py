"""pcpilotd: session broker, exclusive input grab and panic hotkey (runs as root).

Invariants (see the spec artifact "PCPilot — arquitetura e protocolo"):

* Devices are grabbed only inside a session whose UI acked ``state active``
  for *that* session (the overlay is visible), and at least one keyboard able
  to send the hotkey must be grabbed or the session fails closed.
* A grab is accepted only when the compositor believes no key/button of the
  device is held. Each device is opened twice: the grabbing fd, and a
  *witness* fd that is never grabbed. evdev delivers every event batch either
  to the grabber alone or to all clients, so the witness receives exactly what
  the compositor receives and its key state is the compositor's key state.
* Every way a session ends goes through :meth:`Daemon.end_session`, which
  ungrabs *before* telling anyone. If the process dies the kernel drops the
  grabs; a watchdog thread kills the process if the event loop stalls while
  any input fd is open.
* Outside a session no input device is even open.
"""

import argparse
import asyncio
import errno
import itertools
import json
import logging
import os
import pwd
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time

from . import evdev
from . import protocol as P

log = logging.getLogger("pcpilotd")

DEFAULTS = {
    "hotkey": "Ctrl+Alt+Esc",
    "idle_timeout_s": 120,
    "max_session_s": 1800,
    "agent_uids": [0],
    # None = active user of seat0 (logind); or a user name / uid.
    "desktop_user": None,
    "exclude_name_patterns": [],
    # Literal device-name prefix; when set only matching *virtual* devices are
    # captured. Mandatory whenever socket or config are not the defaults.
    "device_name_filter": None,
    # The UI must be the process holding the one listening socket bound to
    # this path (the compositor). "{uid}" is replaced by the desktop uid.
    "ui_wayland_socket": "/run/user/{uid}/wayland-0",
    # Optional extra check (off by default): allowed /proc/<pid>/exe targets.
    "ui_exe_paths": None,
    "ui_ack_timeout_s": 2.0,
    "ui_ping_interval_s": 2.0,
    "ui_ping_misses": 3,
    "release_wait_s": 3.0,
    "ui_timeout_s": 20.0,
    "rescan_interval_s": 1.0,
    "hello_timeout_s": 2.0,
}

_POSITIVE_NUMBERS = (
    "idle_timeout_s",
    "max_session_s",
    "ui_ack_timeout_s",
    "ui_ping_interval_s",
    "release_wait_s",
    "ui_timeout_s",
    "rescan_interval_s",
    "hello_timeout_s",
)

KEY_POLL_S = 0.02  # acquisition polling while keys are held
UNSTICK_GRACE_S = 1.0  # after release_wait_s: synthetic releases for this long, then give up
REGRAB_ATTEMPTS = 5  # grabs undone because a press reached the compositor in the grab window
STALL_EXIT_S = 5.0  # watchdog: loop silent this long with an input fd open -> exit
MAX_UI_ALLOWANCE_S = 300.0
MAX_PUMP_BATCHES = 64
MAX_UNAUTH_CONNS = 16  # connections that have not identified yet
MAX_AGENT_CONNS = 32
MAX_PENDING_UI_OPS = 16
MAX_SEND_BUFFER = 4 << 20  # unread bytes queued for one peer before it is dropped
CLOSE_LINGER_S = 1.0


class ConfigError(Exception):
    pass


def load_config(path, must_exist):
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        if must_exist:
            raise ConfigError(f"config file not found: {path}")
        data = {}
    except (OSError, ValueError, RecursionError) as e:
        raise ConfigError(f"cannot read config {path}: {e}")
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    for key in data:
        if key not in DEFAULTS:
            log.warning("config: unknown key %r ignored", key)
    cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    validate_config(cfg)
    return cfg


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_config(cfg):
    for key in _POSITIVE_NUMBERS:
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(f"{key} must be a positive number")
    if not _is_int(cfg["ui_ping_misses"]) or cfg["ui_ping_misses"] < 1:
        raise ConfigError("ui_ping_misses must be a positive integer")
    uids = cfg["agent_uids"]
    if not isinstance(uids, list) or not all(_is_int(u) for u in uids):
        raise ConfigError("agent_uids must be a list of integers")
    du = cfg["desktop_user"]
    if isinstance(du, bool) or (du is not None and not isinstance(du, (int, str))):
        raise ConfigError("desktop_user must be null, a user name or a uid")
    pats = cfg["exclude_name_patterns"]
    if not isinstance(pats, list) or not all(isinstance(p, str) for p in pats):
        raise ConfigError("exclude_name_patterns must be a list of strings")
    flt = cfg["device_name_filter"]
    if flt is not None and (not isinstance(flt, str) or len(flt) < 3):
        raise ConfigError("device_name_filter must be null or a name prefix of at least 3 characters")
    if not isinstance(cfg["ui_wayland_socket"], str) or not cfg["ui_wayland_socket"].startswith("/"):
        raise ConfigError("ui_wayland_socket must be an absolute path")
    exe = cfg["ui_exe_paths"]
    if exe is not None and (not isinstance(exe, list) or not exe or not all(isinstance(p, str) for p in exe)):
        raise ConfigError("ui_exe_paths must be null or a non-empty list of paths")
    if not isinstance(cfg["hotkey"], str):
        raise ConfigError("hotkey must be a string")
    Hotkey(cfg["hotkey"])  # raises ConfigError


# --- hotkey -----------------------------------------------------------------

_MODIFIERS = {
    "ctrl": (evdev.KEY_LEFTCTRL, evdev.KEY_RIGHTCTRL),
    "control": (evdev.KEY_LEFTCTRL, evdev.KEY_RIGHTCTRL),
    "alt": (evdev.KEY_LEFTALT, evdev.KEY_RIGHTALT),
    "shift": (evdev.KEY_LEFTSHIFT, evdev.KEY_RIGHTSHIFT),
    "super": (evdev.KEY_LEFTMETA, evdev.KEY_RIGHTMETA),
    "meta": (evdev.KEY_LEFTMETA, evdev.KEY_RIGHTMETA),
    "win": (evdev.KEY_LEFTMETA, evdev.KEY_RIGHTMETA),
}

_KEYS = {
    "esc": 1, "escape": 1, "backspace": 14, "tab": 15, "enter": 28, "return": 28,
    "space": 57, "scrolllock": 70, "pause": 119, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109, "insert": 110, "delete": 111,
    "f11": 87, "f12": 88,
}
_KEYS.update({f"f{i}": 58 + i for i in range(1, 11)})
_KEYS.update({c: 16 + i for i, c in enumerate("qwertyuiop")})
_KEYS.update({c: 30 + i for i, c in enumerate("asdfghjkl")})
_KEYS.update({c: 44 + i for i, c in enumerate("zxcvbnm")})
_KEYS.update({str(d): 2 + i for i, d in enumerate("123456789")})
_KEYS["0"] = 11


class Hotkey:
    """``Mod+Mod+Key``; any left/right variant of a modifier counts, and extra
    held modifiers do not prevent the match (it is an escape hatch)."""

    def __init__(self, text):
        parts = [p.strip().lower() for p in text.split("+")]
        if not parts or not all(parts):
            raise ConfigError(f"invalid hotkey {text!r}")
        *mods, key = parts
        try:
            self.groups = [frozenset(_MODIFIERS[m]) for m in mods]
        except KeyError as e:
            raise ConfigError(f"unknown modifier {e.args[0]!r} in hotkey {text!r}")
        if key in _MODIFIERS or key not in _KEYS:
            raise ConfigError(f"unsupported hotkey key {key!r}")
        self.trigger = _KEYS[key]
        self.text = text

    def matches(self, pressed):
        return self.trigger in pressed and all(g & pressed for g in self.groups)

    def can_send(self, info):
        """Whether the device has the keys needed to type the chord."""
        return info.has_key(self.trigger) and all(any(info.has_key(k) for k in g) for g in self.groups)


# --- peer authentication ----------------------------------------------------

SO_PEERPIDFD = getattr(socket, "SO_PEERPIDFD", 77)
NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLMSG_ERROR = 2
NLMSG_DONE = 3
NLM_F_REQUEST = 0x1
NLM_F_DUMP = 0x300
UDIAG_SHOW_VFS = 0x2
UNIX_DIAG_VFS = 1
TCP_LISTEN = 10
_NLMSG = struct.Struct("=IHHII")
_UNIX_DIAG_REQ = struct.Struct("=BBHIII2I")
_UNIX_DIAG_MSG = struct.Struct("=BBBBI2I")
_NLATTR = struct.Struct("=HH")


def peer_credentials(sock):
    pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return pid, uid, gid


def peer_pidfd(sock):
    """A pidfd for the connecting process (pins its identity against pid reuse)."""
    fd = struct.unpack("i", sock.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD, 4))[0]
    os.set_inheritable(fd, False)
    return fd


def pidfd_alive(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return not poller.poll(0)  # a pidfd turns readable when the process exits


def pidfd_pid(fd):
    try:
        with open(f"/proc/self/fdinfo/{fd}", "r", encoding="ascii") as f:
            for line in f:
                if line.startswith("Pid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def seat_active_uid(seat="seat0"):
    try:
        with open(f"/run/systemd/seats/{seat}", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("ACTIVE_UID="):
                    return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return None


def unix_listeners():
    """Listening AF_UNIX sockets bound to a filesystem path, from the kernel
    (NETLINK_SOCK_DIAG/unix_diag): ``[(sock_ino, (vfs_major, vfs_minor, vfs_ino))]``."""
    out = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW | socket.SOCK_CLOEXEC, NETLINK_SOCK_DIAG) as s:
        s.settimeout(2.0)
        req = _UNIX_DIAG_REQ.pack(socket.AF_UNIX, 0, 0, 1 << TCP_LISTEN, 0, UDIAG_SHOW_VFS, 0xFFFFFFFF, 0xFFFFFFFF)
        s.sendto(_NLMSG.pack(_NLMSG.size + len(req), SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST | NLM_F_DUMP, 1, 0) + req, (0, 0))
        while True:
            data = s.recv(1 << 17)
            off = 0
            while off + _NLMSG.size <= len(data):
                length, type_, _flags, _seq, _pid = _NLMSG.unpack_from(data, off)
                if length < _NLMSG.size or off + length > len(data):
                    raise OSError(errno.EPROTO, "malformed sock_diag reply")
                if type_ == NLMSG_DONE:
                    return out
                if type_ == NLMSG_ERROR:
                    code = struct.unpack_from("=i", data, off + _NLMSG.size)[0]
                    raise OSError(-code, "sock_diag: " + os.strerror(-code))
                if type_ == SOCK_DIAG_BY_FAMILY and length >= _NLMSG.size + _UNIX_DIAG_MSG.size:
                    _fam, _typ, state, _pad, ino, _c0, _c1 = _UNIX_DIAG_MSG.unpack_from(data, off + _NLMSG.size)
                    attr, end = off + _NLMSG.size + _UNIX_DIAG_MSG.size, off + length
                    vfs = None
                    while attr + _NLATTR.size <= end:
                        alen, atype = _NLATTR.unpack_from(data, attr)
                        if alen < _NLATTR.size:
                            break
                        if atype & 0x3FFF == UNIX_DIAG_VFS and alen >= _NLATTR.size + 8:
                            vino, vdev = struct.unpack_from("=II", data, attr + _NLATTR.size)
                            vfs = (vdev >> 20, vdev & 0xFFFFF, vino)  # kernel dev_t: 12/20 bits
                        attr += (alen + 3) & ~3
                    if state == TCP_LISTEN and vfs is not None:
                        out.append((ino, vfs))
                off += (length + 3) & ~3


def pid_holds_socket(pid, inode):
    target = f"socket:[{inode}]"
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return False
    for fd in fds:
        try:
            if os.readlink(f"/proc/{pid}/fd/{fd}") == target:
                return True
        except OSError:
            continue
    return False


def pid_exe(pid):
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None
    if exe.endswith(" (deleted)"):
        exe = exe[: -len(" (deleted)")]
    return exe


# --- systemd ----------------------------------------------------------------


class Notifier:
    """sd_notify over a non-blocking datagram socket (no-op without NOTIFY_SOCKET)."""

    def __init__(self):
        addr = os.environ.get("NOTIFY_SOCKET")
        self.sock = None
        if not addr:
            return
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK)
            self.sock.connect(addr)
        except OSError as e:
            log.warning("sd_notify unavailable: %s", e)
            self.sock = None

    def __call__(self, msg):
        if self.sock is not None:
            try:
                self.sock.send(msg.encode())
            except OSError:
                pass


# --- devices ----------------------------------------------------------------


class Captured:
    """One input device opened for a session, through two fds."""

    def __init__(self, info, dev, wit):
        self.info = info
        self.dev = dev  # O_RDWR; the fd we grab. Receives every event.
        self.wit = wit  # witness, never grabbed: receives exactly what the compositor receives
        self.phys = set()  # keys held per the kernel (dev's snapshot + events)
        self.comp = set()  # keys held per the compositor (wit's snapshot + events)
        self.dev_dropping = False  # between SYN_DROPPED and the next SYN_REPORT
        self.wit_dropping = False
        self.state = "acquiring"  # -> "grabbed" | "failed"
        self.closed = False
        self.task = None
        self.warned = False

    def label(self):
        return f"{self.info.devnode} ({self.info.name})"


class _Skip(Exception):
    pass


class DeviceSet:
    """Devices captured by one session. ``close()`` is synchronous and final.

    All key state lives here and is updated in one place (``_ingest_*``),
    from both reader callbacks and acquisition polling, with the hotkey
    evaluated on every snapshot and every press. EVIOCGKEY is only used for
    the initial snapshot and after SYN_DROPPED, because it also discards the
    caller's queued key events.
    """

    def __init__(self, daemon, session):
        self.d = daemon
        self.s = session
        self.cfg = daemon.cfg
        self.loop = daemon.loop
        self.caps = {}  # rdev -> Captured
        self.closed = False
        self._open_errors = set()
        self._capture_errors = {}

    def candidates(self):
        out = {}
        for info in evdev.list_devices():
            ok, _why = evdev.capture_verdict(
                info, self.cfg["exclude_name_patterns"], self.cfg["device_name_filter"]
            )
            if ok:
                out[info.rdev] = info
        return out

    def scan(self):
        """Open new candidate devices, drop vanished ones, restart failed
        acquisitions. Returns the acquire tasks started (caller supervises)."""
        if self.closed:
            return []
        present = self.candidates()
        for rdev, cap in list(self.caps.items()):
            if rdev not in present or present[rdev].name != cap.info.name:
                self._drop(cap, "removed")
                if self.closed:
                    return []
        tasks = []
        for rdev, info in present.items():
            cap = self.caps.get(rdev)
            if cap is None:
                cap = self._open(info)
                if self.closed:  # the chord was already held: session ended
                    return tasks
                if cap is None:
                    continue
            elif cap.state != "failed" or (cap.task and not cap.task.done()):
                continue
            cap.state = "acquiring"
            cap.task = self.loop.create_task(self._acquire(cap))
            tasks.append(cap.task)
        return tasks

    def _open(self, info):
        devs = []
        try:
            for write in (True, False):
                dev = evdev.InputDevice(info.devnode, write=write)
                self.d.open_inputs.add(dev)
                devs.append(dev)
            dev, wit = devs
            # The node must still be the device we classified via sysfs.
            name = dev.name()
            if dev.rdev() != info.rdev or wit.rdev() != info.rdev or name != info.name:
                raise _Skip()
            flt = self.cfg["device_name_filter"]
            if flt and not name.startswith(flt):
                raise _Skip()
            phys, comp = dev.pressed_keys(), wit.pressed_keys()
        except _Skip:
            for dev in devs:
                self.d.close_input(dev)
            return None
        except OSError as e:
            for dev in devs:
                self.d.close_input(dev)
            if info.rdev not in self._open_errors:
                self._open_errors.add(info.rdev)
                log.warning("cannot open %s (%s): %s", info.devnode, info.name, e)
            return None
        cap = Captured(info, dev, wit)
        cap.phys, cap.comp = phys, comp
        self.caps[info.rdev] = cap
        try:
            self.loop.add_reader(dev.fd, self._on_dev_readable, cap)
            self.loop.add_reader(wit.fd, self._on_wit_readable, cap)
        except Exception as e:
            self._drop(cap, f"cannot watch: {e}")
            return None
        self._check_hotkey(cap)  # the chord may already be held when the overlay appears
        return cap

    # --- one place for key state ------------------------------------------

    def _check_hotkey(self, cap):
        if self.d.hotkey.matches(self.pressed_union()):
            log.info("hotkey %s on %s", self.d.hotkey.text, cap.label())
            self.d.end_session(P.R_USER_HOTKEY)
            return True
        return False

    def pressed_union(self):
        out = set()
        for cap in self.caps.values():
            out |= cap.phys
        return out

    def _ingest_dev(self, cap, events):
        """Kernel-side state + hotkey. False once the session/device is gone."""
        for type_, code, value in events:
            if type_ == evdev.EV_SYN:
                if code == evdev.SYN_DROPPED:
                    cap.dev_dropping = True
                elif code == evdev.SYN_REPORT and cap.dev_dropping:
                    cap.dev_dropping = False
                    try:
                        cap.phys = cap.dev.pressed_keys()
                    except OSError as e:
                        self._drop(cap, f"error: {e}")
                        return False
                    if self._check_hotkey(cap):
                        return False
                continue
            if type_ != evdev.EV_KEY or cap.dev_dropping:
                continue
            if value:
                cap.phys.add(code)
                if self._check_hotkey(cap):
                    return False
            else:
                if code not in cap.phys and cap.dev.grabbed:
                    # Cannot happen while tracking is exact (the kernel only emits a
                    # release for a key it had down); the witness covers the compositor.
                    log.warning("%s: release of key %d that was not tracked as held", cap.label(), code)
                cap.phys.discard(code)
        return not (self.closed or cap.closed)

    def _ingest_wit(self, cap, events):
        """Compositor-side state: the witness sees exactly what it sees."""
        for type_, code, value in events:
            if type_ == evdev.EV_SYN:
                if code == evdev.SYN_DROPPED:
                    cap.wit_dropping = True
                elif code == evdev.SYN_REPORT and cap.wit_dropping:
                    cap.wit_dropping = False
                    try:
                        cap.comp = cap.wit.pressed_keys()  # libinput resyncs the same way
                    except OSError as e:
                        self._drop(cap, f"error: {e}")
                        return False
                continue
            if type_ != evdev.EV_KEY or cap.wit_dropping:
                continue
            if value:
                cap.comp.add(code)
            else:
                cap.comp.discard(code)
        return not (self.closed or cap.closed)

    def _on_dev_readable(self, cap):
        if self.closed or cap.closed:
            return
        try:
            events = cap.dev.read_events()
        except OSError as e:
            self._drop(cap, f"read error: {e}")
            return
        self._ingest_dev(cap, events)

    def _on_wit_readable(self, cap):
        if self.closed or cap.closed:
            return
        try:
            events = cap.wit.read_events()
        except OSError as e:
            self._drop(cap, f"read error: {e}")
            return
        self._ingest_wit(cap, events)

    def _pump(self, cap):
        """Process everything queued on both fds now. False if gone."""
        for dev, ingest in ((cap.dev, self._ingest_dev), (cap.wit, self._ingest_wit)):
            for _ in range(MAX_PUMP_BATCHES):
                if self.closed or cap.closed:
                    return False
                try:
                    events = dev.read_events()
                except OSError as e:
                    self._drop(cap, f"read error: {e}")
                    return False
                if not events:
                    break
                if not ingest(cap, events):
                    return False
        return not (self.closed or cap.closed)

    # --- acquisition ------------------------------------------------------

    async def _acquire(self, cap):
        """Grab ``cap`` such that the compositor holds no key of it.

        Ungrabbed, both fds see the same events, so ``phys`` and ``comp`` move
        together: wait (up to ``release_wait_s``) until both are empty, then
        synthesize releases (never for multitouch devices) for up to
        ``UNSTICK_GRACE_S`` more, then give up.

        After the grab, the witness says exactly what reached the compositor
        while the grab was going in. If it holds keys, their releases would now
        come only to us: keep the grab until they are physically released,
        ungrab, and replay exactly those releases. Never accepts with the
        compositor holding a key.
        """
        deadline = time.monotonic() + self.cfg["release_wait_s"]
        give_up = deadline + UNSTICK_GRACE_S
        regrabs = 0
        while True:
            if not self._pump(cap):
                return False
            now = time.monotonic()
            if cap.phys or cap.comp:
                if now >= give_up:
                    return self._fail(cap, f"keys stay pressed: {sorted(cap.phys | cap.comp)}")
                if now >= deadline:
                    if cap.info.is_multitouch:
                        return self._fail(cap, "touch contact still active")
                    self._force_release(cap)
                await asyncio.sleep(KEY_POLL_S)
                continue
            try:
                cap.dev.grab()
            except OSError as e:
                if e.errno == errno.EBUSY and now < give_up:
                    await asyncio.sleep(KEY_POLL_S)
                    continue
                return self._fail(cap, f"cannot grab: {e}")
            if not self._pump(cap):
                return False
            missed = set(cap.comp)  # frozen while we hold the grab: exact
            if not missed:
                cap.state = "grabbed"
                log.info("grabbed %s", cap.label())
                self._capture_errors.pop(cap.info.rdev, None)
                return True
            regrabs += 1
            log.info("%s: %s pressed while grabbing, reconciling (%d)", cap.label(), sorted(missed), regrabs)
            while missed & cap.phys and time.monotonic() < give_up:
                await asyncio.sleep(KEY_POLL_S)
                if not self._pump(cap):
                    return False
            try:
                cap.dev.ungrab()
            except OSError as e:
                self._drop(cap, f"ungrab failed: {e}")
                return False
            released = missed - cap.phys  # their release came only to us
            if released and not cap.info.is_multitouch:
                self._replay_releases(cap, released)
            if regrabs > REGRAB_ATTEMPTS:
                return self._fail(cap, "keys kept changing while grabbing")

    def _force_release(self, cap):
        """While NOT grabbed, after the deadline: release keys still held
        (kernel 1->0, delivered to everyone, the compositor included), and
        replay a release for keys only the compositor still holds."""
        if not cap.warned:
            log.warning("%s: still held after %.1fs, sending synthetic release", cap.label(), self.cfg["release_wait_s"])
            cap.warned = True
        events = []
        for code in sorted(cap.phys):
            events += [(evdev.EV_KEY, code, 0), (evdev.EV_SYN, evdev.SYN_REPORT, 0)]
        self._inject(cap, events)
        if cap.comp - cap.phys:
            self._replay_releases(cap, cap.comp - cap.phys)

    def _replay_releases(self, cap, codes):
        """While NOT grabbed: deliver releases the compositor missed. The kernel
        already has these keys up and drops a bare release as "no change", so
        send press+release; libinput ignores a press of a key it has down and
        applies the release."""
        log.info("%s: replaying missed release of %s", cap.label(), sorted(codes))
        events = []
        for code in sorted(codes):
            events += [(evdev.EV_KEY, code, 1), (evdev.EV_SYN, evdev.SYN_REPORT, 0),
                       (evdev.EV_KEY, code, 0), (evdev.EV_SYN, evdev.SYN_REPORT, 0)]
        self._inject(cap, events)

    def _inject(self, cap, events):
        if not events:
            return
        try:
            cap.dev.write_events(events)
        except OSError as e:
            log.warning("%s: injecting events failed: %s", cap.label(), e)

    def _fail(self, cap, why):
        cap.state = "failed"
        if self._capture_errors.get(cap.info.rdev) != why:
            self._capture_errors[cap.info.rdev] = why
            log.warning("not capturing %s: %s", cap.label(), why)
        try:
            cap.dev.ungrab()
        except OSError:
            pass
        self.d.devices_changed(self.s)
        return False

    # --- queries / teardown -----------------------------------------------

    def grabbed(self):
        return [c for c in self.caps.values() if c.state == "grabbed" and c.dev.grabbed]

    def hotkey_ready(self):
        """At least one grabbed keyboard can type the hotkey."""
        return any(self.d.hotkey.can_send(c.info) for c in self.grabbed())

    def _drop(self, cap, why):
        if self.caps.get(cap.info.rdev) is cap:
            del self.caps[cap.info.rdev]
        self._close_cap(cap)
        log.info("dropped %s: %s", cap.label(), why)
        self._capture_errors.pop(cap.info.rdev, None)
        self.d.devices_changed(self.s)

    def _close_cap(self, cap):
        """Isolated per step: a failure never skips the rest."""
        cap.closed = True
        for dev in (cap.dev, cap.wit):
            if dev.fd >= 0:
                try:
                    self.loop.remove_reader(dev.fd)
                except Exception:
                    pass
        try:
            cap.dev.ungrab()
        except Exception:
            pass
        self.d.close_input(cap.dev)
        self.d.close_input(cap.wit)

    def close(self):
        """Ungrab and close everything. Synchronous; acquire tasks see ``closed``."""
        self.closed = True
        caps, self.caps = list(self.caps.values()), {}
        for cap in caps:
            try:
                self._close_cap(cap)
            except Exception:
                pass
        if caps:
            log.info("released %d device(s)", len(caps))


# --- connections and session ------------------------------------------------


class Conn:
    _ids = itertools.count(1)

    def __init__(self, loop, reader, writer, pid, uid, gid):
        self.n = next(Conn._ids)
        self.loop = loop
        self.reader = reader
        self.writer = writer
        self.pid, self.uid, self.gid = pid, uid, gid
        self.pidfd = None
        self.role = None  # "agent" | "ui"
        self.client = ""
        self.aborted = False
        self.closed = False

    def send(self, msg):
        """Queue a message; a peer that lets MAX_SEND_BUFFER pile up is dropped."""
        if self.closed:
            return
        tr = self.writer.transport
        if tr.is_closing():
            return
        data = P.encode(msg)
        if tr.get_write_buffer_size() + len(data) > MAX_SEND_BUFFER:
            log.warning("%r is not reading; dropping it", self)
            self.abort()
            return
        tr.write(data)

    def close(self):
        """Flush what is queued, but never wait for a peer that does not read."""
        self.closed = True
        tr = self.writer.transport
        try:
            tr.close()
            self.loop.call_later(CLOSE_LINGER_S, tr.abort)
        except Exception:
            pass

    def abort(self):
        self.closed = True
        try:
            self.writer.transport.abort()
        except Exception:
            pass

    def release_pidfd(self):
        if self.pidfd is not None:
            try:
                os.close(self.pidfd)
            except OSError:
                pass
            self.pidfd = None

    def __repr__(self):
        return f"<conn {self.n} {self.role or '?'} pid={self.pid} uid={self.uid} {self.client}>"


STARTING = "starting"
ACTIVE = "active"


class Session:
    def __init__(self, loop, sid, owner, label):
        self.id = sid
        self.owner = owner
        self.label = label
        self.phase = STARTING
        self.ending = False
        self.end_reason = None
        self.ack = loop.create_future()
        self.devices = None
        self.ticker = None
        self.pings_unanswered = 0
        self.started = time.monotonic()
        self.last_activity = self.started

    def touch(self):
        self.last_activity = time.monotonic()


class Pending:
    def __init__(self, conn, id_, op, session_id, timer):
        self.conn = conn
        self.id = id_
        self.op = op
        self.session_id = session_id
        self.timer = timer


def reason_error(reason):
    """Error code for requests cut short by a session ending with ``reason``."""
    if reason in (P.R_USER_HOTKEY, P.R_USER_RELEASE):
        return P.E_USER_ABORTED
    if reason in (P.R_UI_DISCONNECTED, P.R_UI_TIMEOUT):
        return P.E_UI_UNAVAILABLE
    if reason in (P.R_SHUTDOWN, P.R_ERROR, P.R_INPUT_ERROR):
        return P.E_FAILED
    return P.E_NO_SESSION


def _number(v, default=0.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return max(0.0, float(v))


def _sanitize_label(v):
    text = "".join(ch for ch in str(v) if ch.isprintable()).strip()
    return text[:40] or "IA"


class Daemon:
    def __init__(self, cfg, socket_path):
        self.cfg = cfg
        self.socket_path = socket_path
        self.hotkey = Hotkey(cfg["hotkey"])
        self.loop = None
        self.server = None
        self.conns = set()
        self.ui = None
        self.session = None
        self.ui_pending = {}  # daemon-side ui request id -> Pending
        self.open_inputs = set()  # every open /dev/input fd (watchdog + emergency release)
        self._ui_ids = itertools.count(1)
        self._session_ids = itertools.count(1)
        self._stopping = False
        self._stopped = None
        self._socket_ino = None
        self.notify = Notifier()
        self.heartbeat = time.monotonic()
        self._last_watchdog_ping = 0.0

    # --- lifecycle --------------------------------------------------------

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(self._on_loop_exception)
        self._stopped = self.loop.create_future()
        prepare_socket_path(self.socket_path)
        self.server = await asyncio.start_unix_server(
            self._on_client, path=self.socket_path, limit=P.MAX_LINE + 2
        )
        os.chmod(self.socket_path, 0o666)  # auth is SO_PEERCRED, not file mode
        self._socket_ino = os.stat(self.socket_path).st_ino
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.loop.add_signal_handler(sig, self.shutdown, P.R_SHUTDOWN)
        self._beat()
        threading.Thread(target=self._watchdog, name="pcpilotd-watchdog", daemon=True).start()
        log.info(
            "listening on %s (hotkey %s, filter %r)",
            self.socket_path, self.hotkey.text, self.cfg["device_name_filter"],
        )
        self.notify("READY=1")
        try:
            await self._stopped
        finally:
            self.notify("STOPPING=1")
            self.end_session(P.R_SHUTDOWN)
            self.server.close()
            for c in list(self.conns):
                c.close()
            await asyncio.sleep(0.05)  # let short final messages flush; never wait on a peer
            for c in list(self.conns):
                c.abort()
            self._unlink_socket()

    def shutdown(self, reason=P.R_SHUTDOWN):
        if self._stopping:
            return
        self._stopping = True
        log.info("shutting down")
        self.end_session(reason)
        if self._stopped is not None and not self._stopped.done():
            self._stopped.set_result(None)

    def close_input(self, dev):
        try:
            dev.close()
        except Exception:
            pass
        finally:
            self.open_inputs.discard(dev)

    def emergency_release(self):
        """Last-resort cleanup from ``finally`` in main."""
        for dev in list(self.open_inputs):
            self.close_input(dev)

    def _unlink_socket(self):
        try:
            if self._socket_ino is not None and os.stat(self.socket_path).st_ino == self._socket_ino:
                os.unlink(self.socket_path)
        except OSError:
            pass

    def _beat(self):
        now = time.monotonic()
        self.heartbeat = now
        if now - self._last_watchdog_ping >= 2.0:
            self._last_watchdog_ping = now
            self.notify("WATCHDOG=1")
        if not self._stopping:
            self.loop.call_later(0.5, self._beat)

    def _watchdog(self):
        """Exit (the kernel then drops every grab) if the loop stalls while an
        input fd is open. Nothing here may block or depend on logging."""
        msg = b"pcpilotd: event loop stalled with input devices open; exiting to release them\n"
        while True:
            time.sleep(1.0)
            if self.open_inputs and time.monotonic() - self.heartbeat > STALL_EXIT_S:
                try:
                    os.set_blocking(2, False)
                    os.write(2, msg)
                except BaseException:
                    pass
                os._exit(70)

    def _on_loop_exception(self, loop, context):
        exc = context.get("exception")
        log.error("unhandled error: %s", context.get("message"), exc_info=exc)
        if self.session is not None:
            self.end_session(P.R_ERROR)

    def _supervise(self, s, task):
        """A session task that dies (exception or unexpected cancel) ends the session."""

        def done(t):
            if t.cancelled():
                failed = True
            else:
                exc = t.exception()
                failed = exc is not None
                if failed:
                    log.error("session task failed", exc_info=exc)
            if failed and self.session is s:
                self.end_session(P.R_ERROR)

        task.add_done_callback(done)
        return task

    def devices_changed(self, s):
        """After a device dropped or failed: keep the hotkey guarantee."""
        if self.session is s and s.phase == ACTIVE and not s.devices.hotkey_ready():
            log.warning("no grabbed keyboard can send the hotkey anymore")
            self.end_session(P.R_INPUT_ERROR)

    # --- clients ----------------------------------------------------------

    async def _on_client(self, reader, writer):
        try:
            pid, uid, gid = peer_credentials(writer.get_extra_info("socket"))
        except OSError:
            writer.transport.abort()
            return
        conn = Conn(self.loop, reader, writer, pid, uid, gid)
        if uid not in self.cfg["agent_uids"] and uid != self.desktop_uid():
            log.warning("rejected peer pid=%d uid=%d: not an agent or the desktop user", pid, uid)
            conn.abort()
            return
        if sum(1 for c in self.conns if c.role is None) >= MAX_UNAUTH_CONNS:
            log.warning("rejected peer pid=%d: too many unidentified connections", pid)
            conn.abort()
            return
        self.conns.add(conn)
        try:
            while not conn.closed:
                timeout = self.cfg["hello_timeout_s"] if conn.role is None else None
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout)
                except asyncio.TimeoutError:
                    log.warning("%r did not identify in time", conn)
                    break
                except (ValueError, asyncio.LimitOverrunError):
                    if conn.role is not None:
                        conn.send(P.err(None, P.E_BAD_REQUEST, "line too long"))
                    break
                except ConnectionError:
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    msg = P.decode(line)
                except ValueError as e:
                    if conn.role is None:
                        log.warning("%r sent garbage before identifying; dropping it", conn)
                        break
                    conn.send(P.err(None, P.E_BAD_REQUEST, str(e)[:200]))
                    continue
                if conn.role is None:
                    try:
                        self._identify(conn, msg)
                    except Exception:
                        log.exception("error identifying %r", conn)
                        break
                    if conn.role is None:
                        break
                    if conn.role == "ui":
                        continue  # the hello was the identification
                try:
                    self._dispatch(conn, msg)
                except Exception:
                    # A bug in our own handling: fail safe.
                    log.exception("error handling %r", msg.get("op"))
                    if self.session is not None:
                        self.end_session(P.R_ERROR)
                    if "id" in msg:
                        conn.send(P.err(msg.get("id"), P.E_FAILED, "internal error"))
        finally:
            self._on_disconnect(conn)

    def _identify(self, conn, msg):
        """First message decides the role. Leaves ``role`` None to reject."""
        args = msg.get("args")
        if msg.get("op") == "hello" and isinstance(args, dict) and args.get("role") == "ui":
            self._ui_hello(conn, msg)
            return
        if conn.uid not in self.cfg["agent_uids"]:
            log.warning("rejected %r: uid not in agent_uids", conn)
            conn.send(P.err(msg.get("id"), P.E_UNAUTHORIZED, "not an authorized agent"))
            conn.close()
            return
        if sum(1 for c in self.conns if c.role == "agent") >= MAX_AGENT_CONNS:
            conn.send(P.err(msg.get("id"), P.E_BUSY, "too many agent connections"))
            conn.close()
            return
        conn.role = "agent"
        log.info("agent connected: %r", conn)

    def _dispatch(self, conn, msg):
        if conn.role == "ui":
            self._ui_message(conn, msg)
        else:
            self._agent_message(conn, msg)

    def _on_disconnect(self, conn):
        conn.closed = True
        self.conns.discard(conn)
        if conn is self.ui:
            log.info("UI disconnected")
            self.ui = None
            self.end_session(P.R_UI_DISCONNECTED)
            for uid in list(self.ui_pending):
                p = self.ui_pending.pop(uid)
                p.timer.cancel()
                p.conn.send(P.err(p.id, P.E_UI_UNAVAILABLE, "UI disconnected"))
        elif conn.role == "agent":
            log.info("agent disconnected: %r", conn)
            s = self.session
            if s is not None and s.owner is conn:
                self.end_session(P.R_AGENT_DISCONNECTED)
            for uid, p in list(self.ui_pending.items()):
                if p.conn is conn:
                    p.timer.cancel()
                    del self.ui_pending[uid]
        conn.release_pidfd()
        conn.close()  # flushes a queued final reply; aborts after CLOSE_LINGER_S

    # --- UI side ----------------------------------------------------------

    def desktop_uid(self):
        du = self.cfg["desktop_user"]
        if du is None:
            return seat_active_uid()
        if isinstance(du, int):
            return du
        try:
            return pwd.getpwnam(du).pw_uid
        except KeyError:
            return None

    def authorize_ui(self, conn):
        """UI = desktop uid AND the (still running, pidfd-pinned) peer holds the
        one listening socket bound to the compositor's wayland socket file.
        Kernel data only: unix_diag for the listener, /proc/<pid>/fd for
        ownership, the pidfd to rule out pid reuse around the check."""
        duid = self.desktop_uid()
        if duid is None:
            return False, "no active desktop user"
        if conn.uid != duid:
            return False, f"uid {conn.uid} is not the desktop user ({duid})"
        if conn.pidfd is None:
            return False, "no pidfd for the peer"
        if not pidfd_alive(conn.pidfd) or pidfd_pid(conn.pidfd) != conn.pid:
            return False, "peer process is gone"
        path = self.cfg["ui_wayland_socket"].replace("{uid}", str(duid))
        try:
            st = os.stat(path)
        except OSError as e:
            return False, f"cannot stat {path}: {e.strerror}"
        if not stat.S_ISSOCK(st.st_mode):
            return False, f"{path} is not a socket"
        vfs = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino & 0xFFFFFFFF)
        try:
            bound = [ino for ino, v in unix_listeners() if v == vfs]
        except OSError as e:
            return False, f"sock_diag failed: {e}"
        if len(bound) != 1:
            return False, f"{len(bound)} listening sockets bound to {path}"
        if not pid_holds_socket(conn.pid, bound[0]):
            return False, f"pid {conn.pid} does not hold the listener of {path}"
        if not pidfd_alive(conn.pidfd):  # the pid we just read is still that process
            return False, "peer process is gone"
        allowed = self.cfg["ui_exe_paths"]
        if allowed:
            exe = pid_exe(conn.pid)
            if exe is None or os.path.realpath(exe) not in {os.path.realpath(p) for p in allowed}:
                return False, f"exe {exe!r} not allowed"
        return True, ""

    def _ui_hello(self, conn, msg):
        id_ = msg.get("id")
        try:
            conn.pidfd = peer_pidfd(conn.writer.get_extra_info("socket"))
        except OSError as e:
            log.warning("no pidfd for %r: %s", conn, e)
        ok, why = self.authorize_ui(conn)
        if not ok:
            log.warning("rejected UI %r: %s", conn, why)
            conn.send(P.err(id_, P.E_UNAUTHORIZED, why))
            conn.close()
            return
        if self.ui is not None:
            log.warning("rejected UI %r: another UI is connected", conn)
            conn.send(P.err(id_, P.E_BUSY, "another UI is connected"))
            conn.close()
            return
        conn.role = "ui"
        self.ui = conn
        conn.send(P.ok(id_, {"protocol": P.PROTOCOL_VERSION, "hotkey": self.hotkey.text}))
        log.info("UI connected: %r", conn)

    def _drop_ui(self, why):
        ui = self.ui
        log.warning("dropping UI %r: %s", ui, why)
        self.ui = None
        self.end_session(P.R_UI_DISCONNECTED)
        if ui is not None:
            ui.abort()

    def _ui_message(self, conn, msg):
        if "event" in msg:
            s = self.session
            sid = msg.get("session")
            if s is None or not _is_int(sid) or sid != s.id:
                return  # stale (another session) or malformed: ignore
            ev = msg.get("event")
            if ev == P.EV_STATE_ACK and msg.get("active") is True:
                if s.phase == STARTING and not s.ack.done():
                    s.ack.set_result(True)
            elif ev == P.EV_PONG:
                s.pings_unanswered = 0
            return
        id_ = msg.get("id")
        if "ok" in msg:
            self._ui_response(msg)
        elif msg.get("op") == "hello":
            conn.send(P.ok(id_, {"protocol": P.PROTOCOL_VERSION, "hotkey": self.hotkey.text}))
        elif "op" in msg:
            conn.send(P.err(id_, P.E_UNKNOWN_OP, str(msg.get("op"))[:64]))

    def _ui_response(self, msg):
        id_ = msg.get("id")
        if not _is_int(id_):
            return
        p = self.ui_pending.pop(id_, None)
        if p is None:
            return  # late reply after a timeout / session end
        p.timer.cancel()
        s = self.session
        if s is not None and s.id == p.session_id:
            s.touch()
        if msg.get("ok") is True:
            p.conn.send(P.ok(p.id, msg.get("result")))
        else:
            error = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            p.conn.send(P.err(p.id, str(error.get("code") or P.E_FAILED)[:64], str(error.get("message") or "")[:2000]))

    def _ui_timed_out(self, ui_id):
        p = self.ui_pending.pop(ui_id, None)
        if p is None:
            return
        p.conn.send(P.err(p.id, P.E_UI_TIMEOUT, f"UI did not answer {p.op}"))
        s = self.session
        if s is not None and s.id == p.session_id:
            log.warning("UI did not answer %s in time; ending the session", p.op)
            self.end_session(P.R_UI_TIMEOUT)

    def _ui_op_timeout(self, op, args):
        extra = 0.0
        if op == "key.type" and isinstance(args.get("text"), str):
            extra += len(args["text"]) * _number(args.get("delay_ms")) / 1000.0
        extra += _number(args.get("wait_ms")) / 1000.0
        extra += _number(args.get("duration_ms")) / 1000.0
        return self.cfg["ui_timeout_s"] + min(extra, MAX_UI_ALLOWANCE_S)

    # --- agent side -------------------------------------------------------

    def _agent_message(self, conn, msg):
        if "op" not in msg:
            return  # stray event/response from an agent: ignore
        id_ = msg.get("id")
        op = msg.get("op")
        args = msg.get("args")
        if args is None:
            args = {}
        if not isinstance(op, str):
            conn.send(P.err(id_, P.E_BAD_REQUEST, "op must be a string"))
            return
        if not isinstance(args, dict):
            conn.send(P.err(id_, P.E_BAD_ARGS, "args must be an object"))
            return
        if op == "hello":
            client = args.get("client")
            conn.client = _sanitize_label(client) if client else ""
            conn.send(P.ok(id_, {
                "protocol": P.PROTOCOL_VERSION,
                "ui_connected": self.ui is not None,
                "hotkey": self.hotkey.text,
                "session": self.status(conn),
            }))
        elif op == "session.status":
            conn.send(P.ok(id_, self.status(conn)))
        elif op == "session.begin":
            self._begin(conn, id_, args)
        elif op == "session.end":
            s = self.session
            if s is not None and s.owner is conn:
                self.end_session(P.R_AGENT_REQUEST)
            conn.send(P.ok(id_, {"active": False}))
        elif op == "session.release":
            had = self.session is not None
            if had:
                log.info("session released by %r", conn)
                self.end_session(P.R_USER_RELEASE)
            conn.send(P.ok(id_, {"released": had}))
        elif op.startswith("ui.") and len(op) > 3:
            self._forward(conn, id_, op[3:], args)
        else:
            conn.send(P.err(id_, P.E_UNKNOWN_OP, op[:64]))

    def status(self, conn):
        s = self.session
        res = {
            "active": s is not None and s.phase == ACTIVE,
            "starting": s is not None and s.phase == STARTING,
            "owner_is_me": s is not None and s.owner is conn,
            "aborted": conn.aborted,
            "ui_connected": self.ui is not None,
            "idle_timeout_s": self.cfg["idle_timeout_s"],
            "max_session_s": self.cfg["max_session_s"],
            "remaining_s": None,
            "idle_remaining_s": None,
            "max_remaining_s": None,
            "label": s.label if s is not None else None,
        }
        if s is not None and s.phase == ACTIVE:
            now = time.monotonic()
            idle = max(0.0, self.cfg["idle_timeout_s"] - (now - s.last_activity))
            mx = max(0.0, self.cfg["max_session_s"] - (now - s.started))
            res["idle_remaining_s"] = round(idle, 1)
            res["max_remaining_s"] = round(mx, 1)
            res["remaining_s"] = round(min(idle, mx), 1)
        return res

    def _forward(self, conn, id_, op, args):
        if conn.aborted:
            conn.send(P.err(id_, P.E_USER_ABORTED, "the user took control; wait for the user"))
            return
        s = self.session
        if s is None or s.owner is not conn or s.phase != ACTIVE:
            conn.send(P.err(id_, P.E_NO_SESSION, "no active session (call session.begin)"))
            return
        if self.ui is None:
            conn.send(P.err(id_, P.E_UI_UNAVAILABLE, "UI not connected"))
            return
        if len(self.ui_pending) >= MAX_PENDING_UI_OPS:
            conn.send(P.err(id_, P.E_BUSY, "too many UI operations in flight"))
            return
        ui_id = next(self._ui_ids)
        timer = self.loop.call_later(self._ui_op_timeout(op, args), self._ui_timed_out, ui_id)
        self.ui_pending[ui_id] = Pending(conn, id_, op, s.id, timer)
        s.touch()
        self.ui.send({"id": ui_id, "op": op, "args": args, "session": s.id})

    def _begin(self, conn, id_, args):
        """Reserve the session synchronously, so an end/release/EOF processed
        right after this request always finds (and cancels) it."""
        resume = args.get("resume") is True
        if conn.aborted and not resume:
            conn.send(P.err(id_, P.E_USER_ABORTED, "the user took control; pass resume:true only if the user asked to continue"))
            return
        if self._stopping:
            conn.send(P.err(id_, P.E_FAILED, "daemon is shutting down"))
            return
        s = self.session
        if s is not None:
            if s.owner is conn and s.phase == ACTIVE:
                conn.send(P.ok(id_, {"active": True}))
            else:
                conn.send(P.err(id_, P.E_BUSY, "another session is active or starting"))
            return
        if self.ui is None:
            conn.send(P.err(id_, P.E_UI_UNAVAILABLE, "UI not connected"))
            return
        ok, why = self.authorize_ui(self.ui)  # identity + liveness, again
        if not ok:
            self._drop_ui(why)
            conn.send(P.err(id_, P.E_UI_UNAVAILABLE, "UI failed re-validation"))
            return

        s = Session(self.loop, next(self._session_ids), conn, _sanitize_label(args.get("label") or conn.client or "IA"))
        self.session = s
        log.info("session %d starting for %r (%s)", s.id, conn, s.label)
        self.ui.send(P.event(P.EV_STATE, active=True, label=s.label, hotkey=self.hotkey.text, session=s.id))
        self._supervise(s, self.loop.create_task(self._begin_wait(s, conn, id_, resume)))

    async def _begin_wait(self, s, conn, id_, resume):
        try:
            result = await self._begin_steps(s, conn, resume)
        except Exception:
            log.exception("session.begin failed")
            if self.session is s:
                self.end_session(P.R_ERROR)
            result = (P.E_FAILED, "internal error")
        if isinstance(result, tuple):
            conn.send(P.err(id_, *result))
        else:
            conn.send(P.ok(id_, result))

    async def _begin_steps(self, s, conn, resume):
        def ended():
            return (reason_error(s.end_reason), f"session ended: {s.end_reason}")

        await asyncio.wait([s.ack], timeout=self.cfg["ui_ack_timeout_s"])
        if s.ending:
            return ended()
        if not s.ack.done():
            log.warning("UI did not ack session %d in time", s.id)
            self.end_session(P.R_UI_TIMEOUT)
            return (P.E_UI_UNAVAILABLE, "UI did not show the overlay in time")

        # Overlay is visible for this session: now (and only now) take the devices.
        s.devices = DeviceSet(self, s)
        tasks = s.devices.scan()
        results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
        if s.ending:
            return ended()
        for r in results:
            if isinstance(r, BaseException):
                raise r
        if not s.devices.hotkey_ready():
            log.warning("no keyboard able to send %s could be grabbed", self.hotkey.text)
            self.end_session(P.R_INPUT_ERROR)
            return (P.E_FAILED, f"no keyboard able to send {self.hotkey.text} could be captured")
        s.phase = ACTIVE
        s.started = s.last_activity = time.monotonic()
        if resume:
            conn.aborted = False
        s.ticker = self._supervise(s, self.loop.create_task(self._ticker(s)))
        log.info("session %d active (%d device(s) grabbed)", s.id, len(s.devices.grabbed()))
        return {"active": True}

    async def _ticker(self, s):
        cfg = self.cfg
        now = time.monotonic()
        next_scan = now + cfg["rescan_interval_s"]
        next_ping = now + cfg["ui_ping_interval_s"]
        while self.session is s:
            now = time.monotonic()
            idle_left = cfg["idle_timeout_s"] - (now - s.last_activity)
            max_left = cfg["max_session_s"] - (now - s.started)
            if idle_left <= 0:
                self.end_session(P.R_IDLE_TIMEOUT)
                return
            if max_left <= 0:
                self.end_session(P.R_MAX_DURATION)
                return
            if now >= next_ping:
                if s.pings_unanswered >= cfg["ui_ping_misses"]:
                    log.warning("UI missed %d pings; ending the session", s.pings_unanswered)
                    self.end_session(P.R_UI_TIMEOUT)
                    return
                s.pings_unanswered += 1
                if self.ui is not None:
                    self.ui.send(P.event(P.EV_PING, session=s.id))
                next_ping = now + cfg["ui_ping_interval_s"]
            if now >= next_scan:
                for t in s.devices.scan():  # hotplug
                    self._supervise(s, t)
                next_scan = now + cfg["rescan_interval_s"]
                if self.session is not s:
                    return
            await asyncio.sleep(max(0.01, min(idle_left, max_left, next_scan - now, next_ping - now, 0.5)))

    # --- the one way out --------------------------------------------------

    def end_session(self, reason):
        """End the current session (any phase). Synchronous and idempotent.

        Order: ungrab and close every device (the watchdog keeps guarding while
        any fd is open), then UI ``state inactive``, then fail pending
        requests, then ``session_ended`` to the owner.
        """
        s = self.session
        if s is None or s.ending:
            return
        s.ending = True
        s.end_reason = reason
        try:
            if s.devices is not None:
                s.devices.close()
        except Exception:
            log.exception("error while releasing devices")
        finally:
            self.session = None
        if s.ticker is not None and s.ticker is not asyncio.current_task():
            s.ticker.cancel()
        if not s.ack.done():
            s.ack.set_result(False)  # wakes a begin waiting for the ack
        if self.ui is not None:
            self.ui.send(P.event(P.EV_STATE, active=False, reason=reason, session=s.id))
        if reason in (P.R_USER_HOTKEY, P.R_USER_RELEASE):
            for c in self.conns:
                if c.role == "agent":
                    c.aborted = True
        code = reason_error(reason)
        for ui_id in list(self.ui_pending):
            p = self.ui_pending.pop(ui_id)
            p.timer.cancel()
            p.conn.send(P.err(p.id, code, f"session ended: {reason}"))
        if s.phase == ACTIVE and not s.owner.closed:
            s.owner.send(P.event("session_ended", reason=reason))
        log.info("session %d ended: %s", s.id, reason)


def prepare_socket_path(path):
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o755, exist_ok=True)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError(f"{path} exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        os.unlink(path)  # stale socket from a dead daemon
        return
    finally:
        probe.close()
    raise RuntimeError(f"another daemon is already listening on {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pcpilotd", description="PCPilot daemon")
    ap.add_argument("--socket", help="socket path (default: $PCPILOT_SOCKET or %s)" % P.DEFAULT_SOCKET)
    ap.add_argument("--config", help="config path (default: %s)" % P.DEFAULT_CONFIG)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    fmt = "%(levelname)s %(message)s" if "JOURNAL_STREAM" in os.environ else "%(asctime)s pcpilotd %(levelname)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt, stream=sys.stderr)

    socket_path = args.socket or P.socket_path()
    config_path = args.config or P.DEFAULT_CONFIG
    try:
        cfg = load_config(config_path, must_exist=config_path != P.DEFAULT_CONFIG)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    production = socket_path == P.DEFAULT_SOCKET and config_path == P.DEFAULT_CONFIG
    if not production and not cfg["device_name_filter"]:
        log.error(
            "refusing to start: a non-default socket or config requires device_name_filter "
            "(so test runs can never grab the real keyboard/mouse)"
        )
        return 2
    if os.geteuid() != 0:
        log.warning("not running as root: input devices cannot be grabbed")

    daemon = Daemon(cfg, socket_path)
    try:
        asyncio.run(daemon.serve())
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    finally:
        daemon.emergency_release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
