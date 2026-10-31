// v2.2 regressions: wheel bursts, per-character typing, pointer-driven window actions
// (activate/close/maximize/minimize), the hover before a click, and the read-only
// desktop.info / window.list fields. Real Ops/path code against a tiny window world.
import assert from 'node:assert/strict';
import {setTimeoutScale} from 'gi://GLib';
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Input, Token} from '../../../extension/pcpilot@local/input.js';
import {Ops} from '../../../extension/pcpilot@local/ops.js';
import {HUMAN_TYPING_MAX_CHARS, TYPING_BUDGET_MS, typingCadence} from '../../../extension/pcpilot@local/path.js';
import {seeded} from './v21-scenarios.mjs';

const sleep = ms => new Promise(r => setTimeout(r, ms));
const mean = xs => xs.reduce((a, b) => a + b, 0) / xs.length;
const sum = xs => xs.reduce((a, b) => a + b, 0);

export async function runV22Scenarios() {
    const saved = {
        monitors: Main.layoutManager.monitors, display: global.display, wm: global.workspace_manager,
        windows: global.get_window_actors, pointer: global.get_pointer, time: global.get_current_time,
        random: Math.random, overview: Main.overview.visible,
    };
    Main.layoutManager.monitors = [{x: 0, y: 0, width: 1920, height: 1080, index: 0, geometry_scale: 1}];
    Main.layoutManager.primaryIndex = 0;
    let wins = [], pointer = [100, 100], workspace = 0, log = [], onButton = null;
    const display = {focus_window: null, sort_windows_by_stacking: a => a, connect: () => 1, disconnect() {}};
    global.display = display;
    global.get_window_actors = () => wins.map(meta_window => ({meta_window}));
    global.get_pointer = () => pointer;
    global.get_current_time = () => 1;
    global.workspace_manager = {
        get_active_workspace_index: () => workspace, get_n_workspaces: () => 3,
        get_workspace_by_index: () => ({activate() {}}),
    };
    class FakeWindow {
        constructor(id, {x = 100, y = 100, width = 600, height = 400, focused = false, minimized = false,
            maximized = false, workspaceIndex = 0} = {}) {
            Object.assign(this, {id, x, y, width, height, minimized, maximized, workspaceIndex, fullscreen: false});
            if (focused)
                display.focus_window = this;
        }
        get_id() { return this.id }
        get_title() { return `Window ${this.id}` }
        get_gtk_application_id() { return `app.${this.id}` }
        get_wm_class() { return '' }
        get_pid() { return this.id }
        get_monitor() { return 0 }
        get_frame_rect() { return {x: this.x, y: this.y, width: this.width, height: this.height} }
        get_buffer_rect() { return this.get_frame_rect() }
        has_focus() { return display.focus_window === this }
        is_override_redirect() { return false }
        get_window_type() { return 0 }
        is_on_all_workspaces() { return false }
        get_workspace() { return {index: () => this.workspaceIndex} }
        is_maximized() { return this.maximized }
        is_fullscreen() { return this.fullscreen }
        maximize() { this.maximized = true; log.push(['maximize', this.id]) }
        unmaximize() { this.maximized = false; log.push(['unmaximize', this.id]) }
        minimize() { this.minimized = true; log.push(['minimize', this.id]) }
        delete() { log.push(['delete', this.id]); wins = wins.filter(w => w !== this) }
    }
    const topAt = (x, y) => [...wins].reverse().find(w => !w.minimized && x >= w.x && x < w.x + w.width &&
        y >= w.y && y < w.y + w.height) ?? null;
    const input = {
        _keys: [], _buttons: new Set(),
        position: () => ({x: pointer[0], y: pointer[1]}),
        move(x, y) { pointer = [x, y]; log.push(['move', x, y, Date.now()]) },
        button(b, down) { log.push(['button', b, down, Date.now()]); onButton?.(b, down) },
        key() {},
        scroll(dir) { log.push(['scroll', dir, Date.now()]) },
        tap(k) { log.push(['tap', k, Date.now()]) },
        chord() {},
    };
    const ops = new Ops({
        overlay: {cursor: {ripple() {}, scrollHint() {}, release() {}}, engageCursor() {}},
        input,
    });
    const realGlideTo = ops._glideTo;
    ops._glideTo = async (x, y, style, token) => {
        token.check();
        log.push(['glide', x, y, style]);
        input.move(x, y);
    };
    ops._activate = async w => { log.push(['activate', w.id]); display.focus_window = w };
    ops._fixKeyFocus = () => false;
    const reset = () => { wins = []; log = []; onButton = null; display.focus_window = null; pointer = [100, 100] };
    const ofKind = k => log.filter(e => e[0] === k);
    // Clicking a window focuses it (click-to-focus), like the compositor.
    const focusOnClick = (_b, down) => { if (down) display.focus_window = topAt(...pointer) ?? display.focus_window };
    void realGlideTo;

    setTimeoutScale(0.1);
    try {
        // ---- B1: window.action activate/close/maximize/minimize are mouse-driven in human style ----
        {
            reset();
            const a = new FakeWindow(1, {x: 100, y: 100}), b = new FakeWindow(2, {x: 900, y: 100, focused: true});
            wins = [a, b];
            onButton = focusOnClick;
            const r = await ops._windowAction({id: 1, action: 'activate'}, new Token());
            assert.equal(r.via, 'pointer');
            const [, gx, gy, gstyle] = ofKind('glide')[0];
            assert.equal(gstyle, 'human');
            assert.ok(gx > a.x && gx < a.x + a.width && gy >= a.y && gy <= a.y + 20, `title bar spot ${gx},${gy}`);
            const order = log.map(e => e[0]);
            assert.ok(order.indexOf('glide') < order.indexOf('button'), 'pointer arrives before the click');
            assert.deepEqual(ofKind('activate'), [], 'no programmatic activation when the click focused it');
            assert.equal(ofKind('button').filter(e => e[2]).length, 1);
            assert.equal(display.focus_window, a);

            // Already focused: nothing to do, no pointer.
            log = [];
            assert.equal((await ops._windowAction({id: 1, action: 'activate'}, new Token())).via, 'focused');
            assert.equal(log.length, 0);

            // Covered title bar, minimized, other workspace, fast/instant: programmatic.
            reset();
            const under = new FakeWindow(3, {x: 100, y: 100});
            const over = new FakeWindow(4, {x: 50, y: 60, width: 900, height: 200});
            wins = [under, over];
            display.focus_window = over;
            onButton = focusOnClick;
            assert.equal((await ops._windowAction({id: 3, action: 'activate'}, new Token())).via, 'programmatic');
            assert.deepEqual(ofKind('button'), []);
            for (const variant of [{minimized: true}, {workspaceIndex: 2}]) {
                reset();
                const w = new FakeWindow(5, variant);
                wins = [w];
                assert.equal((await ops._windowAction({id: 5, action: 'activate'}, new Token())).via, 'programmatic');
                assert.deepEqual(ofKind('button'), []);
            }
            for (const style of ['fast', 'instant']) {
                reset();
                const w = new FakeWindow(6);
                wins = [w];
                const rr = await ops._windowAction({id: 6, action: 'activate', style}, new Token());
                assert.equal(rr.via, 'programmatic');
                assert.deepEqual([ofKind('glide'), ofKind('button')], [[], []]);
            }
        }
        {
            // close with the real button located by the caller: glide, click it, done.
            reset();
            const w = new FakeWindow(10, {x: 200, y: 150, focused: true});
            wins = [w];
            const at = {x: w.x + w.width - 24, y: w.y + 22};
            onButton = (_b, down) => {
                if (down && Math.hypot(pointer[0] - at.x, pointer[1] - at.y) < 3)
                    w.delete();
            };
            const r = await ops._windowAction({id: 10, action: 'close', at}, new Token());
            assert.equal(r.via, 'pointer');
            assert.deepEqual(ofKind('glide').map(e => [e[1], e[2]]), [[at.x, at.y]]);
            assert.equal(ofKind('delete').length, 1, 'closed once, by the click');
            const order = log.map(e => e[0]);
            assert.ok(order.indexOf('glide') < order.indexOf('button') && order.indexOf('button') < order.indexOf('delete'));

            // The click did nothing (wrong spot): programmatic fallback, still one close.
            reset();
            const v = new FakeWindow(11, {x: 200, y: 150, focused: true});
            wins = [v];
            const r2 = await ops._windowAction({id: 11, action: 'close', at: {x: v.x + 100, y: v.y + 100}}, new Token());
            assert.equal(r2.via, 'programmatic');
            assert.equal(ofKind('delete').length, 1);
            assert.equal(ofKind('button').filter(e => e[2]).length, 1);

            // Without `at` the real button is unknown: rest where it usually is, then act
            // programmatically; never a blind click.
            reset();
            const u = new FakeWindow(12, {x: 200, y: 150, focused: true});
            wins = [u];
            const r3 = await ops._windowAction({id: 12, action: 'close'}, new Token());
            assert.equal(r3.via, 'programmatic');
            assert.deepEqual(ofKind('button'), []);
            const [, bx, by] = ofKind('glide')[0];
            assert.ok(bx >= u.x + u.width - 40 && bx <= u.x + u.width - 10 && by >= u.y && by <= u.y + 20, `${bx},${by}`);
            assert.equal(ofKind('delete').length, 1);

            // `at` outside the window is refused before anything moves.
            reset();
            const t = new FakeWindow(13);
            wins = [t];
            await assert.rejects(ops._windowAction({id: 13, action: 'close', at: {x: 1500, y: 900}}, new Token()),
                e => e.code === 'bad_args');
            assert.equal(log.length, 0);
        }
        {
            // maximize: double-click the title bar when the desktop does that; else programmatic.
            reset();
            const w = new FakeWindow(20, {x: 200, y: 150, focused: true});
            wins = [w];
            const realDouble = ops._titlebarDoubleClickMaximizes;
            ops._titlebarDoubleClickMaximizes = () => true;
            let presses = 0;
            onButton = (_b, down) => {
                if (down && ++presses === 2)
                    w.maximized = true;
            };
            const r = await ops._windowAction({id: 20, action: 'maximize'}, new Token());
            assert.equal(r.via, 'pointer');
            assert.equal(presses, 2);
            assert.deepEqual(ofKind('maximize'), []);
            ops._titlebarDoubleClickMaximizes = realDouble;
            Gio.Settings.values['action-double-click-titlebar'] = 'minimize';
            assert.equal(ops._titlebarDoubleClickMaximizes(), false);
            Gio.Settings.values['action-double-click-titlebar'] = 'toggle-maximize';
            assert.equal(ops._titlebarDoubleClickMaximizes(), true);
            Gio.Settings.broken = true;
            assert.equal(ops._titlebarDoubleClickMaximizes(), false);
            Gio.Settings.broken = false;

            reset();
            const u = new FakeWindow(21, {x: 200, y: 150, focused: true});
            wins = [u];
            const r2 = await ops._windowAction({id: 21, action: 'minimize'}, new Token());
            assert.equal(r2.via, 'programmatic');
            assert.equal(ofKind('minimize').length, 1);
            assert.equal(ofKind('glide').length, 1, 'the pointer goes to the title bar first');
            reset();
            const f = new FakeWindow(22);
            wins = [f];
            await ops._windowAction({id: 22, action: 'maximize', style: 'fast'}, new Token());
            assert.deepEqual(ofKind('glide'), []);
            assert.equal(ofKind('maximize').length, 1);
        }

        // ---- B2: the hover before a click is 200-450 ms (human), none otherwise ----
        {
            setTimeoutScale(1);
            reset();
            for (let i = 0; i < 4; i++) {
                log = [];
                const t0 = Date.now();
                await ops._pointerClick({x: 300, y: 300}, new Token());
                const press = ofKind('button').find(e => e[2])[3];
                assert.ok(press - t0 >= 195 && press - t0 <= 560, `hover ${press - t0} ms`);
            }
            log = [];
            const t0 = Date.now();
            await ops._pointerClick({x: 300, y: 300, style: 'fast'}, new Token());
            assert.ok(ofKind('button').find(e => e[2])[3] - t0 < 100);
            setTimeoutScale(0.1);
        }

        // ---- B3: wheel notches in irregular bursts ----
        {
            setTimeoutScale(1);
            reset();
            Math.random = seeded(7);
            const t0 = Date.now();
            const res = await ops._pointerScroll({dy: 12, x: 500, y: 400}, new Token());
            Math.random = saved.random;
            const total = Date.now() - t0;
            const scrolls = ofKind('scroll');
            assert.equal(scrolls.length, res.notches);
            assert.equal(res.net.dy, 12);
            assert.equal(res.net.dx, 0);
            assert.ok(res.notches >= 12 && res.notches <= 14, `${res.notches} notches`);
            assert.ok(scrolls.every(e => [Clutter.ScrollDirection.DOWN, Clutter.ScrollDirection.UP].includes(e[1])),
                'discrete wheel directions only');
            const gaps = scrolls.slice(1).map((e, i) => e[2] - scrolls[i][2]);
            const pauses = gaps.filter(g => g >= 240), inBurst = gaps.filter(g => g < 240);
            assert.ok(pauses.length >= 2, `looking pauses: ${gaps}`);
            assert.ok(pauses.every(g => g <= 1100), `pause too long: ${pauses}`);
            assert.ok(inBurst.length >= 6 && inBurst.every(g => g >= 45), `in-burst gaps: ${inBurst}`);
            const sd = Math.sqrt(mean(gaps.map(g => (g - mean(gaps)) ** 2)));
            assert.ok(sd / mean(gaps) > 0.6, `rhythm is irregular: cv ${sd / mean(gaps)}`);
            assert.ok(new Set(inBurst.map(g => Math.round(g / 10))).size >= 4, 'in-burst gaps vary');
            // Bursts: runs of 1-6 notches split by pauses; the pointer only drifts a few px.
            const bursts = [];
            let run = 1;
            gaps.forEach(g => { if (g >= 240) { bursts.push(run); run = 1 } else run++ });
            bursts.push(run);
            assert.ok(bursts.every(n => n >= 1 && n <= 6) && bursts.length >= 3, `bursts ${bursts}`);
            assert.ok(total <= 14500, `${total} ms`);
            for (const m of ofKind('move'))
                assert.ok(Math.hypot(m[1] - 500, m[2] - 400) <= 8, `drift ${m[1]},${m[2]}`);
            assert.ok(ofKind('move').length >= 3, 'the pointer drifts between bursts');

            // dx and a negative dy: the other wheel directions.
            reset();
            await ops._pointerScroll({dy: -2, dx: 3, x: 500, y: 400, style: 'fast'}, new Token());
            assert.deepEqual(ofKind('scroll').map(e => e[1]), [
                Clutter.ScrollDirection.UP, Clutter.ScrollDirection.UP,
                Clutter.ScrollDirection.RIGHT, Clutter.ScrollDirection.RIGHT, Clutter.ScrollDirection.RIGHT]);
            reset();
            Math.random = seeded(3);
            const neg = await ops._pointerScroll({dy: -9, x: 500, y: 400}, new Token());
            Math.random = saved.random;
            assert.equal(neg.net.dy, -9);
            assert.ok(ofKind('scroll').filter(e => e[1] === Clutter.ScrollDirection.UP).length >= 9);

            // Cancel mid-burst: stops at once, nothing more is emitted afterwards.
            reset();
            const token = new Token();
            const work = ops._pointerScroll({dy: 30, x: 500, y: 400}, token).catch(e => e.code);
            await sleep(500);
            token.cancel();
            assert.equal(await work, 'inactive');
            const n = log.length;
            await sleep(900);
            assert.equal(log.length, n, 'no event after the cancel');
            setTimeoutScale(0.1);

            // A huge count is faster, not longer, and keeps its net distance.
            reset();
            Math.random = seeded(11);
            const big = await ops._pointerScroll({dy: 50, x: 500, y: 400}, new Token());
            Math.random = saved.random;
            assert.equal(big.net.dy, 50);
            assert.ok(big.notches >= 50 && big.notches <= 54, `${big.notches} notches`);
        }
        {
            // The real input path sends discrete wheel events, one notch each.
            const real = new Input();
            const sent = [];
            real._pointer.notify_discrete_scroll = (...a) => sent.push(a);
            real._pointer.notify_scroll_continuous = () => assert.fail('smooth scrolling must not be used');
            real.scroll(Clutter.ScrollDirection.DOWN);
            assert.equal(sent.length, 1);
            assert.equal(sent[0][1], Clutter.ScrollDirection.DOWN);
            assert.equal(sent[0][2], Clutter.ScrollSource.WHEEL);
            real.destroy();
        }

        // ---- B4: typing little by little ----
        const commits = [];
        Main.inputMethod.currentFocus = {};
        Main.inputMethod.commit = t => commits.push([t, Date.now()]);
        const pastes = [];
        ops._paste = async t => { pastes.push(t) };
        ops._typeableSet = () => ({layout: 'us', extra: '', unsafe: ''});
        {
            setTimeoutScale(1);
            Math.random = seeded(5);
            const text = 'Olá, tudo bem? Estou digitando.';
            const t0 = Date.now();
            const res = await ops._keyType({text}, new Token());
            Math.random = saved.random;
            assert.equal(res.method, 'im');
            assert.equal(commits.map(c => c[0]).join(''), text);
            assert.ok(commits.every(c => [...c[0]].length <= 2), 'never more than 2 characters per commit');
            assert.ok(commits.length >= text.length * 0.85, `${commits.length} commits for ${text.length} chars`);
            const times = commits.map(c => c[1]);
            const gaps = times.slice(1).map((t, i) => t - times[i]);
            assert.ok(times[0] - t0 < 100, 'starts right away');
            assert.ok(mean(gaps) >= 60 && mean(gaps) <= 260, `mean gap ${mean(gaps)}`);
            assert.ok(Math.max(...gaps) >= 150, 'word pauses');
            assert.ok(Math.max(...gaps) <= 1500, `longest pause ${Math.max(...gaps)}`);
            assert.ok(Math.min(...gaps) < 130, 'quick keys too');
            assert.ok(new Set(gaps.map(g => Math.round(g / 25))).size >= 5, 'jittered cadence');
            commits.length = 0;

            // Cancel: typing stops at once.
            const token = new Token();
            Math.random = seeded(5);
            const work = ops._keyType({text}, token).catch(e => e.code);
            await sleep(700);
            token.cancel();
            assert.equal(await work, 'inactive');
            Math.random = saved.random;
            const before = commits.length;
            assert.ok(before > 0 && before < text.length);
            await sleep(700);
            assert.equal(commits.length, before, 'nothing typed after the cancel');
            commits.length = 0;
            setTimeoutScale(0.02);
        }
        {
            // Long texts switch to the brisk cadence and always fit; nothing is ever sent whole.
            const long = 'palavra '.repeat(30).trim(); // 239 chars
            Math.random = seeded(9);
            const plan = typingCadence([...long]);
            Math.random = saved.random;
            assert.equal(plan.profile, 'brisk');
            assert.ok(sum(plan.gaps) <= TYPING_BUDGET_MS && sum(plan.gaps) <= 14500);
            const nonSpace = plan.gaps.filter((_, i) => long[i] !== ' ');
            assert.ok(mean(nonSpace) >= 15 && mean(nonSpace) <= 75, `brisk gap ${mean(nonSpace)}`);
            assert.ok(long.length > HUMAN_TYPING_MAX_CHARS);
            commits.length = 0;
            Math.random = seeded(9);
            await ops._keyType({text: long}, new Token());
            Math.random = saved.random;
            assert.equal(commits.map(c => c[0]).join(''), long);
            assert.ok(commits.every(c => [...c[0]].length <= 2) && commits.length >= long.length * 0.9);

            // Short human text keeps a natural 70-190 ms mean; 40-80 chars hesitate.
            for (let seed = 1; seed <= 40; seed++) {
                const t = 'Isto é um texto de teste normal, sem pressa.';
                const p = typingCadence([...t], TYPING_BUDGET_MS, seeded(seed));
                assert.equal(p.profile, 'human');
                assert.ok(mean(p.gaps) >= 70 && mean(p.gaps) <= 260, `mean ${mean(p.gaps)}`);
                assert.ok(p.gaps.every(Number.isInteger));
            }
            let hesitations = 0;
            for (let seed = 1; seed <= 30; seed++) {
                // Plain spaces pause at most 450 ms: anything longer is a hesitation.
                const p = typingCadence([...'abc def '.repeat(12)], TYPING_BUDGET_MS, seeded(seed));
                if (p.gaps.some(g => g > 460 && g <= 1100))
                    hesitations++;
            }
            assert.ok(hesitations >= 28, `hesitations ${hesitations}/30`);

            // Beyond what even the brisk cadence fits: plain chunks (fast behaviour), no rejection.
            const huge = 'x'.repeat(1500);
            assert.equal(typingCadence([...huge]).profile, 'bulk');
            commits.length = 0;
            await ops._keyType({text: huge}, new Token());
            assert.equal(commits.map(c => c[0]).join(''), huge);
            assert.ok(commits.every(c => c[0].length <= 64) && commits.length === Math.ceil(huge.length / 64));

            // fast = chunks of 64, instant = the whole string, explicit delay_ms = one per char.
            commits.length = 0;
            await ops._keyType({text: long, style: 'fast'}, new Token());
            assert.ok(commits.every(c => c[0].length <= 64) && commits.length === Math.ceil(long.length / 64));
            commits.length = 0;
            await ops._keyType({text: long, style: 'instant'}, new Token());
            assert.equal(commits.length, 1);
            commits.length = 0;
            await ops._keyType({text: 'abcdef', delay_ms: 20}, new Token());
            assert.deepEqual(commits.map(c => c[0]), [...'abcdef']);
        }
        {
            // The shell's own UI (overview search) gets real key events, not input-method commits.
            commits.length = 0;
            log = [];
            Main.overview.visible = true;
            Math.random = seeded(4);
            const shellRes = await ops._keyType({text: 'calc'}, new Token());
            Math.random = saved.random;
            Main.overview.visible = false;
            assert.equal(shellRes.method, 'keyval');
            assert.equal(commits.length, 0);
            assert.equal(ofKind('tap').length, 4);

            // keyval route: one tap per character, paced.
            Main.inputMethod.currentFocus = null;
            log = [];
            Math.random = seeded(2);
            await ops._keyType({text: 'ab cd'}, new Token());
            Math.random = saved.random;
            assert.deepEqual(ofKind('tap').length, 5);

            // Clipboard route: only the exotic characters, one at a time (never the whole string).
            pastes.length = 0;
            log = [];
            await ops._keyType({text: 'ação é'}, new Token());
            assert.deepEqual(pastes, ['ç', 'ã', 'é']);
            pastes.length = 0;
            await ops._keyType({text: 'ação é', method: 'clipboard'}, new Token());
            assert.ok(pastes.every(p => [...p].length === 1) && pastes.join('') === 'ação é');
            // Fast style keeps the old behaviour: a run per paste.
            pastes.length = 0;
            await ops._keyType({text: 'ação é', style: 'fast', method: 'clipboard'}, new Token());
            assert.deepEqual(pastes, ['ação é']);
            // Too many clipboard characters for one op: refused with a hint, never hangs.
            await assert.rejects(ops._keyType({text: 'ç'.repeat(100), method: 'clipboard'}, new Token()),
                e => e.code === 'bad_args' && /split/.test(e.message));
            pastes.length = 0;
            await ops._keyType({text: 'ç'.repeat(40), method: 'clipboard'}, new Token());
            assert.equal(pastes.join(''), 'ç'.repeat(40));
            Main.inputMethod.currentFocus = {};
        }

        // ---- B5: read-only desktop state ----
        {
            reset();
            const a = new FakeWindow(30, {x: 10, y: 20, maximized: true, focused: true}), b = new FakeWindow(31);
            b.fullscreen = true;
            wins = [a, b];
            pointer = [321, 123];
            workspace = 2;
            Main.overview.visible = true;
            Gio.Settings.values['color-scheme'] = 'prefer-dark';
            const info = ops._desktopInfo();
            assert.deepEqual(info, {
                workspace: {active: 2, count: 3}, overview: true, pointer: {x: 321, y: 123}, focus: 30,
                scheme: 'dark',
                monitors: [{index: 0, x: 0, y: 0, width: 1920, height: 1080, scale: 1, primary: true}],
            });
            Main.overview.visible = false;
            for (const [v, want] of [['prefer-light', 'light'], ['default', 'default'], ['weird', null]]) {
                Gio.Settings.values['color-scheme'] = v;
                assert.equal(ops._desktopInfo().scheme, want);
            }
            Gio.Settings.broken = true;
            assert.equal(ops._desktopInfo().scheme, null);
            Gio.Settings.broken = false;
            display.focus_window = null;
            assert.equal(ops._desktopInfo().focus, null);
            // No input, no windows touched, and it is an op like any other.
            assert.deepEqual(log, []);
            assert.deepEqual(ops.run('desktop.info', {}, new Token()).workspace, {active: 2, count: 3});
            assert.deepEqual(ops._screenInfo().monitors[0].index, 0);
            const rows = ops._windowList().windows;
            const row = Object.fromEntries(rows.map(r => [r.id, r]));
            assert.deepEqual([row[30].maximized, row[30].fullscreen, row[30].monitor], [true, false, 0]);
            assert.deepEqual([row[31].maximized, row[31].fullscreen, row[31].monitor], [false, true, 0]);
            assert.ok(['id', 'title', 'app_id', 'pid', 'x', 'y', 'width', 'height', 'buffer', 'focused', 'minimized',
                'workspace', 'new', 'ai'].every(k => k in row[30]), 'existing fields stay');
        }
    } finally {
        Math.random = saved.random;
        Main.overview.visible = saved.overview;
        Main.layoutManager.monitors = saved.monitors;
        global.display = saved.display;
        global.workspace_manager = saved.wm;
        global.get_window_actors = saved.windows;
        global.get_pointer = saved.pointer;
        global.get_current_time = saved.time;
        setTimeoutScale(0.1);
    }
    return ['v22-window-pointer-actions', 'v22-hover-before-click', 'v22-scroll-bursts', 'v22-scroll-discrete',
        'v22-typing-per-character', 'v22-typing-fits-budget', 'v22-typing-clipboard-individual', 'v22-desktop-info'];
}
