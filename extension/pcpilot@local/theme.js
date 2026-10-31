// Per-AI looks for the overlay: frame, pill, toast and AI cursor. Pure data, no GNOME
// imports. Every colour lives here so it can be tweaked in one place.
//
// The daemon's active `state` event names the theme ("claude" | "codex" | "gemini" |
// "neutral"); anything missing or unknown gets the neutral look. Whatever the theme, the
// pill text and the frame stay the same safety signal: only colours and the glyph change.

export const THEME_NAMES = ['claude', 'codex', 'gemini', 'neutral'];
export const DEFAULT_THEME = 'neutral';

// rim:      colour stops of the frame line, laid along the monitor diagonal.
// edge:     1 px inner outline that keeps the frame readable on light content.
// glow:     tint of the soft inner glow (never black); glowAlpha its strength at the line.
// pill:     the "is controlling your PC" pill (and the end toast).
// badge:    the round brand badge (pill, toast and cursor): background stops + glyph colour.
// glyph:    'spark' | 'prompt' | 'sparkle' | 'dot' (drawn with cairo in paint.js).
// cursor:   AI arrow fill stops (top to bottom), outline; trail and ripple colours.
export const THEMES = {
    claude: {
        rim: ['#e0896a', '#d97757', '#c15f3c'],
        edge: '#7a3a24',
        glow: '#e07a55',
        glowAlpha: 0.30,
        pill: {
            bg: 'rgba(38, 35, 32, 0.93)',
            border: 'rgba(217, 119, 87, 0.65)',
            text: '#faf6ef',
            muted: 'rgba(250, 246, 239, 0.72)',
            accent: '#f0a585',
        },
        badge: {bg: ['#e38a6b', '#c96442'], glyph: '#fdf7ef'},
        glyph: 'spark',
        cursor: {fill: ['#e58b6c', '#c96442'], outline: '#fffaf3'},
        trail: '#d97757',
        ripple: '#e0896a',
    },
    codex: {
        rim: ['#3d8bff', '#6366f1', '#a855f7'],
        edge: '#1e1b4b',
        glow: '#6d6bff',
        glowAlpha: 0.30,
        pill: {
            bg: 'rgba(12, 12, 14, 0.93)',
            border: 'rgba(129, 120, 255, 0.60)',
            text: '#ffffff',
            muted: 'rgba(255, 255, 255, 0.70)',
            accent: '#a5b4fc',
        },
        badge: {bg: ['#3d8bff', '#8b5cf6'], glyph: '#ffffff'},
        glyph: 'prompt',
        cursor: {fill: ['#ffffff', '#e4e5ee'], outline: '#191923'},
        trail: '#6d6bff',
        ripple: '#818cf8',
    },
    gemini: {
        rim: ['#4285f4', '#9b72cb', '#d96570'],
        edge: '#2a1f4d',
        glow: '#8f7ae0',
        glowAlpha: 0.30,
        pill: {
            bg: 'rgba(19, 19, 22, 0.93)',
            border: 'rgba(155, 114, 203, 0.65)',
            text: '#f1f1f4',
            muted: 'rgba(241, 241, 244, 0.72)',
            accent: '#c3a6f0',
        },
        badge: {bg: ['#4285f4', '#9b72cb', '#d96570'], glyph: '#ffffff'},
        glyph: 'sparkle',
        cursor: {fill: ['#4f8ff7', '#a46fd0', '#dd6b78'], outline: '#ffffff'},
        trail: '#9b72cb',
        ripple: '#b07fd6',
    },
    neutral: {
        rim: ['#8aa0bd', '#64748b', '#7d93b2'],
        edge: '#1e293b',
        glow: '#7f95b5',
        glowAlpha: 0.30,
        pill: {
            bg: 'rgba(24, 29, 38, 0.93)',
            border: 'rgba(148, 163, 184, 0.60)',
            text: '#f8fafc',
            muted: 'rgba(248, 250, 252, 0.72)',
            accent: '#cbd5e1',
        },
        badge: {bg: ['#94a3b8', '#64748b'], glyph: '#ffffff'},
        glyph: 'dot',
        cursor: {fill: ['#8193ad', '#56657d'], outline: '#ffffff'},
        trail: '#7f95b5',
        ripple: '#94a3b8',
    },
};

// The theme for a state event's `theme` value; unknown, missing or malformed → neutral.
export function themeFor(name) {
    const key = typeof name === 'string' ? name.trim().toLowerCase() : '';
    const id = Object.hasOwn(THEMES, key) ? key : DEFAULT_THEME;
    return {id, ...THEMES[id]};
}

// '#rrggbb' → [r, g, b] in 0..1 (cairo); anything else → mid grey.
export function rgb(hex) {
    const m = /^#([0-9a-f]{6})$/i.exec(String(hex));
    if (!m)
        return [0.5, 0.5, 0.5];
    const n = parseInt(m[1], 16);
    return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}

// '#rrggbb' → CSS 'rgba(...)' with the supplied alpha.
export function withAlpha(hex, alpha) {
    const [r, g, b] = rgb(hex).map(v => Math.round(v * 255));
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
