"""Test harness for pcpilotd: fake uinput devices, a fake UI and an agent client.

SAFETY (the user is using this machine while tests run):

* Every fake device is named ``pcpilot-test-<pid>-...`` and the daemon under
  test always runs with ``device_name_filter`` set to that per-run prefix, so
  it can only ever open/grab devices created by this process.
* A runtime udev rule (``/run/udev/rules.d``, tmpfs) tags ``pcpilot-test-*``
  devices with ``LIBINPUT_IGNORE_DEVICE=1``, so the real GNOME session never
  sees them. ``FakeDevice`` verifies the tag before it lets a test emit a
  single event; without it, emitting raises.
* ``DaemonProcess.input_nodes()`` lists the ``/dev/input`` nodes the daemon
  holds open; tests assert they are all test devices.

Nothing here opens, reads or grabs a real input device.
"""

import errno
import fcntl
import itertools
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from pcpilot import evdev
from pcpilot import protocol as P

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_PREFIX = "pcpilot-test-"
RUN_PREFIX = f"{TEST_PREFIX}{os.getpid()}-"

UDEV_RULE_PATH = "/run/udev/rules.d/99-pcpilot-test-ignore.rules"
UDEV_RULE = (
    "# pcpilot test-suite: keep fake uinput devices away from libinput/the desktop.\n"
    "# Only matches devices named pcpilot-test-*. Safe to delete; gone on reboot.\n"
    'ACTION!="remove", SUBSYSTEM=="input", ATTRS{name}=="pcpilot-test-*", '
    'ENV{LIBINPUT_IGNORE_DEVICE}="1"\n'
)

KEYBOARD_KEYS = tuple(sorted(set(range(evdev.KEY_ESC, 89)) | {
    evdev.KEY_RIGHTCTRL, evdev.KEY_RIGHTALT, evdev.KEY_RIGHTSHIFT, evdev.KEY_RIGHTMETA,
}))  # ESC..F12 plus right-side modifiers
POINTER_BUTTONS = (evdev.BTN_LEFT, evdev.BTN_RIGHT, evdev.BTN_MIDDLE)
POINTER_AXES = (evdev.REL_X, evdev.REL_Y, evdev.REL_WHEEL)

_rule_lock = threading.Lock()
_rule_ready = False


def ensure_udev_ignore_rule():
    """Install the libinput-ignore rule for test devices (idempotent).

    The rule is intentionally left installed: removing it while another test
    run (e.g. the e2e suite) still has devices would expose them to the
    desktop. It lives on tmpfs and only matches ``pcpilot-test-*``.
    """
    global _rule_ready
    with _rule_lock:
        if _rule_ready:
            return
        try:
            with open(UDEV_RULE_PATH, "r", encoding="utf-8") as f:
                current = f.read()
        except FileNotFoundError:
            current = None
        if current != UDEV_RULE:
            os.makedirs(os.path.dirname(UDEV_RULE_PATH), exist_ok=True)
            tmp = UDEV_RULE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(UDEV_RULE)
            os.replace(tmp, UDEV_RULE_PATH)
            subprocess.run(["udevadm", "control", "--reload"], check=True, timeout=10)
        _rule_ready = True


def udev_properties(devnode):
    out = subprocess.run(
        ["udevadm", "info", "--query=property", f"--name={devnode}"],
        capture_output=True, text=True, timeout=10,
    )
    props = {}
    for line in out.stdout.splitlines():
        k, _, v = line.partition("=")
        props[k] = v
    return props


_dev_counter = itertools.count(1)


class FakeDevice:
    """A uinput keyboard or pointer that the desktop provably ignores."""

    def __init__(self, kind="keyboard", tag=None):
        if kind not in ("keyboard", "pointer"):
            raise ValueError(kind)
        ensure_udev_ignore_rule()
        self.kind = kind
        self.name = f"{RUN_PREFIX}{tag or kind}-{next(_dev_counter)}"
        assert self.name.startswith(TEST_PREFIX)
        if kind == "keyboard":
            self.ui = evdev.UInputDevice(self.name, keys=KEYBOARD_KEYS)
        else:
            self.ui = evdev.UInputDevice(self.name, keys=POINTER_BUTTONS, rel=POINTER_AXES)
        self.verified = False
        try:
            self._verify_ignored()
        except BaseException:
            self.ui.destroy()
            raise

    def _verify_ignored(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            node = self.ui.find_event_node()
            if node and os.path.exists(node):
                props = udev_properties(node)
                if props.get("LIBINPUT_IGNORE_DEVICE") == "1":
                    self.verified = True
                    return
            time.sleep(0.05)
        raise RuntimeError(f"udev did not tag {self.name} with LIBINPUT_IGNORE_DEVICE=1; refusing to use it")

    @property
    def devnode(self):
        return self.ui.devnode

    @property
    def rdev(self):
        st = os.stat(self.devnode)
        return (os.major(st.st_rdev), os.minor(st.st_rdev))

    # --- emitting (only for verified devices) -----------------------------

    def _emit(self, type_, code, value):
        if not self.verified:
            raise RuntimeError("refusing to emit on a device the desktop may see")
        self.ui.emit(type_, code, value)

    def press(self, code):
        self._emit(evdev.EV_KEY, code, 1)

    def release(self, code):
        self._emit(evdev.EV_KEY, code, 0)

    def tap(self, code):
        self.press(code)
        self.release(code)

    def move(self, dx, dy):
        if not self.verified:
            raise RuntimeError("refusing to emit on a device the desktop may see")
        self.ui.emit(evdev.EV_REL, evdev.REL_X, dx, syn=False)
        self.ui.emit(evdev.EV_REL, evdev.REL_Y, dy)

    def hotkey(self, mods=(evdev.KEY_LEFTCTRL, evdev.KEY_LEFTALT), key=evdev.KEY_ESC):
        for m in mods:
            self.press(m)
        self.press(key)
        self.release(key)
        for m in reversed(mods):
            self.release(m)

    # --- observing --------------------------------------------------------

    def held_keys(self):
        dev = evdev.InputDevice(self.devnode)
        try:
            return dev.pressed_keys()
        finally:
            dev.close()

    def grabbed_by_someone(self):
        """True if another fd holds EVIOCGRAB (our own grab attempt gets EBUSY)."""
        dev = evdev.InputDevice(self.devnode)
        try:
            try:
                dev.grab()
            except OSError as e:
                if e.errno == errno.EBUSY:
                    return True
                raise
            dev.ungrab()
            return False
        finally:
            dev.close()

    def open_reader(self):
        return evdev.InputDevice(self.devnode)

    def destroy(self):
        self.ui.destroy()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.destroy()


class CompositorView:
    """What a compositor (libinput) would believe is held on a FakeDevice:
    a non-grabbing reader, EVIOCGKEY snapshot at open, then its events.
    It only ever receives what the compositor receives."""

    def __init__(self, fake):
        self.dev = evdev.InputDevice(fake.devnode)
        self.held = self.dev.pressed_keys()
        self.log = []  # every EV_KEY (code, value) delivered to this reader

    def update(self):
        while True:
            events = self.dev.read_events()
            if not events:
                return self.held
            for type_, code, value in events:
                if type_ != evdev.EV_KEY:
                    continue
                self.log.append((code, value))
                if value == 1:
                    self.held.add(code)
                elif value == 0:
                    self.held.discard(code)

    def close(self):
        self.dev.close()


def wait_until(pred, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() >= deadline:
            return v
        time.sleep(interval)


# --- daemon under test ------------------------------------------------------


class DaemonProcess:
    """Runs ``python -m pcpilot.daemon`` against a temp socket/config."""

    def __init__(self, workdir=None, preamble=None, **overrides):
        """``preamble``: Python source run inside the daemon process before
        ``pcpilot.daemon.main`` (fault injection: monkeypatching)."""
        self.preamble = preamble
        self._own_dir = workdir is None
        self.dir = workdir or tempfile.mkdtemp(prefix="pcpilotd-test-")
        os.chmod(self.dir, 0o755)  # so the setuid "intruder" client can reach the socket
        self.socket = os.path.join(self.dir, "pcpilot.sock")
        self.config_path = os.path.join(self.dir, "config.json")
        self.wayland_socket = os.path.join(self.dir, "wayland-test")
        self.log_path = os.path.join(self.dir, "daemon.log")
        self.config = {
            "device_name_filter": RUN_PREFIX,
            "desktop_user": os.getuid(),
            "agent_uids": [os.getuid()],
            "ui_wayland_socket": self.wayland_socket,
            "idle_timeout_s": 30,
            "max_session_s": 60,
            "ui_ack_timeout_s": 1.0,
            "release_wait_s": 1.5,
            "ui_timeout_s": 2.0,
            "rescan_interval_s": 0.2,
        }
        self.config.update(overrides)
        flt = self.config.get("device_name_filter")
        if not (isinstance(flt, str) and flt.startswith(TEST_PREFIX)):
            raise AssertionError("tests must run the daemon with a pcpilot-test- device filter")
        self.proc = None
        self._log = None

    def start(self, timeout=5.0):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f)
        env = dict(os.environ)
        env.pop("NOTIFY_SOCKET", None)
        env.pop("PCPILOT_SOCKET", None)
        try:
            old_ino = os.stat(self.socket).st_ino  # e.g. another daemon's socket
        except OSError:
            old_ino = None
        self._log = open(self.log_path, "ab")
        if self.preamble:
            launcher = ["-c", self.preamble + "\nimport sys\nfrom pcpilot.daemon import main\nsys.exit(main(sys.argv[1:]))\n"]
        else:
            launcher = ["-m", "pcpilot.daemon"]
        self.proc = subprocess.Popen(
            [sys.executable, *launcher, "--socket", self.socket, "--config", self.config_path, "-v"],
            cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"daemon exited with {self.proc.returncode}:\n{self.log()}")
            try:
                ino = os.stat(self.socket).st_ino
            except OSError:
                ino = None
            if ino is not None and ino != old_ino:
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.connect(self.socket)
                    return self
                except OSError:
                    pass
            time.sleep(0.02)
        raise RuntimeError(f"daemon did not start:\n{self.log()}")

    def log(self):
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def input_nodes(self):
        """``/dev/input/event*`` nodes the daemon process currently holds open."""
        nodes = []
        if self.proc is None or self.proc.poll() is not None:
            return nodes
        fd_dir = f"/proc/{self.proc.pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            return nodes
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target.startswith("/dev/input/event"):
                nodes.append(target)
        return nodes

    def assert_only_test_devices_open(self):
        for node in self.input_nodes():
            name = _sysfs_name(node)
            if name is None or not name.startswith(TEST_PREFIX):
                raise AssertionError(f"daemon holds a non-test input device open: {node} ({name!r})")

    def stop(self, sig=signal.SIGTERM, timeout=5.0):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self._log is not None:
            self._log.close()
            self._log = None
        return self.proc.returncode if self.proc else None

    def cleanup(self):
        self.stop()
        if self._own_dir:
            shutil.rmtree(self.dir, ignore_errors=True)


def _sysfs_name(devnode):
    event = os.path.basename(devnode)
    try:
        with open(f"/sys/class/input/{event}/device/name", "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


# --- socket clients ---------------------------------------------------------


class _LineClient:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self._file = self.sock.makefile("rb")
        self._wlock = threading.Lock()
        self.messages = []  # every message received, in order
        self._cond = threading.Condition()
        self.closed = False
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def send(self, msg):
        with self._wlock:
            self.sock.sendall(P.encode(msg))

    def _read_loop(self):
        try:
            for line in self._file:
                msg = P.decode(line)
                self._on_message(msg)
                with self._cond:
                    self.messages.append(msg)
                    self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self.closed = True
                self._cond.notify_all()

    def _on_message(self, msg):
        pass

    def mark(self):
        """Index to pass as ``since=`` so a wait only considers newer messages."""
        with self._cond:
            return len(self.messages)

    def wait_for(self, pred, timeout=5.0, since=0):
        """Wait for a received message matching ``pred`` (at index >= ``since``);
        returns it or None."""
        deadline = time.monotonic() + timeout
        with self._cond:
            seen = since
            while True:
                for msg in self.messages[seen:]:
                    if pred(msg):
                        return msg
                seen = len(self.messages)
                left = deadline - time.monotonic()
                if left <= 0 or self.closed:
                    return None
                self._cond.wait(left)

    def wait_closed(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self.closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if threading.current_thread() is not self._thread:
            self._thread.join(2.0)
        self._file.close()  # makefile() keeps the fd alive on its own
        self.sock.close()


class AgentClient(_LineClient):
    def __init__(self, path, client="test-agent", hello=True):
        super().__init__(path)
        self._ids = itertools.count(1)
        if hello:
            r = self.call("hello", {"client": client})
            assert r.get("ok"), r

    def request(self, op, args=None):
        """Send without waiting; returns the request id."""
        id_ = next(self._ids)
        self.send(P.request(id_, op, args))
        return id_

    def response(self, id_, timeout=10.0):
        return self.wait_for(lambda m: m.get("id") == id_ and "ok" in m, timeout)

    def call(self, op, args=None, timeout=10.0):
        """Send and wait for the reply message (``{"id","ok",...}``)."""
        return self.response(self.request(op, args), timeout)

    def events(self, name=None):
        with self._cond:
            return [m for m in self.messages if "event" in m and (name is None or m["event"] == name)]

    def wait_event(self, name, timeout=5.0, since=0, **fields):
        return self.wait_for(
            lambda m: m.get("event") == name and all(m.get(k) == v for k, v in fields.items()), timeout, since
        )


class FakeUI(_LineClient):
    """Plays the GNOME extension. Holds the "compositor" listening socket the
    daemon checks (``hold_wayland``), acks ``state active`` echoing its
    session id (``auto_ack``; ``ack_session`` overrides the echoed id),
    answers ``ping`` with ``pong`` (``answer_pings``) and answers forwarded
    ops via ``handler(op, args) -> result`` (raise ``UIError`` for an error
    reply). ``handler`` may return ``None`` to not answer at all."""

    def __init__(self, daemon, hold_wayland=True, auto_ack=True, ack_delay=0.0, handler=None, hello=True,
                 answer_pings=True, ack_session=None):
        self.wayland = None
        if hold_wayland:
            self.wayland = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                os.unlink(daemon.wayland_socket)
            except FileNotFoundError:
                pass
            self.wayland.bind(daemon.wayland_socket)
            self.wayland.listen(1)
        self.auto_ack = auto_ack
        self.ack_delay = ack_delay
        self.answer_pings = answer_pings
        self.ack_session = ack_session
        self.handler = handler or (lambda op, args: {})
        self.requests = []
        super().__init__(daemon.socket)
        self.hello = None
        if hello:
            self.send({"op": "hello", "id": 1, "args": {"role": "ui", "version": 1}})
            self.hello = self.wait_for(lambda m: m.get("id") == 1 and "ok" in m)

    def states(self):
        with self._cond:
            return [m for m in self.messages if m.get("event") == "state"]

    def wait_state(self, active, timeout=5.0, since=0):
        return self.wait_for(lambda m: m.get("event") == "state" and m.get("active") is active, timeout, since)

    def send_ack(self, session):
        self.send({"event": "state_ack", "active": True, "session": session})

    def _on_message(self, msg):
        if msg.get("event") == "state" and msg.get("active") is True and self.auto_ack:
            session = msg.get("session") if self.ack_session is None else self.ack_session

            def ack():
                if self.ack_delay:
                    time.sleep(self.ack_delay)
                try:
                    self.send_ack(session)
                except OSError:
                    pass
            threading.Thread(target=ack, daemon=True).start()
        elif msg.get("event") == "ping" and self.answer_pings:
            try:
                self.send({"event": "pong", "session": msg.get("session")})
            except OSError:
                pass
        elif "op" in msg and "id" in msg:
            self.requests.append(msg)
            threading.Thread(target=self._answer, args=(msg,), daemon=True).start()

    def _answer(self, msg):
        try:
            result = self.handler(msg["op"], msg.get("args") or {})
            if result is None:
                return
            reply = P.ok(msg["id"], result)
        except UIError as e:
            reply = P.err(msg["id"], e.code, e.message)
        try:
            self.send(reply)
        except OSError:
            pass

    def close(self):
        super().close()
        if self.wayland is not None:
            self.wayland.close()


class UIError(Exception):
    def __init__(self, code, message=""):
        super().__init__(code)
        self.code = code
        self.message = message


def intruder_connect(socket_path, uid=65534, payload=None, timeout=5.0):
    """Connect as another uid (default nobody) in a child process.

    Returns the raw bytes the daemon sent before closing (b"" = closed silently).
    """
    payload = payload if payload is not None else P.encode(P.request(1, "hello", {"client": "intruder"}))
    code = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.settimeout(%r)\n"
        "s.connect(sys.argv[1]); s.sendall(bytes.fromhex(sys.argv[2]))\n"
        "buf=b''\n"
        "try:\n"
        "    while True:\n"
        "        d=s.recv(65536)\n"
        "        if not d: break\n"
        "        buf+=d\n"
        "except socket.timeout:\n"
        "    sys.stdout.write('TIMEOUT:')\n"
        "except ConnectionResetError:\n"
        "    pass\n"
        "sys.stdout.write(buf.hex())\n"
    ) % timeout

    def demote():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)

    out = subprocess.run(
        [sys.executable, "-I", "-c", code, socket_path, payload.hex()],
        preexec_fn=demote, capture_output=True, text=True, timeout=timeout + 5, cwd="/",
    )
    text = out.stdout.strip()
    if text.startswith("TIMEOUT:"):
        raise AssertionError("daemon did not close the intruder connection")
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return bytes.fromhex(text)
