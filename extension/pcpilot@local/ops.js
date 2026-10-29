// UI op handlers. Coordinates are logical stage pixels. Every handler is async and
// yields to the main loop between steps; long ones check the session token so that
// `state inactive` cancels them mid-way.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Keyboard from 'resource:///org/gnome/shell/ui/status/keyboard.js';

import {BUTTONS, Input} from './input.js';
import {MODIFIER_KEYVALS, charToKeyval, parseChord} from './keys.js';
import {animate, glide} from './motion.js';
import {STYLES, minJerk, planPath, scrollAnchor, scrollCadence, typingCadence} from './path.js';
import {E_BAD_ARGS, E_FAILED, E_INACTIVE, E_NOT_FOUND, E_UNKNOWN_OP, OpError} from './protocol.js';
import {SessionState, restoreWindow, sameProp, windowState} from './session.js';
import {clampToMonitors, findWindow, listWindows, rect, windowInfo} from './windows.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot');
Gio._promisify(Shell.Screenshot.prototype, 'screenshot_area');
Gio._promisify(Gio.File.prototype, 'replace_async');
Gio._promisify(Gio.File.prototype, 'read_async');
Gio._promisify(Gio.File.prototype, 'delete_async');
Gio._promisify(Gio.File.prototype, 'make_directory_async');
Gio._promisify(Gio.File.prototype, 'query_info_async');
Gio._promisify(Gio.File.prototype, 'set_attributes_async');
Gio._promisify(Gio.InputStream.prototype, 'read_bytes_async');
Gio._promisify(Gio.OutputStream.prototype, 'close_async');
Gio._promisify(Gio.AppInfo, 'launch_default_for_uri_async');
Gio._promisify(Gio.File.prototype, 'query_default_handler_async');

const KEEP_SCREENSHOTS = 10;
const MAX_TEXT = 20000;
const IM_CHUNK = 64;
const KEYVAL_YIELD_EVERY = 16;
const CHANNEL_SWITCH_MS = 40; // IM commits and key events travel different paths
const PASTE_SETTLE_MS = 300;
const CLIPBOARD_READ_MS = 2000;
// key.type must finish well inside the per-op limit (extension 18s, daemon 20s).
const MAX_TYPE_MS = 15000;
const MAX_LAUNCH_WAIT_MS = 15000;
const HUMAN_TYPING_EXTRA_MS = 4000;
const GEOMETRY_MS = {human: 250, fast: 120, instant: 0};
const WRAPUP_CAP_MS = 8000;
const CLOSE_WAIT_MS = 1500;
const MIN_WINDOW_PX = 80;

// Layouts whose current group types plain ASCII through keyvals (the virtual keyboard
// can only press keyvals present in the active layout; there is no keymap query API).
const LATIN_LAYOUT = /^(us|br|gb|pt|es|latam|it|de|fr|ca|ie|nl|dk|no|se|fi|ch|be|pl|cz|sk|hu|ro|tr|ee|lv|lt|si|hr|is|za|au|nz|epo)\b/;
// ASCII characters that the layout only has on a third/fourth level or as dead keys:
// Mutter's keyval picker cannot type them (shift+AltGr is "Unhandled level"), so they
// go through the clipboard when there is no input-method focus.
const LAYOUT_UNSAFE = {
    br: '`~^<>',
    pt: '`~^',
    es: '`^',
    latam: '`^~',
    de: '^`',
    fr: '^`~',
};
const LAYOUT_EXTRA = {
    br: 'çÇ',
    pt: 'çÇ',
    es: 'ñÑçÇ',
    latam: 'ñÑ',
    de: 'äöüÄÖÜß',
};
const TERMINAL = /terminal|ptyxis|console|kgx|kitty|alacritty|foot|wezterm|tilix|konsole|xterm|ghostty|terminator|blackbox/i;

const MODIFIER_NAMES = Object.keys(MODIFIER_KEYVALS);

function num(v, name, {min = -Infinity, max = Infinity, int = false, def} = {}) {
    if (v === undefined || v === null) {
        if (def !== undefined)
            return def;
        throw new OpError(E_BAD_ARGS, `${name} is required`);
    }
    if (typeof v !== 'number' || !Number.isFinite(v))
        throw new OpError(E_BAD_ARGS, `${name} must be a number`);
    if (int && !Number.isInteger(v))
        throw new OpError(E_BAD_ARGS, `${name} must be an integer`);
    if (v < min || v > max)
        throw new OpError(E_BAD_ARGS, `${name} must be in [${min}, ${max}]`);
    return v;
}

function point(p, name) {
    if (!p || typeof p !== 'object')
        throw new OpError(E_BAD_ARGS, `${name} must be {x,y}`);
    const x = num(p.x, `${name}.x`), y = num(p.y, `${name}.y`);
    Input.checkPoint(x, y);
    return [x, y];
}

function optionalPoint(args) {
    if (args.x === undefined && args.y === undefined)
        return null;
    return point(args, 'point');
}

// Motion style: "human" (default) | "fast" | "instant".
function styleOf(args) {
    const s = args.style ?? 'human';
    if (!STYLES.includes(s))
        throw new OpError(E_BAD_ARGS, `style must be one of ${STYLES.join(', ')}`);
    return s;
}

function randomBetween(lo, hi) {
    return lo + Math.random() * (hi - lo);
}

function str(v, name, {optional = false} = {}) {
    if (v === undefined || v === null) {
        if (optional)
            return null;
        throw new OpError(E_BAD_ARGS, `${name} is required`);
    }
    if (typeof v !== 'string')
        throw new OpError(E_BAD_ARGS, `${name} must be a string`);
    return v;
}

function clipboardGetText() {
    return new Promise(resolve => {
        St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD, (_cb, text) => resolve(text));
    });
}

function clipboardGetContent(mimetype) {
    return new Promise(resolve => {
        St.Clipboard.get_default().get_content(St.ClipboardType.CLIPBOARD, mimetype,
            (_cb, bytes) => resolve(bytes));
    });
}

// Resolves with `promise`, but rejects as soon as `token` is cancelled or after `ms`
// (for calls that cannot be cancelled, e.g. reading a selection owned by another app).
function bounded(promise, token, ms, what) {
    return new Promise((resolve, reject) => {
        let id = 0, dispose = null;
        const finish = (fn, v) => {
            if (id)
                GLib.source_remove(id);
            id = 0;
            dispose?.();
            fn(v);
        };
        id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            id = 0;
            finish(reject, new OpError(E_FAILED, `${what} timed out`));
            return GLib.SOURCE_REMOVE;
        });
        dispose = token.onCancel(() => finish(reject, new OpError(E_INACTIVE, 'session ended')));
        promise.then(v => finish(resolve, v), e => finish(reject, e));
    });
}

function plainSleep(ms) {
    return new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
        resolve();
        return GLib.SOURCE_REMOVE;
    }));
}

async function pngSize(file) {
    const stream = await file.read_async(GLib.PRIORITY_DEFAULT, null);
    try {
        const bytes = (await stream.read_bytes_async(24, GLib.PRIORITY_DEFAULT, null)).toArray();
        if (bytes.length < 24 || bytes[1] !== 0x50 || bytes[2] !== 0x4e || bytes[3] !== 0x47)
            throw new OpError(E_FAILED, 'screenshot is not a PNG');
        const be = o => ((bytes[o] << 24) | (bytes[o + 1] << 16) | (bytes[o + 2] << 8) | bytes[o + 3]) >>> 0;
        return [be(16), be(20)];
    } finally {
        stream.close(null);
    }
}

export class Ops {
    constructor({overlay, input}) {
        this._overlay = overlay;
        this._input = input;
        this._shots = [];
        this._shotSeq = 0;
        this.session = new SessionState();
        this._handlers = {
            'screen.info': this._screenInfo,
            'screenshot': this._screenshot,
            'pointer.move': this._pointerMove,
            'pointer.click': this._pointerClick,
            'pointer.drag': this._pointerDrag,
            'pointer.scroll': this._pointerScroll,
            'pointer.position': this._pointerPosition,
            'key.press': this._keyPress,
            'key.type': this._keyType,
            'app.search': this._appSearch,
            'app.launch': this._appLaunch,
            'open_uri': this._openUri,
            'window.list': this._windowList,
            'window.action': this._windowAction,
            'clipboard.get': this._clipboardGet,
            'clipboard.set': this._clipboardSet,
            'status': this._status,
            'session.wrapup': this._wrapup,
        };
    }

    run(op, args, token) {
        const h = this._handlers[op];
        if (!h)
            throw new OpError(E_UNKNOWN_OP, `unknown op: ${op}`);
        if (args === null || typeof args !== 'object' || Array.isArray(args))
            throw new OpError(E_BAD_ARGS, 'args must be an object');
        return h.call(this, args, token);
    }

    // ---- screen ----

    _screenInfo() {
        const lm = Main.layoutManager;
        return {
            width: global.screen_width,
            height: global.screen_height,
            monitors: lm.monitors.map(m => ({
                x: m.x, y: m.y, width: m.width, height: m.height,
                scale: m.geometry_scale,
                primary: m.index === lm.primaryIndex,
            })),
        };
    }

    async _screenshot(args, token) {
        let area = null;
        if (args.region !== undefined && args.region !== null) {
            const r = args.region;
            if (typeof r !== 'object')
                throw new OpError(E_BAD_ARGS, 'region must be {x,y,width,height}');
            let x = num(r.x, 'region.x'), y = num(r.y, 'region.y');
            let w = num(r.width, 'region.width', {min: 1}), h = num(r.height, 'region.height', {min: 1});
            // Clamp to the screen (model-space rounding can overshoot by a pixel).
            const x2 = Math.min(Math.round(x + w), global.screen_width);
            const y2 = Math.min(Math.round(y + h), global.screen_height);
            x = Math.max(0, Math.round(x));
            y = Math.max(0, Math.round(y));
            w = x2 - x;
            h = y2 - y;
            if (w <= 0 || h <= 0)
                throw new OpError(E_BAD_ARGS, 'region does not intersect the screen');
            const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
            area = [x * scale, y * scale, w * scale, h * scale];
        }
        const cursor = args.cursor === true;
        // Debug/test only: capture with the overlay visible to verify it is drawn.
        const includeOverlay = args.include_overlay === true;
        token.check();

        const dir = await this._shotDir(token);
        const path = GLib.build_filenamev([dir, `shot-${GLib.get_real_time()}-${++this._shotSeq}.png`]);
        const file = Gio.File.new_for_path(path);
        const stream = await file.replace_async(null, false, Gio.FileCreateFlags.PRIVATE,
            GLib.PRIORITY_DEFAULT, token.cancellable);
        let ok = false;
        try {
            const shooter = new Shell.Screenshot();
            const capture = async () => {
                token.check(); // never capture for a session that is over
                if (area)
                    await shooter.screenshot_area(...area, stream);
                else
                    await shooter.screenshot(cursor, stream);
            };
            if (includeOverlay) {
                await capture();
            } else {
                await this._overlay.withHidden(async () => {
                    await this._overlay.waitPaint();
                    await capture();
                });
            }
            await stream.close_async(GLib.PRIORITY_DEFAULT, null);
            token.check();
            const [width, height] = await pngSize(file);
            token.check();
            ok = true;
            this._rememberShot(file);
            return {path, width, height};
        } finally {
            if (!ok) {
                await stream.close_async(GLib.PRIORITY_DEFAULT, null).catch(() => {});
                await file.delete_async(GLib.PRIORITY_DEFAULT, null).catch(() => {});
            }
        }
    }

    // $XDG_RUNTIME_DIR/pcpilot, created 0700; refuses anything but a real directory owned
    // by us (no symlink). The runtime dir itself belongs to the desktop user anyway.
    async _shotDir(token) {
        const dir = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'pcpilot']);
        const file = Gio.File.new_for_path(dir);
        try {
            await file.make_directory_async(GLib.PRIORITY_DEFAULT, token.cancellable);
        } catch (e) {
            if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS))
                throw new OpError(E_FAILED, `cannot create ${dir}: ${e.message}`);
        }
        const info = await file.query_info_async('standard::type,unix::uid,unix::mode',
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, GLib.PRIORITY_DEFAULT, token.cancellable);
        if (info.get_file_type() !== Gio.FileType.DIRECTORY ||
            info.get_attribute_uint32('unix::uid') !== new Gio.Credentials().get_unix_user())
            throw new OpError(E_FAILED, `${dir} is not a directory owned by this user`);
        if ((info.get_attribute_uint32('unix::mode') & 0o077) !== 0) {
            const mode = new Gio.FileInfo();
            mode.set_attribute_uint32('unix::mode', 0o700);
            await file.set_attributes_async(mode, Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                GLib.PRIORITY_DEFAULT, token.cancellable);
        }
        return dir;
    }

    _rememberShot(file) {
        this._shots.push(file);
        while (this._shots.length > KEEP_SCREENSHOTS)
            this._shots.shift().delete_async(GLib.PRIORITY_LOW, null).catch(() => {});
    }

    // ---- pointer ----

    // Absolute motion is applied asynchronously by Mutter's input thread, and the very
    // first event of a fresh virtual pointer can land with only one axis applied: verify
    // through the real pointer position and resend if needed.
    async _moveTo(x, y, token) {
        for (let attempt = 0; attempt < 4; attempt++) {
            this._input.move(x, y);
            for (let i = 0; i < 5; i++) {
                await token.sleep(4);
                const [px, py] = global.get_pointer();
                if (Math.abs(px - x) <= 1 && Math.abs(py - y) <= 1)
                    return;
            }
        }
        console.warn(`pcpilot: pointer did not reach ${x},${y}`);
    }

    // Glide along a human/fast path, then land exactly (also covers the first-event quirk).
    async _glideTo(x, y, style, token, opts = {}) {
        this._overlay.engageCursor?.();
        if (style !== 'instant') {
            const [px, py] = global.get_pointer();
            const path = planPath([px, py], [x, y], style, opts);
            if (path.duration > 0)
                await glide(this._input, path, token);
        }
        await this._moveTo(x, y, token);
    }

    async _pointerMove(args, token) {
        const [x, y] = point(args, 'point');
        await this._glideTo(x, y, styleOf(args), token);
        return {};
    }

    _pointerPosition() {
        return this._input.position();
    }

    _modifiers(list) {
        if (list === undefined || list === null)
            return [];
        if (!Array.isArray(list))
            throw new OpError(E_BAD_ARGS, 'modifiers must be an array');
        return list.map(m => {
            const k = MODIFIER_KEYVALS[String(m).toLowerCase()];
            if (k === undefined)
                throw new OpError(E_BAD_ARGS, `modifier must be one of ${MODIFIER_NAMES.join(', ')}`);
            return k;
        });
    }

    _button(name, def = 'left') {
        const b = BUTTONS[name ?? def];
        if (b === undefined)
            throw new OpError(E_BAD_ARGS, 'button must be left, right or middle');
        return b;
    }

    async _pointerClick(args, token) {
        const p = optionalPoint(args);
        const style = styleOf(args);
        const button = this._button(args.button);
        const count = num(args.count, 'count', {min: 1, max: 3, int: true, def: 1});
        const mods = this._modifiers(args.modifiers);
        if (p)
            await this._glideTo(...p, style, token);
        if (style === 'human')
            await token.sleep(Math.round(randomBetween(40, 110))); // settle before pressing
        for (const k of mods)
            this._input.key(k, true);
        try {
            if (mods.length > 0)
                await token.sleep(30); // modifiers travel on the keyboard device
            for (let i = 0; i < count; i++) {
                if (i > 0)
                    await token.sleep(40);
                this._overlay.cursor?.ripple();
                this._input.button(button, true);
                await token.sleep(10);
                this._input.button(button, false);
            }
        } finally {
            this._input.button(button, false);
            for (const k of [...mods].reverse())
                this._input.key(k, false);
        }
        this._fixKeyFocus();
        return {};
    }

    async _pointerDrag(args, token) {
        const [x1, y1] = point(args.from, 'from');
        const [x2, y2] = point(args.to, 'to');
        const style = styleOf(args);
        const button = this._button(args.button);
        const given = args.duration_ms !== undefined && args.duration_ms !== null;
        const duration = num(args.duration_ms, 'duration_ms', {min: 0, max: 10000, def: 300});
        await this._glideTo(x1, y1, style, token);
        await token.sleep(style === 'human' ? Math.round(randomBetween(40, 90)) : 20);
        this._input.button(button, true);
        try {
            await token.sleep(50);
            if (style === 'instant') {
                const steps = Math.max(5, Math.round(duration / 16));
                for (let i = 1; i <= steps; i++) {
                    const t = i / steps;
                    this._input.move(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t);
                    await token.sleep(Math.max(1, Math.round(duration / steps)));
                }
            } else {
                // A drag is deliberate: gentle bow, no overshoot. An explicit duration_ms is
                // how long the button stays pressed: the glide may be capped (1.5 s in
                // human style, 0 for a drag of < 2 px), the hold never is.
                const path = planPath([x1, y1], [x2, y2], style,
                    {drag: true, duration: given ? Math.max(duration, 1) : undefined});
                const t0 = GLib.get_monotonic_time();
                await glide(this._input, path, token);
                const held = (GLib.get_monotonic_time() - t0) / 1000;
                if (given && held < duration)
                    await token.sleep(Math.round(duration - held));
            }
            await this._moveTo(x2, y2, token);
            await token.sleep(30);
        } finally {
            this._input.button(button, false);
        }
        return {};
    }

    // Where to scroll without coordinates: a plausible resting spot in the focused window.
    _scrollAnchor() {
        const w = global.display.focus_window;
        if (!w)
            return null;
        const f = w.get_frame_rect();
        return clampToMonitors(...scrollAnchor(f));
    }

    async _pointerScroll(args, token) {
        const style = styleOf(args);
        const dx = num(args.dx, 'dx', {min: -50, max: 50, int: true, def: 0});
        const dy = num(args.dy, 'dy', {min: -50, max: 50, int: true, def: 0});
        const p = optionalPoint(args) ?? this._scrollAnchor();
        if (p)
            await this._glideTo(...p, style, token);
        if (style === 'human')
            await token.sleep(Math.round(randomBetween(60, 140)));
        const steps = [
            [Math.abs(dy), dy > 0 ? Clutter.ScrollDirection.DOWN : Clutter.ScrollDirection.UP],
            [Math.abs(dx), dx > 0 ? Clutter.ScrollDirection.RIGHT : Clutter.ScrollDirection.LEFT],
        ];
        for (const [n, dir] of steps) {
            const gaps = scrollCadence(n, style);
            if (n > 0)
                this._overlay.cursor?.scrollHint(
                    dir === Clutter.ScrollDirection.DOWN || dir === Clutter.ScrollDirection.RIGHT ? 1 : -1);
            for (let i = 0; i < n; i++) {
                this._input.scroll(dir);
                await token.sleep(gaps[i]);
            }
        }
        return {anchor: p ? {x: p[0], y: p[1]} : null};
    }

    // ---- keyboard ----

    // Keyboard events go to the stage's key focus actor when there is one, not to the
    // focused window. Hiding the overview programmatically (e.g. the startup overview)
    // leaves key focus on an overview actor, so keys silently vanish and apps never get
    // text-input focus. Without a modal grab (menus, dialogs, overview all push one) and
    // with a focused window, shell key focus is stale: clear it so input reaches the app.
    // Returns true when focus was changed (callers give the client a moment to react).
    _fixKeyFocus() {
        const focus = global.stage.key_focus;
        if (!focus || Main.modalCount > 0 || !global.display.focus_window)
            return false;
        if (Main.overview.visible || Main.overview.animationInProgress)
            return false;
        global.stage.set_key_focus(null);
        return true;
    }

    async _keyPress(args, token) {
        let keys = args.keys;
        if (typeof keys === 'string')
            keys = [keys];
        if (!Array.isArray(keys) || keys.length === 0)
            throw new OpError(E_BAD_ARGS, 'keys must be a string or a non-empty array');
        const repeat = num(args.repeat, 'repeat', {min: 1, max: 100, int: true, def: 1});
        const chords = keys.map(parseChord); // validate everything first
        if (this._fixKeyFocus())
            await token.sleep(50);
        for (let r = 0; r < repeat; r++) {
            for (const c of chords) {
                token.check();
                this._input.chord(c);
                await token.sleep(20);
            }
        }
        return {};
    }

    _typeableSet() {
        const src = Keyboard.getInputSourceManager().currentSource;
        const layout = (src?.xkbId ?? 'us').split(/[+(]/)[0];
        if (!LATIN_LAYOUT.test(layout))
            return null; // nothing reliably typeable as keyvals
        return {layout, extra: LAYOUT_EXTRA[layout] ?? '', unsafe: LAYOUT_UNSAFE[layout] ?? ''};
    }

    _canKeyval(ch, typeable) {
        if (ch === '\n' || ch === '\t')
            return true;
        if (!typeable)
            return false;
        const cp = ch.codePointAt(0);
        if (typeable.unsafe.includes(ch))
            return false;
        return (cp >= 0x20 && cp <= 0x7e) || typeable.extra.includes(ch);
    }

    async _keyType(args, token) {
        const text = str(args.text, 'text').replace(/\r\n?/g, '\n');
        if (text.length > MAX_TEXT)
            throw new OpError(E_BAD_ARGS, `text longer than ${MAX_TEXT}`);
        const delay = num(args.delay_ms, 'delay_ms', {min: 0, max: 1000, def: 0});
        const style = styleOf(args);
        // Optional override (tests/debugging): "im" | "keyval" | "clipboard".
        const forced = str(args.method, 'method', {optional: true});
        if (forced && !['im', 'keyval', 'clipboard'].includes(forced))
            throw new OpError(E_BAD_ARGS, 'method must be im, keyval or clipboard');

        if (this._fixKeyFocus())
            await token.sleep(100); // let the client re-enable text-input
        const imFocus = Main.inputMethod?.currentFocus ?? null;
        if (forced === 'im' && !imFocus)
            throw new OpError(E_FAILED, 'no input method focus');
        const useIm = forced ? forced === 'im' : imFocus !== null;
        const typeable = this._typeableSet();

        // Plan runs: [kind, text] with kind im | keyval | clipboard | newline.
        const runs = [];
        const push = (kind, ch) => {
            const last = runs[runs.length - 1];
            if (last && last[0] === kind && kind !== 'newline')
                last[1] += ch;
            else
                runs.push([kind, ch]);
        };
        for (const ch of text) {
            let kind;
            if (ch === '\n')
                kind = 'newline';
            else if (forced === 'clipboard')
                kind = 'clipboard';
            else if (useIm)
                kind = 'im';
            else if (forced === 'keyval' || this._canKeyval(ch, typeable))
                kind = 'keyval';
            else
                kind = 'clipboard';
            push(kind, ch);
        }

        // Per-character pauses: explicit delay_ms wins; else "human" gets a light cadence
        // (at most HUMAN_TYPING_EXTRA_MS in total); fast/instant type as quickly as possible.
        const chars = [...text];
        const cadence = delay === 0 && style === 'human'
            ? typingCadence(chars, HUMAN_TYPING_EXTRA_MS) : null;
        let charIndex = 0;
        const pause = () => (cadence ? cadence[charIndex++] ?? 0 : (charIndex++, delay));

        // Refuse what cannot finish inside the op time limit (the agent should split it).
        const pastes = runs.filter(r => r[0] === 'clipboard').length;
        const estimate = chars.length * delay + pastes * (PASTE_SETTLE_MS + 100) +
            (cadence ? cadence.reduce((a, b) => a + b, 0) : 0);
        if (estimate > MAX_TYPE_MS) {
            throw new OpError(E_BAD_ARGS,
                `typing would take ~${Math.ceil(estimate / 1000)}s (max ${MAX_TYPE_MS / 1000}s): ` +
                'split the text or lower delay_ms');
        }

        const methods = new Set();
        let lastChannel = null;
        const switchTo = async channel => {
            if (lastChannel && lastChannel !== channel)
                await token.sleep(CHANNEL_SWITCH_MS);
            lastChannel = channel;
        };
        for (const [kind, chunk] of runs) {
            token.check();
            if (kind === 'newline') {
                await switchTo('key');
                this._input.tap(Clutter.KEY_Return);
                await token.sleep(pause());
            } else if (kind === 'im') {
                methods.add('im');
                await switchTo('im');
                await this._typeIm(chunk, pause, cadence !== null || delay > 0, token);
            } else if (kind === 'keyval') {
                methods.add('keyval');
                await switchTo('key');
                await this._typeKeyvals(chunk, pause, cadence !== null || delay > 0, token);
            } else {
                methods.add('clipboard');
                await switchTo('key');
                await this._paste(chunk, token);
                let gap = 0;
                for (const _ of chunk)
                    gap += pause();
                await token.sleep(Math.min(gap, 400));
            }
        }
        let method = 'keyval';
        if (methods.size > 1)
            method = 'mixed';
        else if (methods.size === 1)
            [method] = methods;
        return {method};
    }

    // pause() yields the next per-character delay; paced = commit one character at a time.
    async _typeIm(text, pause, paced, token) {
        const chars = [...text];
        const step = paced ? 1 : IM_CHUNK;
        for (let i = 0; i < chars.length; i += step) {
            token.check();
            Main.inputMethod.commit(chars.slice(i, i + step).join(''));
            let gap = 0;
            for (let j = i; j < Math.min(i + step, chars.length); j++)
                gap += pause();
            await token.sleep(gap);
        }
    }

    async _typeKeyvals(text, pause, paced, token) {
        let n = 0;
        for (const ch of text) {
            token.check();
            this._input.tap(charToKeyval(ch));
            const gap = pause();
            if (paced || ++n % KEYVAL_YIELD_EVERY === 0)
                await token.sleep(gap);
        }
    }

    _focusIsTerminal() {
        const w = global.display.focus_window;
        if (!w)
            return false;
        const app = Shell.WindowTracker.get_default().get_window_app(w);
        return TERMINAL.test(`${app?.get_id() ?? ''} ${w.get_wm_class() ?? ''} ${w.get_gtk_application_id() ?? ''}`);
    }

    // Paste text through the clipboard, then put the previous clipboard content back.
    async _paste(text, token) {
        const clipboard = St.Clipboard.get_default();
        const type = St.ClipboardType.CLIPBOARD;
        const savedText = await bounded(clipboardGetText(), token, CLIPBOARD_READ_MS, 'clipboard read');
        let saved = null;
        if (savedText === null) {
            const mimes = clipboard.get_mimetypes(type) ?? [];
            const mime = mimes.find(m => m.startsWith('image/')) ?? mimes[0];
            if (mime) {
                const bytes = await bounded(clipboardGetContent(mime), token, CLIPBOARD_READ_MS,
                    'clipboard read');
                if (bytes)
                    saved = [mime, bytes];
            }
        }
        token.check(); // nothing touched yet: a session that ended leaves the clipboard alone
        clipboard.set_text(type, text);
        try {
            await token.sleep(30);
            const chord = this._focusIsTerminal()
                ? [Clutter.KEY_Control_L, Clutter.KEY_Shift_L, Clutter.KEY_v]
                : [Clutter.KEY_Control_L, Clutter.KEY_v];
            this._input.chord(chord);
        } finally {
            // Give the client time to read the selection, then put the user's content back
            // only if the selection is still ours (the user may have copied something new).
            await plainSleep(PASTE_SETTLE_MS);
            const now = await Promise.race([clipboardGetText(), plainSleep(500).then(() => undefined)]);
            if (now === text) {
                if (savedText !== null)
                    clipboard.set_text(type, savedText);
                else if (saved)
                    clipboard.set_content(type, saved[0], saved[1]);
            }
        }
    }

    // ---- apps / windows ----

    _appSearch(args) {
        const query = str(args.query, 'query');
        const sys = Shell.AppSystem.get_default();
        const ids = Shell.AppSystem.search(query).flat();
        const apps = [];
        for (const id of ids) {
            const app = sys.lookup_app(id);
            if (app)
                apps.push({id: app.get_id(), name: app.get_name()});
            if (apps.length >= 10)
                break;
        }
        return {apps};
    }

    _resolveApp(name) {
        const sys = Shell.AppSystem.get_default();
        let app = sys.lookup_app(name) ?? sys.lookup_app(`${name}.desktop`) ??
            sys.lookup_desktop_wmclass?.(name) ?? null;
        if (!app) {
            const [first] = Shell.AppSystem.search(name).flat();
            if (first)
                app = sys.lookup_app(first);
        }
        if (!app)
            throw new OpError(E_NOT_FOUND, `no app matching ${name}`);
        return app;
    }

    // Spawn an app (optionally with URIs) and collect the pids we started, so its windows
    // can be matched without guessing (the tracker may not map them to the .desktop).
    _spawn(info, uris) {
        const pids = [];
        const ctx = global.create_app_launch_context(0, -1);
        if (info instanceof Gio.DesktopAppInfo) {
            info.launch_uris_as_manager(uris, ctx, GLib.SpawnFlags.SEARCH_PATH, null,
                (_info, pid) => pids.push(pid));
        } else {
            info.launch_uris(uris, ctx);
        }
        for (const pid of pids)
            this.session.launchedPid(pid);
        return pids;
    }

    // Wait for a new window of a process we spawned or, when byApp is set (the app had no
    // window before our launch, e.g. a D-Bus activated one that gives no pid), of `app`.
    async _waitNewWindow(app, pids, allBefore, waitMs, token, byApp) {
        const deadline = GLib.get_monotonic_time() + waitMs * 1000;
        const ready = x => x.get_compositor_private() && x.get_frame_rect().width > 0;
        const ours = x => pids.includes(x.get_pid()) ||
            (byApp && app !== null && app.get_windows().includes(x));
        while (GLib.get_monotonic_time() < deadline) {
            await token.sleep(100);
            const w = listWindows().find(x => !allBefore.has(x.get_id()) && ready(x) && ours(x));
            if (w)
                return w;
        }
        return null;
    }

    async _appLaunch(args, token) {
        const app = this._resolveApp(str(args.app, 'app'));
        const uris = args.uris ?? [];
        if (!Array.isArray(uris) || uris.some(u => typeof u !== 'string'))
            throw new OpError(E_BAD_ARGS, 'uris must be an array of strings');
        const waitMs = num(args.wait_ms, 'wait_ms', {min: 0, max: MAX_LAUNCH_WAIT_MS, def: 8000});
        const running = app.get_windows().length > 0;
        const allBefore = new Set(listWindows().map(w => w.get_id()));
        const result = {app_id: app.get_id(), name: app.get_name()};

        if (running && uris.length === 0) {
            // Already running: bring it forward rather than spawning another window.
            const [w] = app.get_windows();
            await this._activateRecorded(w, token);
            await token.sleep(100);
            result.window = this._info(w);
            return result;
        }
        let pids;
        try {
            pids = this._spawn(app.get_app_info(), uris);
        } catch (e) {
            throw new OpError(E_FAILED, `launch failed: ${e.message}`);
        }
        // Opening URIs in a running app often reuses an existing window: don't wait long.
        // For a running app only a window of the process we spawned is ours; a new window
        // of the app's existing process may well be the user's.
        const w = await this._waitNewWindow(app, pids, allBefore,
            running ? Math.min(waitMs, 1500) : waitMs, token, !running);
        if (w) {
            // Returned by our op: the AI's. If the session started the app, so is the
            // rest of its family; a running app's other windows are the user's.
            this.session.author(w, {family: !running});
            // Focus-stealing prevention can leave a freshly launched window unfocused;
            // the agent launched it to use it.
            await this._activate(w, token);
            await token.sleep(100);
            result.window = this._info(w);
        } else if (running) {
            const f = global.display.focus_window;
            const mine = f && app.get_windows().includes(f) ? f : app.get_windows()[0];
            if (mine)
                result.window = this._info(mine);
        }
        return result;
    }

    async _defaultHandler(uri) {
        if (GLib.uri_peek_scheme(uri) !== 'file')
            return Gio.AppInfo.get_default_for_uri_scheme(GLib.uri_peek_scheme(uri));
        try {
            return await Gio.File.new_for_uri(uri).query_default_handler_async(GLib.PRIORITY_DEFAULT, null);
        } catch {
            return null;
        }
    }

    async _openUri(args, token) {
        const uri = str(args.uri, 'uri');
        if (!GLib.uri_peek_scheme(uri))
            throw new OpError(E_BAD_ARGS, 'uri needs a scheme (https://, file://, ...)');
        const waitMs = num(args.wait_ms, 'wait_ms', {min: 0, max: MAX_LAUNCH_WAIT_MS, def: 8000});
        const handler = await this._defaultHandler(uri);
        const app = handler ? Shell.AppSystem.get_default().lookup_app(handler.get_id()) : null;
        if (!handler || (app && app.get_windows().length > 0)) {
            // The handler is already running (its new tab/window is not attributable to
            // us), or unknown: just open it.
            try {
                await Gio.AppInfo.launch_default_for_uri_async(uri, global.create_app_launch_context(0, -1), null);
            } catch (e) {
                throw new OpError(E_FAILED, e.message);
            }
            return {};
        }
        // The session starts the handler: its window (and family) is the AI's.
        const allBefore = new Set(listWindows().map(w => w.get_id()));
        let pids;
        try {
            pids = this._spawn(handler, [uri]);
        } catch (e) {
            throw new OpError(E_FAILED, e.message);
        }
        const w = await this._waitNewWindow(app, pids, allBefore, waitMs, token, true);
        if (!w)
            return {};
        this.session.author(w, {family: true});
        await this._activate(w, token);
        return {window: this._info(w)};
    }

    // Main.activateWindow() hides the overview, but that is a no-op while the overview is
    // still animating (e.g. the login/startup animation): retry until it is really gone.
    async _activate(w, token) {
        Main.activateWindow(w, global.get_current_time());
        for (let i = 0; i < 40 && (Main.overview.visible || Main.overview.animationInProgress); i++) {
            await token.sleep(50);
            if (Main.overview.visible && !Main.overview.animationInProgress)
                Main.overview.hide();
        }
        if (!w.has_focus())
            w.activate(global.get_current_time());
        if (this._fixKeyFocus())
            await token.sleep(50);
    }

    // windowInfo plus `new` (created during this session) and `ai` (opened by the AI:
    // what the wrapup will clean up).
    _info(w) {
        return {...windowInfo(w), new: this.session.isNew(w), ai: this.session.isAuthored(w)};
    }

    _windowList() {
        return {windows: listWindows().map(w => this._info(w))};
    }

    // Leave fullscreen/maximized/minimized so the frame can be moved/resized freely.
    // `mut` collects the properties actually mutated (for the journal).
    async _freeGeometry(w, token, mut) {
        if (w.minimized) {
            mut.add('minimized');
            w.unminimize();
            await token.sleep(60);
        }
        if (w.is_fullscreen()) {
            mut.add('fullscreen');
            w.unmake_fullscreen();
            await token.sleep(150);
        }
        if (w.is_maximized()) {
            mut.add('maximized');
            w.unmaximize();
            await token.sleep(150);
        }
    }

    // Interpolate the frame to `target` on the frame clock; in human style the pointer
    // glides to `grip` meanwhile (purely visual: no button is pressed).
    async _animateGeometry(w, target, grip, style, token, mut) {
        await this._freeGeometry(w, token, mut);
        const f = w.get_frame_rect();
        const from = {x: f.x, y: f.y, width: f.width, height: f.height};
        const to = {
            x: w.allows_move() ? Math.round(target.x) : from.x,
            y: w.allows_move() ? Math.round(target.y) : from.y,
            width: w.allows_resize() ? Math.round(target.width) : from.width,
            height: w.allows_resize() ? Math.round(target.height) : from.height,
        };
        const dur = GEOMETRY_MS[style];
        mut.add('geometry');
        const geometry = animate(dur, ms => {
            const k = dur > 0 ? minJerk(ms / dur) : 1;
            w.move_resize_frame(true,
                Math.round(from.x + (to.x - from.x) * k), Math.round(from.y + (to.y - from.y) * k),
                Math.round(from.width + (to.width - from.width) * k),
                Math.round(from.height + (to.height - from.height) * k));
        }, token);
        const jobs = [geometry];
        if (style === 'human' && grip) {
            jobs.push(this._glideTo(...clampToMonitors(...grip(to)), style, token));
        }
        const results = await Promise.allSettled(jobs);
        const failed = results.find(r => r.status === 'rejected');
        if (failed)
            throw failed.reason;
        await token.sleep(60); // let the client commit the new size
        return rect(w.get_frame_rect());
    }

    _size(v, name) {
        return num(v, name, {min: MIN_WINDOW_PX, max: 32767});
    }

    // Activate a window on the AI's behalf, journaling what that changed.
    async _activateRecorded(w, token) {
        const ws = global.workspace_manager.get_active_workspace_index();
        const before = windowState(w);
        await this._activate(w, token);
        if (before.minimized) // activation only mutates the minimized state
            this.session.record(w, before, ['minimized']);
        const now = global.workspace_manager.get_active_workspace_index();
        if (now !== ws)
            this.session.recordWorkspace(now);
    }

    // Wait (briefly) for a client to finish applying a new geometry.
    async _settled(w, token) {
        let last = windowState(w).geometry;
        for (let i = 0; i < 8; i++) {
            try {
                await token.sleep(40);
            } catch {
                return; // session over: nothing to journal anyway
            }
            const now = windowState(w).geometry;
            if (sameProp('geometry', last, now))
                return;
            last = now;
        }
    }

    async _windowAction(args, token) {
        const w = findWindow(args.id);
        const before = windowState(w);
        const mut = new Set(); // properties this call really mutated (no-ops add nothing)
        try {
            return await this._windowActionImpl(w, args, token, mut);
        } finally {
            // Journal only effective mutations on a pre-existing window, even if the action
            // was interrupted half-way, and only where the value really moved; the wrapup
            // restores only values that are still the ones we applied.
            if (mut.size > 0 && this.session.baseline(w)) {
                if (mut.has('geometry'))
                    await this._settled(w, token);
                this.session.record(w, before, [...mut]);
            }
        }
    }

    async _windowActionImpl(w, args, token, mut) {
        const style = styleOf(args);
        const time = global.get_current_time();
        const corner = r => [r.x + r.width - 10, r.y + r.height - 10];
        const titlebar = r => [r.x + r.width / 2, r.y + 14];
        switch (args.action) {
        case 'resize': {
            const f = w.get_frame_rect();
            const target = {
                x: num(args.x, 'x', {def: f.x}), y: num(args.y, 'y', {def: f.y}),
                width: this._size(args.width, 'width'), height: this._size(args.height, 'height'),
            };
            return this._animateGeometry(w, target, corner, style, token, mut);
        }
        case 'move': {
            const f = w.get_frame_rect();
            const target = {x: num(args.x, 'x'), y: num(args.y, 'y'), width: f.width, height: f.height};
            return this._animateGeometry(w, target, titlebar, style, token, mut);
        }
        case 'fit': {
            // Grow a window that is smaller than min_fraction of the monitor's work area
            // and centre it; leave bigger (or maximized/fullscreen) windows alone.
            const frac = num(args.min_fraction, 'min_fraction', {min: 0.3, max: 1, def: 0.7});
            const wa = w.get_work_area_current_monitor();
            const f = w.get_frame_rect();
            const small = !w.is_maximized() && !w.is_fullscreen() &&
                (f.width < frac * wa.width || f.height < frac * wa.height);
            if (!small)
                return {changed: false, ...rect(f)};
            const width = Math.min(Math.max(f.width, Math.round(frac * wa.width)), wa.width);
            const height = Math.min(Math.max(f.height, Math.round(frac * wa.height)), wa.height);
            const target = {
                x: wa.x + Math.round((wa.width - width) / 2), y: wa.y + Math.round((wa.height - height) / 2),
                width, height,
            };
            return {changed: true, ...await this._animateGeometry(w, target, corner, style, token, mut)};
        }
        }
        switch (args.action) {
        case 'activate':
            await this._activateRecorded(w, token);
            break;
        case 'close':
            w.delete(time);
            break;
        case 'maximize':
            if (!w.is_maximized()) {
                mut.add('maximized').add('geometry');
                w.maximize();
            }
            break;
        case 'unmaximize':
            if (w.is_maximized()) {
                mut.add('maximized').add('geometry');
                w.unmaximize();
            }
            break;
        case 'minimize':
            if (!w.minimized) {
                mut.add('minimized');
                w.minimize();
            }
            break;
        case 'fullscreen':
            if (!w.is_fullscreen()) {
                mut.add('fullscreen').add('geometry');
                w.make_fullscreen();
            }
            break;
        case 'unfullscreen':
            if (w.is_fullscreen()) {
                mut.add('fullscreen').add('geometry');
                w.unmake_fullscreen();
            }
            break;
        default:
            throw new OpError(E_BAD_ARGS,
                'action must be activate, close, maximize, unmaximize, minimize, fullscreen, ' +
                'unfullscreen, resize, move or fit');
        }
        await token.sleep(50);
        return {};
    }

    // ---- session wrapup ----

    // Leave the desktop as the session found it: close the windows it opened (except
    // `keep`), put back pre-existing windows it changed, refocus where the user was,
    // glide the pointer home and fade the AI cursor. Hard cap WRAPUP_CAP_MS: then it
    // stops where it is and reports incomplete. A session abort cancels it outright.
    async _wrapup(args, token) {
        const keepArg = args.keep ?? [];
        if (!Array.isArray(keepArg))
            throw new OpError(E_BAD_ARGS, 'keep must be an array of window ids');
        const keep = new Set(keepArg.map(Number));
        const closeNew = args.close_new !== false;
        const returnTo = args.return_to ?? 'origin';
        if (returnTo !== 'origin' && !Number.isFinite(Number(returnTo)))
            throw new OpError(E_BAD_ARGS, 'return_to must be "origin" or a window id');

        const result = {closed: [], restored: [], left_open: [], focused: null};
        const cap = token.child();
        let capped = false;
        let capId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, WRAPUP_CAP_MS, () => {
            capId = 0;
            capped = true;
            cap.cancel();
            return GLib.SOURCE_REMOVE;
        });
        try {
            await this._wrapupSteps(keep, closeNew, returnTo, cap, result);
        } catch (e) {
            if (!(capped && !token.cancelled && e instanceof OpError && e.code === E_INACTIVE))
                throw e; // a real abort (or a bug): the session cleanup takes over
            result.incomplete = true;
        } finally {
            if (capId)
                GLib.source_remove(capId);
        }
        token.check();
        this._overlay.cursor?.release({fade: true}); // real cursor back, ours fades out
        return result;
    }

    // Which new windows the wrapup may close. A window is closed only if the AI authored
    // it and nothing in its transient family (root ancestor and everything under it) must
    // be preserved: kept, pre-existing, not authored, born during a wrapup, or already
    // asked to close. So keeping a
    // dialog protects its parent, keeping a parent protects its dialogs, and dialogs of the
    // user's own windows are never touched. Returns [[window, reason|null]] for new windows.
    _closePlan(keep, closeNew) {
        const all = listWindows();
        const rootId = w => (w.find_root_ancestor?.() ?? w).get_id();
        const s = this.session;
        const pinned = w => keep.has(w.get_id()) || !s.isNew(w) || !s.isAuthored(w) ||
            s.spawnedInWrapup(w) || s.attempted(w);
        const protectedRoots = new Set(all.filter(pinned).map(rootId));
        // A window we already asked to close (and refused) protects its whole family,
        // including dialogs it opens later (e.g. a slow "save changes?").
        const refusedRoots = new Set(all.filter(w => s.attempted(w)).map(rootId));
        return all.filter(w => s.isNew(w)).map(w => {
            let reason = null;
            if (keep.has(w.get_id()))
                reason = 'kept';
            else if (!closeNew)
                reason = 'close_new=false';
            else if (s.spawnedInWrapup(w) || s.attempted(w) || refusedRoots.has(rootId(w)))
                reason = 'not_closed'; // a refusal (or its dialog): never asked twice
            else if (!s.isAuthored(w))
                reason = 'not_authored';
            else if (protectedRoots.has(rootId(w)))
                reason = 'family_protected';
            return [w, reason];
        });
    }

    async _wrapupSteps(keep, closeNew, returnTo, token, result) {
        const brief = w => ({id: w.get_id(), title: w.get_title() ?? '', app_id: windowInfo(w).app_id});
        const reported = new Set();
        const leave = (w, reason) => {
            reported.add(w.get_id());
            result.left_open.push({...brief(w), reason});
        };

        // 1. Windows the AI opened during the session (see _closePlan for what is spared).
        const closing = [];
        for (const [w, reason] of this._closePlan(keep, closeNew)) {
            if (reason)
                leave(w, reason);
            else
                closing.push([w.get_id(), brief(w), w]);
        }
        if (closing.length > 0) {
            this.session.setWrapping(true); // windows born now ("save?") are protected
            try {
                const time = global.get_current_time();
                for (const [, , w] of closing) {
                    this.session.markAttempted(w); // one request per window per session
                    w.delete(time); // graceful: the app may ask to save instead
                }
                const deadline = GLib.get_monotonic_time() + CLOSE_WAIT_MS * 1000;
                const open = () => new Set(listWindows().map(x => x.get_id()));
                while (closing.some(([id]) => open().has(id)) && GLib.get_monotonic_time() < deadline)
                    await token.sleep(100);
            } finally {
                this.session.setWrapping(false);
            }
            const now = new Set(listWindows().map(x => x.get_id()));
            for (const [id, info, w] of closing) {
                if (now.has(id))
                    leave(w, 'not_closed');
                else
                    result.closed.push(info);
            }
            // Anything new that popped up meanwhile (e.g. "save changes?") stays, reported.
            for (const w of listWindows()) {
                if (this.session.isNew(w) && !reported.has(w.get_id()))
                    leave(w, keep.has(w.get_id()) ? 'kept' : 'not_closed');
            }
        }

        // 2. Pre-existing windows: undo only properties our ops changed and that still hold
        // the value we applied (an app or the user may have changed them since).
        for (const [w, base, props] of this.session.restorable()) {
            try {
                restoreWindow(w, base, props);
                result.restored.push(w.get_id());
            } catch (e) {
                console.warn(`pcpilot: could not restore window ${w.get_id()}: ${e.message}`);
            }
        }
        const ws = this.session.workspace;
        const applied = this.session.appliedWorkspace;
        const active = global.workspace_manager.get_active_workspace_index();
        if (ws !== null && applied !== null && active === applied && active !== ws)
            global.workspace_manager.get_workspace_by_index(ws)?.activate(global.get_current_time());
        if (result.restored.length > 0)
            await token.sleep(150);

        // 3. Focus where the user was (the agent's terminal), else a Traycer window.
        const windows = listWindows();
        const byId = id => windows.find(x => x.get_id() === Number(id)) ?? null;
        let target = returnTo !== 'origin' ? byId(returnTo) : null;
        if (!target && this.session.focusId !== null)
            target = byId(this.session.focusId);
        if (!target) {
            target = windows.find(x => /traycer/i.test(windowInfo(x).app_id)) ??
                windows.find(x => /traycer/i.test(x.get_title() ?? '')) ?? null;
        }
        if (target) {
            await this._activate(target, token);
            result.focused = brief(target);
        }

        // 4. Pointer back home: where it was, unless that is outside the focused window.
        let home = this.session.pointer;
        if (target) {
            const f = target.get_frame_rect();
            const inside = home && home[0] >= f.x && home[0] < f.x + f.width &&
                home[1] >= f.y && home[1] < f.y + f.height;
            if (!inside)
                home = [f.x + Math.round(f.width / 2), f.y + Math.round(f.height / 2)];
        }
        if (home) {
            await this._glideTo(...clampToMonitors(...home), 'human', token);
        }
    }

    // ---- misc ----

    async _clipboardGet(_args, token) {
        return {text: (await bounded(clipboardGetText(), token, CLIPBOARD_READ_MS, 'clipboard read')) ?? ''};
    }

    _clipboardSet(args) {
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, str(args.text, 'text'));
        return {};
    }

    _status(args) {
        this._overlay.setStatus(str(args.text, 'text'));
        return {};
    }
}
