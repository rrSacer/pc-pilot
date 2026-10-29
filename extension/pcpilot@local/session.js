// What the session did to the desktop, so the wrapup undoes exactly that and nothing else.
//
// - Snapshot (taken right before the ack): windows with state, focus, pointer, workspace.
//   `new` = created after it. That is a time fact, not a permission.
// - Authorship: a window is the AI's (closable by the wrapup) only if
//   (a) one of our ops returned it (app.launch), or
//   (b) it was created while an AI-driven op was running or within ATTRIBUTION_MS after
//       one, AND it belongs to the same application as that op's target: the app/pid of
//       the focused window or of the window under the pointer when the op started or
//       ended, or an app the session launched/opened.
//   Windows of any other application stay `new` but are left alone. Residual: a window
//   the same application opens on its own during an AI op is still taken as the AI's.
// - Journal: for pre-existing windows, the properties our ops changed and the value we
//   last applied. The wrapup restores a property only if it still holds that value.
// - Wrapup memory: windows already asked to close and windows that appeared during a
//   wrapup (e.g. "save changes?") are never sent a close (again).

import GLib from 'gi://GLib';

import {listWindows, windowInfo} from './windows.js';

export const ATTRIBUTION_MS = 3000;
const GEOMETRY_SLOP = 2; // px; clients may round sizes
export const PROPS = ['geometry', 'maximized', 'fullscreen', 'minimized'];

function nowMs() {
    return GLib.get_monotonic_time() / 1000;
}

function workspaceOf(w) {
    return w.is_on_all_workspaces() ? -1 : (w.get_workspace()?.index() ?? -1);
}

// Keys identifying a window's application: its app id (unless the tracker only made up a
// per-window one) and its process.
export function windowKeys(w) {
    const keys = [];
    const appId = windowInfo(w).app_id;
    if (appId && !appId.startsWith('window:'))
        keys.push(`app:${appId}`);
    const pid = w.get_pid();
    if (pid > 0)
        keys.push(`pid:${pid}`);
    return keys;
}

// The AI's current targets: the focused window and the window under the pointer.
function targetKeys() {
    const keys = new Set();
    const focus = global.display.focus_window;
    if (focus)
        windowKeys(focus).forEach(k => keys.add(k));
    const [px, py] = global.get_pointer();
    const under = listWindows().find(w => {
        if (w.minimized || !w.showing_on_its_workspace())
            return false;
        const f = w.get_frame_rect();
        return px >= f.x && px < f.x + f.width && py >= f.y && py < f.y + f.height;
    });
    if (under)
        windowKeys(under).forEach(k => keys.add(k));
    return keys;
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
        this._authored = new Set(); // explicitly returned by our ops
        this._candidates = new Map(); // id -> Set of target keys active at its creation
        this._attribution = []; // [{inFlight, until, targets: Set}]
        this._launched = new Set(); // target keys of apps the session launched/opened
        this._spawnedInWrapup = new Set();
        this._attempted = new Set();
        this._journal = new Map(); // id -> Map(prop -> value we last applied)
        this._appliedWorkspace = null; // active workspace index we switched to, if any
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
        if (this._wrapping) {
            this._spawnedInWrapup.add(id); // a reaction to our close request: protected
            return;
        }
        const now = nowMs();
        this._attribution = this._attribution.filter(e => e.inFlight || now <= e.until);
        if (this._attribution.length === 0)
            return;
        // The window's app is often unknown this early; remember the targets and match
        // lazily (isAuthored).
        const targets = new Set(this._launched);
        for (const e of this._attribution)
            e.targets.forEach(k => targets.add(k));
        this._candidates.set(id, targets);
    }

    // AI-driven ops (input, launch, window actions) bracket themselves with these.
    beginAiOp() {
        const entry = {inFlight: true, until: 0, targets: targetKeys()};
        this._attribution.push(entry);
        return entry;
    }

    endAiOp(entry) {
        if (!entry)
            return;
        targetKeys().forEach(k => entry.targets.add(k)); // e.g. what a click landed on
        entry.inFlight = false;
        entry.until = nowMs() + ATTRIBUTION_MS;
    }

    // An op returned this window as its result (e.g. app.launch): it is the AI's, and so
    // is its application from now on.
    author(w) {
        if (this._snap && this.isNew(w)) {
            this._authored.add(w.get_id());
            windowKeys(w).forEach(k => this._launched.add(k));
        }
    }

    // The session launched/opened this application (app.launch, open_uri's handler).
    launchedApp(appId) {
        if (this._snap && appId)
            this._launched.add(`app:${appId}`);
    }

    setWrapping(on) {
        this._wrapping = on;
    }

    isNew(w) {
        return this._snap !== null && !this._snap.windows.has(w.get_id());
    }

    isAuthored(w) {
        if (!this.isNew(w))
            return false;
        if (this._authored.has(w.get_id()))
            return true;
        const targets = this._candidates.get(w.get_id());
        return targets !== undefined && windowKeys(w).some(k => targets.has(k));
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

    // After one of our ops ran on a pre-existing window, journal the properties (among
    // `props`) that it actually changed since `before`, with the value it applied.
    record(w, before, props = PROPS) {
        if (!this._snap || this.isNew(w) || !before)
            return;
        const now = windowState(w);
        const changed = props.filter(p => !sameProp(p, before[p], now[p]));
        if (changed.length === 0)
            return;
        let entry = this._journal.get(w.get_id());
        if (!entry) {
            entry = new Map();
            this._journal.set(w.get_id(), entry);
        }
        for (const p of changed)
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
