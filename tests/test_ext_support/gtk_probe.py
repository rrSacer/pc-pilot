#!/usr/bin/env python3
"""GTK4 probe window for the extension tests. Run inside the headless shell.

Logs JSON lines to $PROBE_LOG: widget layout (relative to the window's frame origin,
i.e. Mutter's frame_rect), clicks, key presses, scrolls, drags and text changes.
"""

import json
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

LOG = open(os.environ.get("PROBE_LOG", "/dev/stdout"), "a", buffering=1, encoding="utf-8")
TITLE = os.environ.get("PROBE_TITLE", "PCPilot Ext Probe")


def log(**kv):
    LOG.write(json.dumps(kv, ensure_ascii=False) + "\n")


def bounds(widget, window):
    ok, r = widget.compute_bounds(window)
    if not ok:
        return None
    return [round(r.origin.x), round(r.origin.y), round(r.size.width), round(r.size.height)]


def on_activate(app):
    win = Gtk.ApplicationWindow(application=app, title=TITLE)
    win.set_default_size(800, 600)
    window_motion = Gtk.EventControllerMotion()
    window_motion.connect("motion", lambda c, x, y: log(ev="motion", source="window", x=x, y=y, t=time.monotonic()))
    win.add_controller(window_motion)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    for m in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{m}")(12)

    entry = Gtk.Entry()
    entry.connect("changed", lambda e: log(ev="entry", text=e.get_text(), t=time.monotonic()))
    box.append(entry)

    tv = Gtk.TextView()
    tv.get_buffer().connect("changed", lambda b: log(
        ev="textview", text=b.get_text(b.get_start_iter(), b.get_end_iter(), False)))
    sw = Gtk.ScrolledWindow()
    sw.set_min_content_height(120)
    sw.set_child(tv)
    box.append(sw)

    button = Gtk.Button(label="Clique")
    click = Gtk.GestureClick(button=0)
    click.connect("pressed", lambda g, n, x, y: log(
        ev="click", n=n, button=g.get_current_button(), x=round(x), y=round(y),
        state=int(g.get_current_event_state())))
    button.add_controller(click)
    box.append(button)

    area = Gtk.DrawingArea()
    area.set_content_width(400)
    area.set_content_height(200)
    area.set_draw_func(lambda a, cr, w, h: (cr.set_source_rgb(0.85, 0.9, 0.85), cr.paint()))
    drag = Gtk.GestureDrag()
    drag.connect("drag-begin", lambda g, x, y: log(ev="drag_begin", x=round(x), y=round(y)))
    drag.connect("drag-end", lambda g, dx, dy: log(ev="drag_end", dx=round(dx), dy=round(dy)))
    area.add_controller(drag)
    area_click = Gtk.GestureClick(button=0)
    area_click.connect("pressed", lambda g, n, x, y: log(
        ev="area_click", n=n, button=g.get_current_button(), x=round(x), y=round(y),
        state=int(g.get_current_event_state())))
    area.add_controller(area_click)
    scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES |
                                       Gtk.EventControllerScrollFlags.DISCRETE)
    scroll.connect("scroll", lambda c, dx, dy: (log(ev="scroll", dx=dx, dy=dy, t=time.monotonic()), False)[1])
    area.add_controller(scroll)
    box.append(area)

    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", lambda c, kv, code, st: (log(
        ev="key", name=Gdk.keyval_name(kv), code=code,
        ctrl=bool(st & Gdk.ModifierType.CONTROL_MASK),
        shift=bool(st & Gdk.ModifierType.SHIFT_MASK),
        alt=bool(st & Gdk.ModifierType.ALT_MASK)), False)[1])
    win.add_controller(keys)

    win.set_child(box)

    def report():
        layout = {name: bounds(w, win) for name, w in
                  (("entry", entry), ("textview", tv), ("button", button), ("area", area))}
        sx, sy = win.get_surface_transform()  # shadow margin inside the surface
        log(ev="layout", pid=os.getpid(), title=TITLE, widgets=layout,
            window=[win.get_width(), win.get_height()], surface_transform=[sx, sy])
        return False

    def dialog(title):
        d = Gtk.Window(title=title, transient_for=win, modal=True)
        d.set_default_size(320, 160)
        d.set_child(Gtk.Label(label=title))
        d.connect("close-request", lambda _d: (log(ev="dialog_close", title=title), False)[1])
        d.present()
        log(ev="dialog", title=title)
        return d

    # PROBE_DIALOG=1: open a modal dialog transient for this window once it is mapped.
    if os.environ.get("PROBE_DIALOG") == "1":
        win.connect("map", lambda w: GLib.timeout_add(500, lambda: (dialog(TITLE + " Dialog"), False)[1]))

    # PROBE_CONFIRM_CLOSE=1: refuse the first close and ask "save changes?" in a modal
    # dialog instead (like an editor with unsaved text); later closes go through.
    state = {"asked": False}

    def on_close(_w):
        log(ev="close_request", asked=state["asked"])
        if os.environ.get("PROBE_CONFIRM_CLOSE") == "1" and not state["asked"]:
            state["asked"] = True
            # PROBE_CONFIRM_DELAY_MS: a slow app that asks only after a while.
            delay = int(os.environ.get("PROBE_CONFIRM_DELAY_MS", "0"))
            if delay > 0:
                GLib.timeout_add(delay, lambda: (dialog(TITLE + " Save?"), False)[1])
            else:
                dialog(TITLE + " Save?")
            return True
        return False

    win.connect("close-request", on_close)
    win.connect("map", lambda w: GLib.timeout_add(300, report))
    win.present()
    entry.grab_focus()


from gi.repository import Gio  # noqa: E402

app = Gtk.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
app.connect("activate", on_activate)
sys.exit(app.run([]))
