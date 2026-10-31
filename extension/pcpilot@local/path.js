// Human-like pointer trajectories, wheel bursts and typing cadence. Pure math, no GNOME
// imports.
//
// human: a hand-sized reach: a real arc (perpendicular bow of 6-14 % of the distance,
//        random side, sometimes a gentle S), clear acceleration, then a long slow homing,
//        one or two tiny hesitations, a few px of settle and subpixel tremor.
// fast:  straight line, minimum-jerk timing, ~100-180 ms.
// instant: no path (the caller jumps).
//
// Durations of a human reach (HUMAN_SPEED_MULTIPLIER = 1): 100 px ~ 0.9-1.1 s, 300 px
// ~ 1.4-1.7 s, 1000-1300 px ~ 3.1-4.4 s, capped at MAX_GLIDE_MS. Every op built on them
// stays inside the 18 s guard (see the budgets in ops.js).

export const STYLES = ['human', 'fast', 'instant'];

// Larger = more deliberate. Shared by human motion/cadence; fast and instant ignore it.
export const HUMAN_SPEED_MULTIPLIER = 1;
export const MAX_GLIDE_MS = 5500;
const TARGET_WIDTH = 24; // assumed target size for Fitts' law (px)

function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
}

// Minimum-jerk position profile: 0 → 1 with zero velocity/acceleration at the ends.
export function minJerk(t) {
    t = clamp(t, 0, 1);
    return t * t * t * (10 + t * (-15 + 6 * t));
}

function lerp(a, b, s) {
    return [a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s];
}

// Time to reach AND home in on a target: linear in distance plus a log term, ±8 %.
// 100 px -> ~1.0 s, 300 px -> ~1.56 s, 1000 px -> ~3.3 s, 1300 px -> ~4.1 s.
export function fittsDuration(dist, rand = Math.random) {
    const base = 620 + dist * 2.4 + 60 * Math.log2(dist / TARGET_WIDTH + 1);
    return clamp(base * (0.92 + rand() * 0.16) * HUMAN_SPEED_MULTIPLIER, 650, MAX_GLIDE_MS);
}

// Lateral profile of the arc: 0 at both ends, |peak| = 1. A plain bow (peak skewed a bit
// towards the start or the end) or, sometimes, a gentle S whose second lobe is about half
// of the first.
function arcShape(rand, sCurve) {
    const k = 0.85 + rand() * 0.5;
    const raw = u => {
        const x = Math.pow(clamp(u, 0, 1), k);
        return sCurve ? Math.sin(2 * Math.PI * x) + 0.55 * Math.sin(Math.PI * x) : Math.sin(Math.PI * x);
    };
    let peak = 0;
    for (let i = 0; i <= 64; i++)
        peak = Math.max(peak, Math.abs(raw(i / 64)));
    return u => raw(u) / peak;
}

// Time warp with short slowdowns ("hesitations"): maps normalised time to normalised
// progress time, 0 -> 0 and 1 -> 1, never going backwards. Each bump is {c, w, a}: centre
// and half-width as fractions of the move, depth a (speed drops to 1 - a at the centre).
function hesitationWarp(bumps) {
    if (bumps.length === 0)
        return t => t;
    const N = 240;
    const phi = [0];
    for (let i = 1; i <= N; i++) {
        const x = (i - 0.5) / N;
        let speed = 1;
        for (const {c, w, a} of bumps) {
            const z = (x - c) / w;
            if (Math.abs(z) < 1)
                speed -= a * 0.5 * (1 + Math.cos(Math.PI * z));
        }
        phi.push(phi[i - 1] + speed / N);
    }
    const end = phi[N];
    return t => {
        const f = clamp(t, 0, 1) * N;
        const i = Math.min(N - 1, Math.floor(f));
        return (phi[i] + (phi[i + 1] - phi[i]) * (f - i)) / end;
    };
}

// Returns {duration, at(ms) -> [x, y]}; at(duration) is exactly `to`.
// opts: {drag: bool (gentler arc, no overshoot), duration: ms override, pace: duration
//        multiplier (< 1 = brisker, used by the wrapup), rand: () => [0,1)}
//
// A human reach is two overlapping minimum-jerk submovements along the line: a ballistic
// one covers 93-98 % of the distance (peak speed about a third of the way in) and a
// corrective one starts before it ends and does the long, slow homing. The ballistic one
// stops a little short of the target or, on long free moves, a few pixels past it. The
// lateral offset is a function of the progress along the line, so the path is a true arc
// whatever the timing; 1-2 short slowdowns, a damped few-pixel settle at the end and
// subpixel hand tremor ride on top.
export function planPath(from, to, style, opts = {}) {
    const rand = opts.rand ?? Math.random;
    const dx = to[0] - from[0], dy = to[1] - from[1];
    const dist = Math.hypot(dx, dy);
    if (style === 'instant' || dist < 2)
        return {duration: 0, at: () => [to[0], to[1]]};

    if (style === 'fast') {
        const duration = opts.duration ?? clamp(100 + dist * 0.04, 100, 180);
        return {duration, at: ms => (ms >= duration ? [to[0], to[1]] : lerp(from, to, minJerk(ms / duration)))};
    }

    // human
    const drag = opts.drag === true;
    const ux = dx / dist, uy = dy / dist; // direction
    const nx = -uy, ny = ux; // normal
    const total = Math.round(clamp(
        opts.duration ?? fittsDuration(dist, rand) * (drag ? 1.2 : 1) * (opts.pace ?? 1),
        120, MAX_GLIDE_MS));

    // Submovements. tb: share of the time the ballistic one lasts; the corrective one
    // starts `lead` before it ends.
    const tb = 0.56 + rand() * 0.08;
    const t0 = tb - (0.1 + rand() * 0.08);
    const overshoot = !drag && dist > 400 && rand() < 0.5;
    const miss = overshoot
        ? clamp(dist * (0.003 + rand() * 0.004), 2, 9) / dist
        : (drag ? 0.01 + rand() * 0.015 : 0.03 + rand() * 0.04);
    const a1 = overshoot ? 1 + miss : 1 - miss;

    // Arc: random side, 7-14 % of the distance (drags are straighter: 3.5-7.5 %).
    const side = rand() < 0.5 ? -1 : 1;
    const depth = dist * (drag ? 0.035 + rand() * 0.04 : 0.07 + rand() * 0.07);
    const shape = arcShape(rand, !drag && dist > 120 && rand() < 0.3);

    // Hesitations: short slowdowns that never stop the hand.
    const bumps = [];
    if (total >= 500) {
        const count = dist >= 60 ? 1 + (rand() < 0.55 ? 1 : 0) : (rand() < 0.4 ? 1 : 0);
        for (let i = 0; i < count; i++) {
            const lo = count === 2 ? (i === 0 ? 0.22 : 0.58) : 0.25;
            const hi = count === 2 ? (i === 0 ? 0.5 : 0.85) : 0.8;
            bumps.push({
                c: lo + rand() * (hi - lo),
                w: clamp((140 + rand() * 120) / total, 0.04, 0.12),
                a: 0.6 + rand() * 0.25,
            });
        }
    }
    const warp = hesitationWarp(bumps);

    // Wobble across the path and physiological tremor (~9 Hz, a fraction of a pixel).
    const amp = Math.min(drag ? 0.3 : 0.65, dist * 0.0015);
    const cycles = 1.5 + rand() * 1.8;
    const phase = rand() * Math.PI * 2;
    const tremorAmp = Math.min(drag ? 0.2 : 0.4, dist * 0.001);
    const tremorCycles = (8 + rand() * 3) * total / 1000;
    const tremorPhase = rand() * Math.PI * 2;

    // Settle: a damped few-pixel movement around the target in the last stretch.
    const settleMs = Math.min(450, total * 0.14);
    const settleAmp = Math.min(1.5 + rand() * 2, dist * 0.08);
    const settleAngle = (rand() - 0.5) * 1.2; // mostly along the line of the move
    const settleSign = rand() < 0.5 ? -1 : 1;

    const at = ms => {
        if (ms >= total)
            return [to[0], to[1]];
        if (ms <= 0)
            return [from[0], from[1]];
        const t = ms / total;
        const tau = warp(t);
        const q = a1 * minJerk(tau / tb) + (1 - a1) * minJerk((tau - t0) / (1 - t0));
        let along = dist * q;
        let across = side * depth * shape(q) +
            amp * Math.sin(2 * Math.PI * cycles * t + phase) * Math.sin(Math.PI * t) +
            tremorAmp * Math.sin(2 * Math.PI * tremorCycles * t + tremorPhase) * Math.sin(Math.PI * t);
        const u = (ms - (total - settleMs)) / settleMs;
        if (u > 0) {
            const s = settleSign * settleAmp * Math.sin(1.5 * Math.PI * u) * (1 - u);
            along += s * Math.cos(settleAngle);
            across += s * Math.sin(settleAngle);
        }
        return [from[0] + ux * along + nx * across, from[1] + uy * along + ny * across];
    };
    return {duration: total, at};
}

// ---- wheel ----

export const SCROLL_BUDGET_MS = 14000; // whole op, including the glide to the anchor
const SCROLL_MIN_GAP_MS = 28;
const SCROLL_MIN_PAUSE_MS = 60;

// Wheel notches the way a finger rolls them: irregular bursts of 2-5 notches 60-140 ms
// apart, a "looking" pause of 250-900 ms between bursts, now and then a one-notch step
// back, and the last burst slowing down. `count` is the net number of notches (> 0);
// every notch is +1 (the requested direction) or -1 (a counter-notch, compensated by an
// extra forward one). Returns [{notches: [+-1], gaps: [ms between consecutive notches
// of the burst], pause: ms before the next burst (0 after the last)}]. When the natural
// pace would exceed budgetMs gaps and pauses shrink proportionally (floors 28/60 ms),
// so very large counts come out faster, never longer.
export function scrollPlan(count, rand = Math.random, budgetMs = SCROLL_BUDGET_MS) {
    const bursts = [];
    if (!(count > 0))
        return bursts;
    const maxBurst = count > 24 ? 8 : 5;
    let remaining = count;
    let counters = count >= 6 ? (count > 30 ? 2 : 1) : 0;
    while (remaining > 0) {
        let size = remaining <= maxBurst ? remaining : 2 + Math.floor(rand() * (maxBurst - 1));
        if (remaining - size === 1) // never strand a lone forward notch
            size += size < maxBurst ? 1 : -1;
        remaining -= size;
        const base = 60 + rand() * 80;
        const gaps = [];
        for (let i = 1; i < size; i++)
            gaps.push(base * (0.8 + rand() * 0.4) * (i === 1 ? 1.1 : 1));
        bursts.push({notches: new Array(size).fill(1), gaps, pause: 0});
        if (remaining > 0 && counters > 0 && rand() < 0.2) {
            counters--;
            remaining++; // the step back is compensated by one more forward notch
            bursts.at(-1).pause = 220 + rand() * 300;
            bursts.push({notches: [-1], gaps: [], pause: 0});
        }
        if (remaining > 0)
            bursts.at(-1).pause = 250 + Math.pow(rand(), 1.4) * 650;
    }
    // The last burst slows down towards the end.
    const last = bursts.at(-1);
    if (last.gaps.length > 0) {
        last.gaps = last.gaps.map((g, i) =>
            g * (1 + 0.8 * (i + 1) / last.gaps.length));
    }
    const total = scrollPlanMs(bursts);
    if (total > budgetMs) {
        const k = budgetMs / total;
        for (const b of bursts) {
            b.gaps = b.gaps.map(g => Math.max(SCROLL_MIN_GAP_MS, g * k));
            b.pause = b.pause > 0 ? Math.max(SCROLL_MIN_PAUSE_MS, b.pause * k) : 0;
        }
    }
    for (const b of bursts) {
        b.gaps = b.gaps.map(Math.round);
        b.pause = Math.round(b.pause);
    }
    return bursts;
}

export function scrollPlanMs(bursts) {
    return bursts.reduce((sum, b) => sum + b.gaps.reduce((a, g) => a + g, 0) + b.pause, 0);
}

// fast/instant: notches back to back.
export function scrollCadence(steps) {
    return new Array(steps).fill(15);
}

// A spot where a person would rest the pointer to scroll a window: right of centre,
// a bit below the middle, jittered, away from the edges and scrollbars.
export function scrollAnchor(frame, rand = Math.random) {
    return [
        Math.round(frame.x + frame.width * (0.55 + rand() * 0.1)),
        Math.round(frame.y + frame.height * (0.5 + rand() * 0.1)),
    ];
}

// ---- typing ----

export const TYPING_BUDGET_MS = 13500; // plan total; leaves room for the op's own overhead
export const HUMAN_TYPING_MAX_CHARS = 120; // longer texts type at the brisk cadence
const BRISK_MIN_SCALE = 0.45; // below this the text goes out in plain chunks instead

// Per-character pauses ("gaps[i]" = wait after committing character i) plus `pair[i]`:
// characters i and i+1 may be committed together (never more than two). Two profiles:
//   human (<= 120 chars): 70-150 ms mean base gap with jitter and the odd slow key, a word
//     pause of 150-450 ms after a space, 250-550 ms after . , ; : ! ?, and a hesitation of
//     0.5-1 s every 40-80 characters (at a word boundary).
//   brisk (> 120 chars): 25-60 ms mean base gap, 60-160 ms after a space, 80-220 ms after
//     punctuation, a 0.3-0.5 s hesitation every 80-150 characters.
// If the raw total exceeds capMs everything is scaled to fit it (human: any amount;
// brisk: down to 45 %, beyond that the profile is 'bulk' = no pacing, the caller types in
// plain chunks). The returned integers always sum to <= capMs.
export function typingCadence(chars, capMs = TYPING_BUDGET_MS, rand = Math.random) {
    const n = chars.length;
    const brisk = n > HUMAN_TYPING_MAX_CHARS;
    const mean = (brisk ? 25 + rand() * 35 : 70 + rand() * 80) * HUMAN_SPEED_MULTIPLIER;
    const breakEvery = () => (brisk ? 80 + Math.floor(rand() * 71) : 40 + Math.floor(rand() * 41));
    let nextBreak = breakEvery();
    const raw = new Array(n);
    const pair = new Array(n).fill(false);
    for (let i = 0; i < n; i++) {
        const ch = chars[i];
        let d = mean * (0.55 + rand() * 0.9 + (rand() < 0.08 ? 0.7 : 0));
        if (/\s/.test(ch))
            d = brisk ? 60 + rand() * 100 : 150 + rand() * 300;
        else if (/[.,;:!?]/.test(ch))
            d = brisk ? 80 + rand() * 140 : 250 + rand() * 300;
        if (i >= nextBreak && (/\s/.test(ch) || i >= nextBreak + 12)) {
            d = brisk ? 300 + rand() * 200 : 500 + rand() * 500;
            nextBreak = i + breakEvery();
        }
        raw[i] = d;
        if (i > 0 && !pair[i - 1] && i < n - 1 && /[\p{L}\p{N}]/u.test(ch) &&
                /[\p{L}\p{N}]/u.test(chars[i + 1]) && rand() < 0.07)
            pair[i] = true;
    }
    const sum = raw.reduce((a, b) => a + b, 0);
    const k = sum > capMs ? capMs / sum : 1;
    if (brisk && k < BRISK_MIN_SCALE)
        return {profile: 'bulk', gaps: [], pair: []};
    // Integer delays whose total never exceeds capMs: floor each scaled value, then hand
    // the leftover milliseconds (fewer than one per character) to the largest remainders.
    const scaled = raw.map(d => d * k);
    const gaps = scaled.map(Math.floor);
    const budget = Math.min(Math.floor(capMs), Math.round(sum * k));
    let left = budget - gaps.reduce((a, b) => a + b, 0);
    if (left > 0) {
        const order = scaled.map((d, i) => [d - Math.floor(d), i]).sort((a, b) => b[0] - a[0]);
        for (let j = 0; j < order.length && left > 0; j++, left--)
            gaps[order[j][1]]++;
    }
    return {profile: brisk ? 'brisk' : 'human', gaps, pair};
}
