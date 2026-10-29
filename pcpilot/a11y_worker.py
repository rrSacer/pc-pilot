"""AT-SPI reader, run ONLY as the desktop user on its session bus.

Input: {windows:[Meta.Window rows], window:"focused"|id, query:"", max:60}.
Output: {elements:[{role,name,x,y,states}]} (x/y may be null).

Wayland GTK reports surface-relative SCREEN extents. Prefer WINDOW extents,
subtract the accessible top-level WINDOW origin, then add the compositor frame
or buffer origin, selecting the rectangle whose size matches the top-level.
Match top-levels by PID + title;
a unique PID/top-level is a fallback. Unmatched/ambiguous geometry stays unknown.
X11 absolute SCREEN extents are accepted only when the top-level SCREEN origin
matches the compositor frame/buffer origin. No click is ever issued by this worker.
"""

import json
import math
import sys
import time


MAX_LINE = 1 << 20
INTERACTIVE_ROLES = {
    "button", "push button", "push button menu", "check box", "check menu item",
    "combo box", "entry", "text", "password text", "radio button", "radio menu item",
    "toggle button", "switch", "link", "menu", "menu item", "page tab", "list item",
    "tree item", "table cell", "slider", "spin button", "scroll bar", "terminal",
}


def rect(accessible, coord_type):
    try:
        component = accessible.get_component_iface()
        if component is None:
            return None
        bounds = component.get_extents(coord_type)
        values = (bounds.x, bounds.y, bounds.width, bounds.height)
        if not all(math.isfinite(v) and abs(v) < 1_000_000 for v in values):
            return None
        if bounds.width <= 0 or bounds.height <= 0:
            return None
        return values
    except Exception:
        return None


def match_window(pid, title, windows, top_count):
    matches = [w for w in windows if w.get("pid") == pid and not w.get("minimized")]
    titled = [w for w in matches if w.get("title") == title]
    if len(titled) == 1:
        return titled[0]
    if len(matches) == 1 and top_count == 1:
        return matches[0]
    return None


def mapped_center(bounds, top_window, top_screen, screen_bounds, window):
    """Return logical screen center, or None when geometry is not trustworthy."""
    if window is None:
        return None
    frame = (window["x"], window["y"], window["width"], window["height"])
    buffer = window.get("buffer") or {"x": frame[0], "y": frame[1], "width": frame[2], "height": frame[3]}
    surface = (buffer["x"], buffer["y"], buffer["width"], buffer["height"])
    candidate = None
    if bounds and top_window:
        x, y, w, h = bounds
        tx, ty, tw, th = top_window
        # GTK4 4.20/Wayland reports WINDOW extents relative to its frame,
        # while other toolkit surfaces may include CSD shadows (buffer).
        if abs(tw - frame[2]) <= 4 and abs(th - frame[3]) <= 4:
            candidate = (frame[0] + x - tx + w / 2, frame[1] + y - ty + h / 2)
        elif abs(tw - surface[2]) <= 4 and abs(th - surface[3]) <= 4:
            candidate = (surface[0] + x - tx + w / 2, surface[1] + y - ty + h / 2)
    if candidate is None and top_screen and screen_bounds and any(
            abs(top_screen[0] - origin[0]) <= 2 and abs(top_screen[1] - origin[1]) <= 2
            and abs(top_screen[2] - origin[2]) <= 4 and abs(top_screen[3] - origin[3]) <= 4
            for origin in (frame, surface)):
        x, y, w, h = screen_bounds
        candidate = (x + w / 2, y + h / 2)
    if candidate and frame[0] <= candidate[0] < frame[0] + frame[2] and frame[1] <= candidate[1] < frame[1] + frame[3]:
        return tuple(round(v) for v in candidate)
    return None


class Reader:
    def __init__(self):
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        self.atspi = Atspi
        Atspi.init()
        Atspi.set_timeout(300, 800)

    def snapshot(self, request):
        atspi = self.atspi
        windows = request.get("windows", [])
        target = request.get("window", "focused")
        if target == "focused":
            selected = [w for w in windows if w.get("focused")]
        else:
            selected = [w for w in windows if str(w.get("id")) == str(target)]
        if not selected:
            return []
        selected_ids = {str(w["id"]) for w in selected}
        selected_pids = {w.get("pid") for w in selected}
        query = request.get("query", "").casefold()
        maximum = min(200, max(1, int(request.get("max", 60))))
        deadline = time.monotonic() + 10
        desktop = atspi.get_desktop(0)
        result = []
        visited = 0
        for app_index in range(desktop.get_child_count()):
            if time.monotonic() > deadline:
                break
            try:
                app = desktop.get_child_at_index(app_index)
                pid = app.get_process_id()
                if pid not in selected_pids:
                    continue
                tops = [app.get_child_at_index(i) for i in range(min(app.get_child_count(), 100))]
            except Exception:
                continue
            for top in tops:
                try:
                    title = top.get_name() or ""
                    window = match_window(pid, title, windows, len(tops))
                    if window is not None and str(window["id"]) not in selected_ids:
                        continue
                    # Ambiguous top-levels for a selected PID remain readable, but
                    # coordinates must be unknown instead of choosing a window.
                    tw = rect(top, atspi.CoordType.WINDOW)
                    ts = rect(top, atspi.CoordType.SCREEN)
                except Exception:
                    continue
                stack = [top]
                while stack and len(result) < maximum and visited < 5000 and time.monotonic() < deadline:
                    node = stack.pop()
                    visited += 1
                    try:
                        states = node.get_state_set()
                        if states.contains(atspi.StateType.DEFUNCT):
                            continue
                        role = (node.get_role_name() or "unknown").lower()
                        name = node.get_name() or ""
                        visible = states.contains(atspi.StateType.VISIBLE) and states.contains(atspi.StateType.SHOWING)
                        actionable = role in INTERACTIVE_ROLES or states.contains(atspi.StateType.FOCUSABLE)
                        if visible and actionable and (not query or query in f"{role} {name}".casefold()):
                            center = mapped_center(rect(node, atspi.CoordType.WINDOW), tw, ts,
                                                   rect(node, atspi.CoordType.SCREEN), window)
                            flags = []
                            for state_name in ("FOCUSED", "CHECKED", "SELECTED", "EXPANDED", "EDITABLE"):
                                if states.contains(getattr(atspi.StateType, state_name)):
                                    flags.append(state_name.lower())
                            if not states.contains(atspi.StateType.SENSITIVE):
                                flags.append("disabled")
                            result.append({"role": role, "name": name[:300],
                                           "x": center[0] if center else None, "y": center[1] if center else None,
                                           "states": flags})
                        # Hidden intermediate containers sometimes have visible
                        # descendants; traverse them without listing the container.
                        for child_index in reversed(range(min(node.get_child_count(), 500))):
                            child = node.get_child_at_index(child_index)
                            if child:
                                stack.append(child)
                    except Exception:
                        continue
        return result


def main():
    reader = None
    for line in sys.stdin.buffer:
        try:
            if len(line) > MAX_LINE:
                raise ValueError("request too long")
            request = json.loads(line)
            if reader is None:
                reader = Reader()
            response = {"elements": reader.snapshot(request)}
        except Exception as exc:
            print(f"pcpilot AT-SPI: {exc}", file=sys.stderr)
            response = {"error": str(exc)}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
