// Window helpers shared by the ops and the session snapshot/wrapup.

import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {E_BAD_ARGS, E_NOT_FOUND, OpError} from './protocol.js';

// The nearest point that lies on a monitor (layouts with gaps have off-screen areas
// inside the global bounding box).
export function clampToMonitors(x, y) {
    let best = null, bestD = Infinity;
    for (const m of Main.layoutManager.monitors) {
        const cx = Math.min(Math.max(x, m.x), m.x + m.width - 1);
        const cy = Math.min(Math.max(y, m.y), m.y + m.height - 1);
        const d = (cx - x) ** 2 + (cy - y) ** 2;
        if (d < bestD) {
            bestD = d;
            best = [cx, cy];
        }
    }
    return best ?? [x, y];
}

export function onMonitor(x, y) {
    return Main.layoutManager.monitors.some(m =>
        x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height);
}

export function rect(r) {
    return {x: r.x, y: r.y, width: r.width, height: r.height};
}

export function windowInfo(w) {
    const f = w.get_frame_rect();
    const b = w.get_buffer_rect();
    const app = Shell.WindowTracker.get_default().get_window_app(w);
    return {
        id: w.get_id(),
        title: w.get_title() ?? '',
        app_id: app?.get_id() ?? w.get_gtk_application_id() ?? w.get_wm_class() ?? '',
        pid: w.get_pid(),
        ...rect(f),
        buffer: rect(b),
        focused: w.has_focus(),
        minimized: w.minimized,
        maximized: w.is_maximized(),
        fullscreen: w.is_fullscreen(),
        monitor: w.get_monitor(),
        workspace: w.is_on_all_workspaces() ? -1 : (w.get_workspace()?.index() ?? -1),
    };
}

const LISTED_TYPES = new Set([
    Meta.WindowType.NORMAL, Meta.WindowType.DIALOG, Meta.WindowType.MODAL_DIALOG,
    Meta.WindowType.UTILITY,
]);

export function listWindows() {
    const wins = global.get_window_actors()
        .map(a => a.meta_window)
        .filter(w => w && !w.is_override_redirect() && LISTED_TYPES.has(w.get_window_type()));
    // Top of the stack first.
    return global.display.sort_windows_by_stacking(wins).reverse();
}

export function findWindow(id) {
    if (typeof id === 'string' && /^\d+$/.test(id))
        id = Number(id);
    if (typeof id !== 'number')
        throw new OpError(E_BAD_ARGS, 'id must be a window id');
    const w = listWindows().find(x => x.get_id() === id);
    if (!w)
        throw new OpError(E_NOT_FOUND, `no window ${id}`);
    return w;
}
