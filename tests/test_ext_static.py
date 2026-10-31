"""Static contract checks for the GNOME extension (no GJS runtime required)."""
import asyncio
import json
import os
import re
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import pcpilot.protocol as P
from tests.temp_paths import socket_temp_base
from tests.ui_fake_daemon import AgentClient, FakeDaemon

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension/pcpilot@local"


def constants(path):
    text = path.read_text()
    return {name: value for name, value in re.findall(
        r"(?:export\s+)?(?:const\s+)?([ER]_[A-Z0-9_]+)\s*(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", text)}


class ExtensionStaticTests(unittest.TestCase):
    def test_metadata(self):
        data = json.loads((EXT / "metadata.json").read_text())
        self.assertEqual(data["uuid"], "pcpilot@local")
        self.assertEqual(data["shell-version"], ["51"])
        self.assertIn("user", data["session-modes"])

    def test_protocol_constants_match_python(self):
        js = constants(EXT / "protocol.js")
        py = constants(ROOT / "pcpilot/protocol.py")
        # Every extension E_/R_ constant must exist and agree in the shared protocol.
        for name, value in js.items():
            if name.startswith(("E_", "R_")):
                self.assertEqual(py.get(name), value, name)
        # Also ensure all protocol error/reason constants used by the extension mirror.
        used = set(re.findall(r"\b([ER]_[A-Z0-9_]+)\b", "\n".join(
            p.read_text() for p in EXT.glob("*.js"))))
        for name in used:
            self.assertEqual(js.get(name), py.get(name), name)

    def test_every_reason_has_portuguese_toast(self):
        py = constants(ROOT / "pcpilot/protocol.py")
        js = (EXT / "protocol.js").read_text()
        for name in py:
            if name.startswith("R_"):
                self.assertRegex(js, rf"\[{name}\]\s*:\s*['\"][^'\"]+['\"]", name)

    def test_ops_table_matches_contract(self):
        expected = {"screen.info", "screenshot", "pointer.move", "pointer.click",
                    "pointer.drag", "pointer.scroll", "pointer.position", "key.press",
                    "key.type", "app.search", "app.launch", "open_uri", "window.list",
                    "window.action", "clipboard.get", "clipboard.set", "status", "session.wrapup"}
        text = (EXT / "ops.js").read_text()
        body = text.split("this._handlers = {", 1)[1].split("};", 1)[0]
        actual = set(re.findall(r"'([^']+)'\s*:", body))
        self.assertEqual(actual, expected)


class FakeDaemonThemeContractTests(unittest.TestCase):
    def test_fake_daemon_theme_contract(self):
        td = tempfile.mkdtemp(prefix="fake-theme-", dir=socket_temp_base())
        sock_path = os.path.join(td, "daemon.sock")
        self.addCleanup(lambda: os.path.exists(td) and os.system(f"rm -rf {td}"))

        loop = asyncio.new_event_loop()
        daemon = FakeDaemon(sock_path)

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(daemon.start())
            loop.run_forever()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()
        self.addCleanup(lambda: (loop.call_soon_threadsafe(loop.stop), t.join(timeout=2)))

        # Wait for socket to be created
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            threading.Event().wait(0.05)

        ui_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ui_sock.connect(sock_path)
        self.addCleanup(ui_sock.close)
        ui_sock.sendall(P.encode(P.request(1, "hello", {"role": "ui"})))

        buf = b""

        def read_msg():
            nonlocal buf
            while b"\n" not in buf:
                chunk = ui_sock.recv(4096)
                if not chunk:
                    raise EOFError()
                buf += chunk
            line, buf = buf.split(b"\n", 1)
            return P.decode(line)

        m = read_msg()
        self.assertTrue(m.get("ok"))

        state_events = []
        stop_ui = threading.Event()

        def ui_worker():
            while not stop_ui.is_set():
                try:
                    ui_sock.settimeout(0.5)
                    msg = read_msg()
                    if msg.get("event") == "state":
                        state_events.append(msg)
                        sess = msg.get("session")
                        if sess is not None:
                            ui_sock.sendall(P.encode({"event": "state_ack", "active": True, "session": sess}))
                except (socket.timeout, TimeoutError):
                    continue
                except Exception:
                    break

        ut = threading.Thread(target=ui_worker, daemon=True)
        ut.start()
        self.addCleanup(lambda: (stop_ui.set(), ut.join(timeout=2)))

        agent = AgentClient(sock_path)
        self.addCleanup(agent.close)

        # session.begin {theme:'codex'} -> the UI receives state active with theme 'codex' and label
        res = agent.call("session.begin", {"theme": "codex"})
        self.assertIsNotNone(res)
        self.assertTrue(len(state_events) >= 1)
        st = state_events[-1]
        self.assertEqual(st.get("active"), True)
        self.assertEqual(st.get("theme"), "codex")
        self.assertIn("label", st)

        # end, begin without theme -> 'neutral'
        agent.call("session.end")
        agent.call("session.begin", {})
        st = state_events[-1]
        self.assertEqual(st.get("active"), True)
        self.assertEqual(st.get("theme"), "neutral")
        agent.call("session.end")

        # theme 'bogus'/7/None -> 'neutral'
        for bad in ["bogus", 7, None]:
            agent.call("session.begin", {"theme": bad})
            st = state_events[-1]
            self.assertEqual(st.get("active"), True)
            self.assertEqual(st.get("theme"), "neutral")
            agent.call("session.end")

        # fake.state {active:true,theme:'gemini'} -> 'gemini'
        agent.call("fake.state", {"active": True, "theme": "gemini"})
        st = state_events[-1]
        self.assertEqual(st.get("active"), True)
        self.assertEqual(st.get("theme"), "gemini")


if __name__ == "__main__":
    unittest.main()
