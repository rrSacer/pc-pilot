// What the session did to the desktop, so the wrapup undoes exactly that and nothing else.
//
// - Snapshot (taken right before the ack): windows with state, focus, pointer, workspace.
//   `new` = created after it. That is a time fact, not a permission.
// - Authorship: neither time nor app ids are authorization (apps open windows by
//   themselves while the AI works, and other processes share app ids). A window is the
//   AI's (closable by the wrapup) only if
//   (a) app.launch/open_uri returned it to the AI (it matched a process we spawned), or
//   (b) its process is one we spawned, or a descendant of one.
//   Everything else created during the session stays `new` but is left alone, including
//   further windows of D-Bus activated or already-running apps: the AI closes those.
// - Journal: for pre-existing windows, the properties our ops changed and the value we
//   last applied. The wrapup restores a property only if it still holds that value.
// - Wrapup memory: windows already asked to close and windows that appeared during a
//   wrapup (e.g. "save changes?") are never sent a close (again).

import GLib from 'gi://GLib';

import {listWindows} from './windows.js';

const GEOMETRY_SLOP = 2; // px; clients may round sizes
export const PROPS = ['geometry', 'maximized', 'fullscreen', 'minimized'];

function workspaceOf(w) {
    return w.is_on_all_workspaces() ? -1 : (w.get_workspace()?.index() ?? -1);
}

// Parent pid from /proc (an in-memory read), or 0.
function parentPid(pid) {
    try {
        const [ok, bytes] = GLib.file_get_contents(`/proc/${pid}/stat`);
        if (!ok)
            return 0;
        const stat = new TextDecoder().decode(bytes);
        // "pid (comm) state ppid ...": comm may contain spaces/parens, so split after it.
        const rest = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
        return Number(rest[1]) || 0;
    } catch {
        return 0;
    }
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
        this._spawned = new Set(); // pids the session spawned
        this._pidVerdict = new Map(); // pid -> descends from a spawned pid (cache)
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
        // Only to protect windows born during a wrapup (e.g. "save changes?"); creation
        // time never makes a window the AI's.
        if (this._snap && this._wrapping)
            this._spawnedInWrapup.add(w.get_id());
    }

    // app.launch/open_uri returned this new window to the AI.
    author(w) {
        if (this._snap && this.isNew(w))
            this._authored.add(w.get_id());
    }

    // The session spawned this process (app.launch/open_uri).
    launchedPid(pid) {
        if (this._snap && pid > 0) {
            this._spawned.add(pid);
            this._pidVerdict.clear();
        }
    }

    // Is pid one we spawned, or a descendant of one?
    spawnedByUs(pid) {
        if (pid <= 0 || this._spawned.size === 0)
            return false;
        if (this._pidVerdict.has(pid))
            return this._pidVerdict.get(pid);
        let verdict = false;
        for (let p = pid, depth = 0; p > 1 && depth < 32; p = parentPid(p), depth++) {
            if (this._spawned.has(p)) {
                verdict = true;
                break;
            }
        }
        this._pidVerdict.set(pid, verdict);
        return verdict;
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
        return this._authored.has(w.get_id()) || this.spawnedByUs(w.get_pid());
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
