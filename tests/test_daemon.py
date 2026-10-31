import json
import os
import signal
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from pcpilot import evdev, protocol as P
from pcpilot.daemon import ConfigError, DeviceSet, Hotkey, load_config
from tests.daemon_testlib import AgentClient, CompositorView, DaemonProcess, FakeDevice, FakeUI, UIError, intruder_connect, wait_until


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self.devices = []
        self.clients = []
        self.ui = None
        self.daemon = None

    def device(self, kind="keyboard", **kw):
        d = FakeDevice(kind, **kw)
        self.devices.append(d)
        self.addCleanup(d.destroy)
        return d

    def start(self, **cfg):
        self.daemon = DaemonProcess(**cfg).start()
        self.addCleanup(self.daemon.cleanup)
        return self.daemon

    def ui_connect(self, **kw):
        self.ui = FakeUI(self.daemon, **kw)
        self.addCleanup(self.ui.close)
        self.assertTrue(self.ui.hello and self.ui.hello.get("ok"), self.ui.hello)
        return self.ui

    def agent(self, **kw):
        a = AgentClient(self.daemon.socket, **kw)
        self.clients.append(a)
        self.addCleanup(a.close)
        return a

    def begin(self, a, **args):
        return a.call("session.begin", args)

    def begin_ok(self, a, **args):
        r = self.begin(a, **args)
        self.assertTrue(r.get("ok"), r)
        return r

    def end(self, a, reason=None):
        mark = a.mark()
        a.call("session.end")
        if reason:
            self.assertIsNotNone(a.wait_event("session_ended", since=mark, reason=reason))

    def tearDown(self):
        # Check while the daemon is still alive, as required by the harness contract.
        if self.daemon and self.daemon.proc and self.daemon.proc.poll() is None:
            self.daemon.assert_only_test_devices_open()


class SessionTests(DaemonCase):
    def test_session_theme_is_whitelisted_and_reported_to_ui(self):
        self.device()
        self.start()
        ui, agent = self.ui_connect(), self.agent()
        for value in ("claude", "codex", "gemini", "neutral", "surprise", 42, [], {}):
            with self.subTest(theme=value):
                mark = ui.mark()
                self.begin_ok(agent, label="Test", theme=value)
                state = ui.wait_state(True, since=mark)
                expected = value if isinstance(value, str) and value in {"claude", "codex", "gemini", "neutral"} else "neutral"
                self.assertEqual(state["theme"], expected)
                self.assertEqual(state["label"], "Test")
                self.end(agent)
        mark = ui.mark()
        self.begin_ok(agent)
        self.assertEqual(ui.wait_state(True, since=mark)["theme"], "neutral")
        self.end(agent)

    def test_failed_capture_warning_dedupes_until_reason_change_or_device_reappears(self):
        class Loop:
            def create_task(self, coro):
                coro.close()
                return type("Done", (), {"done": lambda self: True})()

        class Daemon:
            cfg = {}
            loop = Loop()
            def devices_changed(self, _session): pass

        class Device:
            def ungrab(self): pass

        daemon = Daemon()
        ds = DeviceSet(daemon, object())
        info = type("Info", (), {"rdev": (1, 2), "name": "Keyboard"})()
        cap = type("Cap", (), {"info": info, "label": lambda self: "Keyboard",
                                "dev": Device(), "state": "failed", "task": None})()
        ds.caps[info.rdev] = cap
        with self.assertLogs("pcpilotd", level="WARNING") as records:
            ds._fail(cap, "reason A")
            ds._fail(cap, "reason A")
            ds._fail(cap, "reason B")
        self.assertEqual(len(records.output), 2)

        # A failed acquisition is scheduled again on every scan. After that
        # device disappears, its reason cache is cleared so a return can warn.
        cap.task = type("Done", (), {"done": lambda self: True})()
        ds.candidates = lambda: {info.rdev: info}
        ds.scan()
        self.assertEqual(cap.state, "acquiring")
        cap.state = "failed"
        ds.scan()
        self.assertEqual(cap.state, "acquiring")
        ds._fail(cap, "reason B")
        ds.candidates = lambda: {}
        ds._close_cap = lambda _cap: None
        ds._drop(cap, "removed")
        returned = type("Cap", (), {"info": info, "label": lambda self: "Keyboard",
                                     "dev": Device(), "state": "failed", "task": None})()
        ds.caps[info.rdev] = returned
        with self.assertLogs("pcpilotd", level="WARNING") as records:
            ds._fail(returned, "reason B")
        self.assertEqual(len(records.output), 1)

    def test_grab_effective_and_exclusive_reader(self):
        k, p = self.device(), self.device("pointer")
        d = self.start(); ui = self.ui_connect(); a = self.agent()
        reader = k.open_reader(); self.addCleanup(reader.close)
        self.begin_ok(a)
        self.assertTrue(wait_until(lambda: k.grabbed_by_someone() and p.grabbed_by_someone()))
        k.tap(evdev.KEY_A)
        time.sleep(.08)
        ready, _, _ = select.select([reader.fd], [], [], .1)
        self.assertFalse(ready, "exclusive grab must keep emitted keys away from other readers")

    def test_hotkey_left_right_and_split_devices(self):
        cases = [((evdev.KEY_LEFTCTRL, evdev.KEY_LEFTALT), False),
                 ((evdev.KEY_RIGHTCTRL, evdev.KEY_RIGHTALT), False),
                 ((evdev.KEY_LEFTCTRL, evdev.KEY_LEFTALT), True)]
        for i, (mods, split) in enumerate(cases):
            with self.subTest(mods=mods, split=split):
                k1 = self.device(tag=f"k{i}a"); k2 = self.device(tag=f"k{i}b") if split else k1
                d = self.start(); ui = self.ui_connect(); a = self.agent()
                self.begin_ok(a); mark_a, mark_u = a.mark(), ui.mark()
                for mod in mods[:1]: k1.press(mod)
                time.sleep(.03)
                for mod in mods[1:]: k2.press(mod)
                time.sleep(.03)
                k1.tap(evdev.KEY_ESC)
                self.assertIsNotNone(a.wait_event("session_ended", since=mark_a, reason="user_hotkey"))
                self.assertIsNotNone(ui.wait_for(lambda m: m.get("event")=="state" and m.get("active") is False and m.get("reason")=="user_hotkey", since=mark_u))
                self.assertFalse(k1.grabbed_by_someone())
                for mod in mods[:1]: k1.release(mod)
                for mod in mods[1:]: k2.release(mod)
                if split: k2.destroy()
                k1.destroy()
                self.assertTrue(a.call("session.end")["ok"])
                self.doCleanups()
                self.setUp()

    def test_escape_without_ctrl_does_not_end(self):
        k=self.device(); d=self.start(); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        k.tap(evdev.KEY_ESC); time.sleep(.1)
        self.assertTrue(a.call("session.status")["result"]["active"])

    def test_ungrab_precedes_notifications(self):
        k=self.device(); d=self.start(); ui=self.ui_connect(); a=self.agent(); self.begin_ok(a)
        ma,mu=a.mark(),ui.mark(); k.hotkey()
        ev=a.wait_event("session_ended",since=ma,reason="user_hotkey")
        st=ui.wait_for(lambda m:m.get("event")=="state" and m.get("active") is False,since=mu)
        self.assertIsNotNone(ev); self.assertFalse(k.grabbed_by_someone())
        self.assertIsNotNone(st); self.assertFalse(k.grabbed_by_someone())

    def test_release_wait_then_grab(self):
        k=self.device(); p=self.device("pointer"); k.press(evdev.KEY_ENTER); p.press(evdev.BTN_LEFT)
        d=self.start(release_wait_s=1.2); self.ui_connect(); a=self.agent()
        rid=a.request("session.begin"); time.sleep(.15)
        self.assertIsNone(a.response(rid,.01)); self.assertFalse(k.grabbed_by_someone())
        k.release(evdev.KEY_ENTER); p.release(evdev.BTN_LEFT)
        self.assertTrue(a.response(rid,3)["ok"]); self.assertTrue(k.grabbed_by_someone())

    def test_release_wait_forces_synthetic_key_release(self):
        k=self.device(); k.press(evdev.KEY_ENTER); d=self.start(release_wait_s=.8); self.ui_connect(); a=self.agent()
        self.begin_ok(a)
        self.assertTrue(wait_until(lambda: evdev.KEY_ENTER not in k.held_keys()))
        self.assertTrue(k.grabbed_by_someone())

    def test_ui_ack_timeout_and_delayed_ack(self):
        k=self.device(); d=self.start(ui_ack_timeout_s=.45); ui=self.ui_connect(auto_ack=False); a=self.agent()
        rid=a.request("session.begin"); active=ui.wait_state(True); self.assertIsNotNone(active)
        self.assertFalse(k.grabbed_by_someone())
        r=a.response(rid,2); self.assertEqual(r["error"]["code"],P.E_UI_UNAVAILABLE)
        inactive=ui.wait_for(lambda m:m.get("event")=="state" and not m.get("active"))
        self.assertEqual(inactive["reason"],"ui_timeout"); self.assertFalse(k.grabbed_by_someone())

    def test_begin_without_ui(self):
        d=self.start(); a=self.agent(); r=self.begin(a); self.assertEqual(r["error"]["code"],P.E_UI_UNAVAILABLE)

    def test_begin_waits_for_delayed_ack(self):
        k=self.device(); d=self.start(ui_ack_timeout_s=.8); ui=self.ui_connect(ack_delay=.25); a=self.agent()
        rid=a.request("session.begin"); self.assertIsNotNone(ui.wait_state(True))
        self.assertFalse(k.grabbed_by_someone())
        self.assertTrue(a.response(rid,2)["ok"]); self.assertTrue(k.grabbed_by_someone())

    def test_hotplug_device_and_removed_device(self):
        k=self.device(); d=self.start(); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        new=self.device(tag="late"); self.assertTrue(wait_until(new.grabbed_by_someone,2))
        new.destroy(); self.assertTrue(a.call("session.status")["result"]["active"])

    def test_hotplug_waits_for_held_key(self):
        base=self.device(tag="base"); d=self.start(); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        late=self.device(tag="held"); late.press(evdev.KEY_ENTER)
        time.sleep(.35); self.assertFalse(late.grabbed_by_someone())
        late.release(evdev.KEY_ENTER); self.assertTrue(wait_until(late.grabbed_by_someone,2))

    def test_idle_timeout(self):
        k=self.device(); d=self.start(idle_timeout_s=.8); ui=self.ui_connect(); a=self.agent(); self.begin_ok(a)
        mark=a.mark(); e=a.wait_event("session_ended",timeout=2,since=mark,reason="idle_timeout")
        self.assertIsNotNone(e); self.assertFalse(k.grabbed_by_someone())

    def test_ui_operations_reset_idle_timer_but_status_does_not(self):
        k=self.device(); d=self.start(idle_timeout_s=.75); ui=self.ui_connect(); a=self.agent()
        self.begin_ok(a)
        # Repeated forwarded operations touch the activity clock.
        for _ in range(4):
            self.assertTrue(a.call("ui.ping")["ok"]); time.sleep(.35)
        self.assertTrue(a.call("session.status")["result"]["active"])
        # status polls do not keep an idle session alive.
        mark=a.mark()
        while a.call("session.status")["result"]["active"]: time.sleep(.1)
        self.assertIsNotNone(a.wait_event("session_ended",since=mark,reason="idle_timeout"))

    def test_max_duration(self):
        k=self.device(); d=self.start(idle_timeout_s=3,max_session_s=.8); ui=self.ui_connect(); a=self.agent(); self.begin_ok(a)
        self.assertIsNotNone(a.wait_event("session_ended",timeout=2,reason="max_duration")); self.assertFalse(k.grabbed_by_someone())

    def test_agent_and_ui_disconnect(self):
        k=self.device(); d=self.start(); ui=self.ui_connect(); a=self.agent(); self.begin_ok(a)
        m=ui.mark(); a.close()
        st=ui.wait_for(lambda x:x.get("event")=="state" and not x.get("active"),since=m)
        self.assertEqual(st["reason"],"agent_disconnected"); self.assertFalse(k.grabbed_by_someone())

        # Separate daemon instance for UI disconnect case.
        self.doCleanups(); self.setUp(); k=self.device(); d=self.start(); ui=self.ui_connect(handler=lambda op,args:None); a=self.agent(); self.begin_ok(a)
        p=a.request("ui.pending"); ui.wait_for(lambda m:m.get("op")=="pending")
        ui.close(); self.assertEqual(a.response(p,1)["error"]["code"],P.E_UI_UNAVAILABLE)
        e=a.wait_event("session_ended",reason="ui_disconnected")
        self.assertIsNotNone(e); self.assertFalse(k.grabbed_by_someone())

    def test_user_aborted_resume_and_agent_membership(self):
        k=self.device(); d=self.start(); ui=self.ui_connect(); a=self.agent(); b=self.agent()
        self.begin_ok(a); mark=a.mark(); k.hotkey()
        self.assertIsNotNone(a.wait_event("session_ended",since=mark,reason="user_hotkey"))
        self.assertEqual(a.call("session.status")["result"]["aborted"],True)
        self.assertEqual(b.call("ui.ping")["error"]["code"],P.E_USER_ABORTED)
        self.assertEqual(a.call("ui.ping")["error"]["code"],P.E_USER_ABORTED)
        self.assertEqual(self.begin(a)["error"]["code"],P.E_USER_ABORTED)
        self.assertTrue(a.call("session.status")["ok"]); self.assertTrue(a.call("session.end")["ok"])
        self.begin_ok(a,resume=True); self.assertFalse(a.call("session.status")["result"]["aborted"])
        # A connection established after a later hotkey is not part of the abort set.
        mark=a.mark(); k.hotkey()
        self.assertIsNotNone(a.wait_event("session_ended",since=mark,reason="user_hotkey"))
        c=self.agent(); self.assertFalse(c.call("session.status")["result"]["aborted"])

    def test_routing_ids_errors_and_owner_checks(self):
        k=self.device(); d=self.start(); ui=self.ui_connect(handler=lambda op,args:None); a=self.agent(); b=self.agent()
        self.assertEqual(a.call("ui.ping")["error"]["code"],P.E_NO_SESSION)
        self.begin_ok(a); req=a.request("ui.key.press",{"key":"A"}); msg=ui.wait_for(lambda m:m.get("op")=="key.press")
        self.assertNotEqual(msg["id"],req); self.assertEqual(msg["args"]["key"],"A")
        self.assertIsInstance(msg.get("session"),int)
        ui.send(P.ok(msg["id"],{"done":True})); self.assertEqual(a.response(req)["result"],{"done":True})
        self.assertEqual(b.call("ui.ping")["error"]["code"],P.E_NO_SESSION)
        self.assertEqual(b.call("session.begin")["error"]["code"],P.E_BUSY)

    def test_ui_error_timeout_and_hotkey_cancels_pending(self):
        k=self.device(); d=self.start(ui_timeout_s=.35); ui=self.ui_connect(handler=lambda op,args:None); a=self.agent(); self.begin_ok(a)
        r=a.call("ui.hang",timeout=2); self.assertEqual(r["error"]["code"],P.E_UI_TIMEOUT)
        self.assertIsNotNone(a.wait_event("session_ended",reason="ui_timeout"))
        self.assertFalse(k.grabbed_by_someone())
        self.begin_ok(a,resume=True)
        p=a.request("ui.hang"); ui.wait_for(lambda m:m.get("op")=="hang")
        k.hotkey(); self.assertEqual(a.response(p,1)["error"]["code"],P.E_USER_ABORTED)

    def test_ui_error_code_is_forwarded(self):
        def reject(op,args): raise UIError("fake_ui_error","rejected")
        k=self.device(); d=self.start(); self.ui_connect(handler=reject); a=self.agent(); self.begin_ok(a)
        self.assertEqual(a.call("ui.action")["error"]["code"],"fake_ui_error")

    def test_session_end_idempotent_and_cli_release(self):
        k=self.device(); d=self.start(); ui=self.ui_connect(); a=self.agent(); b=self.agent(); self.begin_ok(a)
        self.assertTrue(a.call("session.end")["ok"]); self.assertTrue(a.call("session.end")["ok"])
        self.begin_ok(a)
        status=subprocess.run([sys.executable,"-m","pcpilot.cli","--socket",d.socket,"status","--json"],capture_output=True,text=True,timeout=4)
        self.assertEqual(status.returncode,0,status.stderr); self.assertIn('"ui_connected": true',status.stdout)
        p=subprocess.run([sys.executable,"-m","pcpilot.cli","--socket",d.socket,"release"],capture_output=True,text=True,timeout=4)
        self.assertEqual(p.returncode,0,p.stderr); self.assertFalse(k.grabbed_by_someone())
        self.assertIsNotNone(a.wait_event("session_ended",reason="user_release"))
        self.assertEqual(a.call("ui.ping")["error"]["code"],P.E_USER_ABORTED)

    def test_sigterm_shutdown_and_sigkill_kernel_release(self):
        k=self.device(); d=self.start(); ui=self.ui_connect(); a=self.agent(); self.begin_ok(a); ma,mu=a.mark(),ui.mark()
        d.stop(signal.SIGTERM); self.assertEqual(d.proc.returncode,0); self.assertFalse(os.path.exists(d.socket))
        self.assertFalse(k.grabbed_by_someone())
        self.assertIsNotNone(a.wait_event("session_ended",since=ma,reason="shutdown"))
        self.assertIsNotNone(ui.wait_for(lambda m:m.get("event")=="state" and not m.get("active") and m.get("reason")=="shutdown",since=mu))

    def test_sigkill_releases_kernel_grab(self):
        k=self.device(); d=self.start(); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        self.assertTrue(k.grabbed_by_someone()); d.stop(signal.SIGKILL)
        self.assertEqual(d.proc.returncode,-signal.SIGKILL); self.assertFalse(k.grabbed_by_someone())


class AuthenticationAndStartupTests(unittest.TestCase):
    def test_intruder_is_silently_closed(self):
        d=DaemonProcess().start()
        try: self.assertEqual(intruder_connect(d.socket),b"")
        finally: d.assert_only_test_devices_open(); d.cleanup()

    def test_ui_authentication_rejections(self):
        d=DaemonProcess().start()
        try:
            ui=FakeUI(d,hold_wayland=False); self.assertEqual(ui.hello["error"]["code"],P.E_UNAUTHORIZED); self.assertTrue(ui.wait_closed()); ui.close()
        finally: d.assert_only_test_devices_open(); d.cleanup()

    def test_agent_uid_only_config_rejects_ui(self):
        d=DaemonProcess(desktop_user=4242).start()
        try:
            ui=FakeUI(d); self.assertEqual(ui.hello["error"]["code"],P.E_UNAUTHORIZED); ui.close()
        finally: d.assert_only_test_devices_open(); d.cleanup()

    def test_second_valid_ui_gets_busy(self):
        d=DaemonProcess().start()
        try:
            u1=FakeUI(d); u2=FakeUI(d); self.assertEqual(u2.hello["error"]["code"],P.E_BUSY); u1.close(); u2.close()
        finally: d.assert_only_test_devices_open(); d.cleanup()

    def test_startup_requires_filter_and_duplicate_socket_refused(self):
        with tempfile.TemporaryDirectory() as td:
            sock=os.path.join(td,"s"); cfg=os.path.join(td,"c.json")
            with open(cfg,"w") as f: f.write(json.dumps({"desktop_user":os.getuid()}))
            for env in ({}, {"PCPILOT_SOCKET":sock}):
                e=os.environ.copy(); e.update(env); e.pop("PCPILOT_SOCKET",None) if not env else None
                p=subprocess.run([sys.executable,"-m","pcpilot.daemon","--socket",sock,"--config",cfg],env=e,capture_output=True,timeout=4)
                self.assertEqual(p.returncode,2,p.stderr.decode())
            d=DaemonProcess().start()
            try:
                p=subprocess.run([sys.executable,"-m","pcpilot.daemon","--socket",d.socket,"--config",d.config_path],capture_output=True,timeout=4)
                self.assertNotEqual(p.returncode,0)
                a=AgentClient(d.socket); self.assertTrue(a.call("session.status")["ok"]); a.close()
            finally: d.assert_only_test_devices_open(); d.cleanup()

    def test_env_socket_override_requires_filter(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=os.path.join(td,"cfg")
            with open(cfg,"w") as f: f.write("{}")
            env=os.environ.copy(); env["PCPILOT_SOCKET"]=os.path.join(td,"sock")
            p=subprocess.run([sys.executable,"-m","pcpilot.daemon","--config",cfg],env=env,capture_output=True,timeout=4)
            self.assertEqual(p.returncode,2,p.stderr.decode())


class PureUnitTests(unittest.TestCase):
    def test_hotkey_parse_match(self):
        h=Hotkey("Ctrl+Alt+Esc"); self.assertTrue(h.matches({evdev.KEY_RIGHTCTRL,evdev.KEY_LEFTALT,evdev.KEY_ESC}))
        self.assertFalse(h.matches({evdev.KEY_LEFTCTRL,evdev.KEY_ESC}))
        for bad in ("", "Ctrl++Esc", "Ctrl+Nope+Esc", "Ctrl+Alt+Ctrl"):
            with self.assertRaises(ConfigError): Hotkey(bad)

    def test_load_config_validation(self):
        with tempfile.TemporaryDirectory() as td:
            p=os.path.join(td,"cfg")
            with open(p,"w") as f: f.write(json.dumps({"idle_timeout_s":True}))
            with self.assertRaises(ConfigError): load_config(p,True)
            with open(p,"w") as f: f.write("[]")
            with self.assertRaises(ConfigError): load_config(p,True)

    def test_capture_verdict_filter_and_virtuality(self):
        base=evdev.DeviceInfo("event999","/dev/input/event999","pcpilot-test-good",1 << evdev.EV_KEY,
                              sum(1 << c for c in range(30,40)),True,(1,999))
        bad=evdev.DeviceInfo(base.event,base.devnode,"unrelated",base.ev_mask,base.key_mask,base.virtual,base.rdev)
        nonvirtual=evdev.DeviceInfo(base.event,base.devnode,base.name,base.ev_mask,base.key_mask,False,base.rdev)
        self.assertFalse(evdev.capture_verdict(bad,[],"pcpilot-test-")[0])
        self.assertFalse(evdev.capture_verdict(nonvirtual,[],"pcpilot-test-")[0])
        self.assertTrue(evdev.capture_verdict(base,[],"pcpilot-test-")[0])

    def test_protocol_encode_decode(self):
        msg=P.request(7,"ui.ping",{"text":"olá"}); self.assertEqual(P.decode(P.encode(msg)),msg)
        with self.assertRaises(ValueError): P.decode(b"[]\n")


class SecurityRegressionTests(DaemonCase):
    def test_r1_newline_socket_forgery_is_rejected(self):
        d=self.start()
        compositor=_listener_process(d.wayland_socket); self.addCleanup(_stop,compositor)
        # The forger owns a socket whose (abstract) name smuggles a second
        # /proc/net/unix line: "listening, <its own inode>, <wayland path>".
        # The old text parser accepted that; unix_diag reports real records.
        fake=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); self.addCleanup(fake.close)
        ino=os.fstat(fake.fileno()).st_ino
        fake.bind(f"\0x\n0000000000000000: 00000002 00000000 00010000 0001 01 {ino} {d.wayland_socket}")
        fake.listen(1)
        ui=FakeUI(d,hold_wayland=False); self.addCleanup(ui.close)
        self.assertEqual(ui.hello["error"]["code"],P.E_UNAUTHORIZED)
        self.assertIn("does not hold the listener",ui.hello["error"]["message"])

    def test_r1_replaced_wayland_listener_is_rejected(self):
        # Only the listener bound to the path's *current* inode counts: after
        # the path is replaced, a process still holding the old listener is
        # no longer the compositor (two listeners are never "either one").
        d=self.start()
        old_listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); self.addCleanup(old_listener.close)
        old_listener.bind(d.wayland_socket); old_listener.listen(1)
        os.unlink(d.wayland_socket)  # old_listener now listens on an unlinked inode
        replacement=_listener_process(d.wayland_socket); self.addCleanup(_stop,replacement)
        ui=FakeUI(d,hold_wayland=False); self.addCleanup(ui.close)  # this process holds old_listener only
        self.assertEqual(ui.hello["error"]["code"],P.E_UNAUTHORIZED)
        self.assertIn("does not hold the listener",ui.hello["error"]["message"])

    def test_r1_ui_revalidated_at_begin(self):
        k=self.device(); d=self.start(); ui=self.ui_connect(); a=self.agent()
        ui.wayland.close()
        r=self.begin(a)
        self.assertEqual(r["error"]["code"],P.E_UI_UNAVAILABLE)
        self.assertTrue(ui.wait_closed()); self.assertFalse(k.grabbed_by_someone())

    def test_r2_begin_then_end_same_write_cancels(self):
        k=self.device(); d=self.start(); self.ui_connect(); a=self.agent()
        a.sock.sendall(P.encode(P.request(70,"session.begin"))+P.encode(P.request(71,"session.end")))
        self.assertTrue(a.wait_for(lambda m:m.get("id")==71 and m.get("ok")) is not None)
        b=a.wait_for(lambda m:m.get("id")==70 and "ok" in m)
        self.assertIsNotNone(b); self.assertFalse(b["ok"])
        self.assertEqual(b["error"]["code"],P.E_NO_SESSION)
        self.assertTrue(wait_until(lambda:not k.grabbed_by_someone(),.5))

    def test_r2_release_after_begin_cancels_and_owner_eof_cancels(self):
        k=self.device(); d=self.start(); self.ui_connect(); a=self.agent()
        a.sock.sendall(P.encode(P.request(70,"session.begin"))+P.encode(P.request(71,"session.release")))
        self.assertEqual(a.response(71)["result"],{"released":True})
        self.assertEqual(a.response(70)["error"]["code"],P.E_USER_ABORTED)
        self.assertFalse(_ever(k.grabbed_by_someone,.6))
        b=self.agent(); m=self.ui.mark()
        b.sock.sendall(P.encode(P.request(72,"session.begin"))); b.sock.shutdown(socket.SHUT_WR)
        self.assertIsNotNone(self.ui.wait_state(False,since=m))
        self.assertFalse(_ever(k.grabbed_by_someone,.6))

    def test_r3_compositor_key_state_reconciled_on_begin_and_end(self):
        k=self.device(); view=CompositorView(k); self.addCleanup(view.close)
        k.press(evdev.KEY_ENTER); view.update()
        d=self.start(); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        self.assertEqual(view.update(),set())
        self.end(a); self.assertEqual(view.update(),set())

    def test_r3_grab_window_press_release_replayed(self):
        preamble='''
from pcpilot import evdev
original = evdev.InputDevice.grab
done = False
def race(self):
 global done
 if not done:
  done = True
  self.write_events([(evdev.EV_KEY,48,1),(evdev.EV_SYN,evdev.SYN_REPORT,0)])
  original(self)
  self.write_events([(evdev.EV_KEY,48,0),(evdev.EV_SYN,evdev.SYN_REPORT,0)])
 else: original(self)
evdev.InputDevice.grab = race
'''
        k=self.device(); view=CompositorView(k); self.addCleanup(view.close)
        d=self.start(preamble=preamble); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        self.assertTrue(k.grabbed_by_someone()); self.assertEqual(view.update(),set())
        self.assertIn((48,0),view.log); self.assertEqual(view.log[-1],(48,0))
        self.assertIn("replaying missed release of [48]",d.log())

    def test_r3_grab_window_waits_for_physical_release(self):
        preamble='''
from pcpilot import evdev
original = evdev.InputDevice.grab
done = False
def race(self):
 global done
 if not done:
  done = True
  self.write_events([(evdev.EV_KEY,48,1),(evdev.EV_SYN,evdev.SYN_REPORT,0)])
 original(self)
evdev.InputDevice.grab = race
'''
        k=self.device(); view=CompositorView(k); self.addCleanup(view.close)
        d=self.start(preamble=preamble); self.ui_connect(); a=self.agent(); rid=a.request("session.begin")
        time.sleep(.3); self.assertIsNone(a.response(rid,.01)); k.release(48)
        self.assertTrue(a.response(rid,3)["ok"]); self.assertEqual(view.update(),set())
        self.assertIn("replaying missed release",d.log())

    def test_r3_persistent_key_giveup_does_not_capture(self):
        k=self.device(); view=CompositorView(k); self.addCleanup(view.close)
        d=self.start(release_wait_s=.3); ui=self.ui_connect(); a=self.agent(); k.press(evdev.KEY_ENTER)
        stop=threading.Event()
        def repeat():
            while not stop.wait(.005):
                if evdev.KEY_ENTER not in k.held_keys(): k.press(evdev.KEY_ENTER)
        th=threading.Thread(target=repeat,daemon=True); th.start()
        try:
            r=self.begin(a); self.assertEqual(r.get("error",{}).get("code"),P.E_FAILED,r)
            inactive=ui.wait_for(lambda m:m.get("event")=="state" and not m.get("active"))
            self.assertEqual(inactive["reason"],"input_error"); self.assertFalse(k.grabbed_by_someone())
            self.assertEqual(view.update(),k.held_keys())
        finally: stop.set(); th.join(1); k.release(evdev.KEY_ENTER)

    def test_r4_held_hotkey_before_begin_aborts(self):
        k=self.device(); ui=None; d=self.start(); ui=self.ui_connect(); a=self.agent()
        k.press(evdev.KEY_LEFTCTRL); k.press(evdev.KEY_LEFTALT); k.press(evdev.KEY_ESC)
        r=self.begin(a); self.assertEqual(r["error"]["code"],P.E_USER_ABORTED)
        self.assertFalse(k.grabbed_by_someone()); self.assertTrue(a.call("session.status")["result"]["aborted"])

    def test_r4_hotkey_during_release_wait_aborts(self):
        k=self.device(); d=self.start(release_wait_s=1); self.ui_connect(); a=self.agent(); k.press(evdev.KEY_ENTER)
        rid=a.request("session.begin"); self.assertIsNotNone(self.ui.wait_state(True))
        time.sleep(.08); k.hotkey()
        self.assertEqual(a.response(rid,2)["error"]["code"],P.E_USER_ABORTED)
        self.assertFalse(k.grabbed_by_someone())

    def test_r5_ungrab_error_still_closes_every_device(self):
        preamble='''
from pcpilot import evdev
orig=evdev.InputDevice.ungrab
hit=False
def broken(self):
 global hit
 if self.grabbed and not hit:
  hit=True
  raise OSError(5,"injected ungrab failure")
 return orig(self)
evdev.InputDevice.ungrab=broken
'''
        k1=self.device(tag="one"); k2=self.device(tag="two"); d=self.start(preamble=preamble); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        k1.hotkey(); self.assertIsNotNone(a.wait_event("session_ended",reason="user_hotkey"))
        self.assertFalse(k1.grabbed_by_someone()); self.assertFalse(k2.grabbed_by_someone())
        self.assertEqual(d.input_nodes(),[])

    def test_r5_watchdog_exits_even_when_stderr_pipe_full(self):
        preamble='''
import fcntl, os, time
from pcpilot.daemon import DeviceSet
orig=DeviceSet._ingest_dev
blocked=False
r,w=os.pipe()
try: fcntl.fcntl(w, fcntl.F_SETPIPE_SZ, 4096)
except OSError: pass
def stall(self, cap, events):
 global blocked
 if not blocked:
  blocked=True
  os.set_blocking(w,False)
  while True:
   try: os.write(w,b'x'*4096)
   except BlockingIOError: break
  os.dup2(w,2)
  time.sleep(8)
 return orig(self, cap, events)
DeviceSet._ingest_dev=stall
'''
        k=self.device(); d=self.start(preamble=preamble); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        k.tap(evdev.KEY_A)
        d.proc.wait(timeout=12)
        self.assertEqual(d.proc.returncode,70); self.assertFalse(k.grabbed_by_someone())

    def test_r6_pointer_only_fails_closed(self):
        p=self.device("pointer"); d=self.start(); ui=self.ui_connect(); a=self.agent()
        r=self.begin(a); self.assertEqual(r["error"]["code"],P.E_FAILED)
        self.assertEqual(ui.wait_for(lambda m:m.get("event")=="state" and not m.get("active"))["reason"],"input_error")
        self.assertFalse(p.grabbed_by_someone())

    def test_r6_keyboard_grab_failure_releases_pointer(self):
        preamble='''
import errno
from pcpilot import evdev
orig=evdev.InputDevice.grab
def fail_keyboard(self):
 if 'keyboard' in self.name(): raise OSError(errno.EBUSY,'injected')
 return orig(self)
evdev.InputDevice.grab=fail_keyboard
'''
        k=self.device(); p=self.device("pointer"); d=self.start(preamble=preamble); ui=self.ui_connect(); a=self.agent()
        self.assertEqual(self.begin(a)["error"]["code"],P.E_FAILED)
        self.assertEqual(ui.wait_for(lambda m:m.get("event")=="state" and not m.get("active"))["reason"],"input_error")
        self.assertFalse(p.grabbed_by_someone())

    def test_r6_ticker_crash_ends_session(self):
        preamble='''
from pcpilot.daemon import DeviceSet
orig=DeviceSet.scan
count=0
def crash(self):
 global count
 count+=1
 if count>1: raise RuntimeError('injected scan failure')
 return orig(self)
DeviceSet.scan=crash
'''
        k=self.device(); d=self.start(preamble=preamble,rescan_interval_s=.15); ui=self.ui_connect(); a=self.agent(); self.begin_ok(a)
        hotplug=self.device(tag="trigger-scan")
        self.assertIsNotNone(a.wait_event("session_ended",timeout=4,reason="error"),d.log()); self.assertFalse(k.grabbed_by_someone())

    def test_r6_losing_last_keyboard_ends_and_releases_pointer(self):
        k=self.device(); p=self.device("pointer"); d=self.start(); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        k.destroy(); self.assertIsNotNone(a.wait_event("session_ended",reason="input_error"))
        self.assertFalse(p.grabbed_by_someone())

    def test_r7_ui_ping_lease_timeout_releases(self):
        k=self.device(); d=self.start(ui_ping_interval_s=.3,ui_ping_misses=3); ui=self.ui_connect(answer_pings=False); a=self.agent()
        self.begin_ok(a); self.assertIsNotNone(a.wait_event("session_ended",timeout=2,reason="ui_timeout"))
        self.assertFalse(k.grabbed_by_someone())

    def test_r8_wrong_ack_session_is_ignored(self):
        k=self.device(); d=self.start(ui_ack_timeout_s=.35); ui=self.ui_connect(ack_session=999); a=self.agent()
        self.assertEqual(self.begin(a)["error"]["code"],P.E_UI_UNAVAILABLE); self.assertFalse(k.grabbed_by_someone())

    def test_r8_stale_ack_does_not_authorize_next_session(self):
        k=self.device(); d=self.start(ui_ack_timeout_s=.3); ui=self.ui_connect(auto_ack=False); a=self.agent()
        first_id=a.request("session.begin"); first=ui.wait_state(True); old=first["session"]
        self.assertEqual(a.response(first_id,2)["error"]["code"],P.E_UI_UNAVAILABLE)
        # session.end clears the failed attempt; then stale ack arrives during N+1.
        a.call("session.end"); mark=ui.mark(); second_id=a.request("session.begin"); second=ui.wait_state(True,since=mark)
        ui.send_ack(old); time.sleep(.05); self.assertIsNone(a.response(second_id,.01))
        self.assertFalse(k.grabbed_by_someone()); ui.send_ack(second["session"])
        self.assertTrue(a.response(second_id,2)["ok"])

    def test_r9_silent_hello_timeout_and_connection_cap(self):
        d=self.start(hello_timeout_s=2.0)
        time.sleep(.3)  # let the daemon reap DaemonProcess.start()'s readiness probe (it counts as unidentified)
        silent=[socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) for _ in range(17)]
        for s in silent: s.connect(d.socket)
        self.assertTrue(wait_until(lambda:_is_closed(silent[16]),1),"17th unidentified peer should be rejected immediately")
        self.assertFalse(any(_is_closed(s) for s in silent[:16]))  # well inside the 2s hello deadline
        self.assertTrue(wait_until(lambda: all(_is_closed(s) for s in silent[:16]),4))
        for s in silent: s.close()

    def test_r9_agent_slow_reader_dropped_daemon_survives(self):
        d=self.start(); raw=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); raw.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024); raw.connect(d.socket)
        raw.sendall(P.encode(P.request(1,"hello",{"client":"flood"})))
        # Do not read; repeated small status replies eventually exceed the peer budget.
        line=P.encode(P.request(2,"session.status"))
        deadline=time.monotonic()+15
        try:
            while time.monotonic()<deadline:
                try: raw.sendall(line*512)
                except OSError: break
            self.assertTrue(wait_until(lambda:_is_closed(raw),2),d.log())
        finally: raw.close()
        a=self.agent(); self.assertTrue(a.call("session.status")["ok"]); self.assertIsNone(d.proc.poll())

    def test_r9_seventeenth_pending_ui_op_is_busy(self):
        k=self.device(); d=self.start(); ui=self.ui_connect(handler=lambda op,args:None); a=self.agent(); self.begin_ok(a)
        reqs=[a.request("ui.hang") for _ in range(17)]
        busy=a.response(reqs[-1],1); self.assertEqual(busy["error"]["code"],P.E_BUSY)
        # Cancel pending by user hotkey; these operations do not need to time out serially.
        k.hotkey()

    def test_r10_deep_json_unidentified_peer_cannot_end_session(self):
        k=self.device(); d=self.start(); self.ui_connect(); a=self.agent(); self.begin_ok(a)
        raw=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); raw.connect(d.socket)
        raw.sendall(b"["*200000+b"\n"); self.assertTrue(wait_until(lambda:_is_closed(raw),2)); raw.close()
        self.assertTrue(a.call("session.status")["result"]["active"])

    def test_r10_invalid_json_identified_agent_stays_usable(self):
        d=self.start(); a=self.agent(); a.sock.sendall(b"[\n")
        bad=a.wait_for(lambda m:m.get("error",{}).get("code")==P.E_BAD_REQUEST)
        self.assertIsNotNone(bad); self.assertTrue(a.call("session.status")["ok"])

    def test_r11_cli_client_close_reaches_daemon(self):
        from pcpilot.cli import Client
        d=self.start(); c=Client(d.socket); c.call("hello",{"client":"regression"}); c.close()
        self.assertTrue(wait_until(lambda:"agent disconnected" in d.log(),1))


def _listener_process(path):
    """Another process listening on ``path`` (stands in for a compositor)."""
    code = ("import socket,sys,time\n"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.bind(sys.argv[1]); s.listen(1)\n"
            "print('ready',flush=True); time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-I", "-c", code, path], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _stop(proc):
    proc.kill()
    proc.wait()
    proc.stdout.close()


def _ever(pred, duration, interval=0.02):
    """True if ``pred()`` is true at any poll during ``duration`` seconds."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _is_closed(sock):
    try:
        import select as _select
        poller=_select.poll(); poller.register(sock, _select.POLLHUP | _select.POLLERR | _select.POLLNVAL)
        return bool(poller.poll(0))
    except OSError:
        return True


if __name__ == "__main__":
    unittest.main()
