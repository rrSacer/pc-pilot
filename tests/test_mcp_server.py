import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import pathlib
import re
import signal
import struct
import zlib
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from pcpilot import a11y_worker, mcp_server, protocol
from tests.daemon_testlib import wait_until
from tests.mcp_fake_daemon import FakeDaemon, FakePage, FakeWorker


def cleanup_process(process):
    if process.poll() is None:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=3)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream and not stream.closed:
            stream.close()


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_env = {key: os.environ.get(key) for key in
                             ("PCPILOT_SOCKET", "PCPILOT_MAX_SIDE", "PCPILOT_DESKTOP_USER", "PCPILOT_A11Y_RUNTIME_DIR", "PCPILOT_THEME", "PCPILOT_AUTOSTOP_S")}
        os.environ.pop("PCPILOT_THEME", None)
        os.environ.pop("PCPILOT_AUTOSTOP_S", None)
        self.daemon = FakeDaemon(self.tmp.name)
        os.environ["PCPILOT_SOCKET"] = self.daemon.path
        os.environ["PCPILOT_MAX_SIDE"] = "1280"
        os.environ["PCPILOT_DESKTOP_USER"] = str(os.getuid())
        os.environ["PCPILOT_A11Y_RUNTIME_DIR"] = self.daemon.runtime_dir
        self.worker = FakeWorker()
        self.server = mcp_server.Server(worker=self.worker)

    def tearDown(self):
        self.server.close()
        self.daemon.close()
        self.tmp.cleanup()
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def rpc(self, method, params=None, ident=1):
        return self.server.dispatch({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})

    def initialize(self, version="2025-06-18"):
        return self.rpc("initialize", {"protocolVersion": version, "clientInfo": {"name": "unit"}})

    def call_tool(self, name, args):
        return self.rpc("tools/call", {"name": name, "arguments": args})["result"]

    def test_handshake_versions_notifications_ping_and_tools(self):
        result = self.initialize("2025-03-26")["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"]["name"], "pcpilot")
        self.assertIn("tools", result["capabilities"])
        self.assertIsNone(self.server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.rpc("ping")["result"], {})
        listed = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in listed], ["control", "screenshot", "act", "elements", "windows"])
        control_schema = listed[0]["inputSchema"]["properties"]
        self.assertIn("keep", control_schema)
        self.assertIn("cleanup", control_schema)
        self.assertEqual(self.rpc("initialize", {"protocolVersion": "unknown"})["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_derives_theme_and_friendly_label_and_propagates_on_begin(self):
        cases = [("claude-code", "claude", "Claude"), ("CODEX", "codex", "Codex"),
                 ("OpenAI", "codex", "Codex"), ("gemini-cli", "gemini", "Gemini"),
                 ("Antigravity", "gemini", "Gemini"),
                 ("other", "neutral", "other")]
        for name, theme, label in cases:
            with self.subTest(name=name):
                self.server = mcp_server.Server(worker=self.worker)
                self.initialize_info(name)
                self.call_tool("control", {"action": "start"})
                begin = [r for r in self.daemon.requests if r["op"] == "session.begin"][-1]
                self.assertEqual(begin["args"].get("theme"), theme)
                self.assertEqual(begin["args"].get("label"), label)
                self.server.close()
                self.server = mcp_server.Server(worker=self.worker)

    def initialize_info(self, name, title=None):
        info = {"name": name}
        if title is not None:
            info["title"] = title
        return self.rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": info})

    def test_theme_override_is_whitelisted_and_friendly_name_falls_back_to_title(self):
        os.environ["PCPILOT_THEME"] = "gemini"
        self.initialize_info("random-client", "Friendly Title")
        self.call_tool("control", {"action": "start"})
        args = [r["args"] for r in self.daemon.requests if r["op"] == "session.begin"][-1]
        self.assertEqual(args.get("theme"), "gemini")
        self.assertEqual(args.get("label"), "Friendly Title")
        self.initialize_info("claude-code")
        self.call_tool("control", {"action": "stop"})
        self.call_tool("control", {"action": "start"})
        again = [r["args"] for r in self.daemon.requests if r["op"] == "session.begin"][-1]
        self.assertEqual(again, args)

    def test_invalid_theme_override_is_ignored_and_missing_identity_uses_ia(self):
        os.environ["PCPILOT_THEME"] = "purple"
        self.initialize_info("")
        self.call_tool("control", {"action": "start"})
        args = [r["args"] for r in self.daemon.requests if r["op"] == "session.begin"][-1]
        self.assertEqual(args.get("theme"), "neutral")
        self.assertEqual(args.get("label"), "IA")

    def test_autostop_idle_wrapup_and_disable_override(self):
        self.assertEqual(mcp_server.IDLE_AUTOSTOP_S, 12)
        with mock.patch.dict(os.environ, {"PCPILOT_AUTOSTOP_S": "0"}):
            self.server = mcp_server.Server(worker=self.worker)
            self.initialize()
            self.call_tool("control", {"action": "start"})
            # The disabled setting must leave an otherwise idle session active.
            time.sleep(.15)
            self.assertTrue(self.server.daemon.active)
            self.server.close()
        with mock.patch.dict(os.environ, {"PCPILOT_AUTOSTOP_S": ".08"}):
            self.server = mcp_server.Server(worker=self.worker)
            self.initialize()
            self.call_tool("control", {"action": "start"})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.server.daemon.active:
                time.sleep(.01)
        self.assertFalse(self.server.daemon.active)
        ops = [r["op"] for r in self.daemon.requests]
        self.assertEqual(ops.count("ui.session.wrapup"), 1)
        self.assertLess(ops.index("ui.session.wrapup"), ops.index("session.end"))
        wrapup = next(r["args"] for r in self.daemon.requests if r["op"] == "ui.session.wrapup")
        self.assertEqual(wrapup, {"keep": [], "close_new": False, "return_to": "origin"})

    def test_autostop_waits_for_inflight_wait_and_observation_then_resume_is_reported(self):
        with mock.patch.dict(os.environ, {"PCPILOT_AUTOSTOP_S": ".08"}):
            self.server = mcp_server.Server(worker=self.worker)
            self.initialize()
            self.call_tool("control", {"action": "start"})
            started = time.monotonic()
            result = self.call_tool("act", {"actions": [{"wait": 180}], "observe": "screenshot"})
            self.assertGreaterEqual(time.monotonic() - started, .17)
            self.assertEqual(sum(r["op"] == "ui.session.wrapup" for r in self.daemon.requests), 0)
            self.assertGreaterEqual(len(result["content"]), 2)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.server.daemon.active:
                time.sleep(.01)
            self.assertFalse(self.server.daemon.active)
            before = sum(r["op"] == "session.begin" for r in self.daemon.requests)
            resumed = self.call_tool("windows", {})
            self.assertIn("session resumed", [c["text"] for c in resumed["content"]])
            begins = [r["args"] for r in self.daemon.requests if r["op"] == "session.begin"]
            self.assertEqual(len(begins), before + 1)
            self.assertEqual(begins[-1].get("theme"), "neutral")
            again = self.call_tool("windows", {})
            self.assertNotIn("session resumed", [c["text"] for c in again["content"]])

    def test_autostop_never_sends_cleanup_after_user_abort(self):
        with mock.patch.dict(os.environ, {"PCPILOT_AUTOSTOP_S": ".06"}):
            self.server = mcp_server.Server(worker=self.worker)
            self.initialize()
            self.call_tool("control", {"action": "start"})
            self.daemon.emit({"event": "session_ended", "reason": "user_hotkey"})
            time.sleep(.15)
            self.assertFalse(any(r["op"] in {"ui.session.wrapup", "session.end"}
                                 for r in self.daemon.requests))
            count = len(self.daemon.requests)
            status = self.call_tool("control", {"action": "status"})
            self.assertTrue(status["isError"])
            result = self.call_tool("windows", {})
            self.assertTrue(result["isError"])
            self.assertEqual(len(self.daemon.requests), count)

    def test_explicit_stop_cancels_pending_idle_autostop(self):
        with mock.patch.dict(os.environ, {"PCPILOT_AUTOSTOP_S": ".35"}):
            self.server = mcp_server.Server(worker=self.worker)
            self.initialize()
            self.call_tool("control", {"action": "start"})
            time.sleep(.04)
            self.call_tool("control", {"action": "stop"})
            time.sleep(.4)
        ops = [r["op"] for r in self.daemon.requests]
        self.assertEqual(ops.count("ui.session.wrapup"), 1)
        self.assertEqual(ops.count("session.end"), 1)

    def test_eof_cancels_pending_idle_autostop_with_fake_daemon(self):
        fake = self.daemon
        env = dict(os.environ, PCPILOT_SOCKET=fake.path, PCPILOT_DESKTOP_USER=str(os.getuid()),
                   PCPILOT_A11Y_RUNTIME_DIR=fake.runtime_dir, PCPILOT_AUTOSTOP_S=".12")
        proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.addCleanup(cleanup_process, proc)
        proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        proc.stdin.flush(); proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"control","arguments":{"action":"start"}}}\n')
        proc.stdin.flush(); proc.stdout.readline()
        time.sleep(.03)
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=3), 0, proc.stderr.read())
        time.sleep(.15)
        self.assertFalse(any(r["op"] in {"ui.session.wrapup", "session.end"} for r in fake.requests))

    def test_cancelled_idle_wrapup_cannot_end_replacement_owner_session(self):
        fake = self.daemon
        fake.wrapup_gate = threading.Event()
        with mock.patch.dict(os.environ, {"PCPILOT_AUTOSTOP_S": ".04"}):
            self.server = mcp_server.Server(worker=self.worker)
            self.initialize()
            self.server.prepare_request(1)
            self.call_tool("control", {"action": "start"})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not any(r["op"] == "ui.session.wrapup" for r in fake.requests):
                time.sleep(.01)
            self.assertTrue(any(r["op"] == "ui.session.wrapup" for r in fake.requests))
            old_owner = self.server.daemon
            self.server.cancel(1)
            self.server.prepare_request(2)
            fake.wrapup_gate.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.server.daemon is old_owner:
                time.sleep(.01)
            self.assertIsNot(self.server.daemon, old_owner)
            result = self.call_tool("windows", {})
            self.assertFalse(result.get("isError", False), result)
            self.assertFalse(any(r["op"] == "session.end" for r in fake.requests))

    def test_tools_list_compact_json_rpc_size_stays_under_budget(self):
        self.initialize()
        reply = self.rpc("tools/list")
        encoded = json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        self.assertLessEqual(len(encoded), 3400)

    def test_act_maps_all_action_shapes_and_scales_coordinates(self):
        self.initialize()
        result = self.call_tool("act", {"actions": [
            {"click": [640, 360]}, {"double_click": [10, 20]}, {"right_click": [20, 30]},
            {"move": [1279, 719]}, {"drag": [0, 1, 640, 360]}, {"scroll": [1, 2, -3]},
            {"type": "hi"}, {"key": "ctrl+s"}, {"wait": 0}, {"launch": "firefox"},
            {"open": "https://example.test"}, {"focus": 42}, {"window": [42, "close"]},
            {"clipboard_set": "x"}, {"clipboard_get": True}], "observe": "none"})
        self.assertTrue(result["content"][0]["text"].startswith("ok 15 · launched "), result)
        ops = [r["op"] for r in self.daemon.requests]
        self.assertIn("ui.pointer.click", ops)
        self.assertIn("ui.pointer.drag", ops)
        self.assertIn("ui.key.type", ops)
        self.assertIn("ui.clipboard.get", ops)
        clicks = [r["args"] for r in self.daemon.requests if r["op"] == "ui.pointer.click"]
        self.assertEqual(clicks[0]["x"], 960)
        self.assertEqual(clicks[0]["y"], 540)
        self.assertEqual(clicks[1]["count"], 2)
        self.assertEqual(clicks[2]["button"], "right")
        self.assertEqual(clicks[0]["style"], "human")

    def test_act_speed_maps_style_for_pointer_key_and_window_ops(self):
        self.initialize()
        self.call_tool("act", {"actions": [{"click": [640, 360]}, {"type": "hello"},
                                             {"window": [42, "close"]}], "speed": "instant"})
        calls = {r["op"]: r["args"] for r in self.daemon.requests}
        self.assertEqual(calls["ui.pointer.click"]["style"], "instant")
        self.assertEqual(calls["ui.key.type"]["style"], "instant")
        self.assertEqual(calls["ui.window.action"]["style"], "instant")

        self.call_tool("act", {"actions": [{"move": [10, 10]}], "speed": "fast"})
        move = [r["args"] for r in self.daemon.requests if r["op"] == "ui.pointer.move"][-1]
        self.assertEqual(move["style"], "fast")

    def test_human_long_batch_switches_later_actions_to_fast_and_notes_it(self):
        self.initialize()
        result = self.call_tool("act", {"actions": [{"move": [i, i]} for i in range(13)] + [{"type": "hi"}],
                                        "speed": "human"})
        styles = [r["args"].get("style") for r in self.daemon.requests if r["op"] == "ui.pointer.move"]
        self.assertEqual(len(styles), 13)
        self.assertEqual(styles[-1], "fast")
        self.assertIn("fast", result["content"][0]["text"].casefold())
        # Typing is what a person watches: it keeps the human cadence the model asked for.
        self.assertEqual([r["args"]["style"] for r in self.daemon.requests if r["op"] == "ui.key.type"], ["human"])

    def test_aggregate_act_budget_stops_between_actions_without_closing_owner(self):
        self.initialize()
        self.server.daemon.begin()
        original_action = self.server.action
        def slow_action(*args, **kwargs):
            result = original_action(*args, **kwargs)
            time.sleep(.04)
            return result
        self.server.action = slow_action
        with mock.patch.object(mcp_server, "ACT_BUDGET_S", .07):
            result = self.call_tool("act", {"actions": [{"move": [1, 1]} for _ in range(3)], "speed": "fast"})
        self.assertIn("partial: 2/3 done, budget", result["content"][0]["text"])
        self.assertEqual(sum(r["op"] == "ui.pointer.move" for r in self.daemon.requests), 2)
        self.assertFalse(self.server.daemon.closed)
        self.assertTrue(self.server.daemon.active)

    def test_act_rejects_invalid_speed_scroll_and_window_shapes(self):
        self.initialize()
        invalid = [
            {"actions": [{"wait": 0}], "speed": "warp"},
            {"actions": [{"scroll": []}]},
            {"actions": [{"scroll": [1, 2]}]},
            {"actions": [{"window": [42, "resize", 300]}]},
            {"actions": [{"window": [42, "move", 10]}]},
        ]
        for args in invalid:
            with self.subTest(args=args):
                reply = self.rpc("tools/call", {"name": "act", "arguments": args})
                self.assertTrue(reply["result"]["isError"])

    def test_anchor_scroll_has_no_explicit_coordinates(self):
        self.initialize()
        self.call_tool("act", {"actions": [{"scroll": [5]}]})
        args = [r["args"] for r in self.daemon.requests if r["op"] == "ui.pointer.scroll"][-1]
        self.assertEqual(args["dy"], 5)
        self.assertNotIn("x", args)
        self.assertNotIn("y", args)
        self.call_tool("act", {"actions": [{"scroll": [640, 360, -2]}]})
        args = [r["args"] for r in self.daemon.requests if r["op"] == "ui.pointer.scroll"][-1]
        self.assertEqual((args["x"], args["y"], args["dy"], args["style"]), (960, 540, -2, "human"))

    def test_window_fit_resize_move_and_new_window_marker(self):
        self.initialize()
        self.daemon.width, self.daemon.height = 1366, 768
        self.daemon.windows[0]["new"] = True
        self.daemon.windows[0]["ai"] = True
        window_text = self.call_tool("windows", {})["content"][0]["text"]
        self.assertIn("42 * org.test.Editor", window_text)

        self.daemon.windows[0]["ai"] = False
        window_text = self.call_tool("windows", {})["content"][0]["text"]
        self.assertIn('42 org.test.Editor "Editor"', window_text)
        self.assertNotIn("42 * org.test.Editor", window_text)

        del self.daemon.windows[0]["ai"]
        window_text = self.call_tool("windows", {})["content"][0]["text"]
        self.assertIn('42 org.test.Editor "Editor"', window_text)
        self.assertNotIn("42 * org.test.Editor", window_text)

        self.call_tool("act", {"actions": [
            {"window": [42, "fit"]}, {"window": [42, "resize", 640, 360]},
            {"window": [42, "move", 640, 360]}]})
        actions = [r["args"] for r in self.daemon.requests if r["op"] == "ui.window.action"]
        self.assertEqual(actions[0]["action"], "fit")
        self.assertEqual(actions[1]["action"], "resize")
        self.assertEqual((actions[1]["width"], actions[1]["height"]), (683, 384))
        self.assertEqual(actions[2]["action"], "move")
        self.assertEqual((actions[2]["x"], actions[2]["y"]), (683, 384))

    def test_control_stop_wrapup_precedes_end_and_returns_compact_summary(self):
        self.initialize()
        self.server.daemon.begin()
        result = self.call_tool("control", {"action": "stop", "keep": [42], "cleanup": False})
        ops = [r["op"] for r in self.daemon.requests]
        self.assertLess(ops.index("ui.session.wrapup"), ops.index("session.end"))
        wrapup = next(r for r in self.daemon.requests if r["op"] == "ui.session.wrapup")
        self.assertEqual(wrapup["args"], {"keep": [42], "close_new": False, "return_to": "origin"})
        self.assertEqual(len(result["content"]), 1)
        summary = result["content"][0]["text"]
        self.assertNotIn("\n", summary)
        self.assertIn("closed 2", summary)
        self.assertIn("restored 1", summary)
        self.assertIn('"Firefox — Docs"', summary)
        self.assertIn("+1", summary)
        self.assertIn("Traycer", summary)

    def test_control_stop_without_session_is_idempotent_without_wrapup(self):
        self.initialize()
        result = self.call_tool("control", {"action": "stop"})
        self.assertIn("stopped", result["content"][0]["text"])
        self.assertFalse(any(r["op"] in {"ui.session.wrapup", "session.end"} for r in self.daemon.requests))

    def test_control_stop_unknown_wrapup_op_falls_back_to_session_end(self):
        self.initialize()
        self.server.daemon.begin()
        self.daemon.wrapup_error = ("unknown_op", "unsupported")
        result = self.call_tool("control", {"action": "stop"})
        ops = [r["op"] for r in self.daemon.requests]
        self.assertLess(ops.index("ui.session.wrapup"), ops.index("session.end"))
        self.assertIn("stopped", result["content"][0]["text"])

    def test_control_stop_generic_wrapup_error_still_ends_session_with_warning(self):
        self.initialize()
        self.server.daemon.begin()
        self.daemon.wrapup_error = ("failed", "cleanup failed")
        result = self.call_tool("control", {"action": "stop"})
        ops = [r["op"] for r in self.daemon.requests]
        self.assertLess(ops.index("ui.session.wrapup"), ops.index("session.end"))
        self.assertIn("cleanup failed", result["content"][0]["text"])

    def test_control_stop_hotkey_event_during_wrapup_sends_no_session_end(self):
        self.initialize()
        self.server.daemon.begin()
        self.daemon.wrapup_event_before_reply = {"event": "session_ended", "reason": "user_hotkey"}
        result = self.call_tool("control", {"action": "stop"})
        self.assertTrue(result["isError"])
        self.assertIn("USER_TOOK_CONTROL", result["content"][0]["text"])
        self.assertFalse(any(r["op"] == "session.end" for r in self.daemon.requests))

    def test_control_stop_wrapup_user_aborted_error_sends_no_session_end(self):
        self.initialize()
        self.server.daemon.begin()
        self.daemon.wrapup_error = ("user_aborted", "user took control")
        result = self.call_tool("control", {"action": "stop"})
        self.assertTrue(result["isError"])
        self.assertIn("USER_TOOK_CONTROL", result["content"][0]["text"])
        self.assertFalse(any(r["op"] == "session.end" for r in self.daemon.requests))

    def test_control_stop_wrapup_timeout_closes_owner_without_session_end(self):
        self.initialize()
        self.server.daemon.begin()
        self.assertEqual(mcp_server.WRAPUP_TIMEOUT_S, 10)
        self.daemon.wrapup_gate = threading.Event()
        release = threading.Timer(.2, self.daemon.wrapup_gate.set)
        release.start()
        try:
            with mock.patch.object(mcp_server, "WRAPUP_TIMEOUT_S", .05):
                result = self.call_tool("control", {"action": "stop"})
        finally:
            release.cancel()
            self.daemon.wrapup_gate.set()
            release.join(timeout=1)
        self.assertTrue(result["isError"])
        self.assertIn("timeout", result["content"][0]["text"].lower())
        self.assertTrue(self.server.daemon.closed)
        self.assertFalse(any(r["op"] == "session.end" for r in self.daemon.requests))

    def test_control_stop_cleanup_defaults_to_true(self):
        self.initialize()
        self.server.daemon.begin()
        self.call_tool("control", {"action": "stop"})
        wrapup = next(r for r in self.daemon.requests if r["op"] == "ui.session.wrapup")
        self.assertTrue(wrapup["args"]["close_new"])

    def test_control_stop_rejects_invalid_keep_and_cleanup_without_sends(self):
        self.initialize()
        self.server.daemon.begin()
        self.daemon.requests.clear()
        for args in ({"action": "stop", "keep": "42"},
                     {"action": "stop", "keep": [True]},
                     {"action": "stop", "cleanup": "yes"}):
            with self.subTest(args=args):
                reply = self.rpc("tools/call", {"name": "control", "arguments": args})
                self.assertTrue(reply["result"]["isError"])
        self.assertFalse(self.daemon.requests)

    def test_control_stop_after_observed_abort_sends_nothing(self):
        self.initialize()
        self.server.daemon.aborted = True
        self.server.daemon.active = False
        self.daemon.requests.clear()
        result = self.call_tool("control", {"action": "stop"})
        self.assertTrue(result["isError"])
        self.assertIn("USER_TOOK_CONTROL", result["content"][0]["text"])
        self.assertFalse(self.daemon.requests)

    def test_notifications_cancelled_during_wrapup_skips_session_end(self):
        fake = self.daemon
        fake.wrapup_gate = threading.Event()
        env = dict(os.environ, PCPILOT_SOCKET=fake.path, PCPILOT_DESKTOP_USER=str(os.getuid()),
                   PCPILOT_A11Y_RUNTIME_DIR=fake.runtime_dir)
        proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.addCleanup(cleanup_process, proc)
        proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        proc.stdin.flush(); proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"control","arguments":{"action":"start"}}}\n')
        proc.stdin.flush(); proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"control","arguments":{"action":"stop"}}}\n')
        proc.stdin.flush()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(r["op"] == "ui.session.wrapup" for r in fake.requests):
            time.sleep(.01)
        self.assertTrue(any(r["op"] == "ui.session.wrapup" for r in fake.requests))
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":3}}\n')
        proc.stdin.flush()
        cancelled = json.loads(proc.stdout.readline())
        self.assertEqual(cancelled["id"], 3)
        self.assertTrue(cancelled["result"]["isError"])
        fake.wrapup_gate.set()
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
        self.assertFalse(any(r["op"] == "session.end" for r in fake.requests))

    def test_eof_during_wrapup_skips_session_end(self):
        fake = self.daemon
        fake.wrapup_gate = threading.Event()
        env = dict(os.environ, PCPILOT_SOCKET=fake.path, PCPILOT_DESKTOP_USER=str(os.getuid()),
                   PCPILOT_A11Y_RUNTIME_DIR=fake.runtime_dir)
        proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.addCleanup(cleanup_process, proc)
        proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        proc.stdin.flush(); proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"control","arguments":{"action":"start"}}}\n')
        proc.stdin.flush(); proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"control","arguments":{"action":"stop"}}}\n')
        proc.stdin.flush()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(r["op"] == "ui.session.wrapup" for r in fake.requests):
            time.sleep(.01)
        self.assertTrue(any(r["op"] == "ui.session.wrapup" for r in fake.requests))
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
        fake.wrapup_gate.set()
        self.assertFalse(any(r["op"] == "session.end" for r in fake.requests))

    def test_bad_batch_preflight_performs_no_action(self):
        self.initialize()
        result = self.call_tool("act", {"actions": [{"click": [2, 2]}, {"move": [2000, 3]}]})
        self.assertTrue(result["isError"])
        self.assertIn("outside scaled screen", result["content"][0]["text"])
        self.assertFalse(any(r["op"] == "ui.pointer.click" for r in self.daemon.requests))
        for bad_args in ({"actions": []}, {"actions": [{"nonesuch": "x"}]},
                         {"actions": [{"click": [True, 2]}]}, {"actions": [{"clipboard_get": False}]}):
            self.assertTrue(self.call_tool("act", bad_args)["isError"])

    def test_unknown_tool_is_jsonrpc_invalid_params(self):
        self.initialize()
        reply = self.rpc("tools/call", {"name": "not_a_tool", "arguments": {}})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_malformed_nested_arguments_are_errors_not_dispatch_crashes(self):
        self.initialize()
        cases = [
            ("control", {"action": ["start"]}),
            ("act", {"actions": [{"window": [42, ["close"]]}]}),
            ("act", {"actions": [{"window": [42, {"action": "close"}]}]}),
            ("act", {"actions": [{"click": [1, 1]}], "observe": {"mode": "elements"}}),
        ]
        for name, args in cases:
            with self.subTest(args=args):
                reply = self.rpc("tools/call", {"name": name, "arguments": args})
                self.assertIn("result", reply)
                self.assertTrue(reply["result"]["isError"])

    def test_hotkey_during_wait_stops_batch_and_remains_sticky(self):
        self.initialize()
        self.call_tool("control", {"action": "start"})
        timer = threading.Timer(.1, self.daemon.emit,
                                args=({"event": "session_ended", "reason": "user_hotkey"},))
        timer.start()
        result = self.call_tool("act", {"actions": [{"wait": 1000}, {"click": [1, 1]}]})
        timer.join(timeout=1)
        self.assertTrue(result["isError"])
        self.assertIn("USER_TOOK_CONTROL", result["content"][0]["text"])
        self.assertFalse(any(r["op"] == "ui.pointer.click" for r in self.daemon.requests))
        again = self.call_tool("act", {"actions": [{"wait": 0}]})
        self.assertTrue(again["isError"])
        self.assertIn("USER_TOOK_CONTROL", again["content"][0]["text"])

    def test_user_release_during_wait_stops_batch_until_explicit_resume(self):
        self.initialize()
        self.call_tool("control", {"action": "start"})
        timer = threading.Timer(.1, self.daemon.emit,
                                args=({"event": "session_ended", "reason": "user_release"},))
        timer.start()
        result = self.call_tool("act", {"actions": [{"wait": 1000}, {"click": [1, 1]}]})
        timer.join(timeout=1)
        self.assertTrue(result["isError"])
        self.assertIn("USER_TOOK_CONTROL", result["content"][0]["text"])
        self.assertFalse(any(r["op"] == "ui.pointer.click" for r in self.daemon.requests))
        begin_count = sum(r["op"] == "session.begin" for r in self.daemon.requests)
        again = self.call_tool("act", {"actions": [{"wait": 0}]})
        self.assertTrue(again["isError"])
        self.assertIn("USER_TOOK_CONTROL", again["content"][0]["text"])
        self.assertEqual(sum(r["op"] == "session.begin" for r in self.daemon.requests), begin_count)
        resumed = self.call_tool("control", {"action": "start", "resume": True})
        self.assertEqual(resumed["content"][0]["text"], "active")
        self.assertFalse(self.server.daemon.aborted)

    def test_disconnect_fails_pending_call_and_stdio_eof_closes_connection(self):
        self.initialize()
        self.server.daemon.call("session.status")
        self.daemon.close()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not self.server.daemon.disconnected:
            time.sleep(.01)
        with self.assertRaises(protocol.ProtocolError) as caught:
            self.server.daemon.call("session.status")
        self.assertEqual(caught.exception.code, "disconnected")
        self.assertTrue(self.server.daemon.disconnected)

        # A distinct server process must close its fake-daemon peer after stdin EOF.
        tmp2 = tempfile.TemporaryDirectory()
        fake2 = FakeDaemon(tmp2.name)
        env = dict(os.environ, PCPILOT_SOCKET=fake2.path, PCPILOT_DESKTOP_USER=str(os.getuid()),
                   PCPILOT_A11Y_RUNTIME_DIR=fake2.runtime_dir)
        proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.addCleanup(cleanup_process, proc)
        proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        proc.stdin.flush()
        self.assertEqual(json.loads(proc.stdout.readline())["id"], 1)
        proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"control","arguments":{"action":"status"}}}\n')
        proc.stdin.flush()
        self.assertEqual(json.loads(proc.stdout.readline())["id"], 2)
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and (not fake2.clients or any(c.fileno() >= 0 for c in fake2.clients)):
            time.sleep(.01)
        self.assertTrue(fake2.clients)
        self.assertTrue(all(c.fileno() < 0 for c in fake2.clients))
        fake2.close()
        tmp2.cleanup()
        proc.stdout.close()
        proc.stderr.close()

    def test_subprocess_stdin_eof_cancels_active_batch_before_later_click(self):
        fake = self.daemon
        env = dict(os.environ, PCPILOT_SOCKET=fake.path, PCPILOT_DESKTOP_USER=str(os.getuid()),
                   PCPILOT_A11Y_RUNTIME_DIR=fake.runtime_dir)
        proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.addCleanup(cleanup_process, proc)
        proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        proc.stdin.flush()
        self.assertEqual(json.loads(proc.stdout.readline())["id"], 1)
        proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"control","arguments":{"action":"status"}}}\n')
        proc.stdin.flush()
        self.assertEqual(json.loads(proc.stdout.readline())["id"], 2)
        proc.stdin.write('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"act","arguments":{"actions":[{"wait":2000},{"click":[10,10]}]}}}\n')
        proc.stdin.flush()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(r["op"] == "ui.status" for r in fake.requests):
            time.sleep(.01)
        self.assertTrue(any(r["op"] == "ui.status" for r in fake.requests))
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
        self.assertFalse(any(r["op"] == "ui.pointer.click" for r in fake.requests))
        proc.stdout.close()
        proc.stderr.close()

    def test_cancelled_notification_interrupts_active_batch(self):
        fake = self.daemon
        env = dict(os.environ, PCPILOT_SOCKET=fake.path, PCPILOT_DESKTOP_USER=str(os.getuid()),
                   PCPILOT_A11Y_RUNTIME_DIR=fake.runtime_dir)
        proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.addCleanup(cleanup_process, proc)
        proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        proc.stdin.flush()
        proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"control","arguments":{"action":"status"}}}\n')
        proc.stdin.flush()
        proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"act","arguments":{"actions":[{"wait":2000},{"click":[10,10]}]}}}\n')
        proc.stdin.flush()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(r["op"] == "ui.status" for r in fake.requests):
            time.sleep(.01)
        self.assertTrue(any(r["op"] == "ui.status" for r in fake.requests))
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":3}}\n')
        proc.stdin.flush()
        cancelled_reply = json.loads(proc.stdout.readline())
        self.assertEqual(cancelled_reply["id"], 3)
        self.assertTrue(cancelled_reply["result"]["isError"])
        self.assertFalse(any(r["op"] == "ui.pointer.click" for r in fake.requests))
        proc.stdin.write('{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"act","arguments":{"actions":[{"wait":0}]}}}\n')
        proc.stdin.flush()
        resumed_reply = json.loads(proc.stdout.readline())
        self.assertEqual(resumed_reply["id"], 4)
        self.assertEqual(resumed_reply["result"]["content"][0]["text"].strip(), "ok 1")
        self.assertEqual(sum(r["op"] == "session.begin" for r in fake.requests), 2)
        self.assertFalse(any(r["op"] == "ui.pointer.click" for r in fake.requests))
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
        proc.stdout.close()
        proc.stderr.close()

    def test_unmatched_cancel_does_not_cancel_active_request(self):
        fake = self.daemon
        env = dict(os.environ, PCPILOT_SOCKET=fake.path, PCPILOT_DESKTOP_USER=str(os.getuid()),
                   PCPILOT_A11Y_RUNTIME_DIR=fake.runtime_dir)
        proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.addCleanup(cleanup_process, proc)
        try:
            proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
            proc.stdin.flush()
            proc.stdout.readline()
            proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"control","arguments":{"action":"status"}}}\n')
            proc.stdin.flush()
            proc.stdout.readline()
            proc.stdin.write('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"act","arguments":{"actions":[{"wait":300},{"click":[10,10]}]}}}\n')
            proc.stdin.flush()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not any(r["op"] == "ui.status" for r in fake.requests):
                time.sleep(.01)
            proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":999}}\n')
            proc.stdin.flush()
            result = json.loads(proc.stdout.readline())
            self.assertEqual(result["id"], 3)
            self.assertEqual(result["result"]["content"][0]["text"].strip(), "ok 2")
            self.assertTrue(any(r["op"] == "ui.pointer.click" for r in fake.requests))
        finally:
            if proc.poll() is None:
                proc.stdin.close()
                proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()

    def test_queued_request_cancelled_before_execution(self):
        fake = self.daemon
        env = dict(os.environ, PCPILOT_SOCKET=fake.path, PCPILOT_DESKTOP_USER=str(os.getuid()),
                   PCPILOT_A11Y_RUNTIME_DIR=fake.runtime_dir)
        proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.addCleanup(cleanup_process, proc)
        proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        proc.stdin.flush()
        proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"control","arguments":{"action":"status"}}}\n')
        proc.stdin.flush()
        proc.stdout.readline()
        proc.stdin.write('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"act","arguments":{"actions":[{"wait":300},{"click":[10,10]}]}}}\n')
        proc.stdin.write('{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"control","arguments":{"action":"status"}}}\n')
        proc.stdin.flush()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(r["op"] == "ui.status" for r in fake.requests):
            time.sleep(.01)
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":4}}\n')
        proc.stdin.flush()
        first = json.loads(proc.stdout.readline())
        second = json.loads(proc.stdout.readline())
        self.assertEqual(first["id"], 3)
        self.assertEqual(first["result"]["content"][0]["text"].strip(), "ok 2")
        self.assertEqual(second["id"], 4)
        self.assertIn("cancelled before execution", second["result"]["content"][0]["text"])
        clicks = [r for r in fake.requests if r["op"] == "ui.pointer.click"]
        self.assertEqual(len(clicks), 1)
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())

    def test_screenshot_scaled_region_and_path_cleanup(self):
        self.initialize()
        result = self.call_tool("screenshot", {})["content"]
        image = result[0]
        decoded = base64.b64decode(image["data"])
        from PIL import Image
        import io
        with Image.open(io.BytesIO(decoded)) as png:
            self.assertEqual(png.size, (1280, 720))
        self.assertIn("1280x720", result[1]["text"])
        self.assertFalse(os.path.exists(self.daemon.shot_paths[-1]))
        region = self.call_tool("screenshot", {"region": [10, 10, 300, 200]})["content"]
        with Image.open(io.BytesIO(base64.b64decode(region[0]["data"]))) as png:
            self.assertEqual(png.size, (450, 300))
        sent = [r["args"].get("region") for r in self.daemon.requests if r["op"] == "ui.screenshot"]
        self.assertEqual(sent[1], {"x": 15, "y": 15, "width": 450, "height": 300})
        self.assertFalse(os.path.exists(self.daemon.shot_paths[-1]))

    def test_screenshot_source_decode_failure_still_removes_daemon_file(self):
        self.initialize()
        from PIL import Image
        with mock.patch.object(Image, "open", side_effect=OSError("bad image")):
            result = self.call_tool("screenshot", {})
        self.assertTrue(result["isError"])
        self.assertFalse(os.path.exists(self.daemon.shot_paths[-1]))

    def test_screenshot_rejects_png_over_pixel_limit_before_decode(self):
        self.initialize()
        from PIL import Image
        path = pathlib.Path(self.tmp.name, "one.png")
        Image.new("RGB", (1, 1)).save(path)
        data = bytearray(path.read_bytes())
        data[16:24] = struct.pack(">II", 8001, 8000)
        data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]) & 0xffffffff)
        self.daemon.screenshot_file_bytes = bytes(data)
        result = self.call_tool("screenshot", {})
        self.assertTrue(result["isError"])
        self.assertIn("excessive pixel dimensions", result["content"][0]["text"])
        self.assertFalse(os.path.exists(self.daemon.shot_paths[-1]))

    def test_screenshot_source_rejects_symlink_fifo_wrong_owner_and_oversize(self):
        directory = pathlib.Path(self.daemon.runtime_dir, "pcpilot")
        outside = pathlib.Path(self.tmp.name, "outside.png")
        from PIL import Image
        Image.new("RGB", (1, 1)).save(outside)
        final = directory / "shot-1-1.png"
        final.symlink_to(outside)
        with self.assertRaises((protocol.ProtocolError, OSError)):
            with mcp_server.screenshot_source(str(final)):
                pass
        self.assertTrue(outside.exists())
        final.unlink(missing_ok=True)

        with self.assertRaises(protocol.ProtocolError):
            with mcp_server.screenshot_source(str(outside)):
                pass

        os.mkfifo(final)
        with self.assertRaises(protocol.ProtocolError):
            with mcp_server.screenshot_source(str(final)):
                pass
        final.unlink(missing_ok=True)

        final.write_bytes(b"x")
        if os.geteuid() == 0:
            os.chown(final, 65534, 65534)
            with self.assertRaises(protocol.ProtocolError):
                with mcp_server.screenshot_source(str(final)):
                    pass
        else:
            self.skipTest("wrong-owner check requires ability to chown fixture")
        final.unlink()

        fd = os.open(final, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.ftruncate(fd, 64 * 1024 * 1024 + 1)
        finally:
            os.close(fd)
        with self.assertRaises(protocol.ProtocolError):
            with mcp_server.screenshot_source(str(final)):
                pass
        final.unlink(missing_ok=True)

    def test_screenshot_source_rejects_symlink_parent_and_preserves_replaced_path(self):
        runtime = pathlib.Path(self.daemon.runtime_dir)
        parent = runtime / "pcpilot"
        outside_dir = pathlib.Path(self.tmp.name, "outside")
        outside_dir.mkdir()
        (outside_dir / "shot-7-7.png").write_bytes(b"outside")
        parent.rename(runtime / "pcpilot-real")
        parent.symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaises((protocol.ProtocolError, OSError)):
            with mcp_server.screenshot_source(str(parent / "shot-7-7.png")):
                pass
        parent.unlink()
        (runtime / "pcpilot-real").rename(parent)

        shot = parent / "shot-8-8.png"
        from PIL import Image
        Image.new("RGB", (1, 1)).save(shot)
        with mcp_server.screenshot_source(str(shot)) as source:
            self.assertTrue(source.read(8))
            shot.rename(parent / "retained.png")
            shot.write_bytes(b"replacement")
        self.assertTrue(shot.exists())
        self.assertEqual(shot.read_bytes(), b"replacement")

    @unittest.skipUnless(os.geteuid() == 0, "directory ownership case requires root")
    def test_screenshot_source_rejects_untrusted_owned_directory_component(self):
        runtime = pathlib.Path(self.daemon.runtime_dir)
        parent = runtime / "pcpilot"
        moved = runtime / "pcpilot-root-owned"
        parent.rename(moved)
        parent.mkdir()
        os.chown(parent, 65534, 65534)
        (parent / "shot-9-9.png").write_bytes(b"x")
        with self.assertRaises(protocol.ProtocolError):
            with mcp_server.screenshot_source(str(parent / "shot-9-9.png")):
                pass
        parent.rename(runtime / "pcpilot-untrusted")
        moved.rename(parent)

    def test_fractional_scaling_uses_integer_model_bounds_on_both_axes(self):
        self.initialize()
        self.daemon.width, self.daemon.height = 1366, 768
        self.daemon.windows[0].update(x=100, y=50, width=800, height=600)
        full = self.call_tool("screenshot", {"region": [0, 0, 1280, 720]})
        self.assertIn("content", full)
        sent = [r["args"].get("region") for r in self.daemon.requests if r["op"] == "ui.screenshot"][-1]
        self.assertEqual(sent, {"x": 0, "y": 0, "width": 1366, "height": 768})
        result = self.call_tool("act", {"actions": [{"click": [1279, 719]}]})
        self.assertNotIn("isError", result)
        click = [r["args"] for r in self.daemon.requests if r["op"] == "ui.pointer.click"][-1]
        self.assertEqual((click["x"], click["y"]), (1365, 767))
        self.assertIn('94,47,750,562', self.call_tool("windows", {})["content"][0]["text"])
        self.assertIn('e1 button "Save" 562,375', self.call_tool("elements", {})["content"][0]["text"])

    def test_windows_and_accessibility_element_ids(self):
        self.initialize()
        self.assertIn('42 org.test.Editor "Editor" 67,33,533,400 [focused]', self.call_tool("windows", {})["content"][0]["text"])
        elements = self.call_tool("elements", {"query": "Save"})["content"][0]["text"]
        self.assertIn('e1 button "Save" 400,267', elements)
        self.call_tool("act", {"actions": [{"click": "e1"}]})
        clicked = [r["args"] for r in self.daemon.requests if r["op"] == "ui.pointer.click"][-1]
        self.assertEqual((clicked["x"], clicked["y"]), (600, 400))

    def test_stdio_subprocess_handshake_and_eof_cleanup(self):
        env = dict(os.environ, PCPILOT_SOCKET=self.daemon.path, PCPILOT_MAX_SIDE="1280")
        proc = subprocess.run([sys.executable, "-m", "pcpilot.mcp_server"], input=(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        ), text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([line["id"] for line in lines], [1, 2])
        self.assertEqual(lines[1]["result"], {})

    def test_abort_is_sticky_until_explicit_resume(self):
        self.initialize()
        self.server.daemon.aborted = True
        result = self.call_tool("act", {"actions": [{"wait": 100}]})
        self.assertTrue(result["isError"])
        self.assertIn("USER_TOOK_CONTROL", result["content"][0]["text"])
        result = self.call_tool("control", {"action": "start"})
        self.assertTrue(result["isError"])
        self.assertTrue(self.call_tool("control", {"action": "start", "resume": True})["content"])
        self.assertFalse(self.server.daemon.aborted)

    def test_resume_ack_followed_by_hotkey_keeps_abort_sticky(self):
        self.initialize()
        client = self.server.daemon
        client.aborted = True
        client.active = False
        self.daemon.session["aborted"] = True
        self.daemon.event_after_op["session.begin"] = {
            "event": "session_ended", "reason": "user_hotkey"}
        original_call = client.call

        def call_and_wait_for_following_event(op, args=None):
            result = original_call(op, args)
            if op == "session.begin":
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and not client.aborted:
                    time.sleep(.005)
            return result

        client.call = call_and_wait_for_following_event
        with self.assertRaises(protocol.ProtocolError) as caught:
            client.begin(resume=True)
        self.assertEqual(caught.exception.code, "user_aborted")
        self.assertTrue(client.aborted)
        self.assertFalse(client.active)

    def test_prepare_request_preserves_label_and_only_preserves_observed_abort(self):
        server = self.server
        old = server.daemon
        old.label = "Codex test client"
        old.aborted = True
        server.request_cancelled.set()
        server.prepare_request(10)
        self.assertIsNot(server.daemon, old)
        self.assertEqual(server.daemon.label, "Codex test client")
        self.assertTrue(server.daemon.aborted)

        server.cancelled.clear()
        server.request_cancelled.set()
        server.daemon.aborted = False
        server.prepare_request(11)
        self.assertEqual(server.daemon.label, "Codex test client")
        self.assertFalse(server.daemon.aborted)


class A11yGeometryTests(unittest.TestCase):
    def test_worker_cancel_keeps_idle_process_kills_busy_and_eof_close_kills(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = mock.Mock()
                self.stdout = mock.Mock()
                self.killed = False
                self.waited = False
            def poll(self):
                return None if not self.killed else -9
            def kill(self):
                self.killed = True
            def wait(self, timeout=None):
                self.waited = True
                return -9

        worker = mcp_server.AccessibilityWorker()
        idle = FakeProcess()
        worker.process = idle
        worker.cancel()
        self.assertIs(worker.process, idle)
        self.assertFalse(idle.killed)
        worker.reset_cancel()
        worker.busy = True
        worker.cancel()
        self.assertIsNone(worker.process)
        self.assertTrue(idle.killed)
        self.assertTrue(idle.waited)
        idle.stdin.close.assert_called_once()
        idle.stdout.close.assert_called_once()

        worker.reset_cancel()
        eof = FakeProcess()
        worker.process = eof
        worker.close()
        self.assertIsNone(worker.process)
        self.assertTrue(worker.closed)
        # EOF on stdin is the graceful path: the worker hands accessibility back and exits.
        self.assertFalse(eof.killed)
        self.assertTrue(eof.waited)
        eof.stdin.close.assert_called_once()
        eof.stdout.close.assert_called_once()

    def test_window_relative_gtk_geometry_maps_through_buffer_origin(self):
        win = {"x": 100, "y": 50, "width": 800, "height": 600,
               "buffer": {"x": 96, "y": 46, "width": 808, "height": 608}}
        self.assertEqual(a11y_worker.mapped_center((20, 30, 100, 40), (0, 0, 808, 608), None, None,
                                                    win), (166, 96))

    def test_gtk_frame_match_wins_when_buffer_has_shadows(self):
        win = {"x": 100, "y": 50, "width": 800, "height": 600,
               "buffer": {"x": 96, "y": 46, "width": 808, "height": 608}}
        center = a11y_worker.mapped_center((24, 34, 100, 40), (0, 0, 800, 600),
                                           None, None, win)
        self.assertEqual(center, (174, 104))

    def test_screen_extents_at_zero_origin_are_not_treated_as_absolute_window_coords(self):
        win = {"x": 100, "y": 50, "width": 800, "height": 600,
               "buffer": {"x": 96, "y": 46, "width": 808, "height": 608}}
        center = a11y_worker.mapped_center(None, None, (0, 0, 800, 600),
                                           (10, 20, 20, 20), win)
        self.assertIsNone(center)

    def test_ambiguous_or_unmatched_geometry_stays_unknown(self):
        windows = [{"id": 1, "pid": 99, "title": "A"}, {"id": 2, "pid": 99, "title": "B"}]
        self.assertIsNone(a11y_worker.match_window(99, "other", windows, 2))
        self.assertIsNone(a11y_worker.mapped_center((0, 0, 10, 10), (0, 0, 10, 10), None, None, None))

    def test_worker_launcher_uses_desktop_identity_and_private_session_environment(self):
        worker = mcp_server.AccessibilityWorker()
        user = SimpleNamespace(pw_uid=1000, pw_gid=1001, pw_name="desktop", pw_dir="/home/desktop")
        source_env = {"PATH": "/attacker/path", "AT_SPI_BUS_ADDRESS": "unix:path=/wrong/bus",
                      "PYTHONPATH": "/attacker/python", "LD_PRELOAD": "/attacker/lib.so",
                      "REVIEW_DUMMY_ROOT_SECRET": "root-only-placeholder", "LANG": "pt_BR.UTF-8",
                      "PCPILOT_A11Y_RUNTIME_DIR": "/run/user/1000/private",
                      "PCPILOT_A11Y_BUS_ADDRESS": "unix:path=/private/bus"}
        with mock.patch.dict(os.environ, source_env, clear=True), \
             mock.patch.object(mcp_server, "desktop_identity", return_value=user), \
             mock.patch.object(mcp_server.os, "geteuid", return_value=0), \
             mock.patch.object(mcp_server.subprocess, "Popen") as popen:
            worker.start()
        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertEqual(command[:4], ["/usr/bin/setpriv", "--reuid=1000", "--regid=1001", "--init-groups"])
        self.assertIn("/usr/bin/python3", command)
        self.assertIn("-I", command)
        self.assertTrue(os.path.isabs(command[-1]))
        self.assertEqual(env["HOME"], "/home/desktop")
        self.assertEqual(env["USER"], "desktop")
        self.assertEqual(env["LOGNAME"], "desktop")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000/private")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/private/bus")
        self.assertNotIn("AT_SPI_BUS_ADDRESS", env)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["LANG"], "pt_BR.UTF-8")
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("REVIEW_DUMMY_ROOT_SECRET", env)
        self.assertLessEqual(set(env), {"HOME", "USER", "LOGNAME", "PATH", "LANG", "XDG_RUNTIME_DIR",
                                        "DBUS_SESSION_BUS_ADDRESS"})

    def test_desktop_identity_resolves_active_graphical_seat0_only(self):
        sessions_json = json.dumps([{"session": "1"}, {"session": "2"}, {"session": "3"}])
        details = {
            "1": "Active=yes\nType=wayland\nSeat=seat0\nUser=1000\n",
            "2": "Active=yes\nType=wayland\nSeat=seat1\nUser=2000\n",
            "3": "Active=no\nType=x11\nSeat=seat0\nUser=3000\n",
        }
        def output(command, **kwargs):
            if command[1] == "list-sessions":
                return sessions_json
            return details[command[2]]
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(mcp_server.subprocess, "check_output", side_effect=output) as check_output, \
             mock.patch.object(mcp_server.pwd, "getpwuid", return_value="desktop-user") as getpw:
            self.assertEqual(mcp_server.desktop_identity(), "desktop-user")
        getpw.assert_called_once_with(1000)
        self.assertTrue(all(call.args[0][0] == "/usr/bin/loginctl" for call in check_output.call_args_list))

    def test_desktop_identity_rejects_ambiguous_active_seat0(self):
        listing = json.dumps([{"session": "a"}, {"session": "b"}])
        def output(command, **kwargs):
            if command[1] == "list-sessions":
                return listing
            return "Active=yes\nType=x11\nSeat=seat0\nUser=" + ("1000" if command[2] == "a" else "1001") + "\n"
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(mcp_server.subprocess, "check_output", side_effect=output):
            with self.assertRaises(protocol.ProtocolError) as caught:
                mcp_server.desktop_identity()
        self.assertEqual(caught.exception.code, "a11y_unavailable")



PAGE_ITEMS = ([
    {"role": "link", "name": "Home", "doc_y": 10, "doc_x": 20},
    {"role": "entry", "name": "Full name", "doc_y": 60, "doc_x": 120, "value": ""},
    {"role": "check box", "name": "Accept terms", "doc_y": 110, "doc_x": 30},
    {"role": "combo box", "name": "Country", "doc_y": 150, "doc_x": 60, "value": "Brazil"},
    {"role": "button", "name": "Send form", "doc_y": 200, "doc_x": 40},
] + [{"role": "link", "name": f"Item {i}", "doc_y": 600 + i * 50, "doc_x": 50} for i in range(1, 41)])


class ServerFixture:
    """Server + fake daemon + scrollable fake page (viewport 100,150 800x450 in a 1920x1080 screen)."""

    def build(self, items=PAGE_ITEMS):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_env = {key: os.environ.get(key) for key in
                             ("PCPILOT_SOCKET", "PCPILOT_MAX_SIDE", "PCPILOT_DESKTOP_USER",
                              "PCPILOT_A11Y_RUNTIME_DIR", "PCPILOT_THEME", "PCPILOT_AUTOSTOP_S")}
        os.environ.pop("PCPILOT_THEME", None)
        os.environ.pop("PCPILOT_AUTOSTOP_S", None)
        self.daemon = FakeDaemon(self.tmp.name)
        os.environ["PCPILOT_SOCKET"] = self.daemon.path
        os.environ["PCPILOT_MAX_SIDE"] = "1280"
        os.environ["PCPILOT_DESKTOP_USER"] = str(os.getuid())
        os.environ["PCPILOT_A11Y_RUNTIME_DIR"] = self.daemon.runtime_dir
        self.page = FakePage(items)
        self.daemon.scroll_hook = self.page.scroll
        self.daemon.windows[0].update(app_id="firefox", title="Test Page — Mozilla Firefox")
        self.server = mcp_server.Server(worker=self.page)
        self.addCleanup(self.cleanup_fixture)
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "unit"}})

    def cleanup_fixture(self):
        self.server.close()
        self.daemon.close()
        self.tmp.cleanup()
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def rpc(self, method, params=None, ident=1):
        return self.server.dispatch({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})

    def call_tool(self, name, args):
        return self.rpc("tools/call", {"name": name, "arguments": args})["result"]

    def text_of(self, name, args):
        result = self.call_tool(name, args)
        self.assertFalse(result.get("isError"), result)
        return "\n".join(part["text"] for part in result["content"] if part["type"] == "text")

    def ops(self, op):
        return [r["args"] for r in self.daemon.requests if r["op"] == op]


class PageMapTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.build()

    def id_of(self, name):
        out = self.text_of("elements", {"query": name})
        return re.search(r'(e\d+) link "' + re.escape(name) + '"', out).group(1)

    def test_page_map_is_compact_with_header_values_flags_and_scroll_hints(self):
        self.page.tabs = [["Other", False], ["Test Page", True]]
        self.page.items[2]["states"] = ["checked"]
        self.page.focus_name = "Full name"
        out = self.text_of("elements", {"max": 8})
        lines = out.splitlines()
        self.assertEqual(lines[0], 'Firefox · "Test Page" · https://example.test/ · tab 2/2 · focus e2')
        self.assertEqual(lines[1], "tabs: 1 Other | *2 Test Page")
        # Scaled model space: global (120,160) -> (80,107); the check box is aliased and flagged.
        self.assertIn('e1 link "Home" 80,107', lines)
        self.assertIn('e2 entry "Full name" 147,140', lines)
        self.assertRegex(out, r'e3 checkbox "Accept terms" \d+,\d+ \[checked\]')
        self.assertRegex(out, r'e4 combo "Country" \d+,\d+ ="Brazil"')
        # Off-screen items carry a scroll hint instead of a position.
        self.assertRegex(out, r'e6 link "Item 1" ↓0\.9')
        self.assertIn("+37 more (query/role/max)", out)
        self.assertNotIn("?", out)

    def test_query_role_and_max_filter_the_map(self):
        out = self.text_of("elements", {"query": "item 3", "role": "link"})
        names = re.findall(r'"(Item \d+)"', out)
        self.assertEqual(names, ["Item 3"] + [f"Item {n}" for n in range(30, 40)])
        self.assertEqual(self.text_of("elements", {"role": "button"}).count("\n"), 1)
        self.assertEqual(self.page.calls[-1][3], 60)

    def test_ids_are_stable_across_snapshots_and_survive_a_narrower_query(self):
        first = self.text_of("elements", {})
        self.assertIn('e5 button "Send form"', first)
        self.text_of("elements", {"query": "Send"})
        again = self.text_of("elements", {})
        self.assertIn('e5 button "Send form"', again)
        self.call_tool("act", {"actions": [{"click": "e1"}]})
        self.assertTrue(self.ops("ui.pointer.click"))

    def test_absent_tree_explains_what_to_do(self):
        self.page.absent = True
        self.assertIn("ctrl+n", self.text_of("elements", {}))

    def test_click_resolves_the_live_position_not_the_listed_one(self):
        self.text_of("elements", {})
        self.page.scroll_y = 20   # the page moved after the listing: ids must not go stale
        self.call_tool("act", {"actions": [{"click": "e2"}], "speed": "fast"})
        click = self.ops("ui.pointer.click")[-1]
        self.assertEqual((click["x"], click["y"]), (220, 150 + 60 - 20))
        self.assertEqual(self.ops("ui.pointer.scroll"), [])

    def test_offscreen_element_scrolls_in_notches_then_clicks_inside_the_viewport(self):
        item20 = self.id_of("Item 20")
        result = self.call_tool("act", {"actions": [{"click": item20}], "speed": "fast"})
        self.assertFalse(result.get("isError"), result)
        scrolls = self.ops("ui.pointer.scroll")
        self.assertTrue(scrolls)
        self.assertTrue(all(0 < abs(s["dy"]) <= mcp_server.SCROLL_OP_NOTCHES and s["dx"] == 0 for s in scrolls))
        self.assertTrue(all(s["style"] == "fast" and 100 <= s["x"] < 900 and 150 <= s["y"] < 600 for s in scrolls))
        click = self.ops("ui.pointer.click")[-1]
        self.assertTrue(100 <= click["x"] < 900 and 150 <= click["y"] < 600, click)
        self.assertNotIn("ui.screenshot", [r["op"] for r in self.daemon.requests])
        # The first guess (60 px/notch) was too long for a 48 px page; the second reach uses the measurement.
        self.assertAlmostEqual(self.server.notch_px["firefox"], 48, delta=1)
        before = len(self.ops("ui.pointer.scroll"))
        self.call_tool("act", {"actions": [{"click": self.id_of("Item 40")}], "speed": "fast"})
        click = self.ops("ui.pointer.click")[-1]
        self.assertAlmostEqual(click["y"], 150 + 450 * mcp_server.TARGET_BAND, delta=60)
        self.assertGreater(len(self.ops("ui.pointer.scroll")), before)

    def test_scroll_up_for_an_element_above_the_viewport(self):
        self.page.scroll_y = 1500
        out = self.text_of("elements", {"query": "Full name"})
        target = re.search(r'(e\d+) entry', out).group(1)
        self.call_tool("act", {"actions": [{"click": target}], "speed": "fast"})
        self.assertTrue(all(s["dy"] < 0 for s in self.ops("ui.pointer.scroll")))
        click = self.ops("ui.pointer.click")[-1]
        self.assertTrue(150 <= click["y"] < 600)

    def test_failing_to_locate_fails_closed_without_any_click(self):
        item20 = self.id_of("Item 20")
        for state, expected in (("gone", "is gone"), ("unknown", "no reliable position"), ("side", "sideways")):
            with self.subTest(state=state):
                self.page.live[25] = state
                result = self.call_tool("act", {"actions": [{"click": item20}]})
                self.assertTrue(result["isError"])
                self.assertIn(expected, result["content"][0]["text"])
                self.assertIn("elements", result["content"][0]["text"])
        self.assertEqual(self.ops("ui.pointer.click"), [])

    def test_page_that_does_not_scroll_stops_without_clicking(self):
        item20 = self.id_of("Item 20")
        self.page.drop_scroll = True
        result = self.call_tool("act", {"actions": [{"click": item20}], "speed": "fast"})
        self.assertTrue(result["isError"])
        self.assertIn("does not scroll", result["content"][0]["text"])
        self.assertEqual(self.ops("ui.pointer.click"), [])
        self.assertLessEqual(len(self.ops("ui.pointer.scroll")), 8)

    def test_scrolling_respects_the_act_budget_with_a_partial_result(self):
        item40 = self.id_of("Item 40")
        with mock.patch.object(mcp_server, "ACT_BUDGET_S", .0):
            result = self.call_tool("act", {"actions": [{"click": item40}], "speed": "fast"})
        self.assertIn("partial: 0/1", result["content"][0]["text"])
        self.assertEqual(self.ops("ui.pointer.click"), [])

    def test_observe_elements_returns_only_what_changed(self):
        self.text_of("elements", {})
        def tick(op, args):
            if op == "ui.pointer.click":
                self.page.items[2]["states"] = ["checked"]
                self.page.title = "Test Page (1)"
        self.daemon.on_request = tick
        changed = self.text_of("act", {"actions": [{"click": "e3"}], "observe": "elements", "speed": "fast"})
        body = changed.split("\n", 1)[1]
        self.assertIn('"Test Page (1)"', body)
        self.assertRegex(body, r'e3 checkbox "Accept terms" \d+,\d+ \[checked\]')
        self.assertNotIn("Home", body)
        self.assertNotIn("Send form", body)
        same = self.text_of("act", {"actions": [{"wait": 0}], "observe": "elements"})
        self.assertEqual(same.split("\n", 1)[1], "no change")

    def test_observe_elements_reports_new_and_gone_elements(self):
        self.text_of("elements", {})
        def tick(op, args):
            if op == "ui.pointer.click":
                self.page.items.append({"role": "button", "name": "Confirm", "doc_y": 250, "doc_x": 200})
                self.page.items[0]["doc_y"] = 5000   # scrolled away -> no longer visible
        self.daemon.on_request = tick
        out = self.text_of("act", {"actions": [{"click": "e5"}], "observe": "elements", "speed": "fast"})
        self.assertIn('button "Confirm"', out)
        self.assertIn("-1 gone/off-screen", out)
        self.assertNotIn("Send form", out.split("\n", 1)[1])

    def test_observe_after_a_different_listing_scope_is_a_full_listing(self):
        self.text_of("elements", {"query": "Send"})
        out = self.text_of("act", {"actions": [{"wait": 0}], "observe": "elements"})
        self.assertIn('"Home"', out)

    def test_static_rows_without_refs_keep_their_listed_position(self):
        worker = FakeWorker([{"role": "button", "name": "OK", "x": 300, "y": 200, "states": []}])
        self.server.worker = worker
        self.text_of("elements", {})
        self.call_tool("act", {"actions": [{"click": "e1"}]})
        click = self.ops("ui.pointer.click")[-1]
        self.assertEqual((click["x"], click["y"]), (300, 200))


class AccessibilityLifecycleTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.build()

    def events(self):
        return list(self.page.events)

    def test_enabled_for_the_session_and_restored_on_stop(self):
        self.call_tool("control", {"action": "start"})
        self.assertTrue(wait_until(lambda: self.events() == ["acquire"]))
        self.call_tool("elements", {})
        self.call_tool("control", {"action": "stop"})
        self.assertEqual(self.events(), ["acquire", "release"])
        self.call_tool("control", {"action": "start"})
        self.assertTrue(wait_until(lambda: self.events() == ["acquire", "release", "acquire"]))

    def test_first_tool_call_enables_it_too_and_only_once(self):
        self.call_tool("windows", {})
        self.call_tool("windows", {})
        self.assertTrue(wait_until(lambda: self.events() == ["acquire"]))
        time.sleep(.1)
        self.assertEqual(self.events(), ["acquire"])

    def test_user_hotkey_restores_it_at_once(self):
        self.call_tool("control", {"action": "start"})
        self.assertTrue(wait_until(lambda: self.events() == ["acquire"]))
        self.daemon.emit({"event": "session_ended", "reason": "user_hotkey"})
        self.assertTrue(wait_until(lambda: self.events() == ["acquire", "release"]))

    def test_idle_release_and_mcp_eof_restore_it(self):
        with mock.patch.dict(os.environ, {"PCPILOT_AUTOSTOP_S": ".08"}):
            self.server = mcp_server.Server(worker=self.page)
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "unit"}})
        self.call_tool("control", {"action": "start"})
        self.assertTrue(wait_until(lambda: self.events() == ["acquire", "release"], timeout=3))
        self.call_tool("windows", {})
        self.assertTrue(wait_until(lambda: self.events().count("acquire") == 2))
        self.server.cancel()   # EOF on stdin
        self.assertTrue(wait_until(lambda: self.events().count("release") == 2, timeout=3))

    def test_a_failing_switch_never_breaks_the_tools(self):
        def broken():
            raise protocol.ProtocolError("a11y_unavailable", "no bus")
        self.page.acquire = broken
        self.assertEqual(self.call_tool("control", {"action": "start"})["content"][0]["text"], "active")
        time.sleep(.1)
        self.assertNotIn("isError", self.call_tool("windows", {}))
        self.call_tool("control", {"action": "stop"})

    def test_stop_after_abort_still_hands_it_back(self):
        self.call_tool("control", {"action": "start"})
        self.assertTrue(wait_until(lambda: self.events() == ["acquire"]))
        self.server.daemon.aborted = True
        result = self.call_tool("control", {"action": "stop"})
        self.assertTrue(result["isError"])
        self.assertEqual(self.events(), ["acquire", "release"])


class DesktopStateTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.build()

    def test_windows_header_and_flags_are_compact(self):
        self.daemon.desktop = {
            "workspace": {"active": 1, "count": 3}, "overview": False, "pointer": {"x": 960, "y": 540},
            "scheme": "dark", "focus": 42,
            "monitors": [{"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1, "primary": True}]}
        self.daemon.windows = [
            dict(self.daemon.windows[0], id=42, maximized=True, workspace=1, monitor=0),
            {"id": 7, "title": "Term", "app_id": "kitty", "pid": 2, "x": 0, "y": 0, "width": 960, "height": 540,
             "focused": False, "minimized": True, "fullscreen": False, "workspace": -1, "monitor": 0, "new": True},
            {"id": 9, "title": "AI", "app_id": "gedit", "pid": 3, "x": 0, "y": 0, "width": 960, "height": 540,
             "focused": False, "minimized": False, "workspace": 0, "monitor": 0, "new": True, "ai": True}]
        out = self.text_of("windows", {}).splitlines()
        self.assertEqual(out[0], "ws 2/3 · mon 1280x720 · overview off · ptr 640,360 · dark")
        self.assertEqual(out[1], '42 firefox "Test Page — Mozilla Firefox" 67,33,533,400 w2 [focused max]')
        self.assertEqual(out[2], '7 kitty "Term" 0,0,640,360 w* [min new]')
        self.assertEqual(out[3], '9 * gedit "AI" 0,0,640,360 w1')

    def test_windows_with_two_monitors_and_overview_open(self):
        self.daemon.desktop = {
            "workspace": {"active": 0, "count": 1}, "overview": True, "pointer": {"x": 10, "y": 20}, "scheme": None,
            "monitors": [{"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1, "primary": True},
                         {"index": 1, "x": 1920, "y": 0, "width": 960, "height": 540, "scale": 2, "primary": False}]}
        self.daemon.windows[0]["monitor"] = 1
        out = self.text_of("windows", {}).splitlines()
        self.assertEqual(out[0], "ws 1/1 · mon 0:1280x720+0+0* 1:640x360+1280+0@2 · overview on · ptr 7,13")
        self.assertTrue(out[1].endswith(" m1 [focused]"), out[1])

    def test_old_extension_falls_back_to_the_plain_window_list(self):
        out = self.text_of("windows", {}).splitlines()
        # Without desktop.info only the pointer position (ui.pointer.position) is known.
        self.assertEqual(out, ['ptr 333,133', '42 firefox "Test Page — Mozilla Firefox" 67,33,533,400 [focused]'])

    def test_no_screenshot_is_ever_needed_for_state(self):
        self.daemon.desktop = {"workspace": {"active": 0, "count": 1}, "overview": False,
                               "pointer": {"x": 0, "y": 0}, "monitors": []}
        self.text_of("windows", {})
        self.text_of("elements", {})
        self.assertNotIn("ui.screenshot", [r["op"] for r in self.daemon.requests])


class HumanOrchestrationTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.build()
        self.daemon.windows[0]["app_id"] = "kitty"
        self.daemon.desktop = None

    def keys_and_types(self):
        return [(r["op"], r["args"].get("keys") or r["args"].get("text"))
                for r in self.daemon.requests if r["op"] in {"ui.key.press", "ui.key.type"}]

    def test_human_launch_is_super_type_enter_and_waits_for_the_window(self):
        out = self.text_of("act", {"actions": [{"launch": "firefox"}]})
        self.assertEqual(self.keys_and_types(), [("ui.key.press", "super"), ("ui.key.type", "Firefox"),
                                                 ("ui.key.press", "Return")])
        typed = self.ops("ui.key.type")[0]
        self.assertEqual(typed["style"], "human")
        self.assertRegex(out, r'^ok 1 · launched \d+ "Firefox"')
        self.assertEqual(self.ops("ui.app.launch"), [])

    def test_fast_and_instant_launch_stay_programmatic(self):
        for speed in ("fast", "instant"):
            with self.subTest(speed=speed):
                self.daemon.requests.clear()
                self.text_of("act", {"actions": [{"launch": "firefox"}, {"open": "https://x.test"}], "speed": speed})
                self.assertEqual([r["op"] for r in self.daemon.requests if r["op"].startswith(("ui.key", "ui.app.launch", "ui.open_uri"))],
                                 ["ui.app.launch", "ui.open_uri"])
                self.assertEqual(self.ops("ui.app.launch")[0]["style"], speed)

    def test_ids_and_unknown_names_use_the_programmatic_launch_not_the_overview(self):
        for name in ("org.gnome.Nautilus.desktop", "nonesuch"):
            with self.subTest(name=name):
                self.daemon.requests.clear()
                self.text_of("act", {"actions": [{"launch": name}]})
                self.assertEqual(self.ops("ui.app.launch"), [{"app": name, "style": "fast"}])
                self.assertEqual(self.ops("ui.key.press"), [])
                self.assertEqual(self.ops("ui.key.type"), [])

    def test_launch_without_a_window_closes_the_overview_and_fails_closed(self):
        self.daemon.app_search["ghost"] = [{"id": "ghost.desktop", "name": "Ghost"}]
        self.daemon.spawn_on_return = False
        with mock.patch.object(mcp_server.time, "monotonic", side_effect=self.fast_clock()):
            result = self.call_tool("act", {"actions": [{"launch": "ghost"}]})
        self.assertTrue(result["isError"])
        self.assertIn("no window appeared", result["content"][0]["text"])
        self.assertEqual(self.keys_and_types()[-1], ("ui.key.press", "Escape"))

    @staticmethod
    def fast_clock():
        real = time.monotonic
        start = real()
        counter = iter(range(10 ** 6))
        return lambda: start + next(counter) * .5

    def test_overview_state_decides_whether_super_is_pressed(self):
        # Closed: Super opens it (verified through desktop.info), then type + Enter.
        self.daemon.desktop = {"overview": False}
        self.text_of("act", {"actions": [{"launch": "firefox"}]})
        self.assertEqual(self.keys_and_types(), [("ui.key.press", "super"), ("ui.key.type", "Firefox"),
                                                 ("ui.key.press", "Return")])
        self.assertFalse(self.daemon.desktop["overview"])
        # Already open (a fresh shell starts that way): pressing Super would close it.
        self.daemon.requests.clear()
        self.daemon.desktop = {"overview": True}
        self.daemon.windows = self.daemon.windows[-1:]
        self.text_of("act", {"actions": [{"launch": "firefox"}]})
        self.assertEqual(self.keys_and_types(), [("ui.key.type", "Firefox"), ("ui.key.press", "Return")])

    def test_an_overview_that_never_opens_falls_back_to_the_programmatic_launch(self):
        self.daemon.desktop = {"overview": False}
        self.daemon.super_toggles_overview = False
        with mock.patch.object(mcp_server.time, "monotonic", side_effect=self.fast_clock()):
            self.text_of("act", {"actions": [{"launch": "firefox"}]})
        self.assertEqual(self.ops("ui.app.launch"), [{"app": "firefox", "style": "fast"}])
        self.assertEqual(self.ops("ui.key.type"), [])

    def test_super_key_unsupported_falls_back_to_the_programmatic_launch(self):
        original = self.daemon.handle
        def refuse(op, args):
            if op == "ui.key.press" and args.get("keys") == "super":
                class Refused(Exception):
                    code = "bad_args"
                raise Refused("no such key")
            return original(op, args)
        self.daemon.handle = refuse
        self.text_of("act", {"actions": [{"launch": "firefox"}]})
        self.assertEqual(self.ops("ui.app.launch"), [{"app": "firefox", "style": "fast"}])

    def test_open_in_a_focused_browser_is_ctrl_l_type_enter(self):
        self.daemon.windows[0]["app_id"] = "org.mozilla.firefox"
        self.text_of("act", {"actions": [{"open": "https://example.org/a?b=1"}]})
        self.assertEqual(self.keys_and_types(), [("ui.key.press", "ctrl+l"),
                                                 ("ui.key.type", "https://example.org/a?b=1"),
                                                 ("ui.key.press", "Return")])
        self.assertEqual(self.ops("ui.open_uri"), [])

    def test_open_without_a_browser_in_focus_uses_the_default_handler(self):
        self.text_of("act", {"actions": [{"open": "https://example.org"}]})
        self.assertEqual(self.ops("ui.open_uri"), [{"uri": "https://example.org", "style": "fast"}])
        self.assertEqual(self.ops("ui.key.press"), [])

    def test_open_rejects_multiline_addresses(self):
        self.daemon.windows[0]["app_id"] = "firefox"
        self.assertTrue(self.call_tool("act", {"actions": [{"open": "a\nb"}]})["isError"])

    def test_window_buttons_are_passed_to_the_extension_when_found(self):
        located = []
        def button(window, action, windows):
            located.append((window, action))
            return {"found": True, "x": 1290.4, "y": 62}
        self.page.button = button
        self.text_of("act", {"actions": [{"window": [42, "close"]}, {"window": [42, "fit"]}]})
        closes = self.ops("ui.window.action")
        self.assertEqual(closes[0], {"id": 42, "action": "close", "style": "human", "at": {"x": 1290, "y": 62}})
        self.assertNotIn("at", closes[1])           # only close/minimize/maximize use a button
        self.assertEqual(located, [(42, "close")])
        self.text_of("act", {"actions": [{"window": [42, "close"]}], "speed": "fast"})
        self.assertNotIn("at", self.ops("ui.window.action")[-1])    # programmatic styles never look
        self.assertEqual(len(located), 1)

    def test_a_missing_or_failing_button_lookup_still_closes_the_window(self):
        self.page.button = lambda *args: {"found": False}
        self.text_of("act", {"actions": [{"window": [42, "close"]}]})
        self.assertNotIn("at", self.ops("ui.window.action")[-1])
        def broken(*args):
            raise protocol.ProtocolError("a11y_unavailable", "bus gone")
        self.page.button = broken
        self.text_of("act", {"actions": [{"window": [42, "minimize"]}]})
        self.assertEqual(self.ops("ui.window.action")[-1]["action"], "minimize")
        self.assertNotIn("at", self.ops("ui.window.action")[-1])

    def test_focus_forwards_the_style_for_pointer_driven_activation(self):
        self.text_of("act", {"actions": [{"focus": 42}]})
        self.assertEqual(self.ops("ui.window.action")[-1], {"id": 42, "action": "activate", "style": "human"})

    def test_long_text_is_split_below_the_extension_guard(self):
        text_ = ("lorem ipsum dolor " * 40).strip()
        self.text_of("act", {"actions": [{"type": text_}]})
        chunks = [a["text"] for a in self.ops("ui.key.type")]
        self.assertGreater(len(chunks), 2)
        # Every piece stays in the extension's brisk band (121..240), cut at spaces.
        self.assertTrue(all(121 <= len(c) <= 240 for c in chunks), [len(c) for c in chunks])
        self.assertTrue(all(c.endswith(" ") for c in chunks[:-1]))
        self.assertEqual("".join(chunks), text_)
        self.assertTrue(all(a["style"] == "human" for a in self.ops("ui.key.type")))
        self.daemon.requests.clear()
        self.text_of("act", {"actions": [{"type": "short"}]})
        self.assertEqual([a["text"] for a in self.ops("ui.key.type")], ["short"])

    def test_split_text_boundaries(self):
        split = mcp_server.Server.split_text
        self.assertEqual(split("short", 240), ["short"])
        self.assertEqual(split("x" * 260, 240), ["x" * 260])
        for length in (261, 300, 360, 480, 481, 1000, 2399):
            with self.subTest(length=length):
                text_ = ("ab " * length)[:length]
                pieces = split(text_, 240)
                self.assertEqual("".join(pieces), text_)
                self.assertTrue(all(121 <= len(p) <= 240 for p in pieces), [len(p) for p in pieces])
        self.assertEqual(split("y" * 2500, 1000), ["y" * 834, "y" * 833, "y" * 833])

    def test_type_budget_reports_how_much_was_typed(self):
        text_ = "word " * 200
        original = self.server.type_piece
        calls = []
        def slow(piece, style):
            calls.append(piece)
            original(piece, style)
            time.sleep(.05)
        self.server.type_piece = slow
        with mock.patch.object(mcp_server, "ACT_BUDGET_S", .07):
            out = self.text_of("act", {"actions": [{"type": text_}, {"key": "Return"}]})
        self.assertIn("partial: 0/2 done, budget", out)
        self.assertRegex(out, r"typed \d+/1000 chars")
        self.assertEqual(self.ops("ui.key.press"), [])

    def test_extension_refusing_a_chunk_is_retried_in_halves(self):
        attempts = []
        original = self.daemon.handle
        def refuse(op, args):
            if op == "ui.key.type":
                attempts.append(len(args["text"]))
                if len(args["text"]) > 100:
                    class TooLong(Exception):
                        code = "bad_args"
                    raise TooLong("typing would take ~20s (max 15s): split the text")
            return original(op, args)
        self.daemon.handle = refuse
        text_ = "ab cd " * 30
        self.text_of("act", {"actions": [{"type": text_}]})
        self.assertEqual(attempts[0], len(text_))
        self.assertTrue(all(n <= 100 for n in attempts[1:]))
        self.assertEqual(sum(n for n in attempts[1:]), len(text_))


class NavigationWithoutScreenshotTests(ServerFixture, unittest.TestCase):
    def test_open_page_fill_form_and_submit_never_takes_a_screenshot(self):
        self.build()
        self.daemon.windows[0]["app_id"] = "kitty"
        page = self.page
        form = {"typed": [], "clicked": []}
        def effect(op, args):
            if op == "ui.key.type" and page.focus_name:
                form["typed"].append((page.focus_name, args["text"]))
            if op == "ui.pointer.click":
                hit = next((item for item in page.items if page.position(item)[0] == "in"
                            and abs(page.position(item)[2] - args["x"]) < 1 and abs(page.position(item)[3] - args["y"]) < 1), None)
                if hit:
                    page.focus_name = hit["name"]
                    form["clicked"].append(hit["name"])
                    if hit["name"] == "Send form":
                        page.title = "Submitted"
        self.daemon.on_request = effect
        # 1. launch + open (human), 2. read the page map, 3. fill/submit by id, 4. read changes.
        self.text_of("act", {"actions": [{"launch": "firefox"}, {"open": "https://example.test/form"}]})
        listing = self.text_of("elements", {})
        ids = dict(re.findall(r'(e\d+) \w+ "([^"]+)"', listing)[i][::-1] for i in range(5))
        result = self.text_of("act", {"actions": [
            {"click": ids["Full name"]}, {"type": "Ana Souza"},
            {"click": ids["Accept terms"]}, {"click": ids["Send form"]}], "observe": "elements", "speed": "fast"})
        self.assertEqual(form["clicked"], ["Full name", "Accept terms", "Send form"])
        self.assertEqual(form["typed"], [("Full name", "Ana Souza")])
        self.assertIn('"Submitted"', result)
        self.assertNotIn("ui.screenshot", [r["op"] for r in self.daemon.requests])
        self.assertNotIn("screenshot", [r["op"] for r in self.daemon.requests])



class FakeAtspiNode:
    """Minimal stand-in for Atspi.Accessible: enough for the worker's scan/locate."""

    def __init__(self, role, name="", box=None, states=(), children=(), text=None, attrs=None,
                 url=None, selected=None):
        self.role, self.name, self.box = role, name, box
        self.states = set(states)
        self.children = list(children)
        self.text, self.attrs, self.url, self.selected = text, attrs or {}, url, selected

    def get_role_name(self): return self.role
    def get_name(self): return self.name
    def get_state_set(self): return SimpleNamespace(contains=lambda state: state in self.states)
    def get_child_count(self): return len(self.children)
    def get_child_at_index(self, index): return self.children[index]
    def get_attributes(self): return self.attrs
    def get_selection_iface(self):
        return SimpleNamespace(get_n_selected_children=lambda: int(self.selected is not None),
                               get_selected_child=lambda i: self.selected)

    def get_component_iface(self):
        if self.box is None:
            return SimpleNamespace(get_extents=lambda _t: SimpleNamespace(x=-1, y=-1, width=-1, height=-1))
        x, y, w, h = self.box
        return SimpleNamespace(get_extents=lambda _t: SimpleNamespace(x=x, y=y, width=w, height=h))

    def get_text_iface(self):
        return None if self.text is None else self

    def get_document_iface(self):
        return SimpleNamespace(get_document_attribute_value=lambda key: self.url) if self.url else None

    def get_value_iface(self): return None
    def get_process_id(self): return 100


class FakeAtspiModule:
    class StateType:
        pass

    class CoordType:
        WINDOW = "WINDOW"
        SCREEN = "SCREEN"

    class Text:
        """The real binding exposes text only through these static functions."""
        get_character_count = staticmethod(lambda node: len(node.text))
        get_text = staticmethod(lambda node, a, b: node.text[a:b])

    def __init__(self, apps):
        self.apps = apps
        for name in ("DEFUNCT", "VISIBLE", "SHOWING", "FOCUSABLE", "FOCUSED", "CHECKED", "SELECTED",
                     "EXPANDED", "PRESSED", "REQUIRED", "INVALID_ENTRY", "SENSITIVE", "EDITABLE",
                     "ACTIVE", "BUSY"):
            setattr(self.StateType, name, name)

    def get_desktop(self, _index):
        return SimpleNamespace(get_child_count=lambda: len(self.apps),
                               get_child_at_index=lambda i: self.apps[i])


UP = {"VISIBLE", "SHOWING", "SENSITIVE"}


def firefox_tree():
    """A recorded-shape Firefox window: tabs, urlbar, a showing document and a hidden background one."""
    def web(role, name, box, extra=(), **kw):
        return FakeAtspiNode(role, name, box, UP | set(extra), **kw)
    def offscreen(role, name, box, **kw):
        return FakeAtspiNode(role, name, box, {"VISIBLE", "SENSITIVE"}, **kw)
    option = FakeAtspiNode("menu item", "Brazil", None, {"SELECTED"})
    inner = web("document web", "Inner", (50, 400, 300, 100), children=[web("button", "Inner button", (60, 410, 80, 20))])
    document = web("document web", "Test Page", (26, 108, 1280, 887), url="https://example.test/form", children=[
        web("heading", "Sign up", (50, 120, 400, 30), attrs={"level": "2"}),
        web("link", "Home", (50, 160, 40, 17)),
        web("button", "Close", (1200, 130, 40, 20)),
        web("entry", "Full name", (50, 200, 200, 22), {"FOCUSED", "FOCUSABLE", "EDITABLE"}, text="Ana"),
        web("password text", "Password", (50, 230, 200, 22), {"EDITABLE"}, text="secret"),
        web("check box", "Accept terms", (50, 260, 14, 14), {"CHECKED"}),
        web("combo box", "Country", (50, 290, 80, 22), children=[option], selected=option),
        FakeAtspiNode("button", "Disabled action", (50, 320, 100, 22), {"VISIBLE", "SHOWING"}),
        web("button", "Menu", (50, 350, 50, 22), {"EXPANDED"}),
        web("link", "Wrapper", (50, 380, 100, 17), children=[web("link", "Wrapper", (50, 380, 100, 17))]),
        inner,
        web("heading", "Long list", (50, 900, 400, 30), attrs={"level": "2"}),
        offscreen("link", "Item 1", (90, 1008, 44, 17)),
        offscreen("link", "Item 2", (90, 1053, 44, 17)),
        offscreen("link", "Above", (90, -3000, 44, 17)),
    ])
    hidden_tab = FakeAtspiNode("document web", "Other tab", (26, 108, 1280, 887), {"VISIBLE"},
                               children=[web("link", "Ghost link", (50, 160, 40, 17))])
    chrome = [
        FakeAtspiNode("tool bar", "Browser tabs", (26, 23, 1280, 44), UP, children=[
            FakeAtspiNode("page tab list", "", (66, 23, 1000, 44), UP, children=[
                web("page tab", "Other", (66, 23, 200, 44), {"FOCUSABLE"}),
                web("page tab", "Test Page", (266, 23, 200, 44), {"FOCUSABLE", "SELECTED"})])]),
        FakeAtspiNode("tool bar", "Window controls", (1200, 23, 106, 44), UP, children=[
            web("button", "Close", (1266, 23, 40, 44))]),
        FakeAtspiNode("tool bar", "Navigation", (26, 67, 1280, 40), UP, children=[
            web("button", "Stop", (138, 67, 36, 40)),
            web("combo box", "Search or enter address", (313, 71, 742, 32), {"EDITABLE", "FOCUSABLE"},
                text="https://example.test/form?typed", attrs={"id": "urlbar-input"})]),
        FakeAtspiNode("menu", "File", None, set(), children=[web("menu item", "Hidden item", (0, 0, 10, 10))]),
        FakeAtspiNode("panel", "", (26, 107, 1280, 888), UP, children=[hidden_tab, document]),
    ]
    frame = FakeAtspiNode("frame", "Test Page — Mozilla Firefox", (26, 23, 1280, 972), UP | {"ACTIVE"}, children=chrome)
    app = FakeAtspiNode("application", "Firefox", None, set(), children=[frame])
    return app, frame, document


class WorkerReaderTests(unittest.TestCase):
    WINDOWS = [{"id": 5, "pid": 100, "title": "Test Page — Mozilla Firefox", "x": 30, "y": 40,
                "width": 1280, "height": 972, "focused": True, "minimized": False}]

    def reader(self):
        self.app, self.frame, self.document = firefox_tree()
        reader = object.__new__(a11y_worker.Reader)
        reader.atspi = FakeAtspiModule([self.app])
        reader.refs = {}
        reader.next_ref = 1
        return reader

    def test_web_page_map_has_values_flags_visibility_and_scroll_distance(self):
        reply = self.reader().snapshot({"windows": self.WINDOWS, "max": 60})
        names = {row["name"]: row for row in reply["elements"]}
        # Window-relative extents are moved by the compositor frame: (50,160)+(30-26, 40-23).
        self.assertEqual((names["Home"]["x"], names["Home"]["y"]), (74, 186))   # 160 + 17 + 8.5, rounded
        self.assertEqual(names["Home"]["vis"], "in")
        self.assertEqual(names["Full name"]["value"], "Ana")
        self.assertIn("focused", names["Full name"]["states"])
        self.assertEqual(names["Password"]["value"], "(6 chars)")      # never the password itself
        self.assertEqual(names["Accept terms"]["states"], ["checked"])
        self.assertEqual(names["Country"]["value"], "Brazil")
        self.assertEqual(names["Disabled action"]["states"], ["disabled"])
        self.assertEqual(names["Menu"]["states"], ["expanded"])
        self.assertEqual(names["Inner button"]["vis"], "in")           # inside the nested document
        self.assertEqual(names["Item 1"]["vis"], "below")
        self.assertAlmostEqual(names["Item 1"]["dist"], (1008 + 8.5 - (108 + 887 / 2)) / 887, delta=.05)
        self.assertEqual(names["Above"]["vis"], "above")
        self.assertNotIn("Ghost link", names)                           # background tab is not the page
        self.assertNotIn("Hidden item", names)                          # menus are skipped
        self.assertNotIn("Stop", names)                                 # chrome only with a query
        self.assertEqual(sum(row["name"] == "Wrapper" for row in reply["elements"]), 1)  # no duplicate child
        self.assertEqual([h["name"] for h in reply["headings"]], ["Sign up", "Long list"])
        self.assertEqual(reply["headings"][0]["level"], 2)

    def test_page_header_reads_url_title_tabs_loading_address_and_focus(self):
        page = self.reader().snapshot({"windows": self.WINDOWS})["page"]
        self.assertEqual(page["app"], "Firefox")
        self.assertEqual(page["title"], "Test Page")
        self.assertEqual(page["url"], "https://example.test/form")
        self.assertEqual(page["tabs"], [["Other", False], ["Test Page", True]])
        self.assertTrue(page["loading"])                                # the Stop button is showing
        self.assertEqual(page["address"], "https://example.test/form?typed")
        self.assertEqual(page["focus"]["name"], "Full name")
        self.assertEqual(page["vp"], [30, 125, 1280, 887])

    def test_max_picks_visible_first_then_nearest_offscreen_in_document_order(self):
        reply = self.reader().snapshot({"windows": self.WINDOWS, "max": 11})
        names = [row["name"] for row in reply["elements"]]
        self.assertIn("Item 1", names)
        self.assertNotIn("Above", names)
        self.assertEqual(reply["page"]["extra"], 2)
        reply = self.reader().snapshot({"windows": self.WINDOWS, "max": 60, "query": "item"})
        self.assertEqual([row["name"] for row in reply["elements"]], ["Item 1", "Item 2"])
        self.assertEqual(reply["headings"], [])

    def test_query_also_searches_the_browser_chrome_and_role_filters(self):
        reply = self.reader().snapshot({"windows": self.WINDOWS, "query": "stop"})
        self.assertEqual([(row["name"], row.get("chrome")) for row in reply["elements"]], [("Stop", True)])
        reply = self.reader().snapshot({"windows": self.WINDOWS, "role": "entry,checkbox"})
        self.assertEqual([row["name"] for row in reply["elements"]], ["Full name", "Accept terms"])

    def test_locate_requeries_extents_so_ids_follow_scrolling(self):
        reader = self.reader()
        reply = reader.snapshot({"windows": self.WINDOWS})
        item = next(row for row in reply["elements"] if row["name"] == "Item 1")
        node = reader.refs[item["ref"]]["node"]
        far = reader.locate({"ref": item["ref"], "windows": self.WINDOWS})
        self.assertEqual(far["state"], "below")
        self.assertEqual(far["vp"], [30, 125, 1280, 887])
        node.box = (90, 300, 44, 17)                                    # the page scrolled
        near = reader.locate({"ref": item["ref"], "windows": self.WINDOWS})
        self.assertEqual((near["state"], near["x"], near["y"]), ("in", 116, 326))
        node.states.add("DEFUNCT")
        self.assertEqual(reader.locate({"ref": item["ref"], "windows": self.WINDOWS})["state"], "gone")
        self.assertEqual(reader.locate({"ref": 99999, "windows": self.WINDOWS})["state"], "gone")
        node.states.discard("DEFUNCT")
        self.assertEqual(reader.locate({"ref": item["ref"], "windows": []})["state"], "unknown")

    def test_window_button_is_found_in_the_titlebar_only(self):
        reader = self.reader()
        close = reader.button({"window": 5, "action": "close", "windows": self.WINDOWS})
        # (1266 - 26 + 30) + 20, (23 - 23 + 40) + 22: the toolbar one, not the page's own "Close".
        self.assertEqual(close, {"found": True, "x": 1290, "y": 62})
        self.assertEqual(reader.button({"window": 5, "action": "minimize", "windows": self.WINDOWS}), {"found": False})
        self.assertEqual(reader.button({"window": 6, "action": "close", "windows": self.WINDOWS}), {"found": False})
        self.assertEqual(reader.button({"window": 5, "action": "hide", "windows": self.WINDOWS}), {"found": False})
        names = {row["name"] for row in reader.snapshot({"windows": self.WINDOWS, "max": 60})["elements"]}
        self.assertIn("Close", names)    # the page's own button is still a page element

    def test_untrusted_frame_geometry_never_yields_a_position(self):
        windows = [dict(self.WINDOWS[0], width=900, height=500)]
        reply = self.reader().snapshot({"windows": windows})
        self.assertTrue(all(row["x"] is None for row in reply["elements"]))

    def test_app_without_a_tree_is_reported_absent(self):
        reader = self.reader()
        reader.atspi.apps = []
        self.assertEqual(reader.snapshot({"windows": self.WINDOWS}), {"elements": [], "absent": True})

    def test_same_titled_sibling_windows_are_told_apart_by_size_then_focus(self):
        a = {"id": 1, "pid": 9, "title": "T", "x": 0, "y": 0, "width": 800, "height": 600, "focused": False}
        b = {"id": 2, "pid": 9, "title": "T", "x": 5, "y": 5, "width": 1000, "height": 700, "focused": True}
        self.assertIsNone(a11y_worker.match_window(9, "T", [a, b], 2))
        self.assertEqual(a11y_worker.match_window(9, "T", [a, b], 2, size=(800, 600))["id"], 1)
        self.assertEqual(a11y_worker.match_window(9, "T", [a, dict(b, width=800, height=600)], 2,
                                                  size=(800, 600), active=True)["id"], 2)
        self.assertIsNone(a11y_worker.match_window(9, "T", [a, dict(b, width=800, height=600)], 2,
                                                   size=(800, 600), active=False))

    def test_viewport_classification(self):
        view = (0, 100, 1000, 500)
        self.assertEqual(a11y_worker.classify((10, 200, 20, 20), view), ("in", 0.0))
        self.assertEqual(a11y_worker.classify((10, 1100, 20, 20), view)[0], "below")
        self.assertEqual(a11y_worker.classify((10, -400, 20, 20), view)[0], "above")
        self.assertEqual(a11y_worker.classify((5000, 200, 20, 20), view)[0], "side")
        self.assertAlmostEqual(a11y_worker.classify((10, 1100, 20, 20), view)[1], (1110 - 350) / 500)


class AccessibilitySwitchTests(unittest.TestCase):
    """Reference-counted, crash-safe toggling of org.a11y.Status IsEnabled (no real D-Bus)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"PCPILOT_A11Y_STATE_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.bus = {"value": False, "puts": [], "fail_put": False}
        bus = self.bus
        def get(_self):
            return bus["value"]
        def put(_self, value):
            if bus["fail_put"]:
                return False
            bus["puts"].append(value)
            bus["value"] = value
            return True
        for name, fn in (("get", get), ("put", put)):
            patcher = mock.patch.object(a11y_worker.Switch, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alive = set()
        patcher = mock.patch.object(a11y_worker, "_pid_alive", lambda pid: pid in self.alive)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self):
        path = Path(self.tmp.name, "a11y.json")
        return json.loads(path.read_text() or "{}") if path.exists() else {}

    def test_enables_for_the_session_and_restores_the_previous_value(self):
        switch = a11y_worker.Switch()
        self.assertEqual(switch.acquire(), {"changed": True, "enabled": True})
        self.assertEqual(self.record()["prev"], False)                  # recorded BEFORE the change
        self.assertEqual(self.record()["owners"], [os.getpid()])
        self.assertTrue(self.bus["value"])
        self.assertEqual(switch.release(), {"restored": True})
        self.assertFalse(self.bus["value"])
        self.assertEqual(self.bus["puts"], [True, False])
        self.assertEqual(self.record(), {})

    def test_an_already_enabled_setting_is_never_touched_or_restored(self):
        self.bus["value"] = True
        switch = a11y_worker.Switch()
        self.assertEqual(switch.acquire(), {"changed": False, "enabled": True})
        self.assertEqual(switch.release(), {"restored": False})
        self.assertEqual(self.bus["puts"], [])
        self.assertTrue(self.bus["value"])

    def test_the_last_of_several_owners_restores(self):
        self.alive.add(4242)
        Path(self.tmp.name, "a11y.json").write_text(json.dumps({"prev": False, "owners": [4242]}))
        self.bus["value"] = True
        mine = a11y_worker.Switch()
        self.assertEqual(mine.acquire(), {"changed": False, "enabled": True})
        self.assertEqual(sorted(self.record()["owners"]), sorted([4242, os.getpid()]))
        self.assertEqual(mine.release(), {"restored": False})
        self.assertTrue(self.bus["value"])
        self.alive.clear()                                              # the other owner died
        Path(self.tmp.name, "a11y.json").write_text(json.dumps({"prev": False, "owners": [4242]}))
        a11y_worker.Switch().recover()
        self.assertFalse(self.bus["value"])
        self.assertEqual(self.record(), {})

    def test_a_dead_owner_is_recovered_at_the_next_start(self):
        Path(self.tmp.name, "a11y.json").write_text(json.dumps({"prev": False, "owners": [31337]}))
        self.bus["value"] = True
        a11y_worker.Switch().recover()
        self.assertEqual(self.bus["puts"], [False])
        self.assertEqual(self.record(), {})

    def test_recover_leaves_a_live_owner_and_a_user_choice_alone(self):
        self.alive.add(777)
        Path(self.tmp.name, "a11y.json").write_text(json.dumps({"prev": False, "owners": [777]}))
        self.bus["value"] = True
        a11y_worker.Switch().recover()
        self.assertEqual(self.bus["puts"], [])
        Path(self.tmp.name, "a11y.json").write_text("")
        a11y_worker.Switch().recover()                                   # no record: nothing to undo
        self.assertEqual(self.bus["puts"], [])

    def test_a_failed_enable_leaves_no_record_and_a_failed_restore_keeps_it_for_retry(self):
        self.bus["fail_put"] = True
        switch = a11y_worker.Switch()
        self.assertEqual(switch.acquire(), {"changed": False, "enabled": False})
        self.assertEqual(self.record(), {})
        self.bus["fail_put"] = False
        switch.acquire()
        self.bus["fail_put"] = True
        self.assertEqual(switch.release(), {"restored": False})
        self.assertEqual(self.record()["prev"], False)
        self.bus["fail_put"] = False
        a11y_worker.Switch().recover()
        self.assertFalse(self.bus["value"])

    def test_an_unreadable_bus_changes_nothing(self):
        with mock.patch.object(a11y_worker.Switch, "get", lambda _self: None):
            self.assertEqual(a11y_worker.Switch().acquire(), {"changed": False, "enabled": None})
        self.assertEqual(self.bus["puts"], [])
        self.assertEqual(self.record(), {})

    def test_worker_process_restores_on_eof_and_sigterm(self):
        """The real worker script: EOF (a SIGKILLed MCP closes the pipe) and SIGTERM both release."""
        script = (Path(a11y_worker.__file__)).read_text()
        harness = Path(self.tmp.name, "harness.py")
        harness.write_text(
            "import sys, runpy\n"
            "sys.path.insert(0, %r)\n"
            "import a11y_worker as w\n"
            "state = {'v': False}\n"
            "w.Switch.get = lambda self: state['v']\n"
            "def put(self, v):\n    state['v'] = v\n    print('PUT', v, file=sys.stderr, flush=True)\n    return True\n"
            "w.Switch.put = put\n"
            "w.main()\n" % str(Path(a11y_worker.__file__).parent))
        for how in ("eof", "sigterm"):
            with self.subTest(how=how):
                env = dict(os.environ, PCPILOT_A11Y_STATE_DIR=str(Path(self.tmp.name, how)))
                proc = subprocess.Popen([sys.executable, str(harness)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                proc.stdin.write(b'{"op":"acquire"}\n')
                proc.stdin.flush()
                self.assertEqual(json.loads(proc.stdout.readline()), {"changed": True, "enabled": True})
                if how == "eof":
                    proc.stdin.close()
                else:
                    proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=5), 0)
                self.assertEqual(proc.stderr.read().decode().split(), ["PUT", "True", "PUT", "False"])
                for stream in (proc.stdout, proc.stderr):
                    stream.close()
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
        self.assertIn("def main", script)


class AccessibilityWorkerClientTests(unittest.TestCase):
    def test_acquire_release_and_dead_worker_recovery(self):
        worker = mcp_server.AccessibilityWorker()
        sent = []
        def request(payload, timeout=15, *, force=False):
            sent.append((payload["op"], force))
            return {"changed": True, "enabled": True}
        worker.request = request
        worker.acquire()
        self.assertTrue(worker.held)
        worker.release()
        self.assertFalse(worker.held)
        self.assertEqual(sent, [("acquire", False), ("release", True)])
        worker.release()                                                 # idempotent
        self.assertEqual(len(sent), 2)
        recoveries = []
        worker._recover = lambda: recoveries.append(1)
        worker.held = True
        def dead(payload, timeout=15, *, force=False):
            raise OSError("worker died")
        worker.request = dead
        worker.release()
        self.assertEqual(recoveries, [1])

    def test_killing_a_holding_worker_starts_a_detached_recovery(self):
        class Proc:
            def __init__(self): self.stdin, self.stdout, self.killed = mock.Mock(), mock.Mock(), False
            def poll(self): return -9 if self.killed else None
            def kill(self): self.killed = True
            def wait(self, timeout=None): return -9
        worker = mcp_server.AccessibilityWorker()
        user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_name="u", pw_dir="/tmp")
        worker.process, worker.held = Proc(), True
        with mock.patch.object(mcp_server, "desktop_identity", return_value=user), \
             mock.patch.object(mcp_server.subprocess, "Popen") as popen:
            worker.cancel()          # idle: kept
            self.assertFalse(popen.called)
            worker.busy = True
            worker.cancel()          # busy: killed, then --recover in a new session
        command = popen.call_args.args[0]
        self.assertEqual(command[-1], "--recover")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertFalse(worker.held)

    def test_graceful_close_does_not_spawn_a_recovery(self):
        class Proc:
            def __init__(self): self.stdin, self.stdout = mock.Mock(), mock.Mock()
            def poll(self): return None
            def kill(self): raise AssertionError("must not kill")
            def wait(self, timeout=None): return 0
        worker = mcp_server.AccessibilityWorker()
        worker.process, worker.held = Proc(), True
        with mock.patch.object(mcp_server.subprocess, "Popen") as popen:
            worker.close()
        self.assertFalse(popen.called)
        self.assertFalse(worker.held)

    def test_worker_environment_carries_only_the_state_dir_override(self):
        user = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="d", pw_dir="/home/d")
        with mock.patch.dict(os.environ, {"PCPILOT_A11Y_STATE_DIR": "/run/x", "PCPILOT_OTHER": "no"}, clear=True), \
             mock.patch.object(mcp_server, "desktop_identity", return_value=user), \
             mock.patch.object(mcp_server.os, "geteuid", return_value=0):
            _command, env = mcp_server.AccessibilityWorker()._spec()
        self.assertEqual(env["PCPILOT_A11Y_STATE_DIR"], "/run/x")
        self.assertNotIn("PCPILOT_OTHER", env)


if __name__ == "__main__":
    unittest.main()
