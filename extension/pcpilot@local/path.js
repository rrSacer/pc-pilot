// Human-like pointer trajectories. Pure math, no GNOME imports.
//
// human: a quick ballistic phase followed by deliberate homing, a curved reach,
//        occasional corrections, a small overshoot and subpixel hand tremor.
// fast:  straight line, minimum-jerk timing, ~100-180 ms.
// instant: no path (the caller jumps).

export const STYLES = ['human', 'fast', 'instant'];

// Larger = more deliberate. Shared by human motion/cadence; fast and instant ignore it.
export const HUMAN_SPEED_MULTIPLIER = 1;
export const MAX_GLIDE_MS = 3200;
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

// Time to reach AND home in on a target. ~0.5–0.9 s for short reaches and ~1.5–2.3 s
// over 800–1300 px. The distance term prevents long moves feeling like a teleport.
export function fittsDuration(dist, rand = Math.random) {
    const base = 420 + dist * 0.84 + 100 * Math.log2(dist / TARGET_WIDTH + 1);
    return clamp(base * (0.92 + rand() * 0.16) * HUMAN_SPEED_MULTIPLIER, 500, MAX_GLIDE_MS);
}

// Peak velocity early in the reach; more time is spent guiding the last few pixels.
function reachProgress(t) {
    return minJerk(1 - Math.pow(1 - clamp(t, 0, 1), 1.65));
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
    const total = Math.round(clamp(opts.duration ?? fittsDuration(dist, rand) * (opts.drag ? 1.28 : 1),
        120, MAX_GLIDE_MS));

    // Long free moves overshoot a little past the target, then correct.
    const overshoot = !opts.drag && dist > 400 && rand() < 0.7;
    let end = to, mainMs = total, corrMs = 0;
    if (overshoot) {
        const ov = clamp(dist * (0.003 + rand() * 0.005), 3, 10);
        const side = (rand() - 0.5) * ov * 0.6;
        end = [to[0] + ux * ov + nx * side, to[1] + uy * ov + ny * side];
        corrMs = Math.round(total * (0.16 + rand() * 0.06));
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
    const amp = Math.min(opts.drag ? 0.3 : 0.65, dist * 0.0015);
    const cycles = 1.5 + rand() * 1.8;
    const phase = rand() * Math.PI * 2;
    const correction = dist > 140 && rand() < 0.65 ? (rand() - 0.5) * Math.min(12, dist * 0.018) : 0;
    const correctionAt = 0.38 + rand() * 0.22;
    const hesitation = opts.drag ? 0.03 + rand() * 0.035 : 0;

    const at = ms => {
        if (ms >= mainMs + corrMs)
            return [to[0], to[1]];
        if (ms < mainMs) {
            const t = clamp(ms / mainMs, 0, 1);
            // A shallow hesitation within a drag without stopping or backing up.
            const s = reachProgress(t - hesitation * Math.sin(2 * Math.PI * t) * Math.sin(Math.PI * t));
            const [x, y] = bezier(from, p1, p2, end, s);
            const w = amp * Math.sin(2 * Math.PI * cycles * t + phase) * Math.sin(Math.PI * t) +
                correction * Math.exp(-Math.pow((t - correctionAt) / 0.13, 2)) * Math.sin(Math.PI * t);
            if (ms <= 0)
                return [from[0], from[1]];
            return [x + nx * w, y + ny * w];
        }
        return lerp(end, to, minJerk((ms - mainMs) / corrMs));
    };
    return {duration: mainMs + corrMs, at};
}

// Bursts of typing, word/punctuation pauses and occasional thinking gaps. The total
// stays under capMs even for long text, independent of the speed multiplier.
export function typingCadence(chars, capMs = 4000, rand = Math.random) {
    const raw = chars.map(ch => {
        let d = 45 + Math.pow(rand(), 1.5) * 100;
        if (/\s/.test(ch))
            d += 100 + rand() * 170;
        else if (/[.,;:!?]/.test(ch))
            d += 150 + rand() * 250;
        if (rand() < 0.045)
            d += 220 + rand() * 380;
        return d * HUMAN_SPEED_MULTIPLIER;
    });
    // Integer delays whose total never exceeds capMs: floor each scaled value, then hand
    // the leftover milliseconds (fewer than one per character) to the largest remainders.
    const sum = raw.reduce((a, b) => a + b, 0);
    const k = sum > capMs ? capMs / sum : 1;
    const scaled = raw.map(d => d * k);
    const out = scaled.map(Math.floor);
    const budget = Math.min(Math.floor(capMs), Math.round(sum * k));
    let left = budget - out.reduce((a, b) => a + b, 0);
    if (left > 0) {
        const order = scaled.map((d, i) => [d - Math.floor(d), i]).sort((a, b) => b[0] - a[0]);
        for (let j = 0; j < order.length && left > 0; j++, left--)
            out[order[j][1]]++;
    }
    return out;
}

// Wheel strokes: accelerate, then slow down; include small irregular finger resets.
export function scrollCadence(steps, style, rand = Math.random) {
    if (style !== 'human')
        return new Array(steps).fill(15);
    return Array.from({length: steps}, (_, i) =>
        Math.round((48 + 40 * (1 - Math.sin(Math.PI * (i + 0.5) / steps)) + rand() * 35 +
            (i > 0 && i % 5 === 0 ? 45 + rand() * 50 : 0)) * HUMAN_SPEED_MULTIPLIER));
}

// A spot where a person would rest the pointer to scroll a window: right of centre,
// a bit below the middle, jittered, away from the edges and scrollbars.
export function scrollAnchor(frame, rand = Math.random) {
    return [
        Math.round(frame.x + frame.width * (0.55 + rand() * 0.1)),
        Math.round(frame.y + frame.height * (0.5 + rand() * 0.1)),
    ];
}
