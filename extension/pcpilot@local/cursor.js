// The AI's own pointer: while a session is active the real cursor is hidden (Mutter's
// counted cursor-visibility inhibit) and we draw an arrow that follows the pointer, a
// fading trail, a ripple on clicks and a hint on scrolls.
//
// Safety: release() always un-hides the real cursor synchronously, before anything else,
// and is called on every end path (inactive, disable, errors). The inhibit is taken at
// most once and released exactly once per engage.
//
// Cost: nothing ticks while the pointer is still. The trail timeline only runs while
// there are trail points younger than TRAIL_MS, then stops by itself.

import Cairo from 'gi://cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

const TRAIL_MS = 400;
const TRAIL_MAX_POINTS = 64;
const FADE_MS = 300;
const RIPPLE_MS = 380;
const HINT_MS = 450;
// Arrow geometry (px at scale 1); the hotspot is the tip at (PAD, PAD).
const PAD = 3;
const ARROW = [[0, 0], [0, 19], [4.6, 14.9], [7.9, 22.6], [11.1, 21.2], [7.9, 13.7], [13.8, 13.7]];
const FILL = [1.0, 0.18, 0.33]; // brand red/magenta
const TRAIL_RGB = [1.0, 0.27, 0.23];

function nowMs() {
    return GLib.get_monotonic_time() / 1000;
}

const Arrow = GObject.registerClass(
class PCPilotArrow extends St.DrawingArea {
    _init(scale) {
        this._scale = scale;
        super._init({
            reactive: false,
            width: Math.ceil((14 + PAD * 2 + 3) * scale),
            height: Math.ceil((23 + PAD * 2 + 3) * scale),
        });
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const s = this._scale;
        const path = (dx, dy) => {
            cr.newPath();
            ARROW.forEach(([x, y], i) => {
                const px = (x + PAD + dx) * s, py = (y + PAD + dy) * s;
                if (i === 0)
                    cr.moveTo(px, py);
                else
                    cr.lineTo(px, py);
            });
            cr.closePath();
        };
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        // soft shadow
        path(1.2, 1.6);
        cr.setSourceRGBA(0, 0, 0, 0.3);
        cr.fill();
        // body + thin white outline
        path(0, 0);
        cr.setSourceRGBA(...FILL, 1);
        cr.fillPreserve();
        cr.setLineWidth(1.4 * s);
        cr.setSourceRGBA(1, 1, 1, 0.95);
        cr.stroke();
        cr.$dispose?.();
    }
});

const Trail = GObject.registerClass(
class PCPilotTrail extends St.DrawingArea {
    _init() {
        super._init({reactive: false, visible: false});
        this._points = []; // {x, y, t} in stage coords, oldest first
        this._origin = [0, 0];
        this._timeline = null;
        this.connect('destroy', () => this._stopTicking());
    }

    add(x, y) {
        const last = this._points[this._points.length - 1];
        if (last && Math.abs(last.x - x) < 0.5 && Math.abs(last.y - y) < 0.5)
            return;
        this._points.push({x, y, t: nowMs()});
        if (this._points.length > TRAIL_MAX_POINTS)
            this._points.shift();
        this._ensureTicking();
    }

    clear() {
        this._points = [];
        this._stopTicking();
        this.visible = false;
    }

    _ensureTicking() {
        if (this._timeline)
            return;
        // A repeating frame-clock timeline: ticks only while there is something to fade.
        this._timeline = new Clutter.Timeline({duration: 1000, repeat_count: -1, actor: this});
        this._timeline.connect('new-frame', () => this._tick());
        this._timeline.start();
        this._tick();
    }

    _stopTicking() {
        if (!this._timeline)
            return;
        this._timeline.stop();
        this._timeline = null;
    }

    _tick() {
        const cutoff = nowMs() - TRAIL_MS;
        while (this._points.length > 0 && this._points[0].t < cutoff)
            this._points.shift();
        if (this._points.length < 2) {
            this.clear();
            return;
        }
        let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
        for (const p of this._points) {
            x1 = Math.min(x1, p.x);
            y1 = Math.min(y1, p.y);
            x2 = Math.max(x2, p.x);
            y2 = Math.max(y2, p.y);
        }
        const m = 6;
        this._origin = [Math.floor(x1 - m), Math.floor(y1 - m)];
        this.set_position(...this._origin);
        this.set_size(Math.ceil(x2 - x1 + 2 * m), Math.ceil(y2 - y1 + 2 * m));
        this.visible = true;
        this.queue_repaint();
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const pts = this._points;
        const now = nowMs();
        cr.setLineCap(Cairo.LineCap.ROUND);
        for (let i = 1; i < pts.length; i++) {
            const life = 1 - Math.min(1, (now - pts[i].t) / TRAIL_MS);
            if (life <= 0)
                continue;
            cr.setSourceRGBA(...TRAIL_RGB, 0.75 * life);
            cr.setLineWidth(0.8 + 3.6 * life);
            cr.moveTo(pts[i - 1].x - this._origin[0], pts[i - 1].y - this._origin[1]);
            cr.lineTo(pts[i].x - this._origin[0], pts[i].y - this._origin[1]);
            cr.stroke();
        }
        cr.$dispose?.();
    }
});

export class AICursor {
    // layer: a non-reactive actor covering the stage (inside the overlay group, so it is
    // hidden from screenshots together with the border).
    constructor(layer, onChange = null) {
        this._layer = layer;
        this._onChange = onChange;
        this._tracker = global.backend.get_cursor_tracker();
        this._inhibited = false;
        this._engaged = false;
        this._positionId = 0;
        this._scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;

        this._trail = new Trail();
        this._arrow = new Arrow(this._scale);
        this._arrow.visible = false;
        this._effects = new Set(); // ripples / hints in flight
        layer.add_child(this._trail);
        layer.add_child(this._arrow);
    }

    get engaged() {
        return this._engaged;
    }

    get inhibited() {
        return this._inhibited;
    }

    // Hide the real cursor and show ours (idempotent).
    engage() {
        if (this._engaged || !this._layer)
            return;
        this._engaged = true;
        this._arrow.remove_all_transitions();
        this._arrow.opacity = 255;
        this._arrow.visible = true;
        this._positionId = this._tracker.connect('position-invalidated', () => this.sync());
        this.sync(false);
        if (!this._inhibited) {
            this._tracker.inhibit_cursor_visibility();
            this._inhibited = true;
        }
        this._onChange?.();
    }

    // Give the real cursor back. Immediate unless fade: (then ours fades out over FADE_MS,
    // while the real one is already visible).
    release({fade = false} = {}) {
        // The real cursor comes back first, whatever happens below.
        if (this._inhibited) {
            this._inhibited = false;
            try {
                this._tracker.uninhibit_cursor_visibility();
            } catch (e) {
                console.error(`pcpilot: could not restore the cursor: ${e.message}`);
            }
        }
        if (this._positionId) {
            this._tracker.disconnect(this._positionId);
            this._positionId = 0;
        }
        this._engaged = false;
        this._onChange?.();
        if (!this._layer)
            return;
        this._trail.clear();
        for (const e of this._effects)
            e.destroy();
        this._effects.clear();
        this._arrow.remove_all_transitions();
        if (fade && this._arrow.visible) {
            this._arrow.ease({
                opacity: 0,
                duration: FADE_MS,
                mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                onStopped: () => {
                    if (!this._engaged && this._arrow)
                        this._arrow.visible = false;
                },
            });
        } else {
            this._arrow.visible = false;
        }
    }

    destroy() {
        this.release();
        this._arrow?.destroy();
        this._trail?.destroy();
        this._arrow = this._trail = null;
        this._layer = null;
    }

    // Follow the pointer (called on pointer motion and by the motion engine).
    sync(trail = true) {
        if (!this._engaged)
            return;
        const [x, y] = global.get_pointer();
        this._arrow.set_position(Math.round(x - PAD * this._scale), Math.round(y - PAD * this._scale));
        if (trail)
            this._trail.add(x, y);
        this._layer.set_child_above_sibling(this._arrow, null);
    }

    _effect(actor, x, y, props) {
        this._effects.add(actor);
        this._layer.add_child(actor);
        actor.set_position(Math.round(x - actor.width / 2), Math.round(y - actor.height / 2));
        actor.set_pivot_point(0.5, 0.5);
        actor.ease({
            ...props,
            onStopped: () => {
                if (this._effects.delete(actor))
                    actor.destroy();
            },
        });
        this._layer.set_child_above_sibling(this._arrow, null);
    }

    ripple() {
        if (!this._engaged)
            return;
        const [x, y] = global.get_pointer();
        const size = Math.round(30 * this._scale);
        const ring = new St.Widget({
            reactive: false, width: size, height: size,
            style: `border: ${Math.round(2 * this._scale)}px solid rgba(255,46,84,0.9);` +
                `border-radius: ${size}px;`,
            scale_x: 0.35, scale_y: 0.35,
        });
        this._effect(ring, x, y, {
            scale_x: 1.5, scale_y: 1.5, opacity: 0,
            duration: RIPPLE_MS, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }

    // dir: 1 = down/right, -1 = up/left
    scrollHint(dir) {
        if (!this._engaged)
            return;
        const [x, y] = global.get_pointer();
        const icon = new St.Icon({
            reactive: false,
            icon_name: dir > 0 ? 'pan-down-symbolic' : 'pan-up-symbolic',
            icon_size: 16,
            style: 'color: rgba(255,46,84,0.95);',
            opacity: 230,
        });
        const off = 22 * this._scale;
        this._effect(icon, x + off, y + off * dir, {
            translation_y: 10 * dir * this._scale, opacity: 0,
            duration: HINT_MS, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }
}
