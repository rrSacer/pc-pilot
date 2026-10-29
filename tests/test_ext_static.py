"""Static contract checks for the GNOME extension (no GJS runtime required)."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension/pcpilot@local"


def constants(path):
    text = path.read_text()
    return {name: value for name, value in re.findall(
        r"(?:export\s+)?(?:const\s+)?([ER]_[A-Z0-9_]+)\s*(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", text)}


class ExtensionStaticTests(unittest.TestCase):
    def test_metadata(self):
        data = json.loads((EXT / "metadata.json").read_text())
        self.assertEqual(data["uuid"], "pcpilot@local")
        self.assertEqual(data["shell-version"], ["51"])
        self.assertIn("user", data["session-modes"])

    def test_protocol_constants_match_python(self):
        js = constants(EXT / "protocol.js")
        py = constants(ROOT / "pcpilot/protocol.py")
        # Every extension E_/R_ constant must exist and agree in the shared protocol.
        for name, value in js.items():
            if name.startswith(("E_", "R_")):
                self.assertEqual(py.get(name), value, name)
        # Also ensure all protocol error/reason constants used by the extension mirror.
        used = set(re.findall(r"\b([ER]_[A-Z0-9_]+)\b", "\n".join(
            p.read_text() for p in EXT.glob("*.js"))))
        for name in used:
            self.assertEqual(js.get(name), py.get(name), name)

    def test_every_reason_has_portuguese_toast(self):
        py = constants(ROOT / "pcpilot/protocol.py")
        js = (EXT / "protocol.js").read_text()
        for name in py:
            if name.startswith("R_"):
                self.assertRegex(js, rf"\[{name}\]\s*:\s*['\"][^'\"]+['\"]", name)

    def test_ops_table_matches_contract(self):
        expected = {"screen.info", "screenshot", "pointer.move", "pointer.click",
                    "pointer.drag", "pointer.scroll", "pointer.position", "key.press",
                    "key.type", "app.search", "app.launch", "open_uri", "window.list",
                    "window.action", "clipboard.get", "clipboard.set", "status"}
        text = (EXT / "ops.js").read_text()
        body = text.split("this._handlers = {", 1)[1].split("};", 1)[0]
        actual = set(re.findall(r"'([^']+)'\s*:", body))
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
