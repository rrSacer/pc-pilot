"""A private UNIX socket daemon fixture for MCP tests; never touches /run/pcpilot."""
import json
import os
import socket
import tempfile
import threading
from pathlib import Path


class FakeDaemon:
    def __init__(self, directory):
        self.path = str(Path(directory) / "pcpilot-test.sock")
        self.runtime_dir = str(Path(directory) / "runtime")
        Path(self.runtime_dir, "pcpilot").mkdir(parents=True, exist_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(8)
        self.listener.settimeout(.1)
        self.running = True
        self.requests = []
        self.width, self.height = 1920, 1080
        self.windows = [{"id": 42, "title": "Editor", "app_id": "org.test.Editor", "pid": 1,
                         "x": 100, "y": 50, "width": 800, "height": 600,
                         "buffer": {"x": 96, "y": 46, "width": 808, "height": 608},
                         "focused": True, "minimized": False, "workspace": 0}]
        self.session = {"active": False, "owner_is_me": False, "aborted": False,
                        "ui_connected": True, "idle_timeout_s": 120, "remaining_s": 120}
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.clients = []
        self.shot_paths = []
        self.event_after_op = {}
        self.screenshot_file_bytes = None
        self.wrapup_error = None
        self.wrapup_result = {"closed": ["closed-a", "closed-b"],
                              "restored": ["clipboard"],
                              "left_open": [{"title": name, "app_id": "firefox"}
                                            for name in ("Firefox — Docs", "Terminal — build", "Editor — notes", "Browser — search")],
                              "focused": {"title": "Traycer - Codex", "app_id": "Traycer"}}
        self.wrapup_gate = None
        self.wrapup_event_before_reply = None
        self.desktop = None          # dict served by ui.desktop.info; None = unknown_op (old extension)
        self.scroll_hook = None      # called with (args) on ui.pointer.scroll
        self.app_search = {"firefox": [{"id": "firefox.desktop", "name": "Firefox"}]}
        self.on_request = None       # called with (op, args) before handling, for scripted effects
        self.typed_text = ""         # what was typed since the last Super (overview search)
        self.spawn_on_return = True  # Enter in the overview opens a window for a matching app
        self.super_toggles_overview = True
        self.thread.start()

    def _serve(self):
        while self.running:
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            self.clients.append(conn)
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        try:
            self._client_stream(conn)
        except OSError:
            pass

    def _client_stream(self, conn):
        with conn, conn.makefile("rwb") as stream:
            for raw in stream:
                try:
                    req = json.loads(raw)
                    self.requests.append(req)
                    op, args, ident = req["op"], req.get("args", {}), req["id"]
                    result = self.handle(op, args)
                    response = {"id": ident, "ok": True, "result": result}
                except Exception as exc:
                    response = {"id": req.get("id"), "ok": False,
                                "error": {"code": getattr(exc, "code", "failed"), "message": str(exc)}}
                event = self.wrapup_event_before_reply if op == "ui.session.wrapup" else None
                if event:
                    stream.write(json.dumps(event, separators=(",", ":")).encode() + b"\n")
                    stream.flush()
                try:
                    stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                    stream.flush()
                except OSError:
                    return
                event = self.event_after_op.pop(op, None)
                if event:
                    try:
                        stream.write(json.dumps(event, separators=(",", ":")).encode() + b"\n")
                        stream.flush()
                    except OSError:
                        return

    def handle(self, op, args):
        if self.on_request:
            self.on_request(op, args)
        if op == "ui.desktop.info":
            if self.desktop is None:
                class UnknownOp(Exception):
                    code = "unknown_op"
                raise UnknownOp(op)
            return self.desktop
        if op == "ui.key.press" and str(args.get("keys")).casefold() == "super":
            self.typed_text = ""
            if self.desktop is not None and "overview" in self.desktop and self.super_toggles_overview:
                self.desktop["overview"] = not self.desktop["overview"]
        if op == "ui.key.press" and args.get("keys") in {"Escape", "Return"} and self.desktop is not None \
                and "overview" in self.desktop:
            self.desktop["overview"] = False
        if op == "ui.key.type":
            self.typed_text += args.get("text", "")
        if op == "ui.key.press" and args.get("keys") == "Return":
            # Enter in the overview search launches the matching app, like GNOME does.
            app = next((a for apps in self.app_search.values() for a in apps
                        if a["name"].casefold() == self.typed_text.casefold()), None)
            self.typed_text = ""
            if app and self.spawn_on_return:
                for w in self.windows:
                    w["focused"] = False
                self.windows.insert(0, {"id": 100 + len(self.windows), "title": app["name"],
                                        "app_id": app["id"].removesuffix(".desktop"), "pid": 9,
                                        "x": 10, "y": 10, "width": 800, "height": 600,
                                        "buffer": {"x": 10, "y": 10, "width": 800, "height": 600},
                                        "focused": True, "minimized": False, "workspace": 0, "new": True})
        if op == "ui.pointer.scroll" and self.scroll_hook:
            self.scroll_hook(args)
        if op == "ui.app.search":
            return {"apps": self.app_search.get(args.get("query", "").casefold(), [])}
        if op == "hello":
            return {"protocol": 1, "ui_connected": True, "session": self.session}
        if op == "session.begin":
            if not args.get("resume") and self.session.get("aborted"):
                raise RuntimeError("aborted")
            self.session.update(active=True, owner_is_me=True, aborted=False)
            return {"active": True}
        if op == "session.end":
            self.session.update(active=False, owner_is_me=False)
            return {}
        if op == "session.status":
            return self.session
        if op == "ui.screen.info":
            return {"width": self.width, "height": self.height,
                    "monitors": [{"x": 0, "y": 0, "width": self.width, "height": self.height,
                                  "scale": 1, "primary": True}]}
        if op == "ui.window.list":
            return {"windows": self.windows}
        if op == "ui.session.wrapup":
            if self.wrapup_gate is not None:
                self.wrapup_gate.wait(timeout=15)
            if self.wrapup_error:
                class FakeError(Exception):
                    code = self.wrapup_error[0]
                raise FakeError(self.wrapup_error[1])
            return self.wrapup_result
        if op == "ui.screenshot":
            from PIL import Image
            region = args.get("region")
            size = (region["width"], region["height"]) if region else (self.width, self.height)
            path = str(Path(self.runtime_dir, "pcpilot", f"shot-{threading.get_ident()}-{len(self.shot_paths)+1}.png"))
            self.shot_paths.append(path)
            if self.screenshot_file_bytes is not None:
                Path(path).write_bytes(self.screenshot_file_bytes)
            else:
                Image.new("RGB", size, (12, 34, 56)).save(path)
            return {"path": path, "width": size[0], "height": size[1]}
        if op == "ui.pointer.position":
            return {"x": 500, "y": 200}
        return {}

    def emit(self, message):
        """Send an unsolicited daemon event over each connected fake client."""
        if message.get("event") == "session_ended" and message.get("reason") in {"user_hotkey", "user_release"}:
            self.session.update(active=False, owner_is_me=False, aborted=True)
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        for client in self.clients:
            try:
                client.sendall(payload)
            except OSError:
                pass

    def close(self):
        self.running = False
        self.listener.close()
        for client in self.clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
                client.close()
            except OSError:
                pass
        self.thread.join(timeout=1)


class FakeWorker:
    def __init__(self, elements=None):
        self.rows = elements or [{"role": "button", "name": "Save", "x": 600, "y": 400, "states": ["focused"]}]
        self.calls = []
        self.events = []

    def snapshot(self, windows, query="", window="focused", maximum=60, role=""):
        self.calls.append((windows, query, window, maximum))
        rows = self.rows
        if query:
            rows = [r for r in rows if query.casefold() in (r["role"] + " " + r["name"]).casefold()]
        return rows[:maximum]

    def acquire(self):
        self.events.append("acquire")
        return {"changed": True, "enabled": True}

    def release(self):
        self.events.append("release")

    def close(self):
        pass


class FakePage(FakeWorker):
    """A scrollable web page: document-space items, a viewport, and wheel-notch scrolling.

    Wire it with daemon.scroll_hook = page.scroll so ui.pointer.scroll moves the page.
    """

    def __init__(self, items, viewport=(100, 150, 800, 450), notch_px=48, app="Firefox"):
        super().__init__([])
        self.items = [dict(item) for item in items]
        self.viewport = viewport
        self.scroll_y = 0
        self.notch_px = notch_px
        self.app = app
        self.title = "Test Page"
        self.url = "https://example.test/"
        self.tabs = [["Test Page", True]]
        self.loading = False
        self.focus_name = None
        self.scrolls = []
        self.absent = False
        self.locate_calls = []
        self.drop_scroll = False
        self.live = {}

    def scroll(self, args):
        self.scrolls.append(args)
        if not self.drop_scroll:
            self.scroll_y = max(0, self.scroll_y + args["dy"] * self.notch_px)

    def position(self, item):
        vx, vy, vw, vh = self.viewport
        y = vy + item["doc_y"] - self.scroll_y
        x = vx + item["doc_x"]
        if vy <= y < vy + vh:
            return "in", 0.0, x, y
        dist = abs(y - (vy + vh / 2)) / vh
        return ("above" if y < vy else "below"), round(dist, 1), x, y

    def snapshot(self, windows, query="", window="focused", maximum=60, role=""):
        self.calls.append((windows, query, window, maximum))
        if self.absent:
            return {"elements": [], "absent": True}
        rows = []
        for index, item in enumerate(self.items):
            if query and query.casefold() not in f'{item["role"]} {item["name"]}'.casefold():
                continue
            if role and item["role"] not in role.split(","):
                continue
            vis, dist, x, y = self.position(item)
            row = {"ref": index + 1, "role": item["role"], "name": item["name"], "vis": vis,
                   "dist": dist, "o": index, "wid": windows[0]["id"] if windows else None,
                   "x": round(x), "y": round(y), "states": list(item.get("states", []))}
            if item.get("value"):
                row["value"] = item["value"]
            rows.append(row)
        rows.sort(key=lambda r: (r["vis"] != "in", r["dist"]))
        chosen = sorted(rows[:maximum], key=lambda r: r["o"])
        page = {"app": self.app, "title": self.title, "url": self.url, "loading": self.loading,
                "address": None, "tabs": self.tabs, "extra": len(rows) - len(chosen),
                "focus": next(({"ref": r["ref"], "role": r["role"], "name": r["name"]}
                               for r in chosen if r["name"] == self.focus_name), None)}
        return {"elements": chosen, "headings": [], "page": page}

    def locate(self, ref, windows):
        self.locate_calls.append(ref)
        if ref in self.live and self.live[ref] != "ok":
            return {"state": self.live[ref]}
        item = self.items[ref - 1]
        vis, dist, x, y = self.position(item)
        vx, vy, vw, vh = self.viewport
        return {"state": vis, "dist": dist, "x": round(x), "y": round(y), "vp": [vx, vy, vw, vh]}
