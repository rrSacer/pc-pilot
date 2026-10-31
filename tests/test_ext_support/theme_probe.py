"""Theme constants of the extension (extension/pcpilot@local/theme.js) for pixel checks.

theme.js is pure data: Node imports it directly, so tests never duplicate the palette.
The frame line is a gradient through the theme's `rim` stops along the monitor diagonal,
so the colour expected at a given pixel is computed, not hard-coded.
"""
import json
import shutil
from functools import lru_cache
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
THEME_JS = ROOT / "extension/pcpilot@local/theme.js"
THEME_NAMES = ("claude", "codex", "gemini", "neutral")


@lru_cache(maxsize=None)
def theme(name):
    """The extension's themeFor(name) as a dict (id, rim, glow, pill, badge, cursor, ...)."""
    code = (f"import {{themeFor}} from {json.dumps(THEME_JS.as_uri())};"
            f"console.log(JSON.stringify(themeFor({json.dumps(name)})));")
    out = subprocess.run([shutil.which("node"), "--input-type=module", "-e", code],
                         check=True, capture_output=True, text=True, timeout=30).stdout
    return json.loads(out)


def rgb(hex_color):
    n = int(hex_color.lstrip("#"), 16)
    return ((n >> 16) & 255, (n >> 8) & 255, n & 255)


def rim_color(name, x, y, width=1920, height=1080):
    """Colour of the frame line at (x, y) of a width x height monitor."""
    stops = [rgb(c) for c in theme(name)["rim"]]
    if len(stops) == 1:
        return stops[0]
    t = min(1.0, max(0.0, (x * width + y * height) / (width * width + height * height)))
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    f = pos - i
    return tuple(round(a + (b - a) * f) for a, b in zip(stops[i], stops[i + 1]))


def close(a, b, tol=14):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def is_frame_pixel(pixel, x=1, y=540, width=1920, height=1080, tol=14, themes=THEME_NAMES):
    """Is `pixel`, taken at (x, y) on the frame line, the rim colour of any of `themes`?"""
    return any(close(pixel, rim_color(n, x, y, width, height), tol) for n in themes)
