#!/usr/bin/env python3
"""Capture each AI theme in an already running *private headless* test session.

Start dev/headless-shell.sh and tests/ui_fake_daemon.py first. This script never
connects to a real daemon: its socket must live under the supplied headless dir.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.ui_fake_daemon import AgentClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    args = parser.parse_args()
    directory = args.session.resolve()
    env_path = directory / "env"
    if not env_path.is_file():
        parser.error("use a session created by dev/headless-shell.sh")
    env = dict(line.split("=", 1) for line in env_path.read_text().splitlines() if "=" in line)
    expected = str(directory / "pcpilot.sock")
    if (env.get("PCPILOT_HEADLESS_DIR") != str(directory) or
            env.get("PCPILOT_SOCKET") != expected or
            env.get("XDG_RUNTIME_DIR") != str(directory / "runtime") or
            not (directory / "runtime/wayland-pcpilot").is_socket()):
        parser.error("the session must use its own private runtime dir and fake socket")
    output = directory / "review"
    output.mkdir(exist_ok=True)
    timings = {}
    with_client = AgentClient(expected, timeout=30)
    try:
        with_client.call("fake.events")  # the real daemon does not implement this op
        for theme in ("claude", "codex", "gemini", "neutral"):
            with_client.call("session.end")
            with_client.call("session.begin", {"label": theme.title() if theme != "neutral" else "Assistente",
                                                 "theme": theme})
            windows = with_client.call("ui.window.list")["windows"]
            if windows:
                target = next((w for w in windows if "Probe" in w["title"]), windows[0])
                with_client.call("ui.window.action", {"id": target["id"], "action": "activate"})
                with_client.call("ui.window.action", {"id": target["id"], "action": "maximize"})
            with_client.call("ui.pointer.move", {"x": 400, "y": 500, "style": "instant"})
            started = time.monotonic()
            with_client.call("ui.pointer.move", {"x": 1500, "y": 650})
            timings[theme] = round((time.monotonic() - started) * 1000)
            with_client.call("ui.status", {"text": "Organizando as janelas e preparando a próxima ação"})
            for suffix, capture_args in (("", {"include_overlay": True}), ("-clean", {})):
                shot = with_client.call("ui.screenshot", capture_args)
                shutil.copyfile(shot["path"], output / f"{theme}{suffix}.png")
        with_client.call("session.end")
    finally:
        with_client.close()
    (output / "timings.json").write_text(json.dumps(timings, indent=2) + "\n")
    print(json.dumps({"screenshots": str(output), "move_1110px_ms": timings}))


if __name__ == "__main__":
    main()
