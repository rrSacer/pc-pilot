"""Real daemon + extension + MCP, confined to fake input and a private GNOME.

Run: sudo python3 -m unittest -v tests.test_e2e
No real input node or user session is opened. FakeDevice's runtime udev rule
is deliberately retained, as required by its safety harness.
"""
import base64
import io
import json
import os
import pwd
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image
from pcpilot import evdev
from tests.daemon_testlib import AgentClient, DaemonProcess, FakeDevice, wait_until
from tests.temp_paths import socket_temp_base
from tests.test_ext_support.theme_probe import is_frame_pixel

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "dev/headless-shell.sh"
SUPPORT = ROOT / "tests/test_ext_support"


def available():
    return (os.geteuid() == 0 and shutil.which("gnome-shell") is not None
            and any(p.pw_uid == 1000 and p.pw_name == "sacer" for p in pwd.getpwall()))


class MCPClient:
    """Actual newline-delimited stdio transport; retain exact wire byte counts."""
    def __init__(self, env, log):
        self.proc_env = env
        self.proc = subprocess.Popen([sys.executable, "-m", "pcpilot.mcp_server"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, start_new_session=True)
        self.messages = queue.Queue()
        self.ident = 0
        self.sizes = {}
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            self.messages.put(line)
        self.messages.put(None)

    def rpc(self, method, params=None):
        self.ident += 1
        request = {"jsonrpc": "2.0", "id": self.ident, "method": method,
                   "params": params or {}}
        self.proc.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
        self.proc.stdin.flush()
        deadline = time.monotonic() + 40
        while True:
            raw = self.messages.get(timeout=max(.01, deadline-time.monotonic()))
            if raw is None:
                raise AssertionError(f"MCP exited: {self.proc.poll()}")
            reply = json.loads(raw)
            if reply.get("id") == self.ident:
                if "error" in reply:
                    raise AssertionError(reply)
                return reply["result"], len(raw)

    def initialize(self):
        result, _ = self.rpc("initialize", {"protocolVersion": "2025-06-18",
            "capabilities": {}, "clientInfo": {"name": "pcpilot-e2e", "version": "1"}})
        assert result["serverInfo"]["name"] == "pcpilot", result
        self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.proc.stdin.flush()
        tools, size = self.rpc("tools/list")
        assert [t["name"] for t in tools["tools"]] == ["control", "screenshot", "act", "elements", "windows"], tools
        assert size <= 3400, f"tools/list response exceeds wire budget: {size} bytes"
        self.sizes["tools/list"] = size

    def call(self, name, args=None, error=False):
        result, size = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        if not result.get("isError"):
            self.sizes[name + (" zoom" if name == "screenshot" and args else "")] = size
        if not error and result.get("isError"):
            raise AssertionError(f"{name}: {result}")
        return result

    def kill(self):
        self.proc.kill()
        self.proc.wait(timeout=5)

    def close(self):
        # A SIGKILL test leaves the AT-SPI child alive; kill our private group too.
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)
        self.proc.stdin.close()
        self.reader.join(timeout=2)
        self.proc.stdout.close()


@unittest.skipUnless(available(), "requires root, gnome-shell and uid 1000 sacer")
class RealStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="pcp-e-", dir=socket_temp_base())
        cls.dir = Path(cls.tmp.name)
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.reports = {}
        cls.addClassCleanup(lambda: print("\nToken report (JSON-RPC response bytes, including newline; not token estimates): "
                                         + json.dumps(cls.reports, sort_keys=True), flush=True))
        cls.keyboard = FakeDevice(tag="e2e-keyboard")
        cls.addClassCleanup(cls.keyboard.destroy)
        cls.pointer = FakeDevice(kind="pointer", tag="e2e-pointer")
        cls.addClassCleanup(cls.pointer.destroy)
        applications = cls.dir / "data/applications"
        applications.mkdir(parents=True)
        cls.probe_log = cls.dir / "probe.jsonl"
        cls.probe_log.touch()
        # A private desktop entry makes launch traverse the real MCP/app system.
        (applications / "pcpilot-e2e-probe.desktop").write_text(
            '[Desktop Entry]\nType=Application\nName=PCPilot E2E Probe\n'
            f'Exec=/usr/bin/env PROBE_LOG={cls.probe_log} /usr/bin/python3 {SUPPORT / "gtk_probe.py"}\n'
            'Terminal=false\nStartupWMClass=pcpilot-e2e-probe\n')
        cls.second_probe_title = "PCPilot-E2E-Probe-Second"
        (applications / "pcpilot-e2e-probe-second.desktop").write_text(
            '[Desktop Entry]\nType=Application\nName=PCPilot E2E Probe Second\n'
            f'Exec=/usr/bin/env PROBE_LOG={cls.probe_log} PROBE_TITLE={cls.second_probe_title} '
            f'/usr/bin/python3 {SUPPORT / "gtk_probe.py"}\n'
            'Terminal=false\nStartupWMClass=pcpilot-e2e-probe-second\n')
        # A private Firefox (own profile, own state dir for the a11y switch): never the user's profile.
        cls.profile = cls.dir / "ffprofile"
        cls.profile.mkdir()
        (cls.profile / "user.js").write_text("".join(
            f'user_pref("{key}", {value});\n' for key, value in {
                "browser.shell.checkDefaultBrowser": "false", "browser.aboutwelcome.enabled": "false",
                "datareporting.policy.dataSubmissionEnabled": "false", "app.update.auto": "false",
                "app.update.enabled": "false", "browser.startup.page": "0",
                "browser.tabs.warnOnClose": "false", "extensions.update.enabled": "false",
                "browser.sessionstore.resume_from_crash": "false",
                "network.captive-portal-service.enabled": "false"}.items()))
        cls.a11y_state = cls.dir / "a11ystate"
        cls.a11y_state.mkdir()
        (applications / "pcpilot-e2e-firefox.desktop").write_text(
            '[Desktop Entry]\nType=Application\nName=PCPilot E2E Firefox\n'
            f'Exec=/usr/bin/env MOZ_ENABLE_WAYLAND=1 /usr/bin/firefox --no-remote --new-instance '
            f'--profile {cls.profile} file://{ROOT / "tests/mcp_support/form.html"}\n'
            'Terminal=false\nStartupWMClass=firefox\n')
        cls.wrapper_log = open(cls.dir / "wrapper.log", "w+")
        cls.addClassCleanup(cls.wrapper_log.close)
        # Register stop BEFORE start: startup failure may leave partial processes.
        cls.addClassCleanup(cls.stop_shell)
        shell_env = {k: v for k, v in os.environ.items() if not k.startswith("PCPILOT_")}
        shell_env.update(PCPILOT_HEADLESS_SIZE="1920x1080", PCPILOT_TEST_USER="sacer")
        subprocess.run([str(SHELL), "start", str(cls.dir)], cwd=ROOT, env=shell_env,
            stdout=cls.wrapper_log, stderr=subprocess.STDOUT, check=True, timeout=60)
        session = subprocess.check_output([str(SHELL), "env", str(cls.dir)], text=True)
        cls.env = dict(line.split("=", 1) for line in session.splitlines() if line)
        assert cls.env["XDG_RUNTIME_DIR"] == str(cls.dir / "runtime")
        assert cls.env["PCPILOT_SOCKET"] == str(cls.dir / "pcpilot.sock")
        assert cls.env["PCPILOT_WAYLAND_SOCKET"] == str(cls.dir / "runtime/wayland-pcpilot")
        cls.daemon = DaemonProcess(workdir=str(cls.dir), desktop_user="sacer",
            ui_wayland_socket=cls.env["PCPILOT_WAYLAND_SOCKET"],
            idle_timeout_s=90, max_session_s=300, ui_timeout_s=20, ui_ack_timeout_s=3)
        cls.addClassCleanup(cls.daemon.cleanup)
        cls.daemon.start()
        cls.observer = AgentClient(cls.daemon.socket, client="e2e-state-observer")
        cls.addClassCleanup(cls.observer.close)
        assert wait_until(lambda: cls.status()["ui_connected"], timeout=20), cls.daemon.log()

    @classmethod
    def stop_shell(cls):
        subprocess.run([str(SHELL), "stop", str(cls.dir)], cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)

    @classmethod
    def status(cls):
        reply = cls.observer.call("session.status")
        assert reply and reply.get("ok"), reply
        return reply["result"]

    def setUp(self):
        log = open(self.dir / f"{self._testMethodName}.mcp.log", "w+")
        self.addCleanup(log.close)
        env = dict(self.env, PCPILOT_DESKTOP_USER="sacer", PCPILOT_MAX_SIDE="1280",
            PCPILOT_A11Y_RUNTIME_DIR=self.env["XDG_RUNTIME_DIR"],
            PCPILOT_A11Y_STATE_DIR=str(self.a11y_state),
            PCPILOT_A11Y_BUS_ADDRESS=self.env["DBUS_SESSION_BUS_ADDRESS"])
        self.mcp = MCPClient(env, log)
        self.addCleanup(self.mcp.close)
        self.addCleanup(lambda: self.reports.update(self.mcp.sizes))
        self.mcp.initialize()
        self.addCleanup(self.finish_session)

    def finish_session(self):
        # This observer never owns the session; release is for cleanup on failure.
        self.observer.call("session.release")
        self.assertTrue(wait_until(lambda: not self.status()["active"]))
        self.daemon.assert_only_test_devices_open()

    def rows(self):
        rows = []
        for line in self.probe_log.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:  # writer may have a partial last line
                pass
        return rows

    def wait_rows(self, predicate):
        self.assertTrue(wait_until(lambda: predicate(self.rows()), timeout=10), self.rows()[-15:])

    def overlay(self):
        path = self.dir / "raw.png"
        subprocess.run([str(SHELL), "run", str(self.dir), "--", "python3",
            str(SUPPORT / "raw_screenshot.py"), str(path)], cwd=ROOT,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        with Image.open(path) as image:
            pixel = image.convert("RGB").getpixel((1, 540))
        # The frame's 4 px line is themed (the MCP picks the theme from its client's name).
        return is_frame_pixel(pixel)

    def assert_active(self):
        self.assertTrue(self.status()["active"])
        self.daemon.assert_only_test_devices_open()
        for device in (self.keyboard, self.pointer):
            self.assertTrue(device.grabbed_by_someone())
            reader = device.open_reader()
            try:
                reader.read_events()
                if device.kind == "keyboard":
                    device.tap(evdev.KEY_A)
                else:
                    device.move(1, 1)
                time.sleep(.1)
                self.assertEqual(reader.read_events(), [], "grab must exclude a second reader")
            finally:
                reader.close()
        self.assertTrue(self.overlay(), "active overlay border missing")

    def assert_inactive(self):
        self.assertTrue(wait_until(lambda: not self.status()["active"]))
        for device in (self.keyboard, self.pointer):
            self.assertTrue(wait_until(lambda: not device.grabbed_by_someone()))
        self.assertTrue(wait_until(lambda: not self.overlay(), timeout=5), "overlay still visible")

    def test_01_launch_elements_unicode_and_screenshots(self):
        self.mcp.call("act", {"actions": [{"launch": "pcpilot-e2e-probe.desktop"}], "observe": "none"})
        self.wait_rows(lambda rows: any(r.get("ev") == "layout" for r in rows))
        windows = self.mcp.call("windows")["content"][0]["text"]
        wid = int(next(line.split()[0] for line in windows.splitlines() if '"PCPilot Ext Probe"' in line))
        self.mcp.call("act", {"actions": [{"focus": wid}], "observe": "none"})
        focused = self.mcp.call("windows")["content"][0]["text"]
        self.assertTrue(any(line.startswith(str(wid) + " ") and line.endswith("[focused]")
                            for line in focused.splitlines()), focused)
        self.assert_active()
        elements = self.mcp.call("elements")["content"][0]["text"]
        # The first single-line text/entry is the probe's empty Gtk.Entry.
        matches = re.findall(r'^(e\d+) (?:entry|text) "" \d+,\d+', elements, re.M)
        self.assertTrue(matches, elements)
        start = len(self.rows())
        result = self.mcp.call("act", {"actions": [{"click": matches[0]},
            {"type": "ação ç ã é"}, {"key": "Return"}], "observe": "none"})
        self.assertEqual(result["content"], [{"type": "text", "text": "ok 3"}])
        self.wait_rows(lambda rows: any(r.get("ev") == "key" and r.get("name") == "Return" for r in rows[start:]))
        events = self.rows()[start:]
        entries = [r["text"] for r in events if r.get("ev") == "entry"]
        self.assertTrue(entries, events)
        self.assertEqual(entries[-1], "ação ç ã é")
        self.assertTrue(all("ação ç ã é".startswith(value) for value in entries), entries)
        self.assertEqual(sum(r.get("ev") == "key" and r.get("name") == "Return" for r in events), 1)
        self.assertFalse(any(r.get("ev") in ("textview", "click", "area_click") for r in events), events)
        for args, size in (({}, (1280, 720)), ({"region": [100, 100, 300, 200]}, (450, 300))):
            shot = self.mcp.call("screenshot", args)
            with Image.open(io.BytesIO(base64.b64decode(shot["content"][0]["data"]))) as image:
                self.assertEqual(image.size, size)
            self.assertIn(f"{size[0]}x{size[1]}", shot["content"][1]["text"])
            if args:
                self.assertIn("zoom region=", shot["content"][1]["text"])
        self.mcp.call("control", {"action": "stop"})
        self.assert_inactive()

    def test_02_hotkey_resume_stop(self):
        self.mcp.call("control", {"action": "start"})
        self.assert_active()
        self.keyboard.hotkey()
        self.assert_inactive()
        result = self.mcp.call("screenshot", error=True)
        self.assertTrue(result.get("isError"), result)
        self.assertIn("USER_TOOK_CONTROL", result["content"][0]["text"])
        self.mcp.call("control", {"action": "start", "resume": True})
        self.assert_active()
        self.mcp.call("control", {"action": "stop"})
        self.assert_inactive()

    def test_03_mcp_killed_mid_session(self):
        self.mcp.call("control", {"action": "start"})
        self.assert_active()
        mark = len(self.daemon.log())
        self.mcp.kill()
        self.assert_inactive()
        self.assertTrue(wait_until(lambda: "agent_disconnected" in self.daemon.log()[mark:]), self.daemon.log())

    def test_04_v2_window_speed_scroll_and_stop_cleanup(self):
        self.mcp.call("act", {"actions": [{"launch": "pcpilot-e2e-probe.desktop"}],
                              "observe": "none", "speed": "fast"})
        first_text = self.mcp.call("windows")["content"][0]["text"]
        first = next(line for line in first_text.splitlines() if '"PCPilot Ext Probe"' in line)
        first_id = int(first.split()[0])
        self.assertEqual(first.split()[1], "*")

        self.wait_rows(lambda rows: any(r.get("ev") == "layout" and
                                        r.get("title") == "PCPilot Ext Probe" for r in rows))
        layout = next(r for r in reversed(self.rows()) if r.get("ev") == "layout" and
                      r.get("title") == "PCPilot Ext Probe")
        match = re.search(rf"^{first_id} \* \S+ \"PCPilot Ext Probe\" (-?\d+),(-?\d+),\d+,\d+",
                          first, re.M)
        self.assertIsNotNone(match, first)
        win_x, win_y = (int(value) for value in match.groups())
        area_x, area_y, area_w, area_h = layout["widgets"]["area"]
        scale = 1280 / 1920
        point = [round(win_x + (area_x + area_w / 2) * scale),
                 round(win_y + (area_y + area_h / 2) * scale)]

        click_start = len(self.rows())
        self.mcp.call("act", {"actions": [{"click": point}], "observe": "none", "speed": "fast"})
        self.wait_rows(lambda rows: any(r.get("ev") == "area_click" for r in rows[click_start:]))
        click = next(r for r in self.rows()[click_start:] if r.get("ev") == "area_click")
        geometry = {"click": click, "area": layout["widgets"]["area"], "window": first, "point": point}
        self.assertEqual(click["button"], 1, click)
        # Gesture coordinates are local to the DrawingArea, not the top-level
        # window; the area offsets above were used to calculate the screen point.
        self.assertGreaterEqual(click["x"], 0, geometry)
        self.assertLess(click["x"], area_w, geometry)
        self.assertGreaterEqual(click["y"], 0, geometry)
        self.assertLess(click["y"], area_h, geometry)

        scroll_start = len(self.rows())
        self.mcp.call("act", {"actions": [{"scroll": [point[0], point[1], 3]}],
                              "observe": "none", "speed": "fast"})
        self.wait_rows(lambda rows: any(r.get("ev") == "scroll" for r in rows[scroll_start:]))
        scroll = next(r for r in self.rows()[scroll_start:] if r.get("ev") == "scroll")
        self.assertNotEqual(scroll["dy"], 0, scroll)

        self.mcp.call("act", {"actions": [
            {"window": [first_id, "resize", 720, 480]},
            {"window": [first_id, "move", 80, 60]},
        ], "observe": "none", "speed": "fast"})
        self.mcp.call("act", {"actions": [
            {"window": [first_id, "fit"]}, {"scroll": [3]},
        ], "observe": "none", "speed": "instant"})

        stopped = self.mcp.call("control", {"action": "stop", "keep": [first_id]})
        summary = stopped["content"][0]["text"]
        self.assertIn("left open:", summary, summary)
        self.assertIn("PCPilot Ext Probe", summary, summary)
        self.assertFalse(self.status()["active"])

        # Listing windows starts a fresh owner session. The kept window is old
        # in this session and therefore has no new-window marker.
        fresh_text = self.mcp.call("windows")["content"][0]["text"]
        first_row = next(line for line in fresh_text.splitlines() if line.startswith(f"{first_id} "))
        self.assertNotEqual(first_row.split()[1], "*")
        self.assertTrue(self.status()["active"])

        self.mcp.call("act", {"actions": [{"launch": "pcpilot-e2e-probe-second.desktop"}],
                              "observe": "none", "speed": "fast"})
        both_text = self.mcp.call("windows")["content"][0]["text"]
        second = next(line for line in both_text.splitlines() if f'"{self.second_probe_title}"' in line)
        second_id = int(second.split()[0])
        self.assertEqual(second.split()[1], "*")
        self.assertNotEqual(second_id, first_id)

        stopped = self.mcp.call("control", {"action": "stop"})
        summary = stopped["content"][0]["text"]
        self.assertIn("closed 1", summary, summary)
        self.assertFalse(self.status()["active"])
        remaining = self.mcp.call("windows")["content"][0]["text"]
        self.assertTrue(any(line.startswith(f"{first_id} ") for line in remaining.splitlines()), remaining)
        self.assertFalse(any(line.startswith(f"{second_id} ") for line in remaining.splitlines()), remaining)

        # Finish the preserved probe explicitly, then close the fresh session.
        self.mcp.call("act", {"actions": [{"window": [first_id, "close"]}],
                              "observe": "none", "speed": "instant"})
        self.mcp.call("control", {"action": "stop"})
        self.assert_inactive()


    def a11y_enabled(self):
        """org.a11y.Status IsEnabled on the PRIVATE session bus (what Firefox consults)."""
        out = subprocess.run([str(SHELL), "run", str(self.dir), "--", "gdbus", "call", "--session",
            "--dest", "org.a11y.Bus", "--object-path", "/org/a11y/bus", "--method",
            "org.freedesktop.DBus.Properties.Get", "org.a11y.Status", "IsEnabled"],
            cwd=ROOT, capture_output=True, text=True, timeout=15).stdout
        return "true" in out

    def elements_text(self, args=None):
        return self.mcp.call("elements", args or {})["content"][0]["text"]

    @unittest.skipUnless(shutil.which("firefox"), "firefox not installed")
    def test_05_firefox_page_map_live_clicks_scrolling_and_accessibility_restore(self):
        self.assertFalse(self.a11y_enabled(), "baseline must match the user's real setting")
        self.mcp.call("control", {"action": "start"})
        self.assertTrue(wait_until(self.a11y_enabled, timeout=10), "session must switch accessibility on")
        # Default (human) speed: Super, type the app name, Enter. The nested shell starts with the
        # overview already open, so this also covers "do not toggle it shut".
        launched = self.mcp.call("act", {"actions": [{"launch": "PCPilot E2E Firefox"}]})["content"][0]["text"]
        self.assertRegex(launched, r'launched \d+ "')
        last = {}
        def page_ready():
            last["text"] = self.elements_text()
            return '"PCPilot Test Page"' in last["text"].splitlines()[0] and 'entry "Full name"' in last["text"]
        self.assertTrue(wait_until(page_ready, timeout=45, interval=1), last.get("text"))
        listing = last["text"]
        header = listing.splitlines()[0]
        self.assertRegex(header, r'^Firefox · "PCPilot Test Page" · file://.*form\.html')
        for expected in ('checkbox "Accept the terms"', 'checkbox "Send me news"', 'radio "Free"', 'combo "Country"',
                         'button "Send form"', 'button "Inner button"', 'link "Item 1" ↓', "# PCPilot Test Page"):
            self.assertIn(expected, listing)
        self.assertRegex(listing, r'combo "Country" \d+,\d+ ="Brazil"')
        self.assertRegex(listing, r'checkbox "Send me news" \d+,\d+ \[checked\]')
        self.assertRegex(listing, r'button "Disabled action" \d+,\d+ \[disabled\]')
        self.assertNotIn("?", listing.replace("Search with", ""), "positions must be known")
        ids = {name: ident for ident, name in re.findall(r'(e\d+) \w+ "([^"]+)"', listing)}
        windows = self.mcp.call("windows")["content"][0]["text"]
        self.assertRegex(windows, r'Mozilla Firefox".* \[focused')
        # Fill and submit by id: live positions, no screenshot anywhere.
        out = self.mcp.call("act", {"actions": [
            {"click": ids["Full name"]}, {"type": "Ana"}, {"click": ids["Accept the terms"]},
            {"click": ids["Send form"]}], "observe": "elements", "speed": "fast"})["content"]
        diff = "\n".join(part["text"] for part in out if part["type"] == "text")
        self.assertIn('"Submitted"', diff)                       # document.title changed on submit
        self.assertRegex(diff, r'entry "Full name" \d+,\d+ ="Ana"')
        self.assertRegex(diff, r'checkbox "Accept the terms" \d+,\d+ \[checked\]')
        self.assertNotIn('link "Skip to about"', diff)           # unchanged elements stay out
        # An off-screen link is reached by wheel notches, then clicked where it now is.
        far = self.elements_text({"query": "Item 70"})
        self.assertRegex(far, r'link "Item 70" ↓\d')
        item = re.search(r'(e\d+) link "Item 70"', far).group(1)
        self.mcp.call("act", {"actions": [{"click": item}], "speed": "fast"})
        self.assertTrue(wait_until(lambda: "#i70" in self.elements_text().splitlines()[0], timeout=10), self.elements_text())
        self.assertRegex(self.elements_text({"query": "Item 70"}), r'link "Item 70" \d+,\d+')
        stopped = self.mcp.call("control", {"action": "stop"})["content"][0]["text"]
        self.assertIn("stopped", stopped)
        self.assertTrue(wait_until(lambda: not self.a11y_enabled(), timeout=10), "accessibility must be restored")
        self.assertEqual(json.loads((self.a11y_state / "a11y.json").read_text() or "{}"), {})
        self.assert_inactive()

    @unittest.skipUnless(shutil.which("firefox"), "firefox not installed")
    def test_07_firefox_started_before_the_session_exports_its_page_after_a_new_window(self):
        """The user's real case: Firefox is already open when PCPilot flips accessibility on."""
        profile = self.dir / "ffprofile2"
        profile.mkdir()
        shutil.copy(self.profile / "user.js", profile / "user.js")
        for path in (profile, profile / "user.js"):
            shutil.chown(path, "sacer", "sacer")
        log = open(self.dir / "ff-early.log", "w+")
        self.addCleanup(log.close)
        browser = subprocess.Popen([str(SHELL), "run", str(self.dir), "--", "env", "MOZ_ENABLE_WAYLAND=1",
            "/usr/bin/firefox", "--no-remote", "--new-instance", "--profile", str(profile),
            f"file://{ROOT / 'tests/mcp_support/form.html'}"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.addCleanup(lambda: (os.killpg(browser.pid, signal.SIGTERM), browser.wait(timeout=10)))
        self.assertFalse(self.a11y_enabled())
        def firefox_on_the_bus():
            out = subprocess.run([str(SHELL), "run", str(self.dir), "--", "gdbus", "call", "--session",
                "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus", "--method",
                "org.freedesktop.DBus.ListNames"], cwd=ROOT, capture_output=True, text=True, timeout=15).stdout
            return "org.mozilla.firefox." in out
        self.assertTrue(wait_until(firefox_on_the_bus, timeout=40, interval=1))
        time.sleep(10)   # Firefox consults IsEnabled once, early; let it settle with accessibility off
        self.mcp.call("control", {"action": "start"})
        self.assertTrue(wait_until(self.a11y_enabled, timeout=10))
        def firefox_window():
            return re.search(r'^(\d+) \S*[Ff]irefox\S* "PCPilot Test Page', self.mcp.call("windows")["content"][0]["text"], re.M)
        self.assertTrue(wait_until(firefox_window, timeout=40, interval=1))
        self.mcp.call("act", {"actions": [{"focus": int(firefox_window().group(1))}], "speed": "fast"})
        # Started with accessibility off: no page tree yet, and the output says how to get one.
        self.assertTrue(wait_until(lambda: "ctrl+n" in self.elements_text(), timeout=10), self.elements_text())
        # A new window created after the switch makes Firefox export every window.
        self.mcp.call("act", {"actions": [{"key": "ctrl+n"}, {"wait": 2500}, {"key": "ctrl+w"}, {"wait": 1500}],
                              "speed": "fast"})
        last = {}
        def page_ready():
            last["text"] = self.elements_text()
            return 'entry "Full name"' in last["text"]
        self.assertTrue(wait_until(page_ready, timeout=30, interval=1), last.get("text"))
        self.assertRegex(last["text"].splitlines()[0], r'^Firefox · "PCPilot Test Page"')
        # Closing a window: the real Close button is located through AT-SPI and clicked by the pointer.
        wid = int(firefox_window().group(1))
        self.mcp.call("act", {"actions": [{"window": [wid, "close"]}]})
        self.assertTrue(wait_until(lambda: not firefox_window(), timeout=15, interval=1),
                        self.mcp.call("windows")["content"][0]["text"])
        self.mcp.call("control", {"action": "stop"})
        self.assertTrue(wait_until(lambda: not self.a11y_enabled(), timeout=10))


    def test_06_accessibility_is_restored_when_the_mcp_dies(self):
        for how in ("sigkill", "sigterm-group"):
            with self.subTest(how=how):
                self.assertTrue(wait_until(lambda: not self.a11y_enabled(), timeout=10))
                mcp = self.mcp
                if how != "sigkill":
                    log = open(self.dir / f"{how}.log", "w+")
                    self.addCleanup(log.close)
                    mcp = MCPClient(dict(self.mcp.proc_env), log)
                    self.addCleanup(mcp.close)
                    mcp.initialize()
                mcp.call("control", {"action": "start"})
                self.assertTrue(wait_until(self.a11y_enabled, timeout=10))
                mcp.call("elements")          # the worker is alive for the whole session
                if how == "sigkill":
                    mcp.kill()                # SIGKILL: stdin EOF reaches the worker, which restores
                else:
                    os.killpg(mcp.proc.pid, signal.SIGTERM)   # MCP and worker both get SIGTERM
                self.assertTrue(wait_until(lambda: not self.a11y_enabled(), timeout=10),
                                f"left enabled after {how}")
                self.assert_inactive()


if __name__ == "__main__":
    unittest.main()
