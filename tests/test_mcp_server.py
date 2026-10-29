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
import struct
import zlib
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from pcpilot import a11y_worker, mcp_server, protocol
from tests.mcp_fake_daemon import FakeDaemon, FakeWorker


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
                             ("PCPILOT_SOCKET", "PCPILOT_MAX_SIDE", "PCPILOT_DESKTOP_USER", "PCPILOT_A11Y_RUNTIME_DIR")}
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

    def test_tools_list_compact_json_rpc_size_stays_under_budget(self):
        self.initialize()
        reply = self.rpc("tools/list")
        encoded = json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        self.assertLessEqual(len(encoded), 2900)

    def test_act_maps_all_action_shapes_and_scales_coordinates(self):
        self.initialize()
        result = self.call_tool("act", {"actions": [
            {"click": [640, 360]}, {"double_click": [10, 20]}, {"right_click": [20, 30]},
            {"move": [1279, 719]}, {"drag": [0, 1, 640, 360]}, {"scroll": [1, 2, -3]},
            {"type": "hi"}, {"key": "ctrl+s"}, {"wait": 0}, {"launch": "firefox"},
            {"open": "https://example.test"}, {"focus": 42}, {"window": [42, "close"]},
            {"clipboard_set": "x"}, {"clipboard_get": True}], "observe": "none"})
        self.assertEqual(result["content"][0]["text"], "ok 15\n")
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
        result = self.call_tool("act", {"actions": [{"type": f"x{i}"} for i in range(13)], "speed": "human"})
        styles = [r["args"].get("style") for r in self.daemon.requests if r["op"] == "ui.key.type"]
        self.assertEqual(len(styles), 13)
        self.assertEqual(styles[-1], "fast")
        self.assertIn("fast", result["content"][0]["text"].casefold())

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
        window_text = self.call_tool("windows", {})["content"][0]["text"]
        self.assertIn("42 * org.test.Editor", window_text)
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
        self.assertTrue(eof.killed)
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


if __name__ == "__main__":
    unittest.main()
