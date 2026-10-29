// Human-like pointer trajectories. Pure math, no GNOME imports.
//
// human: cubic Bézier with a random bow, minimum-jerk timing, Fitts-law duration,
//        a faint wobble that vanishes at both ends and, on long moves, a small
//        overshoot followed by a correction.
// fast:  straight line, minimum-jerk timing, ~100-180 ms.
// instant: no path (the caller jumps).

export const STYLES = ['human', 'fast', 'instant'];

const MAX_GLIDE_MS = 1500;
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

function bezier(p0, p1, p2, p3, s) {
    const u = 1 - s;
    const a = u * u * u, b = 3 * u * u * s, c = 3 * u * s * s, d = s * s * s;
    return [
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    ];
}

// Movement time in ms (Fitts: a + b·log2(D/W + 1)), with ±10% jitter, 180-900 ms.
export function fittsDuration(dist, rand = Math.random) {
    const base = 120 + 120 * Math.log2(dist / TARGET_WIDTH + 1);
    return clamp(base * (1 + (rand() - 0.5) * 0.2), 180, 900);
}

// Returns {duration, at(ms) -> [x, y]}; at(duration) is exactly `to`.
// opts: {drag: bool (gentler, no overshoot), duration: ms override, rand: () => [0,1)}
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
    const ux = dx / dist, uy = dy / dist; // direction
    const nx = -uy, ny = ux; // normal
    const total = clamp(opts.duration ?? fittsDuration(dist, rand), 120, MAX_GLIDE_MS);

    // Long free moves overshoot a little past the target, then correct.
    const overshoot = !opts.drag && dist > 400;
    let end = to, mainMs = total, corrMs = 0;
    if (overshoot) {
        const ov = clamp(dist * 0.025, 6, 25);
        const side = (rand() - 0.5) * ov * 0.6;
        end = [to[0] + ux * ov + nx * side, to[1] + uy * ov + ny * side];
        corrMs = Math.max(80, total * 0.2);
        mainMs = Math.max(100, total - corrMs);
    }

    // Bow: perpendicular offset of the control points, random side and size.
    const bowScale = opts.drag ? 0.04 : 0.08 + rand() * 0.1;
    const bow = clamp(dist * bowScale, 0, 120) * (rand() < 0.5 ? -1 : 1);
    const p1 = [from[0] + dx * 0.3 + nx * bow * (0.8 + rand() * 0.4),
        from[1] + dy * 0.3 + ny * bow * (0.8 + rand() * 0.4)];
    const p2 = [from[0] + dx * 0.7 + nx * bow * (0.6 + rand() * 0.4),
        from[1] + dy * 0.7 + ny * bow * (0.6 + rand() * 0.4)];

    // Wobble: a couple of tiny oscillations across the path, zero at both ends.
    const amp = Math.min(opts.drag ? 0.5 : 1.2, dist * 0.002);
    const cycles = 2 + rand() * 2;
    const phase = rand() * Math.PI * 2;

    const at = ms => {
        if (ms >= mainMs + corrMs)
            return [to[0], to[1]];
        if (ms < mainMs) {
            const s = minJerk(ms / mainMs);
            const [x, y] = bezier(from, p1, p2, end, s);
            const w = amp * Math.sin(2 * Math.PI * cycles * s + phase) * Math.sin(Math.PI * s);
            return [x + nx * w, y + ny * w];
        }
        return lerp(end, to, minJerk((ms - mainMs) / corrMs));
    };
    return {duration: mainMs + corrMs, at};
}

// Per-character pauses for "human" typing: 35-90 ms with a longer beat after spaces and
// punctuation now and then; the whole thing is scaled down to stay under `capMs`.
export function typingCadence(chars, capMs = 4000, rand = Math.random) {
    const delays = chars.map(ch => {
        let d = 35 + rand() * 55;
        if (/[\s.,;:!?]/.test(ch) && rand() < 0.5)
            d += 60 + rand() * 120;
        return d;
    });
    const sum = delays.reduce((a, b) => a + b, 0);
    const k = sum > capMs ? capMs / sum : 1;
    return delays.map(d => Math.round(d * k));
}

// Gaps between wheel steps: slower at the ends, quicker in the middle.
export function scrollCadence(steps, style) {
    if (style !== 'human')
        return new Array(steps).fill(15);
    return Array.from({length: steps}, (_, i) =>
        Math.round(25 + 45 * (1 - Math.sin(Math.PI * (i + 0.5) / steps))));
}

// A spot where a person would rest the pointer to scroll a window: right of centre,
// a bit below the middle, jittered, away from the edges and scrollbars.
export function scrollAnchor(frame, rand = Math.random) {
    return [
        Math.round(frame.x + frame.width * (0.55 + rand() * 0.1)),
        Math.round(frame.y + frame.height * (0.5 + rand() * 0.1)),
    ];
}
