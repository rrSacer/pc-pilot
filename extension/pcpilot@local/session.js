// Baseline state of the desktop when a session starts (taken right before the ack), so
// the session can tell which windows it created and put back what it changed.

import {listWindows} from './windows.js';

const GEOMETRY_SLOP = 2; // px; clients may round sizes

function workspaceOf(w) {
    return w.is_on_all_workspaces() ? -1 : (w.get_workspace()?.index() ?? -1);
}

export function windowState(w) {
    const f = w.get_frame_rect();
    return {
        x: f.x, y: f.y, width: f.width, height: f.height,
        maximized: w.is_maximized(),
        fullscreen: w.is_fullscreen(),
        minimized: w.minimized,
        workspace: workspaceOf(w),
    };
}

function sameState(a, b) {
    if (a.maximized !== b.maximized || a.fullscreen !== b.fullscreen ||
        a.minimized !== b.minimized || a.workspace !== b.workspace)
        return false;
    if (a.maximized || a.fullscreen)
        return true; // geometry is the monitor's then
    return Math.abs(a.x - b.x) <= GEOMETRY_SLOP && Math.abs(a.y - b.y) <= GEOMETRY_SLOP &&
        Math.abs(a.width - b.width) <= GEOMETRY_SLOP && Math.abs(a.height - b.height) <= GEOMETRY_SLOP;
}

export class SessionState {
    constructor() {
        this._snap = null;
    }

    get taken() {
        return this._snap !== null;
    }

    snapshot() {
        const focus = global.display.focus_window;
        const [px, py] = global.get_pointer();
        this._snap = {
            windows: new Map(listWindows().map(w => [w.get_id(), windowState(w)])),
            focusId: focus?.get_id() ?? null,
            pointer: [px, py],
            workspace: global.workspace_manager.get_active_workspace_index(),
        };
    }

    clear() {
        this._snap = null;
    }

    // Created after the snapshot (i.e. during this session).
    isNew(w) {
        return this._snap !== null && !this._snap.windows.has(w.get_id());
    }

    get focusId() {
        return this._snap?.focusId ?? null;
    }

    get pointer() {
        return this._snap?.pointer ?? null;
    }

    get workspace() {
        return this._snap?.workspace ?? null;
    }

    // Pre-existing windows whose state differs from the snapshot: [[window, baseline]].
    changed() {
        if (!this._snap)
            return [];
        const out = [];
        for (const w of listWindows()) {
            const base = this._snap.windows.get(w.get_id());
            if (base && !sameState(base, windowState(w)))
                out.push([w, base]);
        }
        return out;
    }
}

// Put a window back to a baseline state. Order matters: leave fullscreen/maximized/
// minimized first, then geometry and workspace, then re-apply the baseline modes.
export function restoreWindow(w, base) {
    const now = windowState(w);
    if (now.minimized)
        w.unminimize();
    if (now.fullscreen && !base.fullscreen)
        w.unmake_fullscreen();
    if (now.maximized && !base.maximized)
        w.unmaximize();
    if (base.workspace >= 0 && now.workspace !== base.workspace)
        w.change_workspace_by_index(base.workspace, false);
    if (!base.maximized && !base.fullscreen)
        w.move_resize_frame(true, base.x, base.y, base.width, base.height);
    if (base.maximized && !now.maximized)
        w.maximize();
    if (base.fullscreen && !now.fullscreen)
        w.make_fullscreen();
    if (base.minimized)
        w.minimize();
}
