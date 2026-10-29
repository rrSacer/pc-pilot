"""Minimal evdev / uinput bindings using only the stdlib (fcntl + struct).

Device discovery reads sysfs first, so a name filter is applied before any
``/dev/input`` node is opened: with ``device_name_filter`` set, devices that do
not match are never touched at all.
"""

import errno
import fcntl
import fnmatch
import glob
import os
import re
import struct

# --- ioctl encoding (asm-generic/ioctl.h) -----------------------------------

_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction, type_, nr, size):
    return (direction << 30) | (size << 16) | (ord(type_) << 8) | nr


def _IO(type_, nr):
    return _IOC(_IOC_NONE, type_, nr, 0)


def _IOR(type_, nr, size):
    return _IOC(_IOC_READ, type_, nr, size)


def _IOW(type_, nr, size):
    return _IOC(_IOC_WRITE, type_, nr, size)


# --- linux/input.h ----------------------------------------------------------

EVIOCGID = _IOR("E", 0x02, 8)
EVIOCGRAB = _IOW("E", 0x90, 4)


def EVIOCGNAME(length):
    return _IOC(_IOC_READ, "E", 0x06, length)


def EVIOCGPHYS(length):
    return _IOC(_IOC_READ, "E", 0x07, length)


def EVIOCGKEY(length):
    return _IOC(_IOC_READ, "E", 0x18, length)


def EVIOCGBIT(ev, length):
    return _IOC(_IOC_READ, "E", 0x20 + ev, length)


# --- linux/uinput.h ---------------------------------------------------------

UI_DEV_CREATE = _IO("U", 1)
UI_DEV_DESTROY = _IO("U", 2)
UI_DEV_SETUP = _IOW("U", 3, 92)  # struct uinput_setup
UI_SET_EVBIT = _IOW("U", 100, 4)
UI_SET_KEYBIT = _IOW("U", 101, 4)
UI_SET_RELBIT = _IOW("U", 102, 4)


def UI_GET_SYSNAME(length):
    return _IOC(_IOC_READ, "U", 44, length)


# --- linux/input-event-codes.h (the subset we use) --------------------------

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
EV_MSC = 0x04
EV_MAX = 0x1F

SYN_REPORT = 0
SYN_DROPPED = 3

REL_X = 0x00
REL_Y = 0x01
REL_WHEEL = 0x08

ABS_MT_SLOT = 0x2F

KEY_MAX = 0x2FF
KEY_ESC = 1
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_ENTER = 28
KEY_LEFTCTRL = 29
KEY_A = 30
KEY_LEFTSHIFT = 42
KEY_RIGHTSHIFT = 54
KEY_LEFTALT = 56
KEY_SPACE = 57
KEY_RIGHTCTRL = 97
KEY_RIGHTALT = 100
KEY_LEFTMETA = 125
KEY_RIGHTMETA = 126
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_TOUCH = 0x14A

BUS_VIRTUAL = 0x06

# Letter keys: a device with any of them counts as a keyboard.
_LETTER_KEYS = tuple(range(16, 26)) + tuple(range(30, 39)) + tuple(range(44, 51))
_POINTER_BUTTONS = (BTN_LEFT, BTN_TOUCH)

_KEY_BYTES = KEY_MAX // 8 + 1
_EVENT = struct.Struct("llHHi")  # struct input_event (native timeval)
EVENT_SIZE = _EVENT.size


def _bits_from_bytes(buf):
    return int.from_bytes(buf, "little")


def _set_bits(mask):
    out = set()
    i = 0
    while mask:
        if mask & 1:
            out.add(i)
        mask >>= 1
        i += 1
    return out


# --- sysfs discovery --------------------------------------------------------


class DeviceInfo:
    """Static description of an ``/dev/input/eventN`` node, read from sysfs."""

    def __init__(self, event, devnode, name, ev_mask, key_mask, virtual, rdev, abs_mask=0):
        self.event = event  # "event5"
        self.devnode = devnode  # "/dev/input/event5"
        self.name = name
        self.ev_mask = ev_mask
        self.key_mask = key_mask
        self.virtual = virtual  # lives under /sys/devices/virtual (uinput etc.)
        self.rdev = rdev  # (major, minor)
        self.abs_mask = abs_mask

    def has_key(self, code):
        return bool(self.key_mask >> code & 1)

    @property
    def is_multitouch(self):
        """Touch contacts live in MT slots, so a bare BTN_TOUCH release can't end them."""
        return bool(self.ev_mask >> EV_ABS & 1) and bool(self.abs_mask >> ABS_MT_SLOT & 1)

    @property
    def is_keyboard(self):
        return bool(self.ev_mask >> EV_KEY & 1) and any(self.has_key(k) for k in _LETTER_KEYS)

    @property
    def is_pointer(self):
        return bool(self.ev_mask >> EV_KEY & 1) and any(self.has_key(b) for b in _POINTER_BUTTONS)

    @property
    def kind(self):
        kinds = [k for k, v in (("keyboard", self.is_keyboard), ("pointer", self.is_pointer)) if v]
        return "+".join(kinds) or "other"

    def __repr__(self):
        return f"<DeviceInfo {self.devnode} {self.name!r} {self.kind}>"


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def _parse_sysfs_bitmap(text):
    """sysfs prints bitmaps as hex longs, most significant word first."""
    words = text.split()
    word_bits = struct.calcsize("l") * 8
    mask = 0
    for i, w in enumerate(reversed(words)):
        mask |= int(w, 16) << (word_bits * i)
    return mask


def _event_sort_key(path):
    m = re.search(r"(\d+)$", path)
    return int(m.group(1)) if m else -1


def read_device_info(event):
    """Read one ``eventN`` from sysfs. Returns None if it vanished meanwhile."""
    base = f"/sys/class/input/{event}"
    try:
        name = _read(f"{base}/device/name")
        ev_mask = _parse_sysfs_bitmap(_read(f"{base}/device/capabilities/ev"))
        key_mask = _parse_sysfs_bitmap(_read(f"{base}/device/capabilities/key"))
        abs_mask = _parse_sysfs_bitmap(_read(f"{base}/device/capabilities/abs"))
        major, minor = (int(x) for x in _read(f"{base}/dev").split(":"))
        real = os.path.realpath(f"{base}/device")
    except (OSError, ValueError):
        return None
    return DeviceInfo(
        event=event,
        devnode=f"/dev/input/{event}",
        name=name,
        ev_mask=ev_mask,
        key_mask=key_mask,
        virtual=real.startswith("/sys/devices/virtual/"),
        rdev=(major, minor),
        abs_mask=abs_mask,
    )


def list_devices():
    events = sorted(
        (os.path.basename(p) for p in glob.glob("/sys/class/input/event*")),
        key=_event_sort_key,
    )
    infos = []
    for event in events:
        info = read_device_info(event)
        if info is not None:
            infos.append(info)
    return infos


def capture_verdict(info, exclude_patterns=(), name_filter=None):
    """Return (capture: bool, reason: str) for one device.

    ``name_filter`` is a literal name prefix used by tests; when set, only
    *virtual* devices whose name starts with it are eligible.
    """
    if name_filter:
        if not info.name.startswith(name_filter):
            return False, "name filter"
        if not info.virtual:
            return False, "name filter (not virtual)"
    if not (info.is_keyboard or info.is_pointer):
        return False, "not a keyboard/pointer"
    lowered = info.name.lower()
    for pat in exclude_patterns:
        if fnmatch.fnmatchcase(lowered, pat.lower()):
            return False, f"excluded ({pat})"
    return True, info.kind


# --- opened device ----------------------------------------------------------


class InputDevice:
    """An opened evdev node (non-blocking)."""

    def __init__(self, path, write=False):
        flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NONBLOCK | os.O_CLOEXEC
        self.path = path
        self.fd = os.open(path, flags)
        self.grabbed = False

    @property
    def closed(self):
        return self.fd < 0

    def fileno(self):
        return self.fd

    def rdev(self):
        st = os.fstat(self.fd)
        return (os.major(st.st_rdev), os.minor(st.st_rdev))

    def name(self):
        buf = bytearray(256)
        fcntl.ioctl(self.fd, EVIOCGNAME(len(buf)), buf)
        return bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace")

    def key_capabilities(self):
        buf = bytearray(_KEY_BYTES)
        fcntl.ioctl(self.fd, EVIOCGBIT(EV_KEY, len(buf)), buf)
        return _bits_from_bytes(buf)

    def pressed_keys(self):
        """Keys/buttons currently held, according to the kernel (EVIOCGKEY)."""
        buf = bytearray(_KEY_BYTES)
        fcntl.ioctl(self.fd, EVIOCGKEY(len(buf)), buf)
        return _set_bits(_bits_from_bytes(buf))

    def grab(self):
        fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        self.grabbed = True

    def ungrab(self):
        if self.grabbed and self.fd >= 0:
            self.grabbed = False
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)

    def read_events(self, max_events=64):
        """One bounded batch of pending events as ``[(type, code, value), ...]``
        (empty when nothing is pending). Callers loop for more, so a device
        that floods cannot pin the caller.

        Raises OSError (ENODEV/EIO...) when the device is gone.
        """
        try:
            data = os.read(self.fd, EVENT_SIZE * max_events)
        except BlockingIOError:
            return []
        if not data:
            raise OSError(errno.ENODEV, "device closed")
        return [_EVENT.unpack_from(data, off)[2:] for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE)]

    def write_event(self, type_, code, value):
        os.write(self.fd, _EVENT.pack(0, 0, type_, code, value))

    def write_events(self, events):
        """Inject ``[(type, code, value), ...]`` with a single write."""
        os.write(self.fd, b"".join(_EVENT.pack(0, 0, t, c, v) for t, c, v in events))

    def close(self):
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        self.grabbed = False
        os.close(fd)  # closing also drops an EVIOCGRAB held through this fd


# --- uinput (used by tests and the e2e harness to create fake devices) -------


class UInputDevice:
    """A virtual input device created through /dev/uinput."""

    def __init__(self, name, keys=(), rel=(), vendor=0x1234, product=0x5678):
        if len(name.encode()) >= 80:
            raise ValueError("name too long")
        self.name = name
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            if keys:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
                for k in keys:
                    fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
            if rel:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                for r in rel:
                    fcntl.ioctl(self.fd, UI_SET_RELBIT, r)
            setup = struct.pack("HHHH80sI", BUS_VIRTUAL, vendor, product, 1, name.encode(), 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
            buf = bytearray(64)
            fcntl.ioctl(self.fd, UI_GET_SYSNAME(len(buf)), buf)
            self.sysname = bytes(buf).split(b"\0", 1)[0].decode()  # "input42"
        except BaseException:
            os.close(self.fd)
            raise
        self.event = None
        self.devnode = None

    def find_event_node(self):
        """Resolve ``eventN`` (devtmpfs creates the node synchronously)."""
        if self.devnode is None:
            nodes = glob.glob(f"/sys/devices/virtual/input/{self.sysname}/event*")
            if not nodes:
                return None
            self.event = os.path.basename(nodes[0])
            self.devnode = f"/dev/input/{self.event}"
        return self.devnode

    def emit(self, type_, code, value, syn=True):
        os.write(self.fd, _EVENT.pack(0, 0, type_, code, value))
        if syn:
            os.write(self.fd, _EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0))

    def destroy(self):
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        finally:
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.destroy()
