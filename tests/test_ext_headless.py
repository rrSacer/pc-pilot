"""End-to-end tests of pcpilot@local in the isolated nested GNOME Shell."""
import os
import pwd
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "tests/test_ext_support"


def available():
    return (os.geteuid() == 0 and bool(subprocess.run(["sh", "-c", "command -v gnome-shell"],
            stdout=subprocess.DEVNULL).returncode == 0) and
            any(p.pw_uid == 1000 and p.pw_name == "sacer" for p in pwd.getpwall()))


@unittest.skipUnless(available(), "requires root, gnome-shell, and uid 1000 sacer")
class ExtensionHeadlessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="pcpilot-ext-test-")
        cls.dir = Path(cls.tmp.name)
        cls.socket = cls.dir / "pcpilot.sock"
        cls.shell_log = open(cls.dir / "shell.log", "w+")
        cls.shell_started = False
        cls.daemon = None
        cls.log = cls.dir / "probe.jsonl"
        cls.log.touch(); cls.log.chmod(0o666)
        apps=cls.dir/"data/applications";apps.mkdir(parents=True)
        cls.extra_logs={}
        for app,title,envs in (("pcpilot-wrapup","WrapupProbe",{}),
                               ("pcpilot-family","Family",{"PROBE_DIALOG":"1"}),
                               ("pcpilot-confirm","Confirm",{"PROBE_CONFIRM_CLOSE":"1","PROBE_CONFIRM_DELAY_MS":"2000"})):
            log=cls.dir/f"{app}.jsonl";log.touch();log.chmod(0o666);cls.extra_logs[app]=log
            env=" ".join([f"PROBE_LOG={log}",f"PROBE_TITLE={title}",*(f"{k}={v}" for k,v in envs.items())])
            (apps/f"{app}.desktop").write_text(f'[Desktop Entry]\nType=Application\nName={title}\n'
                f'Exec=/usr/bin/env {env} /usr/bin/python3 {SUPPORT / "gtk_probe.py"}\nTerminal=false\n')
        try:
            subprocess.run([str(ROOT / "dev/headless-shell.sh"), "start", str(cls.dir)],
                           cwd=ROOT, check=True, stdout=cls.shell_log, stderr=subprocess.STDOUT,
                           timeout=60)
            cls.shell_started = True
            cls._start_daemon()
            ready = cls._agent()
            try: ready.call("fake.wait_ui", {"timeout": 30}, timeout=35)
            finally: ready.close()
            # Run the GTK probe in the nested shell; disown it from the wrapper command.
            subprocess.run([str(ROOT / "dev/headless-shell.sh"), "run", str(cls.dir), "--",
                "sh", "-lc", f"PROBE_LOG={cls.log} python3 {SUPPORT / 'gtk_probe.py'} >/dev/null 2>&1 &"],
                cwd=ROOT, check=True, timeout=20)
            cls._wait_probe(lambda rows: any(x.get("ev") == "layout" for x in rows), 20)
        except Exception:
            cls._cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    @classmethod
    def _cleanup(cls):
        if getattr(cls, "daemon", None):
            cls.daemon.terminate()
            try: cls.daemon.wait(timeout=3)
            except subprocess.TimeoutExpired: cls.daemon.kill(); cls.daemon.wait()
        if getattr(cls, "shell_started", False):
            subprocess.run([str(ROOT / "dev/headless-shell.sh"), "stop", str(cls.dir)],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if getattr(cls, "shell_log", None): cls.shell_log.close()
        if getattr(cls, "tmp", None): cls.tmp.cleanup()

    @classmethod
    def _wait_socket(cls):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if cls.socket.exists():
                import socket
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.settimeout(.1); probe.connect(str(cls.socket)); return
                except OSError: pass
                finally: probe.close()
            if cls.daemon.poll() is not None: raise AssertionError("fake daemon exited")
            time.sleep(.05)
        raise AssertionError("fake daemon socket did not appear")

    @classmethod
    def _start_daemon(cls):
        cls.daemon = subprocess.Popen(["python3", str(ROOT / "tests/ui_fake_daemon.py"),
            "serve", "--socket", str(cls.socket), "--sticky-session"], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls._wait_socket()

    @classmethod
    def _stop_daemon(cls):
        if cls.daemon and cls.daemon.poll() is None:
            cls.daemon.terminate()
            try: cls.daemon.wait(timeout=3)
            except subprocess.TimeoutExpired: cls.daemon.kill(); cls.daemon.wait()
        if cls.daemon and cls.daemon.stdout: cls.daemon.stdout.close()

    @classmethod
    def _agent(cls):
        from tests.ui_fake_daemon import AgentClient
        return AgentClient(str(cls.socket), timeout=30)

    @classmethod
    def _rows(cls):
        if not cls.log.exists(): return []
        out = []
        for line in cls.log.read_text(errors="replace").splitlines():
            try: out.append(__import__("json").loads(line))
            except ValueError: pass
        return out

    @classmethod
    def _test_state(cls):
        path=cls.dir/"runtime/pcpilot/test-state.json"
        try: return __import__("json").loads(path.read_text())
        except (OSError,ValueError): return None

    @classmethod
    def _wait_test_state(cls,pred,timeout=3):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            state=cls._test_state()
            if state and pred(state): return state
            time.sleep(.05)
        raise AssertionError(f"test-state condition timed out: {cls._test_state()}")

    @classmethod
    def _wait_probe(cls, pred, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = cls._rows()
            if pred(rows): return rows
            time.sleep(.05)
        raise AssertionError(f"probe condition timed out; events={cls._rows()[-10:]}")

    @classmethod
    def _begin(cls):
        a = cls._agent(); a.call("session.begin", {"label": "Test"}); return a

    def _end_client(self, a):
        try: a.call("session.end", {"reason": "agent_request"})
        except Exception: pass
        a.close()

    @classmethod
    def _shot(cls, args=None, overlay=False):
        a = cls._agent()
        try: return a.call("ui.screenshot", {**(args or {}), **({"include_overlay": True} if overlay else {})})
        finally: a.close()

    @classmethod
    def _raw(cls, name):
        path = cls.dir / name
        subprocess.run([str(ROOT / "dev/headless-shell.sh"), "run", str(cls.dir), "--",
            "python3", str(SUPPORT / "raw_screenshot.py"), str(path)], cwd=ROOT,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        return Image.open(path).convert("RGB")

    def _failureException(self, exc):
        try:
            self.shell_log.flush(); self.shell_log.seek(0)
            tail = self.shell_log.read().splitlines()[-40:]
            exc.add_note("headless shell log tail:\n" + "\n".join(tail))
        except Exception: pass
        return super()._failureException(exc)

    def test_inactive_refusal_state_ack_and_screenshot_contract(self):
        a = self._agent()
        self.addCleanup(self._end_client, a)
        from pcpilot.protocol import ProtocolError, E_INACTIVE
        a.call("fake.state", {"active": False, "reason": "user_hotkey"})
        with self.assertRaises(ProtocolError) as cm:
            a.call("fake.ui_raw", {"op": "screen.info"})
        self.assertEqual(cm.exception.code, E_INACTIVE)
        ack = a.call("fake.state", {"active": True, "label": "Test"})
        self.assertLess(ack["ack_ms"], 2000)
        self.assertIsInstance(ack.get("session"), int)
        self.assertTrue(any(e.get("event") == "state_ack" and e.get("active") and e.get("session")==ack["session"] for e in a.call("fake.events")["events"]))
        self._wait_test_state(lambda s:s["active"] and s["cursor_inhibited"] and s["cursor_engaged"] and not s["real_cursor_visible"])
        normal = self._shot(); with_overlay = self._shot(overlay=True)
        with Image.open(normal["path"]) as opened: im = opened.convert("RGB")
        self.assertEqual(im.size, (1920, 1080)); self.assertEqual((normal["width"], normal["height"]), im.size)
        with Image.open(with_overlay["path"]) as opened:
            over = opened.convert("RGB")
            px = over.getpixel((1, 540))
        self.assertTrue(all(abs(x-y) <= 10 for x, y in zip(px, (255, 69, 58))), px)
        self.assertTrue(all(abs(x-y) <= 10 for x, y in zip(over.getpixel((7, 540)), (255, 69, 58))))
        self.assertFalse(all(abs(x-y) <= 10 for x, y in zip(over.getpixel((8, 540)), (255, 69, 58))))
        self.assertNotEqual(im.getpixel((1, 540)), px)
        region = self._shot({"region": {"x": 100, "y": 100, "width": 300, "height": 200}})
        with Image.open(region["path"]) as opened: self.assertEqual(opened.size, (300, 200))
        before_toast = self._raw("before-toast.png")
        a.call("fake.state", {"active": False, "reason": "user_hotkey"})
        raw = self._raw("toast.png")
        self.assertFalse(all(abs(x-y) <= 10 for x, y in zip(raw.getpixel((1,540)), (255,69,58))))
        self.assertNotEqual(raw.crop((700, 0, 1220, 130)).tobytes(), before_toast.crop((700, 0, 1220, 130)).tobytes(), "toast should alter the top-centre crop")
        time.sleep(2.6)
        later = self._raw("after-toast.png")
        self.assertNotEqual(later.crop((700, 0, 1220, 130)).tobytes(), raw.crop((700, 0, 1220, 130)).tobytes(), "toast should disappear")
        self._wait_probe(lambda _: True, 2.5)

    def test_pointer_keyboard_typing_clipboard_and_status(self):
        a = self._begin(); from pcpilot.protocol import ProtocolError
        self.addCleanup(self._end_client, a)
        layout = next(r for r in reversed(self._wait_probe(lambda rs: any(x.get("ev")=="layout" for x in rs))) if r.get("ev")=="layout")
        windows = a.call("ui.window.list")["windows"]
        win = next(w for w in windows if w["title"] == layout["title"])
        a.call("ui.window.action", {"id":win["id"],"action":"activate"})
        self._wait_probe(lambda _: any(w["id"]==win["id"] and w["focused"] for w in a.call("ui.window.list")["windows"]))
        self._wait_test_state(lambda s:s["active"] and not s["real_cursor_visible"] and s.get("unfocus_inhibited"))
        ox, oy = win["x"], win["y"]; area = layout["widgets"]["area"]
        x, y = ox + area[0] + 40, oy + area[1] + 40
        # Mutter may report a stale pointer position just after a new session starts;
        # the extension's first move must wait for the compositor to settle.
        a.call("ui.pointer.move", {"x":x,"y":y,"style":"instant"})
        pos = a.call("ui.pointer.position")
        self.assertLessEqual(abs(pos["x"]-x),1); self.assertLessEqual(abs(pos["y"]-y),1)
        for count in (1,2,3):
            a.call("ui.pointer.click", {"x":x,"y":y,"count":count,"style":"instant"})
            self._wait_probe(lambda rs, n=count: any(r.get("ev")=="area_click" and r.get("n")==n for r in rs))
        a.call("ui.pointer.click", {"x":x,"y":y,"button":"right"})
        self._wait_probe(lambda rs: any(r.get("ev")=="area_click" and r.get("button")==3 for r in rs))
        a.call("ui.pointer.click", {"x":x,"y":y})
        self._wait_probe(lambda rs: any(r.get("ev")=="area_click" and r.get("button")==1 for r in rs))
        a.call("ui.pointer.click", {"x":x,"y":y,"modifiers":["ctrl","shift"]})
        self._wait_probe(lambda rs: any(r.get("ev")=="area_click" and r.get("button")==1 and (r.get("state",0)&5)==5 for r in rs))
        a.call("ui.pointer.drag", {"from":{"x":x,"y":y},"to":{"x":x+30,"y":y+20},"duration_ms":100})
        self._wait_probe(lambda rs: any(r.get("ev")=="drag_begin" and r.get("x")==40 and r.get("y")==40 for r in rs))
        self._wait_probe(lambda rs: any(r.get("ev")=="drag_end" and abs(r.get("dx",0)-30)<=1 and abs(r.get("dy",0)-20)<=1 for r in rs))
        a.call("ui.pointer.scroll", {"x":x,"y":y,"dy":3})
        self._wait_probe(lambda rs: sum(1 for r in rs if r.get("ev")=="scroll" and r.get("dy")==1)>=3)
        a.call("ui.pointer.scroll", {"x":x,"y":y,"dx":1,"dy":-2})
        self._wait_probe(lambda rs: sum(1 for r in rs if r.get("ev")=="scroll" and r.get("dy")==-1)>=2 and any(r.get("ev")=="scroll" and r.get("dx")==1 for r in rs))
        a.call("ui.pointer.move", {"x":x,"y":y}); self.assertEqual(a.call("ui.pointer.position"), {"x":x,"y":y})
        with self.assertRaises(ProtocolError) as cm: a.call("ui.pointer.move", {"x":3000,"y":0})
        self.assertEqual(cm.exception.code, "bad_args")
        a.call("ui.key.press", {"keys":"ctrl+shift+t"})
        self._wait_probe(lambda rs: any(r.get("ev")=="key" and r.get("name")=="T" and r.get("ctrl") and r.get("shift") for r in rs))
        with self.assertRaises(ProtocolError): a.call("ui.key.press", {"keys":"not-a-key"})
        entry = layout["widgets"]["entry"]; ex, ey = ox+entry[0]+30, oy+entry[1]+10
        a.call("ui.pointer.click", {"x":ex,"y":ey}); a.call("ui.key.press", {"keys":"ctrl+a"}); a.call("ui.key.press", {"keys":"BackSpace"})
        a.call("ui.key.type", {"text":"ação ç ã é"})
        self._wait_probe(lambda rs: any(r.get("ev")=="entry" and r.get("text")=="ação ç ã é" for r in rs), 5)
        a.call("ui.key.press", {"keys":"ctrl+a"}); a.call("ui.key.type", {"text":"Abc 123! ç Ç", "method":"keyval"})
        self._wait_probe(lambda rs: any(r.get("ev")=="entry" and r.get("text")=="Abc 123! ç Ç" for r in rs), 5)
        tv = layout["widgets"]["textview"]; tx, ty = ox+tv[0]+40, oy+tv[1]+20
        a.call("ui.pointer.click", {"x":tx,"y":ty}); a.call("ui.key.type", {"text":"linha1\nlinha2 ã"})
        self._wait_probe(lambda rs: any(r.get("ev")=="textview" and r.get("text")=="linha1\nlinha2 ã" for r in rs), 5)
        a.call("ui.clipboard.set", {"text":"ORIGINAL"}); a.call("ui.key.type", {"text":"ação é", "method":"clipboard"})
        self.assertEqual(a.call("ui.clipboard.get")["text"], "ORIGINAL")
        self.assertEqual(a.call("ui.status", {"text":"Test status"}), {})

    def test_humanized_motion_and_scroll_anchor(self):
        a=self._begin(); self.addCleanup(self._end_client,a)
        layout=next(r for r in reversed(self._wait_probe(lambda rs:any(x.get("ev")=="layout" for x in rs))) if r.get("ev")=="layout")
        w=next(x for x in a.call("ui.window.list")["windows"] if x["title"]==layout["title"])
        a.call("ui.window.action",{"id":w["id"],"action":"activate"})
        self._wait_probe(lambda _:any(x["id"]==w["id"] and x["focused"] for x in a.call("ui.window.list")["windows"]))
        area=layout["widgets"]["area"]
        p1=(w["x"]+area[0]+20,w["y"]+area[1]+20)
        p2=(w["x"]+area[0]+area[2]-30,w["y"]+area[1]+area[3]-30)
        a.call("ui.pointer.move",{"x":p1[0],"y":p1[1],"style":"instant"})
        # Move to the starting point first so GTK has entered the DrawingArea before
        # measuring the human trajectory.
        a.call("ui.pointer.move",{"x":p1[0],"y":p1[1],"style":"instant"})
        time.sleep(.05)
        before=len([r for r in self._rows() if r.get("ev")=="motion" and r.get("source")=="window"])
        start=time.monotonic(); a.call("ui.pointer.move",{"x":p2[0],"y":p2[1],"style":"human"}); human_s=time.monotonic()-start
        rows=[r for r in self._rows() if r.get("ev")=="motion" and r.get("source")=="window"][before:]
        self.assertGreaterEqual(len(rows),5,rows)
        self.assertGreaterEqual(human_s,.15)
        x0,y0=rows[0]["x"],rows[0]["y"]; x1,y1=rows[-1]["x"],rows[-1]["y"]
        dx,dy=x1-x0,y1-y0; length=max(1,(dx*dx+dy*dy)**.5)
        deviation=max(abs((r["x"]-x0)*dy-(r["y"]-y0)*dx)/length for r in rows)
        self.assertGreater(deviation,1.0,rows)
        # Cursor hiding must not drop Wayland's pointer focus: application motion,
        # click, and drag events all continue to arrive while the AI cursor is active.
        a.call("ui.pointer.click",{"x":p2[0],"y":p2[1],"style":"instant"})
        self._wait_probe(lambda rs:any(r.get("ev")=="area_click" for r in rs))
        a.call("ui.pointer.drag",{"from":{"x":p2[0],"y":p2[1]},"to":{"x":p2[0]+25,"y":p2[1]+20},"style":"instant","duration_ms":120})
        self._wait_probe(lambda rs:any(r.get("ev")=="drag_begin" for r in rs))
        self._wait_probe(lambda rs:any(r.get("ev")=="drag_end" for r in rs))
        start=time.monotonic(); a.call("ui.pointer.move",{"x":p1[0],"y":p1[1],"style":"fast"}); fast_s=time.monotonic()-start
        self.assertLess(fast_s,human_s)
        start=time.monotonic(); a.call("ui.pointer.move",{"x":p2[0],"y":p2[1],"style":"instant"}); instant_s=time.monotonic()-start
        self.assertLess(instant_s,.15)
        a.call("ui.pointer.scroll",{"dy":1,"style":"instant"})
        # The default anchor is chosen inside the focused window, away from its edges.
        result=a.call("ui.pointer.scroll",{"dy":1,"style":"instant"})
        anchor=result.get("anchor",{})
        self.assertTrue(w["x"]+w["width"]*.55 <= anchor.get("x",-1) <= w["x"]+w["width"]*.65,anchor)
        self.assertTrue(w["y"]+w["height"]*.50 <= anchor.get("y",-1) <= w["y"]+w["height"]*.60,anchor)

    def test_cursor_visibility_screenshots_and_trail_fade(self):
        a=self._begin();self.addCleanup(self._end_client,a)
        self._wait_test_state(lambda s:s["active"] and not s["real_cursor_visible"] and s.get("unfocus_inhibited"))
        x,y=200,300
        a.call("ui.pointer.move",{"x":x,"y":y,"style":"instant"})
        plain=self._shot(); decorated=self._shot(overlay=True)
        def red_pixels(path,box):
            with Image.open(path) as im: pixels=im.convert("RGB").crop(box).get_flattened_data()
            return sum(1 for r,g,b in pixels if r>220 and 20<g<90 and 50<b<125)
        box=(x-35,y-35,x+36,y+36)
        self.assertEqual(red_pixels(plain["path"],box),0)
        self.assertGreater(red_pixels(decorated["path"],box),0)
        result={}
        def long_move():
            try: result["move"]=a.call("ui.pointer.move",{"x":1700,"y":800,"style":"human"},timeout=10)
            except Exception as e: result["error"]=e
        worker=threading.Thread(target=long_move,daemon=True);worker.start();time.sleep(.35)
        mid=self._raw("trail-mid.png");worker.join(4);self.assertFalse(worker.is_alive());self.assertNotIn("error",result,str(result))
        time.sleep(1);faded=self._raw("trail-faded.png")
        # Exclude the cursor arrow at the destination; the remaining red marks are its trail.
        away=(50,50,1620,1030)
        mid_count=sum(1 for r,g,b in mid.crop(away).get_flattened_data() if r>220 and 20<g<90 and 50<b<125)
        late_count=sum(1 for r,g,b in faded.crop(away).get_flattened_data() if r>220 and 20<g<90 and 50<b<125)
        self.assertGreater(mid_count,0);self.assertEqual(late_count,0)

    def test_disable_releases_cursor_and_pointer_focus_inhibits(self):
        a=self._begin();a.close()
        self._wait_test_state(lambda s:s["active"] and s.get("unfocus_inhibited"))
        subprocess.run([str(ROOT/"dev/headless-shell.sh"),"run",str(self.dir),"--",
            "gnome-extensions","disable","pcpilot@local"],cwd=ROOT,check=True,timeout=20)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            state=self._test_state()
            if state and state.get("real_cursor_visible") and not state.get("unfocus_inhibited"):break
            time.sleep(.05)
        self.assertTrue(state and state.get("real_cursor_visible") and not state.get("unfocus_inhibited"),state)
        subprocess.run([str(ROOT/"dev/headless-shell.sh"),"run",str(self.dir),"--",
            "gnome-extensions","enable","pcpilot@local"],cwd=ROOT,check=True,timeout=20)
        waiter=self._agent()
        try:waiter.call("fake.wait_ui",{"timeout":8},timeout=10)
        finally:waiter.close()

    def test_wrapup_restores_snapshot_closes_new_and_honors_keep(self):
        a=self._agent()
        try:
            # Put Calculator in the foreground before begin; it becomes the snapshot focus.
            layout=next(r for r in reversed(self._wait_probe(lambda rs:any(x.get("ev")=="layout" for x in rs))) if r.get("ev")=="layout")
            subprocess.run([str(ROOT/"dev/headless-shell.sh"),"run",str(self.dir),"--",
                "gtk-launch","org.gnome.Calculator"],cwd=ROOT,check=True,timeout=15)
            time.sleep(.5)
            a.call("session.begin",{"label":"Focus Calculator"})
            origin=next(w for w in a.call("ui.window.list")["windows"] if w.get("app_id")=="org.gnome.Calculator.desktop")
            a.call("ui.window.action",{"id":origin["id"],"action":"activate","style":"instant"})
            self._wait_probe(lambda _:any(w["id"]==origin["id"] and w["focused"] for w in a.call("ui.window.list")["windows"]))
            a.call("ui.pointer.move",{"x":origin["x"]+origin["width"]//2,"y":origin["y"]+origin["height"]//2,"style":"instant"})
            a.call("session.end",{"reason":"agent_request"})
            a.call("session.begin",{"label":"Wrapup regression"})
            origin=next(w for w in a.call("ui.window.list")["windows"] if w.get("id")==origin["id"])
            self.assertTrue(origin["focused"],origin)
            a.call("ui.window.action",{"id":origin["id"],"action":"activate"})
            self._wait_probe(lambda _:any(w["id"]==origin["id"] and w["focused"] for w in a.call("ui.window.list")["windows"]))
            start=a.call("ui.pointer.position")
            original=next(w for w in a.call("ui.window.list")["windows"] if w["id"]==origin["id"])
            first=a.call("ui.app.launch",{"app":"pcpilot-wrapup.desktop","wait_ms":8000})["window"]
            second=a.call("ui.app.launch",{"app":"pcpilot-wrapup.desktop","wait_ms":8000})["window"]
            self.assertNotEqual(first["id"],second["id"])
            live={w["id"]:w for w in a.call("ui.window.list")["windows"]}
            self.assertTrue(live[first["id"]]["new"]);self.assertTrue(live[second["id"]]["new"])
            self.assertFalse(live[origin["id"]]["new"])
            a.call("ui.window.action",{"id":origin["id"],"action":"maximize","style":"instant"})
            a.call("ui.pointer.move",{"x":1700,"y":900,"style":"instant"})
            result=a.call("ui.session.wrapup",{"keep":[second["id"]]})
            self.assertIn(first["id"],[w["id"] for w in result["closed"]])
            kept=next(w for w in result["left_open"] if w["id"]==second["id"])
            self.assertEqual(kept["reason"],"kept")
            self.assertIn(origin["id"],result["restored"])
            restored=next(w for w in a.call("ui.window.list")["windows"] if w["id"]==origin["id"])
            self.assertEqual((restored["x"],restored["y"],restored["width"],restored["height"]),
                (original["x"],original["y"],original["width"],original["height"]))
            self.assertEqual(result["focused"]["id"],origin["id"])
            pos=a.call("ui.pointer.position");self.assertLessEqual(abs(pos["x"]-start["x"]),2);self.assertLessEqual(abs(pos["y"]-start["y"]),2)
            state=self._wait_test_state(lambda s:s["active"] and s["real_cursor_visible"] and not s["cursor_engaged"])
            again=a.call("ui.session.wrapup",{"keep":[second["id"]]})
            self.assertEqual(again["closed"],[]);self.assertEqual(again["restored"],[])
            # Exercise close_new=false on a new window while preserving the wrapup session.
            third=a.call("ui.app.launch",{"app":"pcpilot-wrapup.desktop","wait_ms":8000})["window"]
            no_close=a.call("ui.session.wrapup",{"close_new":False})
            left=next(w for w in no_close["left_open"] if w["id"]==third["id"])
            self.assertEqual(left["reason"],"close_new=false")
        finally:a.close()

    def test_v2_external_authorship_and_calculator_journal_restore(self):
        a=self._agent();self.addCleanup(self._end_client,a)
        # Focus Calculator and put the pointer inside it before the tested snapshot.
        a.call("session.begin",{"label":"focus baseline"})
        calc=a.call("ui.app.launch",{"app":"org.gnome.Calculator.desktop","wait_ms":8000})["window"]
        a.call("ui.window.action",{"id":calc["id"],"action":"activate","style":"instant"})
        a.call("ui.pointer.move",{"x":calc["x"]+calc["width"]//2,"y":calc["y"]+calc["height"]//2,"style":"instant"})
        a.call("session.end",{"reason":"agent_request"})
        a.call("session.begin",{"label":"authorship and journal"})
        calc=next(w for w in a.call("ui.window.list")["windows"] if w["id"]==calc["id"])
        original=(calc["x"],calc["y"],calc["width"],calc["height"])
        # A different process born while a human motion request is active has no
        # authorship claim, even though the request is still in flight.
        during_log=self.dir/"external-during.jsonl";during_log.touch();during_log.chmod(0o666)
        move_started=threading.Event()
        def long_move():
            mover=self._agent()
            try:
                move_started.set()
                mover.call("ui.pointer.move",{"x":1700,"y":900,"style":"human"},timeout=10)
            finally:mover.close()
        mover=threading.Thread(target=long_move,daemon=True);mover.start();self.assertTrue(move_started.wait(2))
        time.sleep(.1)
        subprocess.run([str(ROOT/"dev/headless-shell.sh"),"run",str(self.dir),"--","sh","-lc",
            f"env PROBE_LOG={during_log} PROBE_TITLE=ExternalDuring /usr/bin/python3 {SUPPORT / 'gtk_probe.py'} >/dev/null 2>&1 &"],
            cwd=ROOT,check=True,timeout=15)
        deadline=time.monotonic()+8;external_during=None
        while time.monotonic()<deadline:
            external_during=next((w for w in a.call("ui.window.list")["windows"] if w["title"]=="ExternalDuring"),None)
            if external_during:break
            time.sleep(.05)
        self.assertIsNotNone(external_during)
        mover.join(timeout=12);self.assertFalse(mover.is_alive(),"pointer move did not settle")
        self.assertFalse(next(w for w in a.call("ui.window.list")["windows"] if w["id"]==external_during["id"])["ai"])
        launch_reply=a.call("ui.app.launch",{"app":"pcpilot-wrapup.desktop","wait_ms":8000})
        launched=launch_reply.get("window")
        if not launched:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                launched=next((w for w in a.call("ui.window.list")["windows"] if w["title"]=="WrapupProbe" and w["new"]),None)
                if launched:break
                time.sleep(.05)
        self.assertIsNotNone(launched,launch_reply)
        tracked=next(w for w in a.call("ui.window.list")["windows"] if w["id"]==launched["id"])
        self.assertTrue(tracked["ai"])
        time.sleep(3.5)  # External windows remain un-authored regardless of when they appear.
        extlog=self.dir/"external.jsonl";extlog.touch();extlog.chmod(0o666)
        subprocess.run([str(ROOT/"dev/headless-shell.sh"),"run",str(self.dir),"--","sh","-lc",
            f"env PROBE_LOG={extlog} PROBE_TITLE=External /usr/bin/python3 {SUPPORT / 'gtk_probe.py'} >/dev/null 2>&1 &"],
            cwd=ROOT,check=True,timeout=15)
        deadline=time.monotonic()+8;external=None
        while time.monotonic()<deadline:
            external=next((w for w in a.call("ui.window.list")["windows"] if w["title"]=="External"),None)
            if external:break
            time.sleep(.05)
        self.assertIsNotNone(external)
        self.assertTrue(external["new"]);self.assertFalse(external["ai"])
        a.call("ui.window.action",{"id":calc["id"],"action":"move","x":40,"y":50,"style":"fast"})
        # A second unrelated app is started just before wrapup as well.
        right_before_log=self.dir/"external-before-wrapup.jsonl";right_before_log.touch();right_before_log.chmod(0o666)
        subprocess.run([str(ROOT/"dev/headless-shell.sh"),"run",str(self.dir),"--","sh","-lc",
            f"env PROBE_LOG={right_before_log} PROBE_TITLE=ExternalBeforeWrapup /usr/bin/python3 {SUPPORT / 'gtk_probe.py'} >/dev/null 2>&1 &"],
            cwd=ROOT,check=True,timeout=15)
        deadline=time.monotonic()+8;external_before=None
        while time.monotonic()<deadline:
            external_before=next((w for w in a.call("ui.window.list")["windows"] if w["title"]=="ExternalBeforeWrapup"),None)
            if external_before:break
            time.sleep(.05)
        self.assertIsNotNone(external_before)
        result=a.call("ui.session.wrapup")
        self.assertIn(launched["id"],[w["id"] for w in result["closed"]])
        self.assertEqual(next(w for w in result["left_open"] if w["id"]==external["id"])["reason"],"not_authored")
        self.assertEqual(next(w for w in result["left_open"] if w["id"]==external_during["id"])["reason"],"not_authored")
        self.assertEqual(next(w for w in result["left_open"] if w["id"]==external_before["id"])["reason"],"not_authored")
        self.assertIn(calc["id"],result["restored"]);self.assertEqual(result["focused"]["id"],calc["id"])
        after=next(w for w in a.call("ui.window.list")["windows"] if w["id"]==calc["id"])
        self.assertEqual((after["x"],after["y"],after["width"],after["height"]),original)
        self.assertTrue(any(w["id"]==external["id"] and not w["ai"] for w in a.call("ui.window.list")["windows"]))

    def test_v2_dbus_text_editor_windows_survive_wrapup(self):
        a=self._agent();self.addCleanup(self._end_client,a)
        a.call("session.begin",{"label":"spawned app family"})
        existing=[w for w in a.call("ui.window.list")["windows"] if w.get("app_id")=="org.gnome.TextEditor.desktop"]
        self.assertFalse(existing,"Text Editor must not already be running in the isolated shell")
        first=a.call("ui.app.launch",{"app":"org.gnome.TextEditor.desktop","wait_ms":10000})["window"]
        self.assertTrue(first.get("focused"),first)
        a.call("ui.window.action",{"id":first["id"],"action":"activate","style":"instant"})
        a.call("ui.key.press",{"keys":"ctrl+n"})
        deadline=time.monotonic()+8;second=None
        while time.monotonic()<deadline:
            matches=[w for w in a.call("ui.window.list")["windows"]
                     if w.get("app_id")=="org.gnome.TextEditor.desktop" and w["id"]!=first["id"]]
            if matches:second=matches[0];break
            time.sleep(.05)
        self.assertIsNotNone(second,"Ctrl+N did not create a second Text Editor window")
        by_id={w["id"]:w for w in a.call("ui.window.list")["windows"]}
        self.assertFalse(by_id[first["id"]]["ai"]);self.assertFalse(by_id[second["id"]]["ai"])
        result=a.call("ui.session.wrapup")
        left={w["id"]:w["reason"] for w in result["left_open"]}
        self.assertEqual(left[first["id"]],"not_authored");self.assertEqual(left[second["id"]],"not_authored")
        remaining={w["id"] for w in a.call("ui.window.list")["windows"]}
        self.assertIn(first["id"],remaining);self.assertIn(second["id"],remaining)

    def test_v2_keep_dialog_family_and_confirm_close_is_idempotent(self):
        a=self._agent();self.addCleanup(self._end_client,a)
        a.call("session.begin",{"label":"transient families"})
        family=a.call("ui.app.launch",{"app":"pcpilot-family.desktop","wait_ms":8000})["window"]
        deadline=time.monotonic()+5;dialog=None
        while time.monotonic()<deadline:
            dialog=next((w for w in a.call("ui.window.list")["windows"] if w["title"]=="Family Dialog"),None)
            if dialog:break
            time.sleep(.05)
        self.assertIsNotNone(dialog)
        self.assertTrue(any(r.get("ev")=="dialog" for r in self._read_jsonl(self.extra_logs["pcpilot-family"])))
        result=a.call("ui.session.wrapup",{"keep":[dialog["id"]]})
        self.assertEqual(next(w for w in result["left_open"] if w["id"]==dialog["id"])["reason"],"kept")
        self.assertEqual(next(w for w in result["left_open"] if w["id"]==family["id"])["reason"],"family_protected")
        remaining={w["id"] for w in a.call("ui.window.list")["windows"]}
        self.assertIn(family["id"],remaining);self.assertIn(dialog["id"],remaining)

        # A separate session avoids the family's still-open authored windows changing
        # what the refusing-close mode reports.
        a.call("session.end",{"reason":"agent_request"});a.call("session.begin",{"label":"refusing close"})
        confirm=a.call("ui.app.launch",{"app":"pcpilot-confirm.desktop","wait_ms":8000})["window"]
        result=a.call("ui.session.wrapup")
        log=self._read_jsonl(self.extra_logs["pcpilot-confirm"])
        deadline=time.monotonic()+4;dialogs=[]
        while time.monotonic()<deadline:
            dialogs=[w for w in a.call("ui.window.list")["windows"] if w["title"]=="Confirm Save?"]
            if dialogs:break
            time.sleep(.05)
        self.assertTrue(dialogs)
        self.assertEqual(next(w for w in result["left_open"] if w["id"]==confirm["id"])["reason"],"not_closed")
        close_count=sum(r.get("ev")=="close_request" for r in log);self.assertEqual(close_count,1)
        dialog_close_count=sum(r.get("ev")=="dialog_close" for r in log)
        again=a.call("ui.session.wrapup")
        log2=self._read_jsonl(self.extra_logs["pcpilot-confirm"])
        self.assertIn(dialogs[0]["id"],[w["id"] for w in again["left_open"]],
            f"late dialog should be family-protected; dialog={dialogs[0]}, left_open={again['left_open']}, windows={a.call('ui.window.list')['windows']}")
        self.assertEqual(sum(r.get("ev")=="close_request" for r in log2),close_count)
        self.assertEqual(sum(r.get("ev")=="dialog_close" for r in log2),dialog_close_count)
        self.assertFalse(again["closed"])

    @staticmethod
    def _read_jsonl(path):
        out=[]
        for line in path.read_text(errors="replace").splitlines():
            try:out.append(__import__("json").loads(line))
            except ValueError:pass
        return out

    def test_cancellation_ends_long_key_type(self):
        from pcpilot.protocol import ProtocolError
        a = self._begin(); b = self._agent()
        self.addCleanup(self._end_client, b)
        self.addCleanup(self._end_client, a)
        layout = next(r for r in reversed(self._wait_probe(lambda rs: any(x.get("ev")=="layout" for x in rs))) if r.get("ev")=="layout")
        win = next(w for w in a.call("ui.window.list")["windows"] if w["title"]==layout["title"])
        entry = layout["widgets"]["entry"]
        ex, ey = win["x"]+entry[0]+30, win["y"]+entry[1]+10
        a.call("ui.window.action", {"id":win["id"],"action":"activate"})
        a.call("ui.pointer.click", {"x":ex,"y":ey})
        a.call("ui.key.press", {"keys":"ctrl+a"}); a.call("ui.key.press", {"keys":"BackSpace"})
        outcome = {}
        def long_type():
            try: outcome["result"] = a.call("ui.key.type", {"text":"x"*200,"delay_ms":50}, timeout=8)
            except Exception as e: outcome["error"] = e
        worker = threading.Thread(target=long_type, daemon=True)
        worker.start(); time.sleep(1)
        started = time.monotonic()
        b.call("session.end", {"reason":"user_hotkey"})
        worker.join(2)
        self.assertFalse(worker.is_alive(), "long key.type did not cancel promptly")
        self.assertIsInstance(outcome.get("error"), ProtocolError)
        self.assertEqual(outcome["error"].code, "inactive")
        self.assertLess(time.monotonic()-started, 2)
        rows = self._rows()
        entries = [r.get("text","") for r in rows if r.get("ev")=="entry"]
        self.assertLess(max((len(s) for s in entries), default=0), 100)
        with self.assertRaises(ProtocolError) as cm:
            a.call("ui.screen.info")
        self.assertIn(cm.exception.code, ("no_session", "inactive"))
        b.call("session.begin", {"label":"Test resumed"})

    def test_cross_session_queued_click_is_stale_and_type_duration_bounded(self):
        from pcpilot.protocol import ProtocolError
        a = self._begin(); c = self._agent(); b = self._agent()
        self.addCleanup(self._end_client, b); self.addCleanup(self._end_client, c); self.addCleanup(self._end_client, a)
        layout = next(r for r in reversed(self._wait_probe(lambda rs: any(x.get("ev")=="layout" for x in rs))) if r.get("ev")=="layout")
        w = next(x for x in a.call("ui.window.list")["windows"] if x["title"]==layout["title"])
        area=layout["widgets"]["area"]; x,y=w["x"]+area[0]+40,w["y"]+area[1]+40
        prior_clicks=sum(1 for r in self._rows() if r.get("ev")=="area_click")
        outcome={}
        def call_a():
            try: a.call("ui.key.type", {"text":"x"*100,"delay_ms":50}, timeout=10)
            except Exception as e: outcome["a"]=e
        def call_c():
            try: c.call("ui.pointer.click", {"x":x,"y":y}, timeout=10)
            except Exception as e: outcome["c"]=e
        ta=threading.Thread(target=call_a,daemon=True); ta.start(); time.sleep(.15)
        tc=threading.Thread(target=call_c,daemon=True); tc.start(); time.sleep(.15)
        b.call("fake.state", {"active":False,"reason":"user_hotkey"})
        b.call("fake.state", {"active":True,"label":"new generation"})
        ta.join(3); tc.join(3)
        self.assertFalse(ta.is_alive()); self.assertFalse(tc.is_alive())
        self.assertEqual(getattr(outcome.get("a"),"code",None),"inactive")
        self.assertEqual(getattr(outcome.get("c"),"code",None),"inactive")
        self.assertEqual(sum(1 for r in self._rows() if r.get("ev")=="area_click"),prior_clicks)
        with self.assertRaises(ProtocolError) as cm:
            b.call("ui.key.type", {"text":"y"*20,"delay_ms":1000})
        self.assertEqual(cm.exception.code,"bad_args"); self.assertIn("take",str(cm.exception))

    def test_health_pongs_are_not_blocked_by_long_operation(self):
        from pcpilot.protocol import ProtocolError
        a=self._begin(); monitor=self._agent(); self.addCleanup(self._end_client,a); self.addCleanup(monitor.close)
        layout=next(r for r in reversed(self._wait_probe(lambda rs:any(x.get("ev")=="layout" for x in rs))) if r.get("ev")=="layout")
        w=next(x for x in a.call("ui.window.list")["windows"] if x["title"]==layout["title"])
        entry=layout["widgets"]["entry"]; a.call("ui.window.action",{"id":w["id"],"action":"activate"})
        a.call("ui.pointer.click",{"x":w["x"]+entry[0]+30,"y":w["y"]+entry[1]+10})
        outcome={}
        def type_long():
            try: outcome["result"]=a.call("ui.key.type",{"text":"x"*100,"delay_ms":100},timeout=15)
            except Exception as e: outcome["error"]=e
        worker=threading.Thread(target=type_long,daemon=True); worker.start()
        session=monitor.call("fake.lease")["session"]
        deadline=time.monotonic()+11
        while worker.is_alive() and time.monotonic()<deadline:
            lease=monitor.call("fake.lease")
            self.assertLess(lease["since_pong_s"],3,lease)
            events=monitor.call("fake.events")["events"]
            self.assertTrue(any(e.get("event")=="pong" and e.get("session")==session for e in events),events[-8:])
            time.sleep(.5)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error",outcome,str(outcome))

    def test_z_daemon_loss_and_reconnect(self):
        a = self._begin(); self.addCleanup(self._end_client, a)
        before = self._raw("before-daemon-loss.png")
        self._stop_daemon()
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            lost = self._raw("daemon-loss.png")
            p = lost.getpixel((1,540))
            if not all(abs(x-y)<=10 for x,y in zip(p,(255,69,58))): break
            time.sleep(.1)
        self.assertFalse(all(abs(x-y)<=10 for x,y in zip(lost.getpixel((1,540)),(255,69,58))))
        self.assertNotEqual(lost.crop((700,0,1220,130)).tobytes(), before.crop((700,0,1220,130)).tobytes())
        self._start_daemon()
        waiter = self._agent()
        try: waiter.call("fake.wait_ui", {"timeout":8}, timeout=10)
        finally: waiter.close()
        a2 = self._agent()
        try: self.assertTrue(a2.call("session.begin", {"label":"Reconnect"})["active"])
        finally: a2.close()

    def test_z_app_and_window_actions(self):
        a = self._begin(); from pcpilot.protocol import ProtocolError
        self.addCleanup(self._end_client, a)
        matches = a.call("ui.app.search", {"query":"calc"})["apps"]
        self.assertTrue(any(x["id"] == "org.gnome.Calculator.desktop" for x in matches))
        launched = a.call("ui.app.launch", {"app":"org.gnome.Calculator.desktop", "wait_ms":8000})
        self.assertIn("window", launched)
        self.assertTrue(launched["window"]["focused"])
        wid = launched["window"]["id"]
        a.call("ui.window.action", {"id":wid,"action":"fullscreen"})
        full = next(w for w in a.call("ui.window.list")["windows"] if w["id"]==wid)
        self.assertEqual((full["x"],full["y"],full["width"],full["height"]),(0,0,1920,1080))
        shot = self._shot(overlay=True)
        with Image.open(shot["path"]) as opened: edge = opened.convert("RGB").getpixel((1,540))
        self.assertTrue(all(abs(x-y)<=10 for x,y in zip(edge,(255,69,58))), edge)
        a.call("ui.window.action", {"id":wid,"action":"maximize"})
        w = next(w for w in a.call("ui.window.list")["windows"] if w["id"]==wid); self.assertEqual(w["width"],1920)
        for action in ("unmaximize", "minimize", "activate", "close"):
            a.call("ui.window.action", {"id":wid,"action":action})
        with self.assertRaises(ProtocolError) as cm: a.call("ui.window.action", {"id":999999,"action":"activate"})
        self.assertEqual(cm.exception.code,"not_found")
        with self.assertRaises(ProtocolError) as cm: a.call("ui.app.launch", {"app":"org.no.such.App.desktop"})
        self.assertEqual(cm.exception.code,"not_found")


@unittest.skipUnless(available(), "requires root, gnome-shell, and uid 1000 sacer")
class FreshHotCornerRegression(unittest.TestCase):
    """A separate compositor guarantees the initial pointer is at the hot corner."""
    @classmethod
    def _wait_socket(cls):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if cls.socket.exists():
                import socket
                probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                try: probe.settimeout(.1); probe.connect(str(cls.socket)); return
                except OSError: pass
                finally: probe.close()
            if cls.daemon.poll() is not None: raise AssertionError("fake daemon exited")
            time.sleep(.05)
        raise AssertionError("fake socket unavailable")

    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix="pcpilot-ext-fresh-")
        cls.dir=Path(cls.tmp.name); cls.socket=cls.dir/"pcpilot.sock"
        cls.desktop=cls.dir/"data/applications/pcpilot-fresh.desktop"
        cls.desktop.parent.mkdir(parents=True)
        cls.log=cls.dir/"fresh-probe.jsonl"; cls.log.touch(); cls.log.chmod(0o666)
        cls.desktop.write_text('[Desktop Entry]\nType=Application\nName=PCPilot Fresh Probe\n'
            f'Exec=/usr/bin/env PROBE_LOG={cls.log} /usr/bin/python3 {SUPPORT / "gtk_probe.py"}\n'
            'Terminal=false\n')
        cls.shell_log=open(cls.dir/"shell.log","w+"); cls.started=False
        try:
            subprocess.run([str(ROOT/"dev/headless-shell.sh"),"start",str(cls.dir)],cwd=ROOT,
                check=True,stdout=cls.shell_log,stderr=subprocess.STDOUT,timeout=60)
            cls.started=True; ExtensionHeadlessTests._start_daemon.__func__(cls)
            a=ExtensionHeadlessTests._agent.__func__(cls)
            try: a.call("fake.wait_ui",{"timeout":30},timeout=35)
            finally: a.close()
        except Exception:
            cls._cleanup(); raise

    @classmethod
    def _cleanup(cls):
        if getattr(cls,"daemon",None):
            cls.daemon.terminate()
            try: cls.daemon.wait(timeout=3)
            except subprocess.TimeoutExpired: cls.daemon.kill(); cls.daemon.wait()
            if cls.daemon.stdout: cls.daemon.stdout.close()
        if getattr(cls,"started",False):
            subprocess.run([str(ROOT/"dev/headless-shell.sh"),"stop",str(cls.dir)],cwd=ROOT,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
        if getattr(cls,"shell_log",None): cls.shell_log.close()
        if getattr(cls,"tmp",None): cls.tmp.cleanup()

    @classmethod
    def tearDownClass(cls): cls._cleanup()

    def test_private_desktop_first_click_unicode_im_and_return(self):
        from tests.ui_fake_daemon import AgentClient
        a=AgentClient(str(self.socket),timeout=30); self.addCleanup(a.close)
        a.call("session.begin",{"label":"Fresh test"})
        started=a.call("ui.app.launch",{"app":"pcpilot-fresh.desktop","wait_ms":8000})
        self.assertIn("window",started)
        self.assertNotEqual(started["window"]["app_id"],"pcpilot-fresh.desktop")
        end=time.monotonic()+10; layout=None
        while time.monotonic()<end:
            for line in self.log.read_text(errors="replace").splitlines():
                try:
                    row=__import__("json").loads(line)
                    if row.get("ev")=="layout": layout=row
                except ValueError: pass
            if layout: break
            time.sleep(.05)
        self.assertIsNotNone(layout)
        win=next(w for w in a.call("ui.window.list")["windows"] if w["title"]==layout["title"])
        b=layout["widgets"]["entry"]; x=win["x"]+b[0]+30; y=win["y"]+b[1]+10
        a.call("ui.pointer.click",{"x":x,"y":y}) # intentionally first pointer action
        result=a.call("ui.key.type",{"text":"ação ç ã é"})
        self.assertEqual(result["method"],"im")
        a.call("ui.key.press",{"keys":"Return"})
        end=time.monotonic()+5; entry=None
        while time.monotonic()<end:
            rows=[]
            for line in self.log.read_text(errors="replace").splitlines():
                try: rows.append(__import__("json").loads(line))
                except ValueError: pass
            entries=[r for r in rows if r.get("ev")=="entry"]
            if entries and entries[-1].get("text")=="ação ç ã é": entry=entries[-1]; break
            time.sleep(.05)
        self.assertIsNotNone(entry, "first-click focus/type failed")
        self.assertTrue(any(r.get("ev")=="key" and r.get("name")=="Return" for r in rows))


if __name__ == "__main__": unittest.main()
