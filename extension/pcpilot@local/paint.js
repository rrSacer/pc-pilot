// Cairo drawing for the overlay: the monitor frame, the brand glyphs and badge.
// Everything here is static artwork, painted only when an actor is resized or the theme
// changes (nothing ticks).

import Cairo from 'gi://cairo';

import {rgb} from './theme.js';

// Frame geometry, px at scale 1.
export const FRAME = {
    line: 4, // the crisp coloured line along the screen edge
    radius: 14, // its inner corners follow a rounded screen
    glow: 20, // tinted glow fading inwards from the line
};

// How far into the monitor the frame paints (strip thickness), px at scale 1.
export function frameDepth() {
    return FRAME.line + Math.max(FRAME.glow, FRAME.radius) + 2;
}

// Four disjoint strips of the same rounded frame. No full-screen canvas/texture and
// no overlap that could darken the top/left corners more than the other corners.
export function frameStrips(width, height) {
    const d = Math.min(frameDepth(), width / 2, height / 2);
    return [
        {x: 0, y: 0, width, height: d},
        {x: 0, y: height - d, width, height: d},
        {x: 0, y: d, width: d, height: height - 2 * d},
        {x: width - d, y: d, width: d, height: height - 2 * d},
    ];
}

function roundedRect(cr, x, y, w, h, r) {
    r = Math.max(0, Math.min(r, w / 2, h / 2));
    cr.newSubPath();
    cr.arc(x + w - r, y + r, r, -Math.PI / 2, 0);
    cr.arc(x + w - r, y + h - r, r, 0, Math.PI / 2);
    cr.arc(x + r, y + h - r, r, Math.PI / 2, Math.PI);
    cr.arc(x + r, y + r, r, Math.PI, 1.5 * Math.PI);
    cr.closePath();
}

// A gradient through `stops` (hex colours, evenly spaced) from (x0,y0) to (x1,y1).
function gradient(x0, y0, x1, y1, stops, alpha = 1) {
    const g = new Cairo.LinearGradient(x0, y0, x1, y1);
    stops.forEach((c, i) => g.addColorStopRGBA(stops.length > 1 ? i / (stops.length - 1) : 0, ...rgb(c), alpha));
    return g;
}

// Paints the frame of a W×H monitor into `cr`, whose origin is at (ox, oy) of the
// monitor (each edge strip paints its own part of the same picture).
export function paintFrame(cr, {width: W, height: H, ox = 0, oy = 0, theme, scale = 1}) {
    const B = FRAME.line * scale, R = FRAME.radius * scale, G = FRAME.glow * scale;
    cr.save();
    cr.translate(-ox, -oy);

    // Soft glow: 1 px rings just inside the line, the theme's tint with an eased falloff.
    cr.setLineWidth(1);
    for (let i = 0; i < G; i++) {
        const k = 1 - i / G;
        const a = theme.glowAlpha * k * k * (0.6 + 0.4 * k);
        if (a < 0.004)
            continue;
        const d = B + i + 0.5;
        cr.setSource(gradient(0, 0, W, H, [theme.glow, theme.glow], a));
        roundedRect(cr, d, d, W - 2 * d, H - 2 * d, Math.max(R - i, 1));
        cr.stroke();
    }

    // The line: screen edge to a rounded inner edge, a gradient along the diagonal.
    cr.setFillRule(Cairo.FillRule.EVEN_ODD);
    cr.newPath();
    cr.rectangle(0, 0, W, H);
    roundedRect(cr, B, B, W - 2 * B, H - 2 * B, R);
    cr.setSource(gradient(0, 0, W, H, theme.rim));
    cr.fill();
    cr.setFillRule(Cairo.FillRule.WINDING);

    // Rim light on the line's inner half, then a thin dark outline against light content.
    cr.setLineWidth(1);
    cr.setSourceRGBA(1, 1, 1, 0.30);
    roundedRect(cr, B - 1.5, B - 1.5, W - 2 * B + 3, H - 2 * B + 3, R + 1.5);
    cr.stroke();
    cr.setSourceRGBA(...rgb(theme.edge), 0.45);
    roundedRect(cr, B + 0.5, B + 0.5, W - 2 * B - 1, H - 2 * B - 1, R - 0.5);
    cr.stroke();
    cr.restore();
}

// Brand glyph centred at (cx, cy), fitting a circle of radius r, in the current source.
export function paintGlyph(cr, kind, cx, cy, r) {
    cr.save();
    cr.translate(cx, cy);
    switch (kind) {
    case 'spark': {
        // A burst of rounded rays of slightly varying length.
        const rays = 10;
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(r * 0.24);
        for (let i = 0; i < rays; i++) {
            const a = (i / rays) * 2 * Math.PI - Math.PI / 2;
            const len = r * (i % 2 === 0 ? 0.92 : 0.74);
            cr.moveTo(Math.cos(a) * r * 0.16, Math.sin(a) * r * 0.16);
            cr.lineTo(Math.cos(a) * len, Math.sin(a) * len);
        }
        cr.stroke();
        break;
    }
    case 'prompt': {
        // ">_": a chevron and a cursor bar.
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        cr.setLineWidth(r * 0.22);
        cr.moveTo(-r * 0.62, -r * 0.48);
        cr.lineTo(-r * 0.12, 0);
        cr.lineTo(-r * 0.62, r * 0.48);
        cr.moveTo(r * 0.08, r * 0.52);
        cr.lineTo(r * 0.68, r * 0.52);
        cr.stroke();
        break;
    }
    case 'sparkle': {
        // Four-point star with concave sides.
        const k = r * 0.2;
        cr.moveTo(0, -r);
        cr.curveTo(k * 0.3, -k, k, -k * 0.3, r, 0);
        cr.curveTo(k, k * 0.3, k * 0.3, k, 0, r);
        cr.curveTo(-k * 0.3, k, -k, k * 0.3, -r, 0);
        cr.curveTo(-k, -k * 0.3, -k * 0.3, -k, 0, -r);
        cr.closePath();
        cr.fill();
        break;
    }
    default: {
        // Neutral: a ring around a dot.
        cr.setLineWidth(r * 0.2);
        cr.arc(0, 0, r * 0.68, 0, 2 * Math.PI);
        cr.stroke();
        cr.arc(0, 0, r * 0.26, 0, 2 * Math.PI);
        cr.fill();
    }
    }
    cr.restore();
}

// Round brand badge: gradient disc with the glyph on top.
export function paintBadge(cr, cx, cy, r, theme, {ring = null} = {}) {
    cr.save();
    if (ring) {
        cr.arc(cx, cy, r + 1.2, 0, 2 * Math.PI);
        cr.setSourceRGBA(...rgb(ring), 0.95);
        cr.fill();
    }
    cr.arc(cx, cy, r, 0, 2 * Math.PI);
    cr.setSource(gradient(cx - r, cy - r, cx + r, cy + r, theme.badge.bg));
    cr.fill();
    cr.setSourceRGBA(...rgb(theme.badge.glyph), 1);
    paintGlyph(cr, theme.glyph, cx, cy, r * 0.62);
    cr.restore();
}

// Vertical gradient helper for the cursor body.
export function verticalGradient(y0, y1, stops) {
    return gradient(0, y0, 0, y1, stops);
}
