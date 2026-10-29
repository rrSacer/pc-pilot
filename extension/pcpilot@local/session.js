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

// A process's identity and parent from /proc/<pid>/stat (an in-memory read): the pid
// alone is not an identity (pids are reused), the pair (pid, starttime) is. Returns
// {key: "pid:starttime", ppid} or null if the process is gone or unreadable.
export function procIdentity(pid) {
    if (!(pid > 0))
        return null;
    try {
        const [ok, bytes] = GLib.file_get_contents(`/proc/${pid}/stat`);
        if (!ok)
            return null;
        const stat = new TextDecoder().decode(bytes);
        // "pid (comm) state ppid ... starttime ...": comm may contain spaces/parens, so
        // split after its last ')'. Fields from there: state=3, ppid=4, starttime=22.
        const rest = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
        const ppid = Number(rest[1]);
        const start = rest[19];
        if (!Number.isInteger(ppid) || !/^\d+$/.test(start ?? ''))
            return null;
        return {key: `${pid}:${start}`, ppid};
    } catch {
        return null;
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
        this._spawned = new Set(); // identities ("pid:starttime") of processes we spawned
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

    // The session spawned this process (app.launch/open_uri): called from the launch's pid
    // callback, right after the fork, to capture its identity before the pid can be reused.
    // A process already gone or unreadable is not recorded (its windows stay the user's).
    launchedPid(pid) {
        if (!this._snap)
            return;
        const id = procIdentity(pid);
        if (id)
            this._spawned.add(id.key);
    }

    // Is this pid, right now, a process we spawned or a live descendant of one? The
    // identity chain is re-read every time (no cached positives: a pid may be reused or
    // re-parented at any moment); anything unknown, exited or unreadable is not ours.
    spawnedByUs(pid) {
        if (this._spawned.size === 0)
            return false;
        for (let p = pid, depth = 0; p > 1 && depth < 32; depth++) {
            const id = procIdentity(p);
            if (!id)
                return false;
            if (this._spawned.has(id.key))
                return true;
            p = id.ppid;
        }
        return false;
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
