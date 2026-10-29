// What the session did to the desktop, so the wrapup undoes exactly that and nothing else.
//
// - Snapshot (taken right before the ack): windows with state, focus, pointer, workspace.
//   `new` = created after it. That is a time fact, not a permission.
// - Authorship: a window is the AI's (closable by the wrapup) only if one of our ops
//   returned it, or it was created while an AI-driven op was running or within
//   ATTRIBUTION_MS after one. The user's input is blocked during a session, so that is
//   (almost always) the AI's doing; anything else new is left alone.
// - Journal: for pre-existing windows, the properties our ops changed and the value we
//   last applied. The wrapup restores a property only if it still holds that value.
// - Wrapup memory: windows already asked to close and windows that appeared during a
//   wrapup (e.g. "save changes?") are never sent a close (again).

import GLib from 'gi://GLib';

import {listWindows} from './windows.js';

export const ATTRIBUTION_MS = 3000;
const GEOMETRY_SLOP = 2; // px; clients may round sizes
export const PROPS = ['geometry', 'maximized', 'fullscreen', 'minimized'];

function nowMs() {
    return GLib.get_monotonic_time() / 1000;
}

function workspaceOf(w) {
    return w.is_on_all_workspaces() ? -1 : (w.get_workspace()?.index() ?? -1);
}

export function windowState(w) {
    const f = w.get_frame_rect();
    return {
        geometry: {x: f.x, y: f.y, width: f.width, height: f.height},
        maximized: w.is_maximized(),
        fullscreen: w.is_fullscreen(),
        minimized: w.minimized,
        workspace: workspaceOf(w),
    };
}

export function sameProp(prop, a, b) {
    if (prop !== 'geometry')
        return a === b;
    return Math.abs(a.x - b.x) <= GEOMETRY_SLOP && Math.abs(a.y - b.y) <= GEOMETRY_SLOP &&
        Math.abs(a.width - b.width) <= GEOMETRY_SLOP && Math.abs(a.height - b.height) <= GEOMETRY_SLOP;
}

export class SessionState {
    constructor() {
        this._snap = null;
        this._createdId = 0;
        this._reset();
    }

    _reset() {
        this._authored = new Set();
        this._spawnedInWrapup = new Set();
        this._attempted = new Set();
        this._journal = new Map(); // id -> Map(prop -> value we last applied)
        this._appliedWorkspace = null; // active workspace index we switched to, if any
        this._inFlight = 0;
        this._activityUntil = 0;
        this._wrapping = false;
    }

    get taken() {
        return this._snap !== null;
    }

    snapshot() {
        this.clear();
        const focus = global.display.focus_window;
        const [px, py] = global.get_pointer();
        this._snap = {
            windows: new Map(listWindows().map(w => [w.get_id(), windowState(w)])),
            focusId: focus?.get_id() ?? null,
            pointer: [px, py],
            workspace: global.workspace_manager.get_active_workspace_index(),
        };
        this._createdId = global.display.connect('window-created', (_d, w) => this._onCreated(w));
    }

    clear() {
        if (this._createdId) {
            global.display.disconnect(this._createdId);
            this._createdId = 0;
        }
        this._snap = null;
        this._reset();
    }

    _onCreated(w) {
        if (!this._snap)
            return;
        const id = w.get_id();
        if (this._wrapping)
            this._spawnedInWrapup.add(id); // a reaction to our close request: protected
        else if (this._inFlight > 0 || nowMs() <= this._activityUntil)
            this._authored.add(id);
    }

    // AI-driven ops (input, launch, window actions) bracket themselves with these.
    beginAiOp() {
        this._inFlight++;
    }

    endAiOp() {
        this._inFlight = Math.max(0, this._inFlight - 1);
        this._activityUntil = nowMs() + ATTRIBUTION_MS;
    }

    // An op returned this window as its result (e.g. app.launch): it is the AI's.
    author(w) {
        if (this._snap && this.isNew(w))
            this._authored.add(w.get_id());
    }

    setWrapping(on) {
        this._wrapping = on;
    }

    isNew(w) {
        return this._snap !== null && !this._snap.windows.has(w.get_id());
    }

    isAuthored(w) {
        return this.isNew(w) && this._authored.has(w.get_id());
    }

    spawnedInWrapup(w) {
        return this._spawnedInWrapup.has(w.get_id());
    }

    attempted(w) {
        return this._attempted.has(w.get_id());
    }

    markAttempted(w) {
        this._attempted.add(w.get_id());
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

    baseline(w) {
        return this._snap?.windows.get(w.get_id()) ?? null;
    }

    // After one of our ops changed `props` of a pre-existing window, remember the values
    // it now has (the state we applied).
    record(w, props) {
        if (!this._snap || this.isNew(w))
            return;
        const now = windowState(w);
        let entry = this._journal.get(w.get_id());
        if (!entry) {
            entry = new Map();
            this._journal.set(w.get_id(), entry);
        }
        for (const p of props)
            entry.set(p, now[p]);
    }

    recordWorkspace(index) {
        if (this._snap)
            this._appliedWorkspace = index;
    }

    get appliedWorkspace() {
        return this._appliedWorkspace;
    }

    // [[window, baseline, props]] for pre-existing windows with a property that we
    // changed, that still holds the value we applied, and that differs from the baseline.
    restorable() {
        if (!this._snap)
            return [];
        const out = [];
        for (const w of listWindows()) {
            const entry = this._journal.get(w.get_id());
            const base = this.baseline(w);
            if (!entry || !base)
                continue;
            const now = windowState(w);
            const props = [...entry].filter(([p, applied]) =>
                sameProp(p, now[p], applied) && !sameProp(p, now[p], base[p])).map(([p]) => p);
            if (props.length > 0)
                out.push([w, base, props]);
        }
        return out;
    }
}

// Put the given properties of a window back to the baseline. Order matters: leave
// fullscreen/maximized/minimized first, then geometry, then re-apply baseline modes.
export function restoreWindow(w, base, props) {
    const want = new Set(props);
    const now = windowState(w);
    if (want.has('minimized') && now.minimized && !base.minimized)
        w.unminimize();
    if (want.has('fullscreen') && now.fullscreen && !base.fullscreen)
        w.unmake_fullscreen();
    if (want.has('maximized') && now.maximized && !base.maximized)
        w.unmaximize();
    if (want.has('geometry') && !base.maximized && !base.fullscreen) {
        const g = base.geometry;
        w.move_resize_frame(true, g.x, g.y, g.width, g.height);
    }
    if (want.has('maximized') && base.maximized && !now.maximized)
        w.maximize();
    if (want.has('fullscreen') && base.fullscreen && !now.fullscreen)
        w.make_fullscreen();
    if (want.has('minimized') && base.minimized && !now.minimized)
        w.minimize();
}
