#!/usr/bin/env python3
"""Minimal stand-in for pcpilotd, for testing the GNOME extension in isolation.

No device grab, no hotkey, no peer-credential checks. It speaks both sides of the
socket protocol:

* the UI (first message ``hello`` with ``args.role == "ui"``): gets ``state`` events
  and forwarded ``ui.*`` requests, exactly like the real daemon;
* agents (any other ``hello``): ``hello``, ``session.begin``/``end``/``status`` and
  ``ui.<op>`` forwarding, so pcpilot-mcp can run against it via ``PCPILOT_SOCKET``.

Fake-only agent ops (for tests): ``fake.wait_ui {timeout}``, ``fake.state {active,
reason?, label?, wait_ack?}`` (send a raw state event), ``fake.events`` (UI events
received so far, e.g. state_ack/pong), ``fake.ui_raw {op, args}`` (forward an op to the
UI even without a session, to test the UI's own ``inactive`` refusal), ``fake.lease``
(session number, whether pinging, seconds since the last pong).

Like the real daemon, ``state`` carries ``session`` (the ack must echo it) and an active
session is kept alive by a ping/pong lease (``--ping-interval``, 0 disables).

    python3 tests/ui_fake_daemon.py serve --socket PATH [--sticky-session]
    python3 tests/ui_fake_daemon.py call --socket PATH [--begin] OP ['{"json":"args"}']
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from pcpilot import protocol as P  # noqa: E402

ACK_TIMEOUT_S = 2.0
UI_TIMEOUT_S = 20.0
PING_INTERVAL_S = 2.0
LEASE_TIMEOUT_S = 6.0  # no pong for this long -> session ends (reason ui_timeout)
THEMES = frozenset({"claude", "codex", "gemini", "neutral"})  # as pcpilot/daemon.py validates


class Conn:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.role = None
        self.name = None

    def send(self, msg):
        if not self.writer.is_closing():
            self.writer.write(P.encode(msg))


class FakeDaemon:
    def __init__(self, path, sticky=False, label="Claude", hotkey="Ctrl+Alt+Esc", log=None,
                 ping_interval=PING_INTERVAL_S, lease_timeout=LEASE_TIMEOUT_S):
        self.path = path
        self.sticky = sticky
        self.label = label
        self.theme = "neutral"
        self.hotkey = hotkey
        self.log = log or (lambda *a: None)
        self.ui = None
        self.ui_connected = asyncio.Event()
        self.pending = {}  # ui request id -> future
        self.next_id = 0
        self.active = False
        self.owner = None
        self.ack_waiter = None
        self.events = []
        self.server = None
        self.session = 0  # number of the current/last session, sent in state events
        self.ping_interval = ping_interval
        self.lease_timeout = lease_timeout
        self.last_pong = 0.0
        self.pinger = None

    async def start(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.server = await asyncio.start_unix_server(self._client, path=self.path, limit=P.MAX_LINE)
        os.chmod(self.path, 0o666)  # the UI runs as the desktop user
        return self

    async def close(self):
        if self.server:
            self.server.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    # ---- connections ----

    async def _client(self, reader, writer):
        conn = Conn(reader, writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = P.decode(line)
                except ValueError as e:
                    conn.send(P.err(None, P.E_BAD_REQUEST, str(e)))
                    continue
                if conn.role == "ui":
                    self._from_ui(msg)
                elif conn.role == "agent":
                    asyncio.ensure_future(self._agent_op(conn, msg))
                else:
                    self._hello(conn, msg)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self._gone(conn)
            writer.close()

    def _hello(self, conn, msg):
        if msg.get("op") != "hello":
            conn.send(P.err(msg.get("id"), P.E_BAD_REQUEST, "hello first"))
            return
        args = msg.get("args") or {}
        if args.get("role") == "ui":
            if self.ui is not None:
                conn.send(P.err(msg.get("id"), P.E_BUSY, "ui already connected"))
                conn.writer.close()
                return
            conn.role = "ui"
            self.ui = conn
            self.ui_connected.set()
            self.log("ui connected")
            conn.send(P.ok(msg.get("id"), {"protocol": P.PROTOCOL_VERSION}))
        else:
            conn.role = "agent"
            conn.name = args.get("client", "agent")
            conn.send(P.ok(msg.get("id"), self._hello_result()))

    def _hello_result(self):
        return {"protocol": P.PROTOCOL_VERSION, "ui_connected": self.ui is not None,
                "session": {"active": self.active}}

    async def _gone(self, conn):
        if conn is self.ui:
            self.log("ui disconnected")
            self.ui = None
            self.ui_connected.clear()
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(P.ProtocolError(P.E_UI_UNAVAILABLE, "ui disconnected"))
            self.pending.clear()
            self.active = False
            self.owner = None
            self._stop_lease()
        elif conn.role == "agent" and conn is self.owner and not self.sticky:
            self._end(P.R_AGENT_DISCONNECTED)

    # ---- UI side ----

    def _from_ui(self, msg):
        if "event" in msg:
            self.events.append(msg)
            if (msg["event"] == "state_ack" and msg.get("session") == self.session
                    and self.ack_waiter and not self.ack_waiter.done()):
                self.ack_waiter.set_result(msg)
            elif msg["event"] == "pong" and msg.get("session") == self.session:
                self.last_pong = time.monotonic()
            return
        fut = self.pending.pop(msg.get("id"), None)
        if fut and not fut.done():
            fut.set_result(msg)

    async def ui_request(self, op, args, timeout=UI_TIMEOUT_S):
        if self.ui is None:
            raise P.ProtocolError(P.E_UI_UNAVAILABLE, "no ui")
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        req = P.request(rid, op, args)
        if self.active:
            req["session"] = self.session
        self.ui.send(req)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            raise P.ProtocolError(P.E_UI_TIMEOUT, f"{op} timed out")

    async def send_state(self, active, reason=None, label=None, wait_ack=True, theme=None):
        if self.ui is None:
            raise P.ProtocolError(P.E_UI_UNAVAILABLE, "no ui")
        if active:
            self.session += 1
            # Like the daemon: the theme belongs to the session; missing/unknown = neutral.
            self.theme = theme if isinstance(theme, str) and theme in THEMES else "neutral"
            self.ack_waiter = asyncio.get_running_loop().create_future()
            t0 = time.monotonic()
            self.ui.send(P.event("state", active=True, session=self.session,
                                 label=label or self.label, theme=self.theme, hotkey=self.hotkey))
            if not wait_ack:
                return {"session": self.session}
            try:
                await asyncio.wait_for(self.ack_waiter, ACK_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.ui.send(P.event("state", active=False, session=self.session,
                                     reason=P.R_UI_TIMEOUT))
                raise P.ProtocolError(P.E_UI_UNAVAILABLE, "no state_ack")
            self._start_lease()
            return {"ack_ms": round((time.monotonic() - t0) * 1000, 1), "session": self.session}
        self._stop_lease()
        self.ui.send(P.event("state", active=False, session=self.session,
                             reason=reason or P.R_AGENT_REQUEST))
        return {}

    # Health lease: ping every ping_interval; no matching pong for lease_timeout ends it.
    def _start_lease(self):
        self._stop_lease()
        self.last_pong = time.monotonic()
        if self.ping_interval > 0:
            self.pinger = asyncio.ensure_future(self._ping_loop(self.session))

    def _stop_lease(self):
        if self.pinger is not None:
            self.pinger.cancel()
            self.pinger = None

    async def _ping_loop(self, session):
        while self.session == session and self.ui is not None:
            self.ui.send(P.event("ping", session=session))
            await asyncio.sleep(self.ping_interval)
            if self.session == session and time.monotonic() - self.last_pong > self.lease_timeout:
                self.log(f"lease expired for session {session}")
                self.pinger = None
                self.active = False
                self.owner = None
                self.ui.send(P.event("state", active=False, session=session, reason=P.R_UI_TIMEOUT))
                return

    def _end(self, reason):
        if not self.active:
            return
        self.active = False
        self.owner = None
        self._stop_lease()
        if self.ui is not None:
            self.ui.send(P.event("state", active=False, session=self.session, reason=reason))

    # ---- agent side ----

    async def _agent_op(self, conn, msg):
        rid, op, args = msg.get("id"), msg.get("op"), msg.get("args") or {}
        try:
            result = await self._dispatch(conn, op, args)
            conn.send(P.ok(rid, result))
        except P.ProtocolError as e:
            conn.send(P.err(rid, e.code, e.message))
        except Exception as e:  # noqa: BLE001
            conn.send(P.err(rid, P.E_FAILED, repr(e)))

    async def _dispatch(self, conn, op, args):
        if op == "hello":
            return self._hello_result()
        if op == "session.begin":
            if self.active and self.owner is not conn and not self.sticky:
                raise P.ProtocolError(P.E_BUSY, "another agent owns the session")
            if not self.active:
                r = await self.send_state(True, label=args.get("label"), theme=args.get("theme"))
                self.active = True
                self.log(f"session active (ack {r.get('ack_ms')} ms)")
            self.owner = conn
            return {"active": True}
        if op == "session.end":
            self._end(args.get("reason", P.R_AGENT_REQUEST))
            return {}
        if op == "session.status":
            return {"active": self.active, "owner_is_me": self.owner is conn, "aborted": False,
                    "ui_connected": self.ui is not None, "idle_timeout_s": 0, "remaining_s": 0}
        if op.startswith("ui."):
            if not self.active or (self.owner is not conn and not self.sticky):
                raise P.ProtocolError(P.E_NO_SESSION, "no session")
            return self._unwrap(await self.ui_request(op[3:], args))
        if op == "fake.wait_ui":
            await asyncio.wait_for(self.ui_connected.wait(), args.get("timeout", 30))
            return {}
        if op == "fake.state":
            r = await self.send_state(bool(args.get("active")), args.get("reason"),
                                      args.get("label"), args.get("wait_ack", True), args.get("theme"))
            self.active = bool(args.get("active"))
            self.owner = conn if self.active else None
            return r
        if op == "fake.lease":
            return {"session": self.session, "pinging": self.pinger is not None,
                    "since_pong_s": round(time.monotonic() - self.last_pong, 2)}
        if op == "fake.events":
            return {"events": list(self.events)}
        if op == "fake.ui_raw":
            return self._unwrap(await self.ui_request(args["op"], args.get("args") or {}))
        raise P.ProtocolError(P.E_UNKNOWN_OP, op)

    @staticmethod
    def _unwrap(reply):
        if reply.get("ok"):
            return reply.get("result", {})
        e = reply.get("error") or {}
        raise P.ProtocolError(e.get("code", P.E_FAILED), e.get("message", ""))


class AgentClient:
    """Blocking agent-side client (for tests and the ``call`` CLI)."""

    def __init__(self, path, client="test", timeout=30.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buf = b""
        self.next_id = 0
        self.events = []
        self.call("hello", {"client": client})

    def close(self):
        self.sock.close()

    def _readline(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return P.decode(line)

    def call(self, op, args=None, timeout=None):
        self.next_id += 1
        rid = self.next_id
        if timeout is not None:
            self.sock.settimeout(timeout)
        self.sock.sendall(P.encode(P.request(rid, op, args)))
        while True:
            msg = self._readline()
            if "event" in msg:
                self.events.append(msg)
                continue
            if msg.get("id") == rid:
                if msg.get("ok"):
                    return msg.get("result", {})
                e = msg.get("error") or {}
                raise P.ProtocolError(e.get("code", P.E_FAILED), e.get("message", ""))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--socket", default=P.socket_path())
    s.add_argument("--sticky-session", action="store_true",
                   help="session survives agent disconnects and is usable by any agent")
    s.add_argument("--label", default="Claude")
    s.add_argument("--ping-interval", type=float, default=PING_INTERVAL_S)
    c = sub.add_parser("call")
    c.add_argument("--socket", default=P.socket_path())
    c.add_argument("--begin", action="store_true", help="session.begin first")
    c.add_argument("op")
    c.add_argument("args", nargs="?", default="{}")
    a = ap.parse_args()

    if a.cmd == "call":
        cl = AgentClient(a.socket, client="cli")
        if a.begin:
            cl.call("session.begin", {"label": "CLI"})
        try:
            print(json.dumps(cl.call(a.op, json.loads(a.args)), ensure_ascii=False))
        except P.ProtocolError as e:
            print(json.dumps({"error": e.code, "message": e.message}, ensure_ascii=False))
            sys.exit(1)
        return

    async def serve():
        d = await FakeDaemon(a.socket, sticky=a.sticky_session, label=a.label,
                             ping_interval=a.ping_interval,
                             log=lambda *m: print("fake-daemon:", *m, flush=True)).start()
        print(f"fake-daemon: listening on {a.socket}", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await d.close()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
