"""A private UNIX socket daemon fixture for MCP tests; never touches /run/pcpilot."""
import json
import os
import socket
import tempfile
import threading
import re
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
                stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                stream.flush()
                event = self.event_after_op.pop(op, None)
                if event:
                    stream.write(json.dumps(event, separators=(",", ":")).encode() + b"\n")
                    stream.flush()

    def handle(self, op, args):
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

    def snapshot(self, windows, query="", window="focused", maximum=60):
        self.calls.append((windows, query, window, maximum))
        rows = self.rows
        if query:
            rows = [r for r in rows if query.casefold() in (r["role"] + " " + r["name"]).casefold()]
        return rows[:maximum]

    def close(self):
        pass
