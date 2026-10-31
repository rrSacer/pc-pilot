"""Run focused Node regression scenarios against the real extension modules."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_TEST = ROOT / "tests/test_ext_support/node/bootstrap.mjs"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for extension unit regressions")
class ExtensionUnitRegressionTests(unittest.TestCase):
    def test_real_extension_modules_regressions(self):
        proc = subprocess.run([shutil.which("node"), "--no-warnings", str(NODE_TEST)],
            cwd=ROOT, capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, f"Node scenario failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(result["ok"], True)
        self.assertTrue({"op-session", "paint-timeout", "screenshot-cancel", "typing-estimate", "paste-cancel",
            "clipboard-ownership", "timeout-queue", "timeout-fail-closed", "stale-generation",
            "overlay-exception", "session-ack", "session-ping-pong", "trail-idle", "path-endpoints", "path-duration",
            "path-curve", "typing-cadence-cap", "glide-cancel", "cursor-inactive-reasons",
            "cursor-connection-loss", "cursor-disable", "cursor-cleanup-exception",
            "cursor-overlay-failures", "balanced-cursor-inhibit", "wrapup-cap", "r1-authorship", "r2-transient-families",
            "rr-spawned-pid-ancestry", "rr-cancelled-launches",
            "r3-idempotent-refusal", "r4-journal-conflicts", "r5-drag-hold", "r6-cadence-budget",
            "monitor-clamp", "r7-real-wrapup-aborts", "r7-real-cap", "path-perpendicular",
            "path-v21-timing", "path-v21-velocity", "theme-lookup-frame", "theme-state-cursor",
            "wrapup-return-home", "screenshot-frame-only",
            "human-move-timing-range", "human-move-shape", "no-opacity-blink", "border-geometry-symmetric",
            "theme-data-sanity", "theme-switch-clean", "wrapup-fade-smooth", "wrapup-fade-cancel", "wrapup-fade-resume",
            "spawn-reserved-pid-reaped"}.issubset(result["checks"]))
        self.assertEqual(result.get("bugs", []), [], "extension defect observed by the regression shim")


if __name__ == "__main__": unittest.main()
