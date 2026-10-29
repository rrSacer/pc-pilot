"""pcpilot: small admin CLI for pcpilotd.

    pcpilot status        session state (via the daemon)
    pcpilot release       force-end any session, from SSH/another TTY (root)
    pcpilot devices       list the input devices a session would capture
    pcpilot test-hotkey   wait for the hotkey on the capturable keyboards
                          (read-only, nothing is grabbed)
"""

import argparse
import json
import os
import select
import socket
import sys
import time

from . import evdev
from . import protocol as P
from .daemon import ConfigError, Hotkey, load_config


class Client:
    def __init__(self, path, timeout=5.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.file = self.sock.makefile("rb")
        self._id = 0

    def call(self, op, args=None):
        self._id += 1
        self.sock.sendall(P.encode(P.request(self._id, op, args)))
        while True:
            line = self.file.readline()
            if not line:
                raise ConnectionError("daemon closed the connection")
            msg = P.decode(line)
            if msg.get("id") != self._id:
                continue  # events
            if not msg.get("ok"):
                e = msg.get("error") or {}
                raise P.ProtocolError(e.get("code", P.E_FAILED), e.get("message", ""))
            return msg.get("result") or {}

    def close(self):
        # makefile() holds its own reference to the fd: close both or the
        # connection stays open (and a session owned by it with it).
        self.file.close()
        self.sock.close()


def _connect(args):
    c = Client(args.socket or P.socket_path())
    try:
        c.call("hello", {"client": "pcpilot-cli"})
    except BaseException:
        c.close()
        raise
    return c


def cmd_status(args):
    c = _connect(args)
    try:
        st = c.call("session.status")
    finally:
        c.close()
    if args.json:
        print(json.dumps(st))
        return 0
    print(f"ui connected: {'yes' if st.get('ui_connected') else 'no'}")
    if st.get("active"):
        print(f"session: active ({st.get('label')}), ends in {st.get('remaining_s')}s at most")
    elif st.get("starting"):
        print("session: starting")
    else:
        print("session: none")
    return 0


def cmd_release(args):
    c = _connect(args)
    try:
        res = c.call("session.release")
    finally:
        c.close()
    print("session released" if res.get("released") else "no session was active")
    return 0


def _load_cfg(args):
    path = args.config or P.DEFAULT_CONFIG
    return load_config(path, must_exist=path != P.DEFAULT_CONFIG)


def cmd_devices(args):
    cfg = _load_cfg(args)
    rows = []
    for info in evdev.list_devices():
        ok, why = evdev.capture_verdict(info, cfg["exclude_name_patterns"], cfg["device_name_filter"])
        if ok or args.all:
            rows.append((info.devnode, "capture" if ok else "skip", why, info.name))
    if args.json:
        print(json.dumps([dict(zip(("path", "action", "reason", "name"), r)) for r in rows]))
        return 0
    for path, action, why, name in rows:
        print(f"{path:<22} {action:<8} {why:<26} {name}")
    if not rows:
        print("no capturable devices")
    return 0


def cmd_test_hotkey(args):
    cfg = _load_cfg(args)
    hotkey = Hotkey(cfg["hotkey"])
    devs = []
    for info in evdev.list_devices():
        ok, _ = evdev.capture_verdict(info, cfg["exclude_name_patterns"], cfg["device_name_filter"])
        if ok and info.is_keyboard:
            try:
                devs.append((info, evdev.InputDevice(info.devnode)))
            except OSError as e:
                print(f"cannot open {info.devnode}: {e}", file=sys.stderr)
    if not devs:
        print("no keyboards to listen on", file=sys.stderr)
        return 1
    print(f"press {hotkey.text} within {args.timeout:.0f}s (listening on {len(devs)} keyboard(s), nothing is grabbed)")
    pressed = {info.rdev: set() for info, _ in devs}
    by_fd = {dev.fd: (info, dev) for info, dev in devs}
    deadline = time.monotonic() + args.timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                print("timeout: hotkey not detected")
                return 1
            ready, _, _ = select.select(list(by_fd), [], [], left)
            for fd in ready:
                info, dev = by_fd[fd]
                try:
                    events = dev.read_events()
                except OSError:
                    del by_fd[fd]
                    continue
                for type_, code, value in events:
                    if type_ != evdev.EV_KEY:
                        continue
                    (pressed[info.rdev].add if value else pressed[info.rdev].discard)(code)
                    union = set().union(*pressed.values())
                    if value and hotkey.matches(union):
                        print(f"hotkey detected on {info.devnode} ({info.name})")
                        return 0
            if not by_fd:
                print("all keyboards went away", file=sys.stderr)
                return 1
    finally:
        for _, dev in devs:
            dev.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pcpilot", description="PCPilot admin CLI")
    ap.add_argument("--socket", help="daemon socket (default: $PCPILOT_SOCKET or %s)" % P.DEFAULT_SOCKET)
    ap.add_argument("--config", help="config file (default: %s)" % P.DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status", help="show session state")
    p.add_argument("--json", action="store_true")
    sub.add_parser("release", help="force-end the current session")
    p = sub.add_parser("devices", help="list devices a session would capture")
    p.add_argument("--all", action="store_true", help="also list skipped devices")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("test-hotkey", help="check that the hotkey is detected (no grab)")
    p.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)
    handler = {
        "status": cmd_status,
        "release": cmd_release,
        "devices": cmd_devices,
        "test-hotkey": cmd_test_hotkey,
    }[args.cmd]
    try:
        return handler(args)
    except (OSError, ConnectionError) as e:
        print(f"pcpilot: {e}", file=sys.stderr)
        return 1
    except P.ProtocolError as e:
        print(f"pcpilot: daemon error: {e}", file=sys.stderr)
        return 1
    except ConfigError as e:
        print(f"pcpilot: config: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
