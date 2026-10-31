// v2.1 regressions that need no window world: human motion shape/timing, the steady
// (never blinking) overlay, border geometry, theme data and clean theme switching, and the
// fade-out that session.wrapup runs. Wrapup-with-windows lives in v2-scenarios.mjs.
import assert from 'node:assert/strict';
import GLib, {setTimeoutScale} from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import PCPilotExtension from '../../../extension/pcpilot@local/extension.js';
import {Overlay} from '../../../extension/pcpilot@local/overlay.js';
import {Token} from '../../../extension/pcpilot@local/input.js';
import {FRAME, frameDepth, frameStrips} from '../../../extension/pcpilot@local/paint.js';
import {MAX_GLIDE_MS, fittsDuration, planPath} from '../../../extension/pcpilot@local/path.js';
import {DEFAULT_THEME, THEMES, THEME_NAMES, themeFor} from '../../../extension/pcpilot@local/theme.js';
import {cursorTracker, seatUnfocus} from './scenarios.mjs';

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Deterministic generator (LCG): reproducible "random" paths.
function seeded(seed) {
    let s = seed >>> 0;
    return () => {
        s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
        return s / 4294967296;
    };
}

const hex = c => [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16));
function luminance([r, g, b]) {
    const f = v => {
        v /= 255;
        return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
const contrast = (a, b) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
};
function rgba(css) {
    const m = /^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/.exec(css);
    assert.ok(m, `not an rgba() colour: ${css}`);
    return {rgb: [+m[1], +m[2], +m[3]], a: +m[4]};
}
// Colour of `fg` painted at alpha over an opaque background.
const over = ({rgb, a}, bg) => rgb.map((v, i) => v * a + bg[i] * (1 - a));

// Records every write to `opacity` (and every ease()) of the given actors.
export function recordOpacity(actors) {
    const log = {writes: [], eases: 0, bySlot: new Map()};
    actors.forEach((actor, slot) => {
        let value = actor.opacity;
        const seq = [];
        log.bySlot.set(slot, seq);
        Object.defineProperty(actor, 'opacity', {
            configurable: true,
            get: () => value,
            set: v => {
                value = v;
                log.writes.push(v);
                seq.push(v);
            },
        });
        const ease = actor.ease?.bind(actor);
        actor.ease = (...a) => {
            log.eases++;
            return ease?.(...a);
        };
    });
    return log;
}

const rectsOverlap = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width &&
    a.y < b.y + b.height && b.y < a.y + a.height;

export async function runV21Scenarios() {
    // The after-paint plumbing every Overlay.show() waits for.
    let paintCb = null;
    const stage = global.stage;
    const saved = {connect: stage.connect, queue: stage.queue_redraw};
    stage.connect = (_sig, cb) => {
        paintCb = cb;
        return 1;
    };
    stage.queue_redraw = () => queueMicrotask(() => paintCb?.());
    const savedMonitors = Main.layoutManager.monitors;
    const savedPrimary = Main.layoutManager.primaryMonitor;
    setTimeoutScale(1); // real time: a 320 ms fade must last 320 ms

    try {
        // ---- A1/A2: human reaches take a human amount of time and have a human shape ----
        {
            const a = [100, 100];
            const at = (dist, seed) => {
                const b = [a[0] + dist * 0.8, a[1] + dist * 0.6];
                return {b, p: planPath(a, b, 'human', {rand: seeded(seed)})};
            };
            const ranges = [[40, 500, 950], [150, 500, 950], [800, 1400, 2400], [1000, 1400, 2400],
                [1300, 1400, 2400], [1900, 2000, MAX_GLIDE_MS]];
            for (const [dist, lo, hi] of ranges) {
                for (let seed = 1; seed <= 60; seed++) {
                    const {b, p} = at(dist, seed);
                    assert.ok(p.duration >= lo && p.duration <= hi,
                        `human ${dist}px seed ${seed}: ${p.duration} ms outside ${lo}-${hi}`);
                    assert.ok(p.duration <= MAX_GLIDE_MS);
                    assert.deepEqual(p.at(0), a);
                    assert.deepEqual(p.at(p.duration), b);
                    const f = fittsDuration(dist, seeded(seed));
                    assert.ok(f >= lo && f <= hi, `fitts ${dist}px: ${f}`);
                }
            }
            for (let seed = 1; seed <= 20; seed++) {
                const fast = planPath(a, [1100, 700], 'fast', {rand: seeded(seed)});
                assert.ok(fast.duration >= 100 && fast.duration <= 180);
            }
            assert.equal(planPath(a, [1100, 700], 'instant').duration, 0);
            // Even a cross-screen click (glide + hover + press) is far inside the 18 s guard.
            assert.ok(MAX_GLIDE_MS + 400 + 200 < 6000);

            // Shape of a 1000 px move over many hands.
            const N = 120, dist = 1000, step = 5;
            const mean = xs => xs.reduce((sum, v) => sum + v, 0) / xs.length;
            let overshoots = 0, slowTails = 0, earlyPeaks = 0, homing = 0, creeping = 0;
            for (let seed = 1; seed <= N; seed++) {
                const {b, p} = at(dist, seed);
                const ux = (b[0] - a[0]) / dist, uy = (b[1] - a[1]) / dist;
                const samples = [];
                for (let ms = 0; ms < p.duration; ms += step)
                    samples.push(p.at(ms));
                samples.push(p.at(p.duration));
                const speeds = samples.slice(1).map((q, i) =>
                    Math.hypot(q[0] - samples[i][0], q[1] - samples[i][1]));
                const peakAt = speeds.indexOf(Math.max(...speeds)) / speeds.length;
                const first = mean(speeds.slice(0, Math.floor(speeds.length / 2)));
                const lastQuarter = mean(speeds.slice(Math.floor(speeds.length * 0.75)));
                const maxOver = Math.max(...samples.map(([x, y]) => (x - b[0]) * ux + (y - b[1]) * uy));
                const maxDev = Math.max(...samples.map(([x, y]) => Math.abs((x - a[0]) * uy - (y - a[1]) * ux)));
                assert.ok(maxOver <= 12, `overshoot ${maxOver}px`);
                assert.ok(maxDev <= 130, `strays ${maxDev}px from the straight line`);
                assert.ok(Math.max(...speeds) <= 25, `jumps ${Math.max(...speeds)}px in one ${step} ms sample`);
                if (peakAt >= 0.25 && peakAt <= 0.5)
                    earlyPeaks++;
                if (lastQuarter < 0.35 * first)
                    slowTails++;
                if (maxOver > 0.5)
                    overshoots++;
                const q = samples[Math.floor(samples.length * 0.75)];
                const tail = Math.hypot(b[0] - q[0], b[1] - q[1]);
                if (tail >= 2)
                    homing++; // slow homing: the last quarter still travels (an overshoot settles by a few px)
                if (tail > 0.5)
                    creeping++;
            }
            assert.ok(earlyPeaks >= N * 0.95, `peak speed early in ${earlyPeaks}/${N}`);
            assert.ok(slowTails >= N * 0.95, `slow final quarter in ${slowTails}/${N}`);
            assert.ok(homing >= N * 0.6, `still homing (>=2px) in the last quarter in ${homing}/${N}`);
            assert.ok(creeping >= N * 0.95, `not a dead stop (>0.5px) in the last quarter in ${creeping}/${N}`);
            assert.ok(overshoots >= N * 0.15 && overshoots < N, `overshoot in ${overshoots}/${N} reaches`);

            // 'fast' stays a straight line.
            const fast = planPath(a, [1100, 700], 'fast');
            for (let i = 1; i < 20; i++) {
                const [x, y] = fast.at(fast.duration * i / 20);
                assert.ok(Math.abs((1100 - a[0]) * (a[1] - y) - (a[0] - x) * (700 - a[1])) /
                    Math.hypot(1100 - a[0], 700 - a[1]) < 1e-8);
            }
        }

        // ---- A3: a steady overlay: no opacity blink, no timers, flash-free captures ----
        {
            const o = new Overlay();
            assert.equal(await o.show('Claude', 'Ctrl+Alt+Esc', 'codex'), true);
            const timeouts = [];
            const realTimeout = GLib.timeout_add;
            GLib.timeout_add = (...a) => {
                timeouts.push(a[1]);
                return realTimeout(...a);
            };
            const actors = [o._pillBox, o._cursorLayer, ...o._frames.map(f => f.frame),
                ...o._frames.flatMap(f => f.strips)];
            const log = recordOpacity(actors);
            await sleep(450); // the old 1400 ms pulse would have fired twice at this timeout scale
            GLib.timeout_add = realTimeout;
            assert.deepEqual(log.writes, [], 'overlay opacity must not change while idle');
            assert.equal(log.eases, 0, 'no implicit animation while idle');
            assert.deepEqual(timeouts, [], 'an idle overlay arms no timers');
            o.setStatus('Clicando em “Salvar”');
            await sleep(60);
            assert.deepEqual(log.writes, [], 'a status change does not blink the overlay');

            // withHidden: pill + toast + AI cursor are hidden only inside the synchronous call.
            let inside = 0;
            const done = o.withHidden(() => {
                inside++;
                assert.equal(o._pillBox.visible, false);
                assert.equal(o._toastBox.visible, false);
                assert.equal(o._cursorLayer.visible, false);
                assert.ok(o._frames.every(f => f.frame.visible), 'frames stay visible');
                return new Promise(r => setTimeout(r, 30));
            });
            assert.equal(inside, 1);
            // Restored before control returns to the main loop, even with the capture pending.
            assert.equal(o._pillBox.visible, true);
            assert.equal(o._cursorLayer.visible, true);
            assert.ok(o._frames.every(f => f.frame.visible));
            await done;
            assert.deepEqual(log.writes, []);
            o.destroy();
        }

        // ---- A4: border geometry is a uniform, disjoint frame on every side ----
        {
            for (const [W, H] of [[1920, 1080], [2560, 1440], [1366, 768], [800, 600], [200, 120], [40, 40]]) {
                const strips = frameStrips(W, H);
                assert.equal(strips.length, 4);
                const d = Math.min(frameDepth(), W / 2, H / 2);
                const [top, bottom, left, right] = strips;
                assert.deepEqual([top.height, bottom.height, left.width, right.width], [d, d, d, d],
                    `equal depth on all sides of ${W}x${H}`);
                assert.deepEqual([top.width, bottom.width], [W, W]);
                assert.deepEqual([left.height, right.height], [H - 2 * d, H - 2 * d]);
                assert.deepEqual([top.x, top.y, bottom.x, bottom.y], [0, 0, 0, H - d]);
                assert.deepEqual([left.x, left.y, right.x, right.y], [0, d, W - d, d]);
                strips.forEach((s, i) => {
                    assert.ok(s.x >= 0 && s.y >= 0 && s.x + s.width <= W && s.y + s.height <= H);
                    strips.slice(i + 1).forEach(t => assert.equal(rectsOverlap(s, t), false,
                        `strips overlap in ${W}x${H}`));
                });
                const area = strips.reduce((s, r) => s + r.width * r.height, 0);
                assert.equal(area, W * H - (W - 2 * d) * (H - 2 * d), `strips tile the outer band of ${W}x${H}`);
            }
            assert.ok(FRAME.line >= 3 && FRAME.line <= 4, 'a crisp 3-4 px line');
            assert.ok(FRAME.glow >= 16 && FRAME.glow <= 24, 'a short 16-24 px glow');
            assert.ok(frameDepth() >= FRAME.line + FRAME.glow);

            // One frame per monitor, positioned at the monitor; pill/toast on the primary only.
            Main.layoutManager.monitors = [
                {x: 0, y: 0, width: 1920, height: 1080, index: 0},
                {x: 1920, y: 0, width: 1280, height: 720, index: 1},
            ];
            Main.layoutManager.primaryMonitor = Main.layoutManager.monitors[0];
            const o = new Overlay();
            assert.equal(o._frames.length, 2);
            o._frames.forEach((f, i) => {
                const m = Main.layoutManager.monitors[i];
                assert.deepEqual([f.frame.x, f.frame.y, f.frame.width, f.frame.height],
                    [m.x, m.y, m.width, m.height]);
                const want = frameStrips(m.width, m.height);
                assert.equal(f.strips.length, 4);
                f.strips.forEach((s, j) => assert.deepEqual([s.x, s.y, s.width, s.height],
                    [want[j].x, want[j].y, want[j].width, want[j].height]));
            });
            assert.deepEqual([o._pillBox.x, o._pillBox.y, o._pillBox.width, o._pillBox.height], [0, 0, 1920, 1080]);
            assert.deepEqual([o._toastBox.x, o._toastBox.width], [0, 1920]);
            o.destroy();
            Main.layoutManager.monitors = savedMonitors;
            Main.layoutManager.primaryMonitor = savedPrimary;

            // Theme data: tinted (never dark) rim and glow, readable pill text, four looks.
            assert.deepEqual([...THEME_NAMES].sort(), ['claude', 'codex', 'gemini', 'neutral']);
            assert.deepEqual(Object.keys(THEMES).sort(), [...THEME_NAMES].sort());
            assert.equal(new Set(THEME_NAMES.map(n => THEMES[n].glyph)).size, 4);
            assert.deepEqual(new Set(THEME_NAMES.map(n => THEMES[n].glyph)),
                new Set(['spark', 'prompt', 'sparkle', 'dot']));
            for (const name of THEME_NAMES) {
                const t = THEMES[name];
                for (const c of t.rim)
                    assert.ok(luminance(hex(c)) >= 0.15, `${name}: ${c} is too dark for a rim`);
                assert.ok(luminance(hex(t.glow)) >= 0.2, `${name}: ${t.glow} is too dark for a glow`);
                assert.ok(t.glowAlpha <= 0.4, `${name}: glow too strong`);
                const bg = rgba(t.pill.bg);
                for (const backdrop of [[0, 0, 0], [255, 255, 255]]) {
                    const surface = over(bg, backdrop);
                    assert.ok(contrast(hex(t.pill.text), surface) >= 7, `${name}: pill text contrast`);
                    assert.ok(contrast(over(rgba(t.pill.muted), surface), surface) >= 4.5, `${name}: muted contrast`);
                }
            }
            assert.equal(new Set(THEME_NAMES.map(n => THEMES[n].rim[0])).size, 4);
        }

        // ---- A5: theme switching reaches every part and leaks nothing ----
        {
            const e = new PCPilotExtension();
            e.enable();
            e._client.stop();
            e._client = {epoch: 90, send() {}, stop() {}};
            e._overlay._waitPainted = async () => true;
            const layer = e._overlay._cursorLayer;
            const live = actor => actor.children.filter(c => !c.destroyed).length;
            const baseline = {layer: live(layer), group: live(e._overlay._group), inhibits: cursorTracker.inhibits};
            const names = ['claude', 'codex', 'gemini', 'neutral', 'bogus', undefined, 'claude', 'gemini', 'codex', 'neutral', null, 'gemini'];
            for (const [i, name] of names.entries()) {
                const c = e._overlay.cursor;
                await e._setActive({active: true, session: 100 + i, theme: name, label: 'T'}, 90);
                const want = themeFor(name);
                assert.equal(want.id, THEME_NAMES.includes(name) ? name : DEFAULT_THEME);
                assert.equal(e._overlay.themeId, want.id);
                assert.equal(c._theme.id, want.id);
                assert.ok(e._overlay._frames.every(f => f.strips.every(s => s._theme.id === want.id)));
                assert.ok(e._overlay._pill.style.includes(want.pill.bg), 'pill carries the theme background');
                assert.ok(e._overlay._pill.style.includes(want.pill.border), 'pill carries the theme border');
                assert.equal(c._trail._points.length, 0, 'no trail from the previous look');
                // Something happening in this session, then the next theme replaces it all.
                const pos = global.get_pointer();
                pos[0] += 7;
                c.sync();
                pos[0] += 7;
                c.sync();
                c.ripple();
                c.scrollHint(1);
                assert.ok(c._trail._points.length >= 1);
                if (i % 2 === 0) {
                    e._setInactive('agent_request');
                    assert.equal(cursorTracker.inhibits, baseline.inhibits, 'real cursor visible after the session');
                    assert.equal(seatUnfocus.n, 0);
                }
            }
            assert.equal(live(layer), baseline.layer, 'cursor layer children do not grow');
            assert.equal(live(e._overlay._group), baseline.group, 'overlay group children do not grow');
            assert.equal(e._overlay.cursor._effects.size, 0);
            e._setInactive('agent_request');
            assert.equal(cursorTracker.inhibits, baseline.inhibits);
            e.disable();
        }

        // ---- A6 (a,b): wrapup fade-out is smooth, bounded and cancellable ----
        {
            const e = new PCPilotExtension();
            e.enable();
            e._client.stop();
            e._client = {epoch: 91, send() {}, stop() {}};
            e._overlay._waitPainted = async () => true;
            const before = cursorTracker.inhibits;
            await e._setActive({active: true, session: 1, theme: 'gemini', label: 'Gemini'}, 91);
            const o = e._overlay;
            const actors = [o._pillBox, o._cursorLayer, ...o._frames.map(f => f.frame)];
            const log = recordOpacity(actors);
            assert.equal(cursorTracker.inhibits, before + 1, 'AI cursor engaged: the real one is hidden');
            const t0 = Date.now();
            await o.finish(new Token());
            const took = Date.now() - t0;
            assert.ok(took >= 250 && took < 1200, `fade lasted ${took} ms`);
            for (const [slot, seq] of log.bySlot) {
                assert.ok(seq.length >= 3, `slot ${slot} faded in steps, not a jump (${seq.length} writes)`);
                assert.ok(seq.every((v, i) => i === 0 || v <= seq[i - 1]), `slot ${slot} fades monotonically: ${seq}`);
                assert.ok(seq[0] <= 255 && seq.at(-1) === 0);
            }
            assert.equal(o._finished, true);
            assert.equal(o._pillBox.visible, false);
            assert.ok(o._frames.every(f => !f.frame.visible));
            assert.equal(cursorTracker.inhibits, before, 'the real cursor is back after the fade');
            assert.equal(o.cursor.engaged, false);

            // A session end afterwards restores full opacity for the next session.
            e._setInactive('agent_request');
            assert.ok(actors.every(a => a.opacity === 255), 'opacity reset by hide()');
            e.disable();
        }
        {
            const e = new PCPilotExtension();
            e.enable();
            e._client.stop();
            e._client = {epoch: 92, send() {}, stop() {}};
            e._overlay._waitPainted = async () => true;
            const before = cursorTracker.inhibits;
            await e._setActive({active: true, session: 2, theme: 'claude'}, 92);
            const o = e._overlay;
            const log = recordOpacity([o._pillBox, o._cursorLayer, ...o._frames.map(f => f.frame)]);
            const token = new Token();
            const fading = o.finish(token).catch(err => err.code);
            await sleep(100);
            assert.ok(log.writes.length > 0, 'fade started');
            token.cancel();
            assert.equal(await fading, 'inactive');
            const frozen = log.writes.length;
            await sleep(150);
            assert.equal(log.writes.length, frozen, 'no fade step after the cancellation');
            // What the extension does when the user takes control mid-fade:
            e._setInactive('user_hotkey');
            assert.equal(cursorTracker.inhibits, before);
            assert.equal(o.cursor.engaged, false);
            assert.ok([o._pillBox, o._cursorLayer].every(a => a.opacity === 255));
            const after = log.writes.length;
            await sleep(120);
            assert.equal(log.writes.length, after, 'nothing animates after the user took over');
            e.disable();
        }
    } finally {
        stage.connect = saved.connect;
        stage.queue_redraw = saved.queue;
        Main.layoutManager.monitors = savedMonitors;
        Main.layoutManager.primaryMonitor = savedPrimary;
        setTimeoutScale(0.1);
    }
    return ['human-move-timing-range', 'human-move-shape', 'no-opacity-blink', 'border-geometry-symmetric',
        'theme-data-sanity', 'theme-switch-clean', 'wrapup-fade-smooth', 'wrapup-fade-cancel'];
}
