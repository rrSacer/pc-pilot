#!/usr/bin/env python3
"""Full-stage screenshot through the headless shell's own org.gnome.Shell.Screenshot,
i.e. WITH the pcpilot overlay/toast visible (the extension hides it from its own shots).

Must run inside the headless session (dev/headless-shell.sh run DIR -- python3 this.py OUT.png).
The shell only serves allow-listed bus names, so we claim
org.gnome.SettingsDaemon.MediaKeys first (no gsd runs in the headless session);
this is the private dbus-run-session bus, never the real desktop's.
"""

import sys

from gi.repository import Gio, GLib

out = sys.argv[1]
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
              "RequestName", GLib.Variant("(su)", ("org.gnome.SettingsDaemon.MediaKeys", 4)),
              None, Gio.DBusCallFlags.NONE, -1, None)
res = bus.call_sync("org.gnome.Shell.Screenshot", "/org/gnome/Shell/Screenshot",
                    "org.gnome.Shell.Screenshot", "Screenshot",
                    GLib.Variant("(bbs)", (False, False, out)),
                    GLib.VariantType("(bs)"), Gio.DBusCallFlags.NONE, 10000, None)
ok, path = res.unpack()
if not ok:
    sys.exit("screenshot failed")
print(path)
