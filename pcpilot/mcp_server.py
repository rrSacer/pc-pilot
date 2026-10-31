"""Small MCP stdio server. stdout is exclusively JSON-RPC; no MCP SDK needed."""

import base64
import io
import json
import math
import os
import pwd
import queue
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from PIL import Image

from . import protocol
from .a11y_worker import alias as role_alias

VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
INSTRUCTIONS = ("Query, don't look: windows = desktop state, elements = UI/web page map (click by id; it "
                "auto-scrolls), screenshot only as a last resort. Batch act actions; observe:elements "
                "returns changes only. Coordinates are scaled screen pixels (longest side <=1280). "
                "End every task with control stop. Idle sessions auto-release after ~12s. "
                "USER_TOOK_CONTROL: stop and wait for the user; "
                "resume only on their explicit request. Close tabs/files you opened (ctrl+w) before stop; "
                "use keep if the user wants windows left open. Use speed:fast for bulk work; "
                "window fit if something opens small.")

WRAPUP_TIMEOUT_S = 10
IDLE_AUTOSTOP_S = 12
ACT_BUDGET_S = 30
HUMAN_BATCH_LIMIT = 12
STYLED_ACTIONS = {"click", "double_click", "right_click", "move", "drag", "scroll", "type", "window", "focus"}
TYPE_CHUNK = {"human": 240, "fast": 1000, "instant": 1000}
SCROLL_OP_NOTCHES = 15      # notches per pointer.scroll op (the extension's per-op guard)
SCROLL_STEPS = 6            # locate/scroll rounds before giving up on an off-screen element
SCROLL_SETTLE_S = .35
NOTCH_PX = 60               # first guess of logical pixels per wheel notch, then calibrated
TARGET_BAND = .42           # where in the viewport an element is brought before its click
BROWSERS = ("firefox", "chrom", "brave", "vivaldi", "opera", "epiphany", "librewolf", "zen", "edge")
REGISTRY_CAP = 800


class BatchBudgetReached(Exception):
    """Stop between UI actions without ending the owner's desktop session."""



def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


def enum(*values):
    return {"type": "string", "enum": list(values)}


POINT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
FOUR = {**POINT, "minItems": 4, "maxItems": 4}
CLICK = {"anyOf": [POINT, {"type": "string"}]}
ACTION_VALUES = {
    "click": CLICK, "double_click": CLICK, "right_click": CLICK, "move": POINT,
    "drag": FOUR, "scroll": {**POINT, "minItems": 1, "maxItems": 3},
    "type": {"type": "string"}, "key": {"type": "string"},
    "wait": {"type": "integer", "minimum": 0, "maximum": 10000},
    "launch": {"type": "string"}, "open": {"type": "string"},
    "focus": {"type": ["string", "integer"]},
    "window": {"type": "array", "minItems": 2, "maxItems": 4},
    "clipboard_set": {"type": "string"}, "clipboard_get": {"const": True},
}
TOOLS = [
    {"name": "control", "description": "End every task with stop. Idle auto-release ~12s. Stop cleans up; keep preserves windows. Resume only if user asks.",
     "inputSchema": schema({"action": enum("start", "stop", "status"), "resume": {"type": "boolean"},
                            "keep": {"type": "array", "items": {"type": ["string", "integer"]}, "maxItems": 200},
                            "cleanup": {"type": "boolean"}}, ["action"])},
    {"name": "screenshot", "description": "Last resort; prefer windows/elements. Region [x,y,w,h] zooms.",
     "inputSchema": schema({"region": FOUR})},
    {"name": "act", "description": "Batch; click:[x,y] or eID (scrolls to it); scroll:[dy] or [x,y,dy]. Window:[id,fit], [id,resize,w,h], [id,move,x,y]. observe:elements = changes only.",
     "inputSchema": schema({"actions": {"type": "array", "minItems": 1, "maxItems": 100,
          "items": {**schema(ACTION_VALUES), "minProperties": 1, "maxProperties": 1}},
                            "observe": enum("none", "screenshot", "elements"),
                            "speed": enum("human", "fast", "instant")}, ["actions"])},
    {"name": "elements", "description": "UI/web page map: role, name, value, x,y. ↓N/↑N = N screens to scroll (click does it). Browsers: URL, tabs, focus. query also searches browser chrome; role filters.",
     "inputSchema": schema({"query": {"type": "string"}, "role": {"type": "string"}, "window": {"type": ["string", "integer"]},
                            "max": {"type": "integer", "minimum": 1, "maximum": 200}})},
    {"name": "windows", "description": "Desktop state: workspace, monitors, overview, pointer; windows with id, scaled bounds, flags; * = AI-opened, cleaned on stop.", "inputSchema": schema()},
]


def text(value):
    return {"type": "text", "text": str(value)}


def bad(message):
    raise protocol.ProtocolError("bad_args", message)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def vector(value, length):
    if not isinstance(value, list) or len(value) != length or not all(number(v) for v in value):
        bad(f"expected {length} finite coordinates")
    return value


class DaemonClient:
    """One persistent owner connection, with an event reader independent of tool calls."""

    def __init__(self):
        self.sock = None
        self.pending = {}
        self.lock = threading.RLock()
        self.serial = 0
        self.aborted = False
        self.active = False
        self.closed = False
        self.disconnected = False
        self.label = "IA"
        self.theme = "neutral"
        self.on_end = None  # called (any thread) when the session ends outside our own stop

    def _ended(self):
        callback = self.on_end
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                print(f"pcpilot session end hook: {exc}", file=sys.stderr)

    def connect(self):
        if self.closed:
            raise protocol.ProtocolError("cancelled", "MCP connection closed; restart MCP")
        if self.sock is not None:
            if self.disconnected:
                raise protocol.ProtocolError("disconnected", "Daemon disconnected; restart MCP connection")
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(protocol.socket_path())
        except BaseException:
            sock.close()
            raise
        sock.settimeout(None)
        with self.lock:
            if self.closed:
                sock.close()
                raise protocol.ProtocolError("cancelled", "MCP connection closed; restart MCP")
            self.sock = sock
        threading.Thread(target=self._read, daemon=True).start()
        self.call("hello", {"client": self.label})

    def _read(self):
        try:
            with self.sock.makefile("rb") as stream:
                while line := stream.readline(protocol.MAX_LINE + 1):
                    msg = protocol.decode(line)
                    if msg.get("event") == "session_ended":
                        with self.lock:
                            self.active = False
                            if msg.get("reason") in {"user_hotkey", "user_release"}:
                                self.aborted = True
                        self._ended()
                    elif "id" in msg:
                        with self.lock:
                            pending = self.pending.get(msg["id"])
                        if pending:
                            waiter, op, args = pending
                            # Apply session state in wire order, before any later
                            # hotkey event. A caller must not clear a newer abort.
                            if msg.get("ok") and op == "session.begin":
                                self.active = True
                                if args.get("resume"):
                                    self.aborted = False
                            elif msg.get("ok") and op == "session.end":
                                self.active = False
                            waiter.put(msg)
        except (OSError, ValueError) as exc:
            if not self.closed:
                print(f"pcpilot daemon reader: {exc}", file=sys.stderr)
        finally:
            self.active = False
            self.disconnected = True
            with self.lock:
                for waiter, _op, _args in self.pending.values():
                    waiter.put({"ok": False, "error": {"code": "disconnected", "message": "Daemon disconnected"}})
            self._ended()

    def check_abort(self):
        if self.closed:
            raise protocol.ProtocolError("cancelled", "MCP connection closed; restart MCP")
        if self.aborted:
            raise protocol.ProtocolError("user_aborted", "User took control")

    def call(self, op, args=None, *, timeout=26):
        self.connect()
        if op.startswith("ui.") or op == "session.end":
            self.check_abort()
        with self.lock:
            self.serial += 1
            ident = self.serial
            waiter = queue.Queue()
            self.pending[ident] = (waiter, op, args or {})
        try:
            # Serialize shutdown and sending: after EOF closes the connection,
            # no subsequent batch action can be submitted on this socket.
            with self.lock:
                if op.startswith("ui.") or op == "session.end":
                    self.check_abort()
                elif self.closed:
                    raise protocol.ProtocolError("cancelled", "MCP connection closed; restart MCP")
                self.sock.sendall(protocol.encode(protocol.request(ident, op, args)))
            try:
                reply = waiter.get(timeout=timeout)
            except queue.Empty:
                self.close()
                raise protocol.ProtocolError("timeout", "Daemon response timed out; connection closed") from None
            if not reply.get("ok"):
                error = reply.get("error", {})
                if error.get("code") == "user_aborted":
                    self.aborted = True
                    self.active = False
                raise protocol.ProtocolError(error.get("code", "failed"), error.get("message", ""))
            return reply.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def begin(self, resume=False):
        if not resume:
            self.check_abort()
        self.call("session.begin", {"label": self.label, "theme": self.theme, "resume": resume})
        self.check_abort()

    def ensure(self):
        self.check_abort()
        if not self.active:
            self.begin()

    def close(self):
        with self.lock:
            self.closed = True
            self.active = False
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()
            for waiter, _op, _args in self.pending.values():
                waiter.put({"ok": False, "error": {"code": "cancelled", "message": "MCP connection closed"}})


def desktop_identity():
    """Select only the active graphical seat0 session unless explicitly overridden."""
    override = os.environ.get("PCPILOT_DESKTOP_USER")
    if override:
        return pwd.getpwuid(int(override)) if override.isdecimal() else pwd.getpwnam(override)
    sessions = json.loads(subprocess.check_output(
        ["/usr/bin/loginctl", "list-sessions", "--json=short"], text=True, timeout=5,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}))
    found = set()
    for session in sessions:
        props = subprocess.check_output([
            "/usr/bin/loginctl", "show-session", str(session["session"]),
            "-p", "Active", "-p", "Type", "-p", "Seat", "-p", "User"], text=True, timeout=5,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        values = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
        if values.get("Active") == "yes" and values.get("Seat") == "seat0" and values.get("Type") in {"wayland", "x11"}:
            found.add(int(values["User"]))
    if len(found) != 1:
        raise protocol.ProtocolError("a11y_unavailable", "Cannot uniquely resolve active graphical user on seat0; set PCPILOT_DESKTOP_USER")
    return pwd.getpwuid(found.pop())


def desktop_runtime(user):
    return os.environ.get("PCPILOT_A11Y_RUNTIME_DIR", f"/run/user/{user.pw_uid}")


@contextmanager
def screenshot_source(path):
    """Confine root reads to verified desktop-owned screenshot fds.

    Directory fds anchor every lookup, including cleanup. No component may be
    a symlink; file type, owner and resource limits are checked on the opened fd.
    """
    user = desktop_identity()
    runtime = desktop_runtime(user)
    directory = os.path.join(runtime, "pcpilot")
    if (not isinstance(path, str) or not os.path.isabs(directory) or
            os.path.normpath(directory) != directory or
            os.path.dirname(path) != directory or
            not re.fullmatch(r"shot-[0-9]+-[0-9]+\.png", os.path.basename(path))):
        raise protocol.ProtocolError("unsafe_screenshot", "Invalid screenshot location or filename")
    name = os.path.basename(path)
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = None
    opened = None
    try:
        for component in directory.split("/")[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=parent)
            os.close(parent)
            parent = child
            if os.fstat(parent).st_uid not in {0, user.pw_uid}:
                raise protocol.ProtocolError("unsafe_screenshot", "Untrusted screenshot directory owner")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != user.pw_uid:
            raise protocol.ProtocolError("unsafe_screenshot", "Screenshot must be a regular desktop-owned file")
        opened = info
        limit = 64 * 1024 * 1024
        if not 0 < info.st_size <= limit:
            raise protocol.ProtocolError("unsafe_screenshot", "Screenshot exceeds size limit")
        # A bounded fd read also handles concurrent file growth, without following
        # a replacement pathname or trusting Pillow to reopen it.
        # Keep the original fd alive through cleanup, so its inode cannot be
        # recycled for a replacement pathname while Pillow processes the bytes.
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise protocol.ProtocolError("unsafe_screenshot", "Screenshot exceeds size limit")
        yield io.BytesIO(data)
    finally:
        try:
            if opened is not None:
                try:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (current.st_dev, current.st_ino, current.st_uid) == (opened.st_dev, opened.st_ino, user.pw_uid):
                        os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)


class AccessibilityWorker:
    def __init__(self):
        self.process = None
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.closed = False
        self.cancelled = False
        self.busy = False
        self.held = False  # this worker switched org.a11y.Status IsEnabled on

    def _spec(self):
        user = desktop_identity()
        runtime = desktop_runtime(user)
        env = {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name,
               "PATH": "/usr/bin:/bin", "LANG": os.environ.get("LANG", "C.UTF-8"),
               "XDG_RUNTIME_DIR": runtime,
               "DBUS_SESSION_BUS_ADDRESS": os.environ.get("PCPILOT_A11Y_BUS_ADDRESS", f"unix:path={runtime}/bus")}
        if os.environ.get("PCPILOT_A11Y_STATE_DIR"):
            env["PCPILOT_A11Y_STATE_DIR"] = os.environ["PCPILOT_A11Y_STATE_DIR"]
        env.update({key: value for key, value in os.environ.items() if re.fullmatch(r"LC_[A-Z_]+", key)})
        command = ["/usr/bin/python3", "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "a11y_worker.py")]
        if os.geteuid() != user.pw_uid:
            command = ["/usr/bin/setpriv", f"--reuid={user.pw_uid}", f"--regid={user.pw_gid}", "--init-groups", *command]
        return command, env

    def start(self):
        command, env = self._spec()
        with self.lock:
            if self.closed or self.cancelled:
                raise protocol.ProtocolError("cancelled", "AT-SPI request cancelled")
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=sys.stderr, env=env, bufsize=0)

    def request(self, payload, timeout=15, *, force=False):
        with self.lock:
            if (self.closed or self.cancelled) and not force:
                raise protocol.ProtocolError("cancelled", "AT-SPI request cancelled")
            self.busy = True
        try:
            with self.io_lock:
                if self.process is None or self.process.poll() is not None:
                    self.start()
                with self.lock:
                    process = self.process
                    if (self.closed or self.cancelled) and not force or process is None:
                        raise protocol.ProtocolError("cancelled", "AT-SPI request cancelled")
                process.stdin.write(protocol.encode(payload))
                process.stdin.flush()
                ready, _, _ = select.select([process.stdout], [], [], timeout)
                if not ready:
                    raise TimeoutError("AT-SPI timed out")
                reply = protocol.decode(process.stdout.readline(protocol.MAX_LINE + 1))
            if "error" in reply:
                raise protocol.ProtocolError("a11y_unavailable", reply["error"])
            return reply
        except (OSError, ValueError, TimeoutError):
            self._stop_process()
            raise
        finally:
            with self.lock:
                self.busy = False

    def snapshot(self, windows, query="", window="focused", maximum=60, role=""):
        return self.request({"windows": windows, "query": query, "window": window,
                             "max": maximum, "role": role})

    def locate(self, ref, windows):
        return self.request({"op": "locate", "ref": ref, "windows": windows}, timeout=8)

    def acquire(self):
        reply = self.request({"op": "acquire"}, timeout=10)
        self.held = reply.get("enabled") is not None
        return reply

    def release(self):
        """Hand org.a11y.Status IsEnabled back; a dead worker is covered by --recover."""
        if not self.held:
            return
        self.held = False
        try:
            self.request({"op": "release"}, timeout=6, force=True)
        except (protocol.ProtocolError, OSError, ValueError, TimeoutError):
            self._recover()

    def _recover(self):
        """Fire-and-forget: a fresh worker undoes a switch its dead predecessor left on."""
        try:
            command, env = self._spec()
            subprocess.Popen([*command, "--recover"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=sys.stderr, env=env, start_new_session=True)
        except (OSError, protocol.ProtocolError, subprocess.SubprocessError) as exc:
            print(f"pcpilot a11y recover: {exc}", file=sys.stderr)

    def _stop_process(self, graceful=False):
        with self.lock:
            process, self.process = self.process, None
            if not process:
                return
            killed = process.poll() is not None  # already dead: nobody released the switch
            closed_stdin = False
            if process.poll() is None:
                if graceful:
                    # EOF on stdin makes the worker restore accessibility and exit.
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                    closed_stdin = True
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        killed = True
                else:
                    process.kill()
                    killed = True
            process.wait(timeout=2)
            for stream in ((process.stdout,) if closed_stdin else (process.stdin, process.stdout)):
                try:
                    stream.close()
                except OSError:
                    pass
            if self.held:
                self.held = False
                if killed:
                    self._recover()

    def cancel(self):
        with self.lock:
            self.cancelled = True
            if self.busy:
                self._stop_process()

    def reset_cancel(self):
        with self.lock:
            self.cancelled = False

    def close(self):
        with self.lock:
            self.closed = True
        self._stop_process(graceful=True)


class Server:
    def __init__(self, daemon=None, worker=None):
        self.daemon = daemon or DaemonClient()
        self.worker = worker or AccessibilityWorker()
        self.max_side = int(os.environ.get("PCPILOT_MAX_SIDE", "1280"))
        if self.max_side < 1:
            raise ValueError("PCPILOT_MAX_SIDE must be positive")
        self.elements = {}   # id -> row; ids stay valid for the session and re-resolve live
        self.keys = {}       # (role, name, k-th) -> id, so a re-listed element keeps its id
        self.next_id = 1
        self.view = None     # last rendered snapshot, for observe diffs
        self.notch_px = {}
        self.notes = []
        self.typed_note = None
        self.a11y_lock = threading.Lock()
        self.a11y_state = "off"
        self.scale_x = self.scale_y = 1.0
        self.width = self.height = self.model_width = self.model_height = 0
        self.cancelled = threading.Event()  # EOF / signal: terminal
        self.request_cancelled = threading.Event()
        self.request_lock = threading.RLock()
        self.current_request = None
        self.pending_requests = set()
        self.cancelled_requests = set()
        self.initialized = False
        self.operation_lock = threading.RLock()
        self.idle_condition = threading.Condition()
        self.idle_deadline = None
        self.idle_released = False
        self.autostop_s = float(os.environ.get("PCPILOT_AUTOSTOP_S", IDLE_AUTOSTOP_S))
        if not math.isfinite(self.autostop_s) or self.autostop_s < 0:
            raise ValueError("PCPILOT_AUTOSTOP_S must be finite and nonnegative")
        self.idle_thread = None
        self.daemon.on_end = self._on_daemon_end

    def _on_daemon_end(self):
        threading.Thread(target=self._a11y_off, name="pcpilot-a11y-off", daemon=True).start()

    def _a11y_on(self):
        """Firefox exports its page only while org.a11y.Status IsEnabled; hold it per session."""
        if getattr(self.worker, "acquire", None) is None:
            return
        with self.a11y_lock:
            if self.a11y_state != "off":
                return
            self.a11y_state = "pending"
        threading.Thread(target=self._a11y_acquire, name="pcpilot-a11y-on", daemon=True).start()

    def _a11y_acquire(self):
        with self.a11y_lock:
            if self.a11y_state != "pending":
                return
            try:
                self.worker.acquire()
                self.a11y_state = "on"
            except (protocol.ProtocolError, OSError, ValueError, TimeoutError) as exc:
                print(f"pcpilot a11y acquire: {exc}", file=sys.stderr)
                self.a11y_state = "off"

    def _a11y_off(self):
        release = getattr(self.worker, "release", None)
        with self.a11y_lock:
            was, self.a11y_state = self.a11y_state, "off"
            if release is not None and was != "off":
                try:
                    release()
                except (protocol.ProtocolError, OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
                    print(f"pcpilot a11y release: {exc}", file=sys.stderr)

    def reset_elements(self):
        self.elements.clear()
        self.keys.clear()
        self.next_id = 1
        self.view = None

    def _idle_watchdog(self):
        while not self.cancelled.is_set():
            with self.idle_condition:
                if self.cancelled.is_set():
                    return
                deadline = self.idle_deadline
                if deadline is None or deadline > time.monotonic():
                    self.idle_condition.wait(None if deadline is None else max(0, deadline - time.monotonic()))
                    continue
            # Hold the same lock as the entire tool call, including local waits,
            # image processing and AT-SPI observations. Recheck after acquiring it.
            with self.operation_lock:
                with self.idle_condition:
                    if self.idle_deadline != deadline:
                        continue
                    self.idle_deadline = None
                if (self.cancelled.is_set() or self.request_cancelled.is_set() or
                        self.daemon.aborted or not self.daemon.active):
                    continue
                try:
                    owner = self.daemon
                    self.stop({"cleanup": False})
                    if self.daemon is owner and not owner.closed:
                        self.idle_released = True
                except (protocol.ProtocolError, OSError, ValueError, KeyError, TypeError) as exc:
                    print(f"pcpilot idle cleanup: {exc}", file=sys.stderr)

    def client_identity(self, info):
        name = str(info.get("name") or "")
        folded = name.casefold()
        theme = next((theme for theme, names in (
            ("claude", ("claude",)), ("codex", ("codex", "openai")),
            ("gemini", ("gemini", "antigravity"))) if any(part in folded for part in names)), "neutral")
        self.daemon.label = ({"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}.get(theme)
                             or str(info.get("title") or name or "IA"))[:80]
        override = os.environ.get("PCPILOT_THEME", "")
        self.daemon.theme = override if override in {"claude", "codex", "gemini", "neutral"} else theme

    def screen(self):
        info = self.daemon.call("ui.screen.info")
        self.width, self.height = info["width"], info["height"]
        if not all(number(v) and v > 0 for v in (self.width, self.height)):
            raise protocol.ProtocolError("failed", "Invalid screen dimensions")
        ratio = min(1, self.max_side / max(self.width, self.height))
        self.model_width = max(1, round(self.width * ratio))
        self.model_height = max(1, round(self.height * ratio))
        self.scale_x = self.model_width / self.width
        self.scale_y = self.model_height / self.height

    def point(self, value):
        x, y = vector(value, 2)
        if not (0 <= x < self.model_width and 0 <= y < self.model_height):
            bad("point outside scaled screen")
        return {"x": min(self.width - 1, round(x / self.scale_x)),
                "y": min(self.height - 1, round(y / self.scale_y))}

    def check_target(self, value):
        """Preflight only: the id exists. Position is resolved live at click time."""
        if isinstance(value, str):
            element = self.elements.get(value)
            if element is None:
                bad(f"unknown element {value}; call elements first")
            if element.get("ref") is None and element.get("x") is None:
                bad(f"element {value} has no reliable position; take a screenshot")
            return None
        return self.point(value)

    def element_point(self, ident, style, deadline):
        """Current click point of an element id; scrolls it into view like a person would."""
        element = self.elements.get(ident)
        if element is None:
            bad(f"unknown element {ident}; call elements first")
        ref = element.get("ref")
        if ref is None:
            if element.get("x") is None:
                bad(f"element {ident} has no reliable position; take a screenshot")
            return {"x": element["x"], "y": element["y"]}
        pending = None  # (y before the last scroll, notches sent, app) -> calibrates px per notch
        stalled = 0
        for step in range(SCROLL_STEPS + 1):
            windows = self.daemon.call("ui.window.list")["windows"]
            located = self.worker.locate(ref, windows)
            state = located.get("state")
            if pending is not None and number(located.get("y")):
                before, sent, app = pending
                moved = before - located["y"]
                if abs(moved) < 3:
                    stalled += 1
                else:
                    stalled = 0
                    if moved * sent > 0 and 8 <= abs(moved / sent) <= 600:
                        self.notch_px[app] = abs(moved / sent)
                pending = None
                if stalled >= 2 and state != "in":
                    bad(f"{ident}: the page does not scroll; call elements or take a screenshot")
            if state == "in":
                x, y = located.get("x"), located.get("y")
                if not (number(x) and number(y) and 0 <= x < self.width and 0 <= y < self.height):
                    bad(f"{ident} is off the screen; call elements or take a screenshot")
                return {"x": round(x), "y": round(y)}
            if state not in {"above", "below"} or not located.get("vp") or step == SCROLL_STEPS:
                why = {"gone": "is gone", "side": "is sideways off the page"}.get(state, "has no reliable position")
                bad(f"{ident} {why}; call elements or take a screenshot")
            vx, vy, vw, vh = located["vp"]
            app = next((w.get("app_id") for w in windows if str(w.get("id")) == str(element.get("wid"))), "")
            delta = located["y"] - (vy + vh * TARGET_BAND)
            notches = round(delta / self.notch_px.get(app, NOTCH_PX)) or (1 if delta > 0 else -1)
            notches = max(-3 * SCROLL_OP_NOTCHES, min(3 * SCROLL_OP_NOTCHES, notches))
            anchor = {"x": min(self.width - 1, max(0, round(vx + vw / 2))),
                      "y": min(self.height - 1, max(0, round(vy + vh * .55)))}
            remaining = notches
            while remaining:
                self.daemon.check_abort()
                if deadline is not None and time.monotonic() >= deadline:
                    raise BatchBudgetReached
                chunk = max(-SCROLL_OP_NOTCHES, min(SCROLL_OP_NOTCHES, remaining))
                self.daemon.call("ui.pointer.scroll", {**anchor, "dx": 0, "dy": chunk, "style": style})
                remaining -= chunk
            pending = (located["y"], notches, app)
            time.sleep(SCROLL_SETTLE_S)
        bad(f"{ident} could not be scrolled into view; call elements or take a screenshot")

    def screenshot(self, args):
        self.screen()
        request = {}
        origin = None
        if "region" in args:
            x, y, w, h = vector(args["region"], 4)
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > self.model_width or y + h > self.model_height:
                bad("region outside scaled screen")
            origin = [x, y, w, h]
            left, top = math.floor(x / self.scale_x), math.floor(y / self.scale_y)
            request["region"] = {"x": left, "y": top,
                                 "width": min(self.width, math.ceil((x + w) / self.scale_x)) - left,
                                 "height": min(self.height, math.ceil((y + h) / self.scale_y)) - top}
        shot = self.daemon.call("ui.screenshot", request)
        with screenshot_source(shot["path"]) as source_file:
            with Image.open(source_file) as source:
                if source.format != "PNG" or source.width * source.height > 64_000_000:
                    raise protocol.ProtocolError("unsafe_screenshot", "Invalid PNG or excessive pixel dimensions")
                source.load()
                if origin is None:
                    size = (self.model_width, self.model_height)
                else:
                    ratio = min(1, self.max_side / max(source.size))
                    size = tuple(max(1, round(v * ratio)) for v in source.size)
                picture = source.convert("RGB")
                if picture.size != size:
                    picture = picture.resize(size, Image.Resampling.LANCZOS)
                output = io.BytesIO()
                picture.save(output, format="PNG", optimize=True)
                encoded = base64.b64encode(output.getvalue()).decode("ascii")
        note = f"{size[0]}x{size[1]}"
        if origin:
            note += f" zoom region={json.dumps(origin, separators=(',', ':'))} actions:global"
        else:
            note += " global coords"
        return [{"type": "image", "mimeType": "image/png", "data": encoded}, text(note)]

    def register(self, rows):
        """Give each row a session-stable id (same role, name and occurrence keeps its id)."""
        seen = {}
        out = []
        for row in rows:
            row = dict(row)
            base = (row["role"], row["name"])
            occurrence = seen.get(base, 0)
            seen[base] = occurrence + 1
            key = base + (occurrence,)
            ident = self.keys.get(key)
            if ident is None:
                ident = f"e{self.next_id}"
                self.next_id += 1
                self.keys[key] = ident
            # A listed point must be on screen, whatever AT-SPI claims for the node.
            if row.get("x") is not None and not (0 <= row["x"] < self.width and 0 <= row["y"] < self.height):
                row["x"] = row["y"] = None
            row["id"] = ident
            self.elements.pop(ident, None)
            self.elements[ident] = row
            out.append(row)
        while len(self.elements) > REGISTRY_CAP:
            stale = next(iter(self.elements))
            del self.elements[stale]
            for key in [k for k, v in self.keys.items() if v == stale]:
                del self.keys[key]
        return out

    def row_text(self, item):
        vis = item.get("vis", "in")
        if vis != "in":
            dist = f"{item.get('dist', 0):.1f}".rstrip("0").rstrip(".") if vis != "side" else ""
            position = {"above": "↑", "below": "↓", "side": "→"}.get(vis, "?") + dist
        elif item.get("x") is None:
            position = "?"
        else:
            px = min(self.model_width - 1, round(item["x"] * self.scale_x))
            py = min(self.model_height - 1, round(item["y"] * self.scale_y))
            position = f"{px},{py}"
        line = f'{item["id"]} {role_alias(item["role"])} {json.dumps(item["name"], ensure_ascii=False)} {position}'
        if item.get("value"):
            line += " =" + json.dumps(item["value"], ensure_ascii=False)
        states = " ".join(item.get("states", []))
        return line + (f" [{states}]" if states else "")

    def page_text(self, page, rows):
        tabs = page.get("tabs") or []
        bits = [page.get("app") or "page", json.dumps(page.get("title", ""), ensure_ascii=False)]
        if page.get("url"):
            bits.append(page["url"][:140])
        if len(tabs) > 1:
            index = next((i for i, tab in enumerate(tabs, 1) if tab[1]), 0)
            bits.append(f"tab {index}/{len(tabs)}")
        if page.get("loading"):
            bits.append("loading")
        if page.get("notready"):
            bits.append("page not ready: wait, or key ctrl+n then ctrl+w once if this browser predates PCPilot")
        address = page.get("address")
        if address and address != page.get("url"):
            bits.append("addr " + json.dumps(address[:100], ensure_ascii=False))
        focus = page.get("focus")
        if focus:
            ident = next((row["id"] for row in rows if row.get("ref") == focus.get("ref")), None)
            bits.append("focus " + (ident or f'{role_alias(focus["role"])} {json.dumps(focus["name"], ensure_ascii=False)}'))
        text_ = " · ".join(bits)
        if len(tabs) > 1:
            text_ += "\ntabs: " + " | ".join(("*" if tab[1] else "") + f"{i} {tab[0][:30]}"
                                             for i, tab in enumerate(tabs[:12], 1))
        return text_

    def list_elements(self, args, diff=False):
        self.screen()
        windows = self.daemon.call("ui.window.list")["windows"]
        if args.get("window", "focused") == "focused" and not any(w.get("focused") for w in windows):
            return [text("No focused window: pass window:<id> (see windows) or focus one.")]
        reply = self.worker.snapshot(windows, args.get("query", ""), args.get("window", "focused"),
                                     args.get("max", 60), args.get("role", ""))
        self.daemon.check_abort()
        if isinstance(reply, list):
            reply = {"elements": reply}
        if not reply.get("elements") and not reply.get("page") and not args.get("query") and not args.get("role"):
            focused = next((w for w in windows if w.get("focused")), {})
            if reply.get("absent") or any(part in str(focused.get("app_id", "")).casefold() for part in BROWSERS):
                return [text("No accessibility tree for this window. A browser started before PCPilot exports "
                             "its page only after a new window: key ctrl+n then ctrl+w once, then elements again; or screenshot.")]
        rows = self.register(reply.get("elements", []))
        page = reply.get("page")
        header = self.page_text(page, rows) if page else ""
        focused = next((w.get("id") for w in windows if w.get("focused")), None)
        scope = (focused, args.get("window", "focused"), args.get("query", ""), args.get("role", ""))
        signature = {row["id"]: (tuple(row.get("states", [])), row.get("value"), row.get("vis", "in") == "in")
                     for row in rows}
        previous, self.view = self.view, {"scope": scope, "sig": signature, "header": header}
        if diff and previous is not None and previous["scope"] == scope:
            lines = [header] if header and header != previous["header"] else []
            for row in rows:
                before = previous["sig"].get(row["id"])
                now = signature[row["id"]]
                if before is None or before[:2] != now[:2] or (now[2] and not before[2]):
                    lines.append(self.row_text(row))
            gone = sum(1 for ident, sig in previous["sig"].items()
                       if ident not in signature or (sig[2] and not signature[ident][2]))
            if gone:
                lines.append(f"-{gone} gone/off-screen")
            return [text("\n".join(lines) or "no change")]
        lines = [header] if header else []
        if page:
            merged = [(h["o"], h) for h in reply.get("headings", [])]
            merged += [(row.get("o", 0) if not row.get("chrome") else 10 ** 9, row) for row in rows]
            merged.sort(key=lambda pair: pair[0])
            for _order, item in merged:
                if "id" in item:
                    lines.append(self.row_text(item))
                else:
                    hint = ""
                    if item.get("vis") in {"above", "below"}:
                        hint = f' {"↑" if item["vis"] == "above" else "↓"}{item["dist"]:g}'
                    lines.append("#" * max(1, min(6, item.get("level") or 1)) + " " + item["name"][:80] + hint)
            if page.get("extra"):
                lines.append(f'+{page["extra"]} more (query/role/max)')
        else:
            lines += [self.row_text(row) for row in rows]
        return [text("\n".join(lines) or "No elements")]

    def action(self, action, style="human", deadline=None):
        name, value = next(iter(action.items()))
        labels = {"click": "Clicando", "double_click": "Clique duplo", "right_click": "Clique direito",
                  "move": "Movendo ponteiro", "drag": "Arrastando", "scroll": "Rolando", "type": "Digitando",
                  "key": "Pressionando atalho", "wait": "Aguardando", "launch": "Abrindo aplicativo",
                  "open": "Abrindo endereço", "focus": "Ativando janela", "window": "Alterando janela",
                  "clipboard_set": "Copiando texto", "clipboard_get": "Lendo área de transferência"}
        label = labels[name]
        if name in {"click", "double_click", "right_click"} and isinstance(value, str):
            element = self.elements.get(value)
            if element:
                label += f' em {element["name"][:80]!r}'
        self.daemon.call("ui.status", {"text": label})
        self.daemon.check_abort()
        if deadline is not None and time.monotonic() >= deadline:
            raise BatchBudgetReached
        if name in {"click", "double_click", "right_click"}:
            target = self.element_point(value, style, deadline) if isinstance(value, str) else self.point(value)
            return self.daemon.call("ui.pointer.click", {**target,
                        "button": "right" if name == "right_click" else "left", "count": 2 if name == "double_click" else 1,
                        "style": style})
        if name == "type":
            return self.type_text(value, style, deadline)
        if name == "launch" and style == "human":
            return self.note_launch(self.human_launch(value, deadline))
        if name == "open" and style == "human":
            return self.human_open(value, deadline)
        if name == "move":
            return self.daemon.call("ui.pointer.move", {**self.point(value), "style": style})
        if name == "drag":
            return self.daemon.call("ui.pointer.drag", {"from": self.point(value[:2]), "to": self.point(value[2:]), "style": style})
        if name == "scroll":
            return self.daemon.call("ui.pointer.scroll", {
                **(self.point(value[:2]) if len(value) == 3 else {}),
                "dx": 0, "dy": value[-1], "style": style})
        if name == "wait":
            end = time.monotonic() + value / 1000
            while time.monotonic() < end:
                self.daemon.check_abort()
                time.sleep(min(.05, max(0, end - time.monotonic())))
            return {}
        if name == "window":
            arguments = {"id": value[0], "action": value[1], "style": style}
            if value[1] == "resize":
                arguments.update(self.window_size(value[2:]))
            elif value[1] == "move":
                arguments.update(self.point(value[2:]))
            return self.daemon.call("ui.window.action", arguments)
        op, arguments = {
            "key": ("key.press", {"keys": value}),
            "launch": ("app.launch", {"app": value}), "open": ("open_uri", {"uri": value}),
            "focus": ("window.action", {"id": value, "action": "activate"}),
            "clipboard_set": ("clipboard.set", {"text": value}), "clipboard_get": ("clipboard.get", {}),
        }[name]
        if name in {"launch", "open", "focus"} and style != "human":
            arguments["style"] = style
        elif name == "focus":
            arguments["style"] = style
        result = self.daemon.call(f"ui.{op}", arguments)
        return self.note_launch(result) if name == "launch" else result

    def note_launch(self, result):
        window = result.get("window") if isinstance(result, dict) else None
        if isinstance(window, dict) and "id" in window:
            self.notes.append(f'launched {window["id"]} {json.dumps(self.window_label(window), ensure_ascii=False)}')
        return result

    # --- text, launch and open: person-like orchestration over extension primitives
    def type_text(self, value, style, deadline):
        pieces = self.split_text(value, TYPE_CHUNK.get(style, 240))
        done = 0
        for index, piece in enumerate(pieces):
            if index and deadline is not None and time.monotonic() >= deadline:
                self.typed_note = f"typed {done}/{len(value)} chars"
                raise BatchBudgetReached
            self.type_piece(piece, style)
            done += len(piece)
        return {}

    @staticmethod
    def split_text(value, limit):
        """Equal-ish pieces <= limit, cut at spaces. Above 120 chars the extension types briskly,
        so for long text every piece stays above that and nothing is left to crawl at 137 ms/char."""
        if len(value) <= limit + (limit // 12 if limit < 1000 else 0):   # 241..260: still one fitted op
            return [value]
        count = -(-len(value) // limit)
        pieces = []
        rest = value
        for left in range(count, 1, -1):
            ideal = -(-len(rest) // left)
            low = max(121 if limit > 120 else 1, ideal - 15)
            cut = rest.rfind(" ", low, ideal + 1)
            cut = cut + 1 if cut != -1 else ideal
            pieces.append(rest[:cut])
            rest = rest[cut:]
        pieces.append(rest)
        return pieces

    def type_piece(self, piece, style):
        try:
            self.daemon.call("ui.key.type", {"text": piece, "style": style})
        except protocol.ProtocolError as exc:
            # The extension refuses text it cannot finish inside its op guard: halve it.
            if exc.code != "bad_args" or len(piece) < 24:
                raise
            half = piece.rfind(" ", 0, len(piece) // 2 + 1)
            half = half + 1 if half > 4 else len(piece) // 2
            self.type_piece(piece[:half], style)
            self.type_piece(piece[half:], style)

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.daemon.check_abort()
            time.sleep(min(.05, max(0, end - time.monotonic())))

    def desktop_info(self):
        try:
            return self.daemon.call("ui.desktop.info") or {}
        except protocol.ProtocolError as exc:
            if exc.code != "unknown_op":
                raise
        try:
            return {"pointer": self.daemon.call("ui.pointer.position")}
        except protocol.ProtocolError as exc:
            if exc.code != "unknown_op":
                raise
            return {}

    def human_launch(self, name, deadline):
        """Super, type the app name, Enter: the way a person opens an app."""
        try:
            apps = self.daemon.call("ui.app.search", {"query": name}).get("apps")
        except protocol.ProtocolError as exc:
            if exc.code != "unknown_op":
                raise
            apps = None
        if apps is not None and not apps:
            # A desktop id or an unknown name cannot be typed into the overview: the programmatic
            # launch resolves ids and reports unknown apps itself.
            return self.daemon.call("ui.app.launch", {"app": name, "style": "fast"})
        label = (apps[0].get("name") if apps else None) or name
        before = {w["id"] for w in self.daemon.call("ui.window.list")["windows"]}
        overview = self.desktop_info().get("overview")  # None: an extension that cannot tell
        if overview is not True:   # never toggle an already open overview shut
            try:
                self.daemon.call("ui.key.press", {"keys": "super"})
            except protocol.ProtocolError as exc:
                if exc.code not in {"bad_args", "unknown_op", "failed"}:
                    raise
                return self.daemon.call("ui.app.launch", {"app": name, "style": "fast"})
            if overview is None:
                self.pause(.7)
            else:
                end = time.monotonic() + 1.5
                while time.monotonic() < end and self.desktop_info().get("overview") is not True:
                    self.pause(.15)
                if self.desktop_info().get("overview") is not True:
                    return self.daemon.call("ui.app.launch", {"app": name, "style": "fast"})
        else:
            self.pause(.2)
        self.type_text(label, "human", deadline)
        self.pause(.4)
        self.daemon.call("ui.key.press", {"keys": "Return"})
        wanted = label.casefold()
        end = time.monotonic() + 8
        while time.monotonic() < end:
            self.pause(.4)
            windows = self.daemon.call("ui.window.list")["windows"]
            fresh = [w for w in windows if w["id"] not in before and not w.get("minimized")]
            running = [w for w in windows if w.get("focused") and wanted in
                       f'{w.get("app_id", "")} {w.get("title", "")}'.casefold()]
            found = (fresh or running or [None])[0]
            if found:
                self.notes.append(f'launched {found["id"]} {json.dumps(self.window_label(found), ensure_ascii=False)}')
                return {}
        try:
            self.daemon.call("ui.key.press", {"keys": "Escape"})
        except protocol.ProtocolError:
            pass
        raise protocol.ProtocolError("failed", f"launch {name}: no window appeared; take a screenshot")

    def human_open(self, uri, deadline):
        """Ctrl+L, type the address, Enter in the focused browser; otherwise the default handler."""
        if "\n" in uri or "\r" in uri:
            bad("open: address must be a single line")
        windows = self.daemon.call("ui.window.list")["windows"]
        focused = next((w for w in windows if w.get("focused")), None)
        if not focused or not any(part in str(focused.get("app_id", "")).casefold() for part in BROWSERS):
            return self.daemon.call("ui.open_uri", {"uri": uri, "style": "fast"})
        self.daemon.call("ui.key.press", {"keys": "ctrl+l"})
        self.pause(.3)
        self.type_text(uri, "human", deadline)
        self.pause(.25)
        self.daemon.call("ui.key.press", {"keys": "Return"})
        return {}

    def windows_text(self):
        self.screen()
        rows = self.daemon.call("ui.window.list")["windows"]
        info = self.desktop_info()
        header = []
        workspace = info.get("workspace")
        count = workspace.get("count") if isinstance(workspace, dict) else None
        if isinstance(workspace, dict) and number(workspace.get("active")):
            header.append(f'ws {workspace["active"] + 1}/{count}')
        monitors = info.get("monitors") if isinstance(info.get("monitors"), list) else []
        if monitors:
            parts = []
            for index, mon in enumerate(monitors):
                size = f'{round(mon["width"] * self.scale_x)}x{round(mon["height"] * self.scale_y)}'
                if len(monitors) > 1:
                    size = f'{index}:{size}+{round(mon["x"] * self.scale_x)}+{round(mon["y"] * self.scale_y)}'
                if number(mon.get("scale")) and mon["scale"] != 1:
                    size += f'@{mon["scale"]:g}'
                parts.append(size + ("*" if len(monitors) > 1 and mon.get("primary") else ""))
            header.append("mon " + " ".join(parts))
        if "overview" in info:
            header.append("overview " + ("on" if info["overview"] else "off"))
        pointer = info.get("pointer")
        if isinstance(pointer, dict) and number(pointer.get("x")) and number(pointer.get("y")):
            header.append(f'ptr {round(pointer["x"] * self.scale_x)},{round(pointer["y"] * self.scale_y)}')
        if info.get("scheme"):
            header.append(str(info["scheme"]))
        lines = [" · ".join(header)] if header else []
        for w in rows:
            bounds = ",".join(str(round(w[k] * (self.scale_x if k in {"x", "width"} else self.scale_y)))
                              for k in ("x", "y", "width", "height"))
            line = f'{w["id"]} {"* " if w.get("ai") else ""}{w.get("app_id", "?")} {json.dumps(w["title"], ensure_ascii=False)} {bounds}'
            if isinstance(count, int) and count > 1 and number(w.get("workspace")):
                line += " w*" if w["workspace"] < 0 else f' w{w["workspace"] + 1}'
            if len(monitors) > 1 and number(w.get("monitor")) and w["monitor"] >= 0:
                line += f' m{w["monitor"]}'
            flags = [name for name, key in (("focused", "focused"), ("min", "minimized"), ("max", "maximized"),
                                            ("full", "fullscreen")) if w.get(key)]
            if w.get("new") and not w.get("ai"):
                flags.append("new")  # AI-opened windows already carry the * prefix
            lines.append(line + (f' [{" ".join(flags)}]' if flags else ""))
        return "\n".join(lines) or "No windows"

    def window_size(self, value):
        width, height = vector(value, 2)
        if width <= 0 or height <= 0:
            bad("window dimensions must be positive")
        return {"width": max(1, round(width / self.scale_x)),
                "height": max(1, round(height / self.scale_y))}

    def validate(self, name, args):
        definition = next((tool["inputSchema"] for tool in TOOLS if tool["name"] == name), None)
        if definition is None:
            bad(f"unknown tool {name}")
        if not isinstance(args, dict) or set(args) - definition["properties"].keys() or any(key not in args for key in definition["required"]):
            bad("invalid tool arguments")
        if name == "control":
            if args["action"] not in ("start", "stop", "status") or ("resume" in args and not isinstance(args["resume"], bool)):
                bad("invalid control action/resume")
            if args.get("resume") and args["action"] != "start":
                bad("resume requires start")
            if any(key in args for key in ("keep", "cleanup")) and args["action"] != "stop":
                bad("keep/cleanup require stop")
            if "cleanup" in args and not isinstance(args["cleanup"], bool):
                bad("cleanup must be boolean")
            if "keep" in args and (not isinstance(args["keep"], list) or len(args["keep"]) > 200 or
                    any(type(ident) not in (str, int) for ident in args["keep"])):
                bad("keep must be a list of window ids")
        elif name == "screenshot" and "region" in args:
            vector(args["region"], 4)
        elif name == "elements":
            if (not isinstance(args.get("query", ""), str) or
                    not isinstance(args.get("window", "focused"), (str, int)) or isinstance(args.get("window"), bool) or
                    type(args.get("max", 60)) is not int or not 1 <= args.get("max", 60) <= 200):
                bad("invalid elements arguments")
        elif name == "act":
            actions = args.get("actions")
            if (not isinstance(actions, list) or not 1 <= len(actions) <= 100 or
                    args.get("observe", "none") not in ("none", "screenshot", "elements") or
                    args.get("speed", "human") not in ("human", "fast", "instant")):
                bad("invalid actions/observe/speed")
            for action in actions:
                if not isinstance(action, dict) or len(action) != 1:
                    bad("each action must have one key")
                key, value = next(iter(action.items()))
                if key not in ACTION_VALUES:
                    bad(f"unknown action {key}")
                if key in ("click", "double_click", "right_click"):
                    if not isinstance(value, str):
                        vector(value, 2)
                elif key in ("move", "drag", "scroll"):
                    if key == "scroll":
                        vector(value, 1 if isinstance(value, list) and len(value) == 1 else 3)
                    else:
                        vector(value, {"move": 2, "drag": 4}[key])
                elif key == "wait":
                    if type(value) is not int or not 0 <= value <= 10000:
                        bad("wait must be 0..10000 ms")
                elif key == "window":
                    if (not isinstance(value, list) or len(value) not in (2, 4) or
                            not isinstance(value[0], (str, int)) or isinstance(value[0], bool) or
                            not isinstance(value[1], str)):
                        bad("invalid window action")
                    if value[1] in ("resize", "move"):
                        if len(value) != 4:
                            bad("window resize/move requires two coordinates")
                        vector(value[2:], 2)
                        if value[1] == "resize" and any(v <= 0 for v in value[2:]):
                            bad("window dimensions must be positive")
                    elif len(value) != 2 or value[1] not in (
                            "activate", "close", "maximize", "unmaximize", "minimize", "fullscreen", "fit"):
                        bad("invalid window action")
                elif key == "focus":
                    if not isinstance(value, (str, int)) or isinstance(value, bool):
                        bad("invalid window id")
                elif key == "clipboard_get":
                    if value is not True:
                        bad("clipboard_get must be true")
                elif not isinstance(value, str):
                    bad(f"{key} requires text")

    @staticmethod
    def window_label(window, *, focus=False):
        title = " ".join(str(window.get("title", "")).split())
        app = str(window.get("app_id", ""))
        if focus and "traycer" in f"{app} {title}".casefold():
            return "Traycer"
        return (title or app.removesuffix(".desktop").rsplit(".", 1)[-1] or "window")[:100]

    def stop(self, args):
        try:
            return self._stop_session(args)
        finally:
            self._a11y_off()

    def _stop_session(self, args):
        # Never auto-begin to tidy up; a previous abort/cancel prohibits *all*
        # further sends, including session.end. Inactive stop is entirely local.
        # Cancellation may replace the owner while an idle wrapup is unwinding.
        # Never end that new connection's session from the old cleanup.
        daemon = self.daemon
        daemon.check_abort()
        self.reset_elements()
        if not daemon.active:
            return [text("stopped")]
        wrapup = None
        failure = None
        try:
            wrapup = daemon.call("ui.session.wrapup", {
                "keep": args.get("keep", []), "close_new": args.get("cleanup", True),
                "return_to": "origin"}, timeout=WRAPUP_TIMEOUT_S)
        except protocol.ProtocolError as exc:
            if daemon.aborted:
                raise protocol.ProtocolError("user_aborted", "User took control") from exc
            if exc.code in {"user_aborted", "cancelled", "timeout", "disconnected"}:
                raise
            daemon.check_abort()
            # Older extensions have no wrapup. Other UI failures must still
            # release a live session, with a compact indication of failed cleanup.
            if exc.code != "unknown_op":
                failure = exc.code
        daemon.check_abort()
        daemon.call("session.end")
        daemon.active = False
        parts = ["stopped"]
        if wrapup is not None:
            parts.append(f"closed {len(wrapup.get('closed', []))}")
            if wrapup.get("restored"):
                parts.append(f"restored {len(wrapup['restored'])}")
            left_open = wrapup.get("left_open", [])
            if left_open:
                names = ", ".join(json.dumps(self.window_label(window), ensure_ascii=False)
                                  for window in left_open[:3])
                if len(left_open) > 3:
                    names += f" +{len(left_open) - 3}"
                parts.append(f"left open: {names}")
            if wrapup.get("focused"):
                parts.append(f"returned to {self.window_label(wrapup['focused'], focus=True)}")
            if wrapup.get("incomplete"):
                parts.append("cleanup incomplete")
        elif failure:
            parts.append(f"cleanup failed ({failure})")
        return [text(" · ".join(parts))]

    def tool(self, name, args):
        with self.operation_lock:
            with self.idle_condition:
                self.idle_deadline = None
                self.idle_condition.notify_all()
            try:
                content = self._tool(name, args)
                if self.idle_released and self.daemon.active:
                    content.append(text("session resumed"))
                    self.idle_released = False
                if name == "control" and args.get("action") == "stop":
                    self.idle_released = False
                return content
            finally:
                with self.idle_condition:
                    if (self.autostop_s and self.daemon.active and not self.daemon.aborted and
                            not self.cancelled.is_set() and not self.request_cancelled.is_set()):
                        self.idle_deadline = time.monotonic() + self.autostop_s
                        if self.idle_thread is None:
                            self.idle_thread = threading.Thread(target=self._idle_watchdog,
                                                                name="pcpilot-idle", daemon=True)
                            self.idle_thread.start()
                    self.idle_condition.notify_all()

    def _tool(self, name, args):
        if self.cancelled.is_set():
            raise protocol.ProtocolError("cancelled", "MCP input closed")
        if self.request_cancelled.is_set():
            raise protocol.ProtocolError("cancelled", "Request cancelled")
        self.validate(name, args)
        deadline = time.monotonic() + ACT_BUDGET_S if name == "act" else None
        if name == "control":
            action = args["action"]
            if action == "start":
                self.daemon.begin(args.get("resume", False))
                self._a11y_on()
                return [text("active")]
            if action == "stop":
                return self.stop(args)
            self.daemon.check_abort()
            status = self.daemon.call("session.status")
            if status.get("aborted"):
                self.daemon.aborted = True
                self.daemon.check_abort()
            return [text(json.dumps(status, separators=(",", ":")))]
        self.daemon.ensure()
        self._a11y_on()
        if name == "screenshot":
            return self.screenshot(args)
        if name == "elements":
            return self.list_elements(args)
        if name == "windows":
            return [text(self.windows_text())]
        self.screen()
        self.notes = []
        self.typed_note = None
        # Preflight all coordinates and ids before executing the batch.
        for action in args["actions"]:
            key, value = next(iter(action.items()))
            if key in ("click", "double_click", "right_click"):
                self.check_target(value)
            elif key == "move" or (key == "scroll" and len(value) == 3):
                self.point(value[:2])
            elif key == "drag":
                self.point(value[:2])
                self.point(value[2:])
            elif key == "window":
                if value[1] == "resize":
                    self.window_size(value[2:])
                elif value[1] == "move":
                    self.point(value[2:])
        style = args.get("speed", "human")
        requested = style
        downgraded = (style == "human" and
                      sum(next(iter(action)) in STYLED_ACTIONS - {"type"} for action in args["actions"]) > HUMAN_BATCH_LIMIT)
        if downgraded:
            style = "fast"
        clipboard = []
        completed = 0
        try:
            for action in args["actions"]:
                self.daemon.check_abort()
                if time.monotonic() >= deadline:
                    break
                try:
                    # Typing is what people watch: a long batch never turns it into a pasted blob.
                    result = self.action(action, "human" if "type" in action and requested == "human" else style,
                                         deadline)
                except BatchBudgetReached:
                    break
                completed += 1
                if "clipboard_get" in action:
                    clipboard.append(result.get("text", ""))
            self.daemon.check_abort()
            partial = completed != len(args["actions"])
            note = f"partial: {completed}/{len(args['actions'])} done, budget" if partial else f"ok {completed}"
            if self.typed_note:
                note += f" · {self.typed_note}"
            note += "".join(f" · {item}" for item in self.notes)
            if downgraded:
                note += " · speed fast (batch)"
            observation = args.get("observe", "none")
            if observation != "none" and time.monotonic() >= deadline:
                note += " · observe skipped (budget)"
                observation = "none"
            content = [text(note + ("\n" + "\n".join(clipboard) if clipboard else ""))]
            if observation == "screenshot":
                content += self.screenshot({})
            elif observation == "elements":
                content += self.list_elements({}, diff=True)
            return content
        except protocol.ProtocolError as exc:
            raise protocol.ProtocolError(exc.code, f"after {completed} actions: {exc.message}") from exc

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        if "id" not in request:
            return None
        ident = request["id"]
        def error(code, message):
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
        if isinstance(ident, (dict, list, bool)):
            return error(-32600, "Invalid request id")
        method, params = request["method"], request.get("params", {})
        if not isinstance(params, dict):
            return error(-32602, "Expected object params")
        if method == "initialize":
            info = params.get("clientInfo", {})
            if not isinstance(info, dict):
                return error(-32602, "Invalid clientInfo")
            if not self.initialized:
                self.client_identity(info)
            version = params.get("protocolVersion")
            self.initialized = True
            result = {"protocolVersion": version if isinstance(version, str) and version in VERSIONS else "2025-06-18",
                      "serverInfo": {"name": "pcpilot", "version": "1.0.0"},
                      "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return error(-32000, "Initialize first")
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            if not isinstance(params.get("name"), str):
                return error(-32602, "Tool name required")
            if params["name"] not in {tool["name"] for tool in TOOLS}:
                return error(-32602, "Unknown tool")
            try:
                content = self.tool(params["name"], params.get("arguments", {}))
                if params["name"] != "control":
                    self.daemon.check_abort()
                result = {"content": content}
            except protocol.ProtocolError as exc:
                message = ("USER_TOOK_CONTROL: stop and wait for the user. " + exc.message
                           if exc.code == "user_aborted" else str(exc))
                result = {"content": [text(message)], "isError": True}
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                print(f"pcpilot tool: {exc}", file=sys.stderr)
                result = {"content": [text(f"failed: {exc}")], "isError": True}
        else:
            return error(-32601, "Method not found")
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def prepare_request(self, ident):
        with self.request_lock:
            # A cancelled request's socket remains closed until that dispatch
            # has unwound. Only the next request can create a new owner.
            if self.request_cancelled.is_set() and not self.cancelled.is_set():
                label, theme, aborted = self.daemon.label, self.daemon.theme, self.daemon.aborted
                self.daemon = DaemonClient()
                self.daemon.on_end = self._on_daemon_end
                self.daemon.label = label
                self.daemon.theme = theme
                # Request cancellation does not create a user abort, and it
                # must not erase one already observed from hotkey/release.
                self.daemon.aborted = aborted
                self.request_cancelled.clear()
                if hasattr(self.worker, "reset_cancel"):
                    self.worker.reset_cancel()
            self.current_request = ident

    def cancel(self, request_id=None):
        with self.request_lock:
            if request_id is not None and request_id != self.current_request:
                if request_id in self.pending_requests:
                    self.cancelled_requests.add(request_id)
                return
            self.request_cancelled.set()
            if request_id is None:
                self.cancelled.set()
            # Close the owner before touching the worker. No additional action
            # from this batch can send, and pcpilotd releases the session.
            self.daemon.close()
            with self.idle_condition:
                self.idle_deadline = None
                self.idle_condition.notify_all()
            if request_id is not None or getattr(self.worker, "held", False):
                # The owner session is over: switch accessibility back (a killed worker is
                # covered by its own EOF handling and by the --recover safety net).
                self._on_daemon_end()
            if request_id is None:
                self.worker.close()
            elif hasattr(self.worker, "cancel"):
                # A healthy idle worker can be kept. A pending AT-SPI read is
                # killed so cancellation cannot wait on an unresponsive app.
                self.worker.cancel()

    def close(self):
        self.cancel()


def main():
    server = Server()
    inbox = queue.Queue()

    def receive(line):
        try:
            if len(line) > protocol.MAX_LINE:
                raise ValueError("request line too long")
            request = json.loads(line)
        except (ValueError, UnicodeError):
            inbox.put((False, None))
            return
        if (isinstance(request, dict) and request.get("jsonrpc") == "2.0" and
                request.get("method") == "notifications/cancelled" and "id" not in request):
            params = request.get("params")
            if isinstance(params, dict) and type(params.get("requestId")) in (str, int):
                server.cancel(params["requestId"])
        else:
            with server.request_lock:
                ident = request.get("id") if isinstance(request, dict) else None
                if type(ident) in (str, int):
                    server.pending_requests.add(ident)
                inbox.put((True, request))

    def read_stdin():
        # os.read avoids a daemon thread holding Python's buffered-stdin lock
        # at interpreter shutdown when SIGTERM arrives with stdin still open.
        buffered = bytearray()
        discarding = False
        try:
            while chunk := os.read(sys.stdin.fileno(), 65536):
                buffered.extend(chunk)
                while (end := buffered.find(b"\n")) != -1:
                    line = bytes(buffered[:end + 1])
                    del buffered[:end + 1]
                    if discarding:
                        discarding = False
                    else:
                        receive(line)
                if len(buffered) > protocol.MAX_LINE:
                    if not discarding:
                        inbox.put((False, None))
                    discarding = True
                    buffered.clear()
            if buffered and not discarding:
                receive(bytes(buffered))
        except OSError as exc:
            print(f"pcpilot stdin: {exc}", file=sys.stderr)
        finally:
            server.cancel()
            inbox.put(None)

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=read_stdin, name="pcpilot-stdin", daemon=True).start()
    try:
        while (item := inbox.get()) is not None:
            parsed, request = item
            if not parsed:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                with server.request_lock:
                    ident = request.get("id") if isinstance(request, dict) else None
                    known_id = type(ident) in (str, int)
                    if known_id:
                        server.pending_requests.discard(ident)
                    skip = known_id and ident in server.cancelled_requests
                    if skip:
                        server.cancelled_requests.discard(ident)
                    server.prepare_request(ident)
                try:
                    if skip:
                        reply = {"jsonrpc": "2.0", "id": ident, "result": {
                            "content": [text("cancelled: request cancelled before execution")], "isError": True}}
                    else:
                        reply = server.dispatch(request)
                finally:
                    with server.request_lock:
                        server.current_request = None
            if reply is not None:
                sys.stdout.write(json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
    finally:
        server.close()


if __name__ == "__main__":
    main()
