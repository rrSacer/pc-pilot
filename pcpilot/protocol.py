"""Shared wire protocol for pcpilotd, its agent clients and the GNOME Shell UI.

Newline-delimited JSON over a Unix stream socket. See the spec artifact
"PCPilot — arquitetura e protocolo" for the full op table.
"""

import json
import os

PROTOCOL_VERSION = 1

DEFAULT_SOCKET = "/run/pcpilot/pcpilot.sock"
DEFAULT_CONFIG = "/etc/pcpilot/config.json"

# Max size of one JSON line; screenshots travel as file paths, never inline.
MAX_LINE = 1 << 20

# Error codes (``error.code``).
E_BAD_REQUEST = "bad_request"
E_BAD_ARGS = "bad_args"
E_UNKNOWN_OP = "unknown_op"
E_UI_UNAVAILABLE = "ui_unavailable"
E_UI_TIMEOUT = "ui_timeout"
E_BUSY = "busy"
E_NO_SESSION = "no_session"
E_USER_ABORTED = "user_aborted"
E_INACTIVE = "inactive"
E_NOT_FOUND = "not_found"
E_FAILED = "failed"
E_UNAUTHORIZED = "unauthorized"  # hello rejected (peer credentials), connection closed

# ``session_ended`` / UI ``state`` reasons.
R_USER_HOTKEY = "user_hotkey"
R_USER_RELEASE = "user_release"  # `pcpilot release`; same abort semantics as the hotkey
R_IDLE_TIMEOUT = "idle_timeout"
R_MAX_DURATION = "max_duration"
R_UI_DISCONNECTED = "ui_disconnected"
R_UI_TIMEOUT = "ui_timeout"  # UI never acked `state active`; begin failed
R_AGENT_REQUEST = "agent_request"
R_AGENT_DISCONNECTED = "agent_disconnected"
R_SHUTDOWN = "shutdown"
R_ERROR = "error"  # internal daemon error
R_INPUT_ERROR = "input_error"  # no keyboard able to send the hotkey could be (or stay) grabbed

# Daemon <-> UI events added for the session lease: every ``state``,
# ``state_ack``, ``ping``, ``pong`` and forwarded op carries ``"session": <int>``.
EV_STATE = "state"
EV_STATE_ACK = "state_ack"
EV_PING = "ping"
EV_PONG = "pong"


def socket_path():
    return os.environ.get("PCPILOT_SOCKET", DEFAULT_SOCKET)


def encode(msg):
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line):
    if len(line) > MAX_LINE:
        raise ValueError("line too long")
    try:
        msg = json.loads(line.decode("utf-8"))
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None
    if not isinstance(msg, dict):
        raise ValueError("message must be a JSON object")
    return msg


def request(id_, op, args=None):
    return {"id": id_, "op": op, "args": args or {}}


def ok(id_, result=None):
    return {"id": id_, "ok": True, "result": result if result is not None else {}}


def err(id_, code, message=""):
    return {"id": id_, "ok": False, "error": {"code": code, "message": message}}


def event(name, **fields):
    return {"event": name, **fields}


class ProtocolError(Exception):
    """Raised by clients when a reply carries ``ok: false``."""

    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
