"""AT-SPI reader, run ONLY as the desktop user on its session bus.

Requests (JSON lines) and replies:
  snapshot (default): {windows, window:"focused"|id, query, role, max}
      -> {elements:[{ref,role,name,x,y,states,value?,vis?,dist?,o?}],
          headings:[...], page:{...}?, absent?}
  locate: {ref, windows} -> {state:in|above|below|side|gone|unknown, x, y, dist, vp}
  acquire / release: switch org.a11y.Status IsEnabled on for this session / restore it.
Native apps (GTK) keep their old shape. Web documents (Firefox, Chromium) add a page map:
interactive elements, headings, scroll hints for off-screen ones, address/tabs/loading/focus.

Wayland GTK reports surface-relative SCREEN extents. Prefer WINDOW extents,
subtract the accessible top-level WINDOW origin, then add the compositor frame
or buffer origin, selecting the rectangle whose size matches the top-level.
Match top-levels by PID + title (+ size / active state for same-titled siblings);
a unique PID/top-level is a fallback. Unmatched/ambiguous geometry stays unknown.
X11 absolute SCREEN extents are accepted only when the top-level SCREEN origin
matches the compositor frame/buffer origin. No click is ever issued by this worker.

Accessibility switch: Firefox only exports its DOM while org.a11y.Status IsEnabled is
true, and setting it also writes gsettings toolkit-accessibility. The worker therefore
records the previous value in a lock-protected state file BEFORE changing anything and
restores it on release, EOF on stdin, SIGTERM/SIGINT, normal exit, or (stale record,
dead owner) at the next worker start / `--recover`.
"""

import fcntl
import json
import math
import os
import signal
import sys
import time
import warnings

warnings.simplefilter("ignore")  # PyGObject deprecation notices would flood the MCP stderr

MAX_LINE = 1 << 20
INTERACTIVE_ROLES = {
    "button", "push button", "push button menu", "check box", "check menu item",
    "combo box", "entry", "text", "password text", "radio button", "radio menu item",
    "toggle button", "switch", "link", "menu", "menu item", "page tab", "list item",
    "tree item", "table cell", "slider", "spin button", "scroll bar", "terminal",
}
WEB_ROLES = {
    "link", "button", "push button", "toggle button", "check box", "radio button",
    "combo box", "entry", "password text", "spin button", "slider", "switch",
    "menu item", "check menu item", "radio menu item", "tree item", "page tab",
}
VALUE_ROLES = {"entry", "password text", "combo box", "spin button", "slider", "text"}
DOC_ROLES = {"document web", "document frame"}
CONTAINER_ROLES = {
    "document web", "document frame", "internal frame", "frame", "window", "dialog",
    "scroll pane", "viewport", "filler", "panel", "landmark", "form", "section",
    "tool bar", "menu bar", "page tab list", "list", "table", "tree", "tree table",
    "application", "alert", "heading", "paragraph", "separator", "label", "text", "static",
    "image", "list item", "table cell", "table row", "caption", "block quote", "math",
}
NO_DESCEND = {"menu", "popup menu", "combo box", "entry", "password text", "link", "button",
              "push button", "toggle button", "check box", "radio button", "image", "separator"}
ALIASES = {
    "push button": "button", "check box": "checkbox", "radio button": "radio",
    "password text": "password", "combo box": "combo", "page tab": "tab",
    "menu item": "item", "toggle button": "toggle", "spin button": "spin",
    "check menu item": "checkitem", "radio menu item": "radioitem",
}
STATE_FLAGS = ("FOCUSED", "CHECKED", "SELECTED", "EXPANDED", "PRESSED", "REQUIRED", "INVALID_ENTRY")
FLAG_NAMES = {"INVALID_ENTRY": "invalid"}
NODE_CAP = 6000
SCAN_SECONDS = 9
REF_CAP = 3000
OFFSCREEN_PEEK = 10
BUTTON_NAMES = {
    "close": {"close", "fechar"}, "minimize": {"minimize", "minimizar"},
    "maximize": {"maximize", "maximizar", "restore", "restaurar", "unmaximize"},
}
BUTTON_NAMES["unmaximize"] = BUTTON_NAMES["maximize"]
LOCK_ATTEMPTS = 50


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


def match_window(pid, title, windows, top_count, size=None, active=False):
    matches = [w for w in windows if w.get("pid") == pid and not w.get("minimized")]
    titled = [w for w in matches if w.get("title") == title]
    if len(titled) == 1:
        return titled[0]
    if len(matches) == 1 and top_count == 1:
        return matches[0]
    # Same-titled siblings (two Firefox windows on one page): size, then focus.
    pool = titled
    if size and pool:
        sized = [w for w in pool if abs(w["width"] - size[0]) <= 4 and abs(w["height"] - size[1]) <= 4
                 or (w.get("buffer") and abs(w["buffer"]["width"] - size[0]) <= 4
                     and abs(w["buffer"]["height"] - size[1]) <= 4)]
        if len(sized) == 1:
            return sized[0]
        pool = sized or pool
    if active and pool:
        focused = [w for w in pool if w.get("focused")]
        if len(focused) == 1:
            return focused[0]
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


def global_rect(bounds, top_window, window):
    """WINDOW-relative rect -> logical screen rect, or None when untrusted."""
    if window is None or not bounds or not top_window:
        return None
    frame = (window["x"], window["y"], window["width"], window["height"])
    buffer = window.get("buffer") or {"x": frame[0], "y": frame[1], "width": frame[2], "height": frame[3]}
    surface = (buffer["x"], buffer["y"], buffer["width"], buffer["height"])
    tx, ty, tw, th = top_window
    for ox, oy, ow, oh in (frame, surface):
        if abs(tw - ow) <= 4 and abs(th - oh) <= 4:
            x, y, w, h = bounds
            return (ox + x - tx, oy + y - ty, w, h)
    return None


def intersect(a, b):
    if a is None or b is None:
        return None
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x1, y1, x2 - x1, y2 - y1) if x2 > x1 and y2 > y1 else None


def classify(box, viewport):
    """('in'|'above'|'below'|'side', screens to scroll to center it)."""
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    vx, vy, vw, vh = viewport
    if vy <= cy < vy + vh and vx <= cx < vx + vw:
        return "in", 0.0
    dist = abs(cy - (vy + vh / 2)) / max(1, vh)
    if cy < vy:
        return "above", dist
    if cy >= vy + vh:
        return "below", dist
    return "side", dist


def alias(role):
    return ALIASES.get(role, role)


def matches(query, role, name):
    return not query or query in f"{role} {alias(role)} {name}".casefold()


# --- accessibility switch (org.a11y.Status IsEnabled) -----------------------

def state_dir():
    base = os.environ.get("PCPILOT_A11Y_STATE_DIR")
    if not base:
        base = os.path.join(os.environ.get("HOME") or "/nonexistent", ".local", "state", "pcpilot")
    return base


def _pid_alive(pid):
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as stream:
            return b"a11y_worker" in stream.read()
    except (OSError, ValueError):
        return False


class Switch:
    """Reference-counted, crash-safe toggle of org.a11y.Status IsEnabled."""

    def __init__(self):
        self.mine = False

    @staticmethod
    def _bus():
        from gi.repository import Gio
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def get(self):
        from gi.repository import Gio, GLib
        try:
            reply = self._bus().call_sync(
                "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("org.a11y.Status", "IsEnabled")), GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE, 2000, None)
            return bool(reply.unpack()[0])
        except Exception as exc:
            print(f"pcpilot a11y get: {exc}", file=sys.stderr)
            return None

    def put(self, value):
        from gi.repository import Gio, GLib
        try:
            self._bus().call_sync(
                "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
                GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", GLib.Variant("b", bool(value)))),
                None, Gio.DBusCallFlags.NONE, 2000, None)
            return True
        except Exception as exc:
            print(f"pcpilot a11y set: {exc}", file=sys.stderr)
            return False

    def _locked(self):
        directory = state_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        handle = open(os.path.join(directory, "a11y.json"), "a+")
        for _ in range(LOCK_ATTEMPTS):
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError:
                time.sleep(.05)
        handle.close()
        raise TimeoutError("a11y state lock busy")

    @staticmethod
    def _load(handle):
        handle.seek(0)
        try:
            record = json.loads(handle.read() or "{}")
        except ValueError:
            record = {}
        return record if isinstance(record, dict) else {}

    @staticmethod
    def _store(handle, record):
        handle.seek(0)
        handle.truncate()
        if record:
            handle.write(json.dumps(record))
        handle.flush()
        os.fsync(handle.fileno())

    def _owners(self, record, drop_me=False):
        me = os.getpid()
        return [pid for pid in record.get("owners", [])
                if isinstance(pid, int) and (pid != me or not drop_me) and (pid == me or _pid_alive(pid))]

    def acquire(self):
        """-> {"changed": bool, "enabled": bool|None}"""
        handle = self._locked()
        try:
            record = self._load(handle)
            owners = self._owners(record, drop_me=True)
            current = self.get()
            if current is None:
                return {"changed": False, "enabled": None}
            if owners:
                record["owners"] = owners + [os.getpid()]
                self._store(handle, record)
                self.mine = True
                return {"changed": False, "enabled": current}
            previous = False if record.get("prev") is False and current else current
            if previous:
                # Already enabled by the user: change and record nothing.
                self._store(handle, {})
                return {"changed": False, "enabled": True}
            self._store(handle, {"prev": False, "owners": [os.getpid()], "since": time.time()})
            self.mine = True
            if not current and not self.put(True):
                self._store(handle, {})
                self.mine = False
                return {"changed": False, "enabled": False}
            return {"changed": not current, "enabled": True}
        finally:
            handle.close()

    def release(self):
        handle = self._locked()
        try:
            record = self._load(handle)
            owners = self._owners(record, drop_me=True)
            self.mine = False
            if owners:
                record["owners"] = owners
                self._store(handle, record)
                return {"restored": False}
            restored = False
            if record.get("prev") is False and self.get():
                restored = self.put(False)
                if not restored:
                    record["owners"] = []
                    self._store(handle, record)  # keep the record for a later retry
                    return {"restored": False}
            self._store(handle, {})
            return {"restored": restored}
        finally:
            handle.close()

    def recover(self):
        """Undo a switch left behind by a dead owner; used at start and by --recover."""
        try:
            handle = self._locked()
        except (OSError, TimeoutError):
            return
        try:
            record = self._load(handle)
            if not record or self._owners(record, drop_me=True):
                return
            if record.get("prev") is False and self.get() and not self.put(False):
                return
            self._store(handle, {})
        finally:
            handle.close()


# --- AT-SPI reader ----------------------------------------------------------

class Reader:
    def __init__(self):
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        self.atspi = Atspi
        Atspi.init()
        Atspi.set_timeout(500, 1500)
        self.refs = {}
        self.next_ref = 1

    # -- helpers
    def state(self, states, name):
        return states.contains(getattr(self.atspi.StateType, name))

    def remember(self, node, ctx, web, docs=()):
        ref = self.next_ref
        self.next_ref += 1
        self.refs[ref] = {"node": node, "top": ctx["top"], "wid": ctx["wid"], "web": web,
                          "docs": list(docs)}
        if len(self.refs) > REF_CAP:
            for stale in list(self.refs)[:len(self.refs) - REF_CAP]:
                del self.refs[stale]
        return ref

    def value_of(self, node, role):
        atspi = self.atspi
        try:
            if role == "password text":
                text = node.get_text_iface()
                count = atspi.Text.get_character_count(text) if text is not None else 0
                return f"({count} chars)" if count else ""
            if role in {"entry", "text"} or (role == "combo box" and self.state(node.get_state_set(), "EDITABLE")):
                text = node.get_text_iface()
                if text is not None:
                    count = atspi.Text.get_character_count(text)
                    return atspi.Text.get_text(text, 0, min(count, 80)).replace("\n", " ") + ("…" if count > 80 else "")
            if role == "combo box":
                return self.selected_option(node)
            if role in {"spin button", "slider"}:
                value = node.get_value_iface()
                if value is not None:
                    number = value.get_current_value()
                    return str(int(number)) if float(number).is_integer() else f"{number:.3g}"
        except Exception:
            return None
        return None

    def selected_option(self, node, depth=0):
        """Name of the selected option below a (non-editable) combo box."""
        selection = node.get_selection_iface()
        if selection and selection.get_n_selected_children():
            return selection.get_selected_child(0).get_name() or ""
        if depth < 2:
            for index in range(min(node.get_child_count(), 60)):
                child = node.get_child_at_index(index)
                if child is None:
                    continue
                if self.state(child.get_state_set(), "SELECTED") and depth:
                    return child.get_name() or ""
                found = self.selected_option(child, depth + 1)
                if found:
                    return found
        return ""

    def flags(self, states):
        result = []
        for name in STATE_FLAGS:
            if states.contains(getattr(self.atspi.StateType, name)):
                result.append(FLAG_NAMES.get(name, name.lower()))
        if not states.contains(self.atspi.StateType.SENSITIVE):
            result.append("disabled")
        return result

    def doc_url(self, node):
        try:
            iface = node.get_document_iface()
            if iface:
                return iface.get_document_attribute_value("DocURL") or ""
        except Exception:
            pass
        return ""

    # -- snapshot
    def snapshot(self, request):
        atspi = self.atspi
        windows = request.get("windows", [])
        target = request.get("window", "focused")
        if target == "focused":
            selected = [w for w in windows if w.get("focused")]
        else:
            selected = [w for w in windows if str(w.get("id")) == str(target)]
        if not selected:
            return {"elements": []}
        selected_ids = {str(w["id"]) for w in selected}
        selected_pids = {w.get("pid") for w in selected}
        query = request.get("query", "").casefold()
        roles = {part.strip().casefold() for part in str(request.get("role", "")).split(",") if part.strip()}
        maximum = min(200, max(1, int(request.get("max", 60))))
        deadline = time.monotonic() + SCAN_SECONDS
        desktop = atspi.get_desktop(0)
        scan = {"native": [], "web": [], "headings": [], "tabs": [], "focus": None, "page": None,
                "visited": 0, "deadline": deadline, "address": None, "loading": False}
        found_app = False
        for app_index in range(desktop.get_child_count()):
            if time.monotonic() > deadline:
                break
            try:
                app = desktop.get_child_at_index(app_index)
                pid = app.get_process_id()
                if pid not in selected_pids:
                    continue
                found_app = True
                tops = [app.get_child_at_index(i) for i in range(min(app.get_child_count(), 100))]
                scan["app"] = app.get_name() or ""
            except Exception:
                continue
            for top in tops:
                try:
                    title = top.get_name() or ""
                    tw = rect(top, atspi.CoordType.WINDOW)
                    ts = rect(top, atspi.CoordType.SCREEN)
                    active = self.state(top.get_state_set(), "ACTIVE")
                    window = match_window(pid, title, windows, len(tops),
                                          (tw[2], tw[3]) if tw else None, active)
                    if window is not None and str(window["id"]) not in selected_ids:
                        continue
                except Exception:
                    continue
                ctx = {"top": top, "tw": tw, "ts": ts, "window": window,
                       "wid": window["id"] if window else None}
                self.scan(top, ctx, scan)
        if not found_app:
            return {"elements": [], "absent": True}
        return self.compose(scan, query, roles, maximum)

    def scan(self, top, ctx, scan):
        atspi = self.atspi
        stack = [(top, None, ())]
        while stack and scan["visited"] < NODE_CAP and time.monotonic() < scan["deadline"]:
            node, viewport, docs = stack.pop()
            scan["visited"] += 1
            try:
                role = (node.get_role_name() or "unknown").lower()
                if role in DOC_ROLES:
                    states = node.get_state_set()
                    if states.contains(atspi.StateType.DEFUNCT) or not self.state(states, "SHOWING"):
                        scan["doc_seen"] = True
                        continue
                    box = rect(node, atspi.CoordType.WINDOW)
                    area = intersect(viewport, box) if viewport else box
                    if area is None:
                        continue
                    if scan["page"] is None:
                        scan["page"] = {"title": node.get_name() or "", "url": self.doc_url(node),
                                        "loading": self.state(states, "BUSY"), "vp": area,
                                        "ctx": ctx}
                    self.push_children(stack, node, area, docs + (node,))
                    continue
                if viewport is not None:
                    self.web_node(node, role, ctx, viewport, docs, scan, stack)
                else:
                    self.native_node(node, role, ctx, scan, stack)
            except Exception:
                continue

    def push_children(self, stack, node, viewport, docs):
        for child_index in reversed(range(min(node.get_child_count(), 500))):
            child = node.get_child_at_index(child_index)
            if child:
                stack.append((child, viewport, docs))

    def native_node(self, node, role, ctx, scan, stack):
        atspi = self.atspi
        if role in {"menu", "popup menu"} and not self.state(node.get_state_set(), "SHOWING"):
            return
        states = node.get_state_set()
        if states.contains(atspi.StateType.DEFUNCT):
            return
        name = node.get_name() or ""
        visible = states.contains(atspi.StateType.VISIBLE) and states.contains(atspi.StateType.SHOWING)
        actionable = role in INTERACTIVE_ROLES or states.contains(atspi.StateType.FOCUSABLE)
        if role == "page tab":
            scan["tabs"].append((name, self.state(states, "SELECTED")))
        elif role == "button" and name == "Stop":
            scan["loading"] = True
        elif role == "combo box" and visible and self.state(states, "EDITABLE") and scan["address"] is None:
            try:
                if (node.get_attributes() or {}).get("id") == "urlbar-input":
                    scan["address"] = self.value_of(node, "combo box") or ""
            except Exception:
                pass
        if visible and actionable:
            center = mapped_center(rect(node, atspi.CoordType.WINDOW), ctx["tw"], ctx["ts"],
                                   rect(node, atspi.CoordType.SCREEN), ctx["window"])
            row = {"role": role, "name": name[:300],
                   "x": center[0] if center else None, "y": center[1] if center else None,
                   "states": self.flags(states), "wid": ctx["wid"]}
            row["ref"] = self.remember(node, ctx, False)
            scan["native"].append(row)
        # Hidden intermediate containers sometimes have visible descendants;
        # traverse them without listing the container.
        for child_index in reversed(range(min(node.get_child_count(), 500))):
            child = node.get_child_at_index(child_index)
            if child:
                stack.append((child, None, ()))

    def web_node(self, node, role, ctx, viewport, docs, scan, stack):
        atspi = self.atspi
        interesting = role in WEB_ROLES
        heading = role == "heading"
        if not interesting and not heading and role not in CONTAINER_ROLES:
            states = node.get_state_set()
            interesting = self.state(states, "FOCUSABLE")
        if interesting or heading:
            states = node.get_state_set()
            if states.contains(atspi.StateType.DEFUNCT):
                return
            box = rect(node, atspi.CoordType.WINDOW)
            name = (node.get_name() or "").strip()
            if box is not None and (name or interesting and role not in {"link", "button", "push button"}):
                gbox = global_rect(box, ctx["tw"], ctx["window"])
                local_vis, dist = classify(box, viewport)
                row = {"role": role, "name": " ".join(name.split())[:100],
                       "vis": local_vis, "dist": round(dist, 1), "o": scan["visited"],
                       "wid": ctx["wid"]}
                if gbox is not None:
                    row["x"], row["y"] = round(gbox[0] + gbox[2] / 2), round(gbox[1] + gbox[3] / 2)
                else:
                    row["x"] = row["y"] = None
                if heading:
                    try:
                        row["level"] = int((node.get_attributes() or {}).get("level", 0))
                    except Exception:
                        row["level"] = 0
                    scan["headings"].append(row)
                else:
                    row["states"] = self.flags(states)
                    value = self.value_of(node, role) if role in VALUE_ROLES else None
                    if value:
                        row["value"] = value
                    row["ref"] = self.remember(node, ctx, True, docs)
                    if self.state(states, "FOCUSED"):
                        scan["focus"] = {"ref": row["ref"], "role": role, "name": row["name"]}
                    scan["web"].append(row)
        if role in NO_DESCEND:
            return
        self.push_children(stack, node, viewport, docs)

    def compose(self, scan, query, roles, maximum):
        page = scan["page"]
        result = {}
        if page is not None:
            pool = list(scan["web"])
            if query or roles:
                pool += [dict(row, chrome=True) for row in scan["native"]]
            keep = [row for row in pool if matches(query, row["role"], row["name"])
                    and (not roles or row["role"] in roles or alias(row["role"]) in roles)]
            inside = [row for row in keep if row.get("vis", "in") == "in"]
            outside = sorted((row for row in keep if row.get("vis", "in") != "in"),
                             key=lambda row: row["dist"])
            if not query and not roles:
                outside = outside[:OFFSCREEN_PEEK]   # a long page is not worth listing in full
            chosen = (inside + outside)[:maximum]
            chosen.sort(key=lambda row: (row.get("chrome", False), row.get("o", 0)))
            headings = []
            if not query and not roles:
                near = [h for h in scan["headings"] if h["name"] and h["dist"] <= 3]
                headings = sorted(near, key=lambda h: h["dist"])[:12]
                headings.sort(key=lambda h: h["o"])
            tabs = scan["tabs"]
            ctx = page["ctx"]
            vp = page["vp"]
            gvp = global_rect(vp, ctx["tw"], ctx["window"])
            result["page"] = {
                "app": scan.get("app", ""), "title": page["title"][:100], "url": page["url"][:300],
                "loading": bool(page["loading"] or scan["loading"]),
                "address": scan["address"], "tabs": [[name[:60], bool(active)] for name, active in tabs[:30]],
                "focus": scan["focus"],
                "vp": [round(v) for v in gvp] if gvp else None,
                "extra": len(keep) - len(chosen)}
            result["elements"] = chosen
            result["headings"] = headings
        elif scan.get("doc_seen") and not query and not roles:
            # A browser whose page is not showing yet (loading, tab switch): say so instead of
            # listing its toolbar as if it were the page.
            result["page"] = {"app": scan.get("app", ""), "title": "", "url": "", "loading": False, "notready": True,
                              "address": scan["address"], "tabs": [[name[:60], bool(active)] for name, active in scan["tabs"][:30]],
                              "focus": None, "vp": None, "extra": 0}
            result["elements"] = []
            result["headings"] = []
        else:
            rows = [row for row in scan["native"] if matches(query, row["role"], row["name"])
                    and (not roles or row["role"] in roles or alias(row["role"]) in roles)]
            result["elements"] = rows[:maximum]
        return result

    # -- window buttons
    def button(self, request):
        """Centre of a window's own close/minimize/maximize button, or {"found": false}."""
        atspi = self.atspi
        names = BUTTON_NAMES.get(request.get("action"))
        windows = request.get("windows", [])
        target = next((w for w in windows if str(w.get("id")) == str(request.get("window"))), None)
        if names is None or target is None:
            return {"found": False}
        desktop = atspi.get_desktop(0)
        for app_index in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(app_index)
                if app.get_process_id() != target.get("pid"):
                    continue
                tops = [app.get_child_at_index(i) for i in range(min(app.get_child_count(), 100))]
            except Exception:
                continue
            for top in tops:
                try:
                    tw = rect(top, atspi.CoordType.WINDOW)
                    window = match_window(target["pid"], top.get_name() or "", windows, len(tops),
                                          (tw[2], tw[3]) if tw else None, self.state(top.get_state_set(), "ACTIVE"))
                    if window is None or str(window["id"]) != str(target["id"]):
                        continue
                    found = self.find_button(top, tw, window, names)
                except Exception:
                    continue
                if found:
                    return {"found": True, "x": found[0], "y": found[1]}
        return {"found": False}

    def find_button(self, top, tw, window, names):
        atspi = self.atspi
        best = None
        queue = [(top, 0)]
        visited = 0
        while queue and visited < 800:
            node, depth = queue.pop(0)
            visited += 1
            try:
                role = (node.get_role_name() or "").lower()
                if role in {"button", "push button"} and (node.get_name() or "").strip().casefold() in names:
                    states = node.get_state_set()
                    box = global_rect(rect(node, atspi.CoordType.WINDOW), tw, window)
                    if (box and self.state(states, "VISIBLE") and self.state(states, "SHOWING")
                            and box[1] - window["y"] <= 80 and box[0] + box[2] / 2 > window["x"] + window["width"] / 2):
                        # Firefox also reports a "Close" for its hidden menu bar: take the right-most,
                        # then the largest (the tab strip's real one).
                        rank = (round(box[0] + box[2] / 2), box[2] * box[3])
                        if best is None or rank > best[0]:
                            best = (rank, (rank[0], round(box[1] + box[3] / 2)))
                    continue
                if depth < 8 and role not in {"document web", "document frame", "menu", "popup menu"}:
                    for index in range(min(node.get_child_count(), 60)):
                        child = node.get_child_at_index(index)
                        if child is not None:
                            queue.append((child, depth + 1))
            except Exception:
                continue
        return best[1] if best else None

    # -- live locate
    def locate(self, request):
        atspi = self.atspi
        record = self.refs.get(request.get("ref"))
        if record is None:
            return {"state": "gone"}
        node = record["node"]
        try:
            states = node.get_state_set()
            if states.contains(atspi.StateType.DEFUNCT):
                return {"state": "gone"}
            window = next((w for w in request.get("windows", [])
                           if str(w.get("id")) == str(record["wid"])), None)
            tw = rect(record["top"], atspi.CoordType.WINDOW)
            ts = rect(record["top"], atspi.CoordType.SCREEN)
            if record["web"]:
                box = rect(node, atspi.CoordType.WINDOW)
                if box is None:
                    return {"state": "unknown"}
                viewport = None
                for doc in record["docs"]:
                    area = rect(doc, atspi.CoordType.WINDOW)
                    viewport = area if viewport is None else intersect(viewport, area)
                if viewport is None:
                    return {"state": "unknown"}
                state, dist = classify(box, viewport)
                gbox = global_rect(box, tw, window)
                gvp = global_rect(viewport, tw, window)
                if gbox is None or gvp is None:
                    return {"state": "unknown"}
                return {"state": state, "dist": round(dist, 2),
                        "x": round(gbox[0] + gbox[2] / 2), "y": round(gbox[1] + gbox[3] / 2),
                        "vp": [round(v) for v in gvp]}
            if not (states.contains(atspi.StateType.VISIBLE) and states.contains(atspi.StateType.SHOWING)):
                return {"state": "unknown"}
            center = mapped_center(rect(node, atspi.CoordType.WINDOW), tw, ts,
                                   rect(node, atspi.CoordType.SCREEN), window)
            if center is None:
                return {"state": "unknown"}
            return {"state": "in", "x": center[0], "y": center[1]}
        except Exception:
            return {"state": "gone"}


def respond(response):
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    switch = Switch()
    if "--recover" in sys.argv:
        switch.recover()
        return
    reader = None

    def leave(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, leave)
    signal.signal(signal.SIGINT, leave)
    try:
        try:
            switch.recover()
        except Exception as exc:
            print(f"pcpilot a11y recover: {exc}", file=sys.stderr)
        for line in sys.stdin.buffer:
            try:
                if len(line) > MAX_LINE:
                    raise ValueError("request too long")
                request = json.loads(line)
                op = request.get("op", "snapshot")
                if op == "acquire":
                    response = switch.acquire()
                elif op == "release":
                    response = switch.release()
                else:
                    if reader is None:
                        reader = Reader()
                    response = (reader.locate(request) if op == "locate" else
                                reader.button(request) if op == "button" else reader.snapshot(request))
            except Exception as exc:
                print(f"pcpilot AT-SPI: {exc}", file=sys.stderr)
                response = {"error": str(exc)}
            respond(response)
    finally:
        # EOF (MCP gone, even SIGKILLed), SIGTERM or a crash: never leave a11y switched on.
        if switch.mine:
            try:
                switch.release()
            except Exception as exc:
                print(f"pcpilot a11y release: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
