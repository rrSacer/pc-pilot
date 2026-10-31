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
                    "window.action", "clipboard.get", "clipboard.set", "status", "session.wrapup",
                    "desktop.info"}
        text = (EXT / "ops.js").read_text()
        body = text.split("this._handlers = {", 1)[1].split("};", 1)[0]
        actual = set(re.findall(r"'([^']+)'\s*:", body))
        self.assertEqual(actual, expected)


    def test_wheel_is_discrete_notches_only(self):
        # Scrolling is one discrete wheel notch per event (a person rolls a wheel); never
        # smooth/continuous scrolling.
        for name in ("ops.js", "input.js"):
            text = (EXT / name).read_text()
            self.assertNotIn("scroll_continuous", text, name)
            self.assertNotIn("notify_scroll", text.replace("notify_discrete_scroll", ""), name)
        self.assertIn("notify_discrete_scroll", (EXT / "input.js").read_text())

    def test_human_defaults_are_slow_arcs_and_paced_typing(self):
        path = (EXT / "path.js").read_text()
        self.assertEqual(re.search(r"HUMAN_SPEED_MULTIPLIER\s*=\s*([\d.]+)", path).group(1), "1")
        self.assertEqual(int(re.search(r"MAX_GLIDE_MS\s*=\s*(\d+)", path).group(1)), 5500)
        self.assertLess(int(re.search(r"SCROLL_BUDGET_MS\s*=\s*(\d+)", path).group(1)), 14500)
        self.assertLess(int(re.search(r"TYPING_BUDGET_MS\s*=\s*(\d+)", path).group(1)), 15000)


class FakeDaemonNewOpsForwardingTests(unittest.TestCase):
    """The fake daemon relays the v2.2 ops (and their results) to the UI untouched."""

    def test_new_ops_and_results_pass_through(self):
        tmp = tempfile.TemporaryDirectory(prefix="fake-ops-", dir=socket_temp_base())
        self.addCleanup(tmp.cleanup)
        sock_path = os.path.join(tmp.name, "daemon.sock")
        loop = asyncio.new_event_loop()
        daemon = FakeDaemon(sock_path)

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(daemon.start())
            loop.run_forever()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()

        async def shutdown():
            await asyncio.sleep(0.2)
            await daemon.close()
            others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)

        def stop_loop():
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(5)
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
            loop.close()

        self.addCleanup(stop_loop)
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            threading.Event().wait(0.05)
        ui_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ui_sock.connect(sock_path)
        self.addCleanup(ui_sock.close)
        ui_sock.sendall(P.encode(P.request(1, "hello", {"role": "ui"})))
        canned = {
            "desktop.info": {"workspace": {"active": 0, "count": 2}, "overview": False, "pointer": {"x": 5, "y": 6},
                             "focus": 7, "scheme": "dark",
                             "monitors": [{"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1,
                                           "primary": True}]},
            "window.action": {"via": "pointer"},
            "window.list": {"windows": [{"id": 7, "maximized": False, "fullscreen": False, "monitor": 0}]},
            "pointer.scroll": {"anchor": {"x": 1, "y": 2}, "notches": 5, "net": {"dx": 0, "dy": 4}},
        }
        seen = []
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

        stop = threading.Event()

        def ui_worker():
            while not stop.is_set():
                try:
                    ui_sock.settimeout(0.5)
                    msg = read_msg()
                except (socket.timeout, TimeoutError):
                    continue
                except Exception:
                    break
                if msg.get("event") == "state" and msg.get("session") is not None:
                    ui_sock.sendall(P.encode({"event": "state_ack", "active": True, "session": msg["session"]}))
                elif "op" in msg:
                    seen.append((msg["op"], msg.get("args")))
                    ui_sock.sendall(P.encode(P.ok(msg["id"], canned.get(msg["op"], {}))))

        ut = threading.Thread(target=ui_worker, daemon=True)
        ut.start()
        self.addCleanup(lambda: (stop.set(), ut.join(timeout=2)))
        self.assertTrue(read_msg().get("ok"))
        agent = AgentClient(sock_path)
        self.addCleanup(agent.close)
        agent.call("session.begin", {})
        self.assertEqual(agent.call("ui.desktop.info"), canned["desktop.info"])
        args = {"id": 7, "action": "close", "style": "human", "at": {"x": 900, "y": 20}}
        self.assertEqual(agent.call("ui.window.action", args), {"via": "pointer"})
        self.assertEqual(agent.call("ui.window.list")["windows"][0]["monitor"], 0)
        self.assertEqual(agent.call("ui.pointer.scroll", {"dy": 4})["net"], {"dx": 0, "dy": 4})
        self.assertEqual(seen, [("desktop.info", {}), ("window.action", args), ("window.list", {}),
                                ("pointer.scroll", {"dy": 4})])
        agent.call("session.end")


class FakeDaemonThemeContractTests(unittest.TestCase):
    def test_fake_daemon_theme_contract(self):
        tmp = tempfile.TemporaryDirectory(prefix="fake-theme-", dir=socket_temp_base())
        self.addCleanup(tmp.cleanup)
        sock_path = os.path.join(tmp.name, "daemon.sock")

        loop = asyncio.new_event_loop()
        daemon = FakeDaemon(sock_path)

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(daemon.start())
            loop.run_forever()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()

        async def shutdown():
            await asyncio.sleep(0.2)  # let the closed clients' handlers see EOF and finish
            await daemon.close()
            others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)

        def stop_loop():
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(5)
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
            loop.close()

        self.addCleanup(stop_loop)  # runs after the client sockets below are closed

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
