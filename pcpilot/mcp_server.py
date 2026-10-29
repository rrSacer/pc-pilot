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

VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
INSTRUCTIONS = ("Prefer elements, shortcuts and launch; batch act actions. Observe only when needed. "
                "Coordinates are global scaled screen pixels (longest side <=1280 by default). "
                "Call control stop when done. USER_TOOK_CONTROL: stop and wait for the user; "
                "resume only on their explicit request.")


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
    "drag": FOUR, "scroll": {**POINT, "minItems": 3, "maxItems": 3},
    "type": {"type": "string"}, "key": {"type": "string"},
    "wait": {"type": "integer", "minimum": 0, "maximum": 10000},
    "launch": {"type": "string"}, "open": {"type": "string"},
    "focus": {"type": ["string", "integer"]},
    "window": {"type": "array", "minItems": 2, "maxItems": 2},
    "clipboard_set": {"type": "string"}, "clipboard_get": {"const": True},
}
TOOLS = [
    {"name": "control", "description": "Start/stop/status. Stop when done; resume only if user asks.",
     "inputSchema": schema({"action": enum("start", "stop", "status"), "resume": {"type": "boolean"}}, ["action"])},
    {"name": "screenshot", "description": "Scaled screen; region [x,y,w,h] zooms. Prefer elements.",
     "inputSchema": schema({"region": FOUR})},
    {"name": "act", "description": "Batch actions; click accepts [x,y] or element id. Observe only if needed.",
     "inputSchema": schema({"actions": {"type": "array", "minItems": 1, "maxItems": 100,
          "items": {**schema(ACTION_VALUES), "minProperties": 1, "maxProperties": 1}},
                            "observe": enum("none", "screenshot", "elements")}, ["actions"])},
    {"name": "elements", "description": "Visible interactive elements; prefer to screenshots. ? = position unknown.",
     "inputSchema": schema({"query": {"type": "string"}, "window": {"type": ["string", "integer"]},
                            "max": {"type": "integer", "minimum": 1, "maximum": 200}})},
    {"name": "windows", "description": "Windows with ids and scaled bounds.", "inputSchema": schema()},
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
        self.label = "AI"

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
                        self.active = False
                        if msg.get("reason") in {"user_hotkey", "user_release"}:
                            self.aborted = True
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

    def check_abort(self):
        if self.closed:
            raise protocol.ProtocolError("cancelled", "MCP connection closed; restart MCP")
        if self.aborted:
            raise protocol.ProtocolError("user_aborted", "User took control")

    def call(self, op, args=None):
        self.connect()
        if op.startswith("ui."):
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
                if self.closed:
                    raise protocol.ProtocolError("cancelled", "MCP connection closed; restart MCP")
                self.sock.sendall(protocol.encode(protocol.request(ident, op, args)))
            try:
                reply = waiter.get(timeout=26)
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
        self.call("session.begin", {"label": self.label, "resume": resume})
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
        self.closed = False
        self.cancelled = False
        self.busy = False

    def start(self):
        user = desktop_identity()
        runtime = desktop_runtime(user)
        env = {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name,
               "PATH": "/usr/bin:/bin", "LANG": os.environ.get("LANG", "C.UTF-8"),
               "XDG_RUNTIME_DIR": runtime,
               "DBUS_SESSION_BUS_ADDRESS": os.environ.get("PCPILOT_A11Y_BUS_ADDRESS", f"unix:path={runtime}/bus")}
        env.update({key: value for key, value in os.environ.items() if re.fullmatch(r"LC_[A-Z_]+", key)})
        command = ["/usr/bin/python3", "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "a11y_worker.py")]
        if os.geteuid() != user.pw_uid:
            command = ["/usr/bin/setpriv", f"--reuid={user.pw_uid}", f"--regid={user.pw_gid}", "--init-groups", *command]
        with self.lock:
            if self.closed or self.cancelled:
                raise protocol.ProtocolError("cancelled", "AT-SPI request cancelled")
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=sys.stderr, env=env, bufsize=0)

    def snapshot(self, windows, query="", window="focused", maximum=60):
        with self.lock:
            if self.closed or self.cancelled:
                raise protocol.ProtocolError("cancelled", "AT-SPI request cancelled")
            self.busy = True
        try:
            if self.process is None or self.process.poll() is not None:
                self.start()
            with self.lock:
                process = self.process
                if self.closed or self.cancelled or process is None:
                    raise protocol.ProtocolError("cancelled", "AT-SPI request cancelled")
            process.stdin.write(protocol.encode({"windows": windows, "query": query,
                                                 "window": window, "max": maximum}))
            process.stdin.flush()
            ready, _, _ = select.select([process.stdout], [], [], 15)
            if not ready:
                raise TimeoutError("AT-SPI timed out")
            reply = protocol.decode(process.stdout.readline(protocol.MAX_LINE + 1))
            if "error" in reply:
                raise protocol.ProtocolError("a11y_unavailable", reply["error"])
            return reply["elements"]
        except (OSError, ValueError, TimeoutError):
            self._stop_process()
            raise
        finally:
            with self.lock:
                self.busy = False

    def _stop_process(self):
        with self.lock:
            process, self.process = self.process, None
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
                process.stdin.close()
                process.stdout.close()

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
        self._stop_process()


class Server:
    def __init__(self, daemon=None, worker=None):
        self.daemon = daemon or DaemonClient()
        self.worker = worker or AccessibilityWorker()
        self.max_side = int(os.environ.get("PCPILOT_MAX_SIDE", "1280"))
        if self.max_side < 1:
            raise ValueError("PCPILOT_MAX_SIDE must be positive")
        self.elements = {}
        self.scale_x = self.scale_y = 1.0
        self.width = self.height = self.model_width = self.model_height = 0
        self.cancelled = threading.Event()  # EOF / signal: terminal
        self.request_cancelled = threading.Event()
        self.request_lock = threading.RLock()
        self.current_request = None
        self.pending_requests = set()
        self.cancelled_requests = set()
        self.initialized = False

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

    def click_point(self, value):
        if isinstance(value, str):
            element = self.elements.get(value)
            if element is None:
                bad(f"unknown element {value}; call elements first")
            if element.get("x") is None:
                bad(f"element {value} has no reliable position; take a screenshot")
            return {"x": element["x"], "y": element["y"]}
        return self.point(value)

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
            note += f" zoom region={json.dumps(origin, separators=(',', ':'))}; actions use global screen coordinates"
        else:
            note += " global screen coordinates"
        return [{"type": "image", "mimeType": "image/png", "data": encoded}, text(note)]

    def list_elements(self, args):
        self.screen()
        windows = self.daemon.call("ui.window.list")["windows"]
        snapshot = self.worker.snapshot(windows, args.get("query", ""), args.get("window", "focused"), args.get("max", 60))
        self.daemon.check_abort()
        self.elements = {f"e{i}": dict(item) for i, item in enumerate(snapshot, 1)}
        # A visible window may extend off-screen. Never offer an off-screen
        # center as a click target, even if AT-SPI calls the node visible.
        for item in self.elements.values():
            if item.get("x") is not None and not (
                    0 <= item["x"] < self.width and 0 <= item["y"] < self.height):
                item["x"] = item["y"] = None
        lines = []
        for ident, item in self.elements.items():
            if item.get("x") is None:
                position = "?"
            else:
                px = min(self.model_width - 1, round(item["x"] * self.scale_x))
                py = min(self.model_height - 1, round(item["y"] * self.scale_y))
                position = f"{px},{py}"
            states = " ".join(item.get("states", []))
            lines.append(f'{ident} {item["role"]} {json.dumps(item["name"], ensure_ascii=False)} {position}' + (f" [{states}]" if states else ""))
        return [text("\n".join(lines) or "No elements")]

    def action(self, action):
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
        if name in {"click", "double_click", "right_click"}:
            return self.daemon.call("ui.pointer.click", {**self.click_point(value),
                        "button": "right" if name == "right_click" else "left", "count": 2 if name == "double_click" else 1})
        if name == "move":
            return self.daemon.call("ui.pointer.move", self.point(value))
        if name == "drag":
            return self.daemon.call("ui.pointer.drag", {"from": self.point(value[:2]), "to": self.point(value[2:])})
        if name == "scroll":
            return self.daemon.call("ui.pointer.scroll", {**self.point(value[:2]), "dx": 0, "dy": value[2]})
        if name == "wait":
            end = time.monotonic() + value / 1000
            while time.monotonic() < end:
                self.daemon.check_abort()
                time.sleep(min(.05, max(0, end - time.monotonic())))
            return {}
        op, arguments = {
            "type": ("key.type", {"text": value}), "key": ("key.press", {"keys": value}),
            "launch": ("app.launch", {"app": value}), "open": ("open_uri", {"uri": value}),
            "focus": ("window.action", {"id": value, "action": "activate"}),
            "window": ("window.action", {"id": value[0], "action": value[1]} if name == "window" else {}),
            "clipboard_set": ("clipboard.set", {"text": value}), "clipboard_get": ("clipboard.get", {}),
        }[name]
        return self.daemon.call(f"ui.{op}", arguments)

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
        elif name == "screenshot" and "region" in args:
            vector(args["region"], 4)
        elif name == "elements":
            if (not isinstance(args.get("query", ""), str) or
                    not isinstance(args.get("window", "focused"), (str, int)) or isinstance(args.get("window"), bool) or
                    type(args.get("max", 60)) is not int or not 1 <= args.get("max", 60) <= 200):
                bad("invalid elements arguments")
        elif name == "act":
            actions = args.get("actions")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 100 or args.get("observe", "none") not in ("none", "screenshot", "elements"):
                bad("invalid actions/observe")
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
                    vector(value, {"move": 2, "drag": 4, "scroll": 3}[key])
                elif key == "wait":
                    if type(value) is not int or not 0 <= value <= 10000:
                        bad("wait must be 0..10000 ms")
                elif key == "window":
                    if (not isinstance(value, list) or len(value) != 2 or
                            not isinstance(value[0], (str, int)) or isinstance(value[0], bool) or
                            value[1] not in ("activate", "close", "maximize", "unmaximize", "minimize", "fullscreen")):
                        bad("invalid window action")
                elif key == "focus":
                    if not isinstance(value, (str, int)) or isinstance(value, bool):
                        bad("invalid window id")
                elif key == "clipboard_get":
                    if value is not True:
                        bad("clipboard_get must be true")
                elif not isinstance(value, str):
                    bad(f"{key} requires text")

    def tool(self, name, args):
        if self.cancelled.is_set():
            raise protocol.ProtocolError("cancelled", "MCP input closed")
        if self.request_cancelled.is_set():
            raise protocol.ProtocolError("cancelled", "Request cancelled")
        self.validate(name, args)
        if name == "control":
            action = args["action"]
            if action == "start":
                self.daemon.begin(args.get("resume", False))
                return [text("active")]
            if action == "stop":
                self.daemon.call("session.end")
                self.daemon.active = False
                self.elements.clear()
                return [text("stopped")]
            status = self.daemon.call("session.status")
            if status.get("aborted"):
                self.daemon.aborted = True
                self.daemon.check_abort()
            return [text(json.dumps(status, separators=(",", ":")))]
        self.daemon.ensure()
        if name == "screenshot":
            return self.screenshot(args)
        if name == "elements":
            return self.list_elements(args)
        if name == "windows":
            self.screen()
            rows = []
            for w in self.daemon.call("ui.window.list")["windows"]:
                bounds = ",".join(str(round(w[k] * (self.scale_x if k in {"x", "width"} else self.scale_y)))
                                  for k in ("x", "y", "width", "height"))
                rows.append(f'{w["id"]} {w.get("app_id", "?")} {json.dumps(w["title"], ensure_ascii=False)} {bounds}' + (" [focused]" if w.get("focused") else ""))
            return [text("\n".join(rows) or "No windows")]
        self.screen()
        # Preflight all coordinates and ids before executing the batch.
        for action in args["actions"]:
            key, value = next(iter(action.items()))
            if key in ("click", "double_click", "right_click"):
                self.click_point(value)
            elif key in ("move", "scroll"):
                self.point(value[:2])
            elif key == "drag":
                self.point(value[:2])
                self.point(value[2:])
        clipboard = []
        completed = 0
        try:
            for action in args["actions"]:
                self.daemon.check_abort()
                result = self.action(action)
                completed += 1
                if "clipboard_get" in action:
                    clipboard.append(result.get("text", ""))
            self.daemon.check_abort()
            content = [text(f"ok {completed}" + ("\n" + "\n".join(clipboard) if clipboard else ""))]
            observation = args.get("observe", "none")
            if observation == "screenshot":
                content += self.screenshot({})
            elif observation == "elements":
                content += self.list_elements({})
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
            self.daemon.label = str(info.get("name", "AI"))[:80]
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
                label, aborted = self.daemon.label, self.daemon.aborted
                self.daemon = DaemonClient()
                self.daemon.label = label
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
