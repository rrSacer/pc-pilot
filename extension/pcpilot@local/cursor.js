// The AI's own pointer: while a session is active the real cursor is hidden (Mutter's
// counted cursor-visibility inhibit) and we draw an arrow that follows the pointer, a
// fading trail, a ripple on clicks and a hint on scrolls.
//
// Safety: release() always un-hides the real cursor synchronously, before anything else,
// and is called on every end path (inactive, disable, errors). Each inhibit is taken at
// most once and released exactly once per engage.
//
// Hiding the cursor alone makes Mutter drop the pointer focus of Wayland clients (no
// motion/clicks reach apps), so, like the shell's magnifier, the seat's unfocus is
// inhibited for as long as the cursor is hidden.
//
// Cost: nothing ticks while the pointer is still. The trail timeline only runs while
// there are trail points younger than TRAIL_MS, then stops by itself.

import Cairo from 'gi://cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {paintBadge, verticalGradient} from './paint.js';
import {rgb, themeFor, withAlpha} from './theme.js';

const TRAIL_MS = 400;
const TRAIL_MAX_POINTS = 64;
const TRAIL_JUMP_PX = 160; // farther than a hand moves between two frames
const RIPPLE_MS = 380;
const HINT_MS = 450;
// Arrow geometry (px at scale 1); the hotspot is the tip at (PAD, PAD).
const PAD = 3;
const ARROW = [[0, 0], [0, 19], [4.6, 14.9], [7.9, 22.6], [11.1, 21.2], [7.9, 13.7], [13.8, 13.7]];

function nowMs() {
    return GLib.get_monotonic_time() / 1000;
}

const Arrow = GObject.registerClass(
class PCPilotArrow extends St.DrawingArea {
    _init(scale, theme) {
        this._scale = scale;
        this._theme = theme;
        super._init({
            reactive: false,
            width: Math.ceil(34 * scale),
            height: Math.ceil((23 + PAD * 2 + 3) * scale),
        });
    }

    setTheme(theme) {
        this._theme = theme;
        this.queue_repaint();
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
        cr.setSource(verticalGradient(PAD * s, (PAD + 23) * s, this._theme.cursor.fill));
        cr.fillPreserve();
        cr.setLineWidth(1.4 * s);
        cr.setSourceRGBA(...rgb(this._theme.cursor.outline), 0.95);
        cr.stroke();
        paintBadge(cr, 24 * s, 20 * s, 6.5 * s, this._theme,
            {ring: this._theme.cursor.outline});
        cr.$dispose?.();
    }
});

const Trail = GObject.registerClass(
class PCPilotTrail extends St.DrawingArea {
    _init(theme) {
        super._init({reactive: false, visible: false});
        this._points = []; // {x, y, t} in stage coords, oldest first
        this._origin = [0, 0];
        this._head = null;
        this._timeline = null;
        this._theme = theme;
        this.connect('destroy', () => this._stopTicking());
    }

    setTheme(theme) {
        this.clear();
        this._theme = theme;
    }

    add(x, y) {
        const last = this._points[this._points.length - 1];
        if (last && Math.abs(last.x - x) < 0.5 && Math.abs(last.y - y) < 0.5)
            return;
        // A jump (instant moves, fast reaches) is not a stroke: start a new trail instead of
        // drawing a long line across the screen that then takes 400 ms to fade.
        if (last && Math.hypot(last.x - x, last.y - y) > TRAIL_JUMP_PX)
            this._points = [];
        this._points.push({x, y, t: nowMs()});
        if (this._points.length > TRAIL_MAX_POINTS)
            this._points.shift();
        if (this._timeline)
            this._tick(); // follow the arrow now, not on the next frame
        else
            this._ensureTicking();
    }

    clear() {
        this._points = [];
        this._head = null;
        this._stopTicking();
        this.visible = false;
    }

    _ensureTicking() {
        if (this._timeline)
            return;
        // A repeating frame-clock timeline: ticks only while there is something to fade.
        // Bound to the stage: this actor is hidden (no frame clock) until it has points.
        this._timeline = new Clutter.Timeline({duration: 1000, repeat_count: -1, actor: global.stage});
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
        if (this._points.length === 0) {
            this.clear(); // everything faded: stop ticking until the pointer moves again
            return;
        }
        if (this._points.length < 2) {
            this.visible = false; // a single point is not a line yet
            return;
        }
        // The newest end sits on the live pointer so the tail never lags behind the arrow.
        const [hx, hy] = global.get_pointer();
        this._head = {x: hx, y: hy};
        let x1 = hx, y1 = hy, x2 = hx, y2 = hy;
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
        const now = nowMs();
        const pts = this._head ? [...this._points, {...this._head, t: now}] : this._points;
        cr.setLineCap(Cairo.LineCap.ROUND);
        for (let i = 1; i < pts.length; i++) {
            const life = 1 - Math.min(1, (now - pts[i].t) / TRAIL_MS);
            if (life <= 0)
                continue;
            cr.setSourceRGBA(...rgb(this._theme.trail), 0.65 * life);
            cr.setLineWidth(1.2 + 4.8 * life);
            cr.moveTo(pts[i - 1].x - this._origin[0], pts[i - 1].y - this._origin[1]);
            cr.lineTo(pts[i].x - this._origin[0], pts[i].y - this._origin[1]);
            cr.stroke();
        }
        cr.$dispose?.();
    }
});

export class AICursor {
    // layer: non-reactive, hidden only for the offscreen screenshot render.
    constructor(layer, onChange = null) {
        this._layer = layer;
        this._onChange = onChange;
        this._tracker = global.backend.get_cursor_tracker();
        this._seat = global.stage.context.get_backend().get_default_seat();
        this._inhibited = false;
        this._unfocusInhibited = false;
        this._engaged = false;
        this._positionId = 0;
        this._scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        this._theme = themeFor();

        this._trail = new Trail(this._theme);
        this._arrow = new Arrow(this._scale, this._theme);
        this._arrow.visible = false;
        this._effects = new Set(); // ripples / hints in flight
        layer.add_child(this._trail);
        layer.add_child(this._arrow);
        // The layer can be destroyed under us (stage teardown): drop the actors, keep the
        // inhibit bookkeeping so release() still gives the real cursor back.
        this._layerDestroyId = layer.connect('destroy', () => this._forgetActors());
    }

    _forgetActors() {
        this._trail?.clear();
        this._effects.clear();
        this._arrow = this._trail = null;
        this._layer = null;
    }

    get engaged() {
        return this._engaged;
    }

    get inhibited() {
        return this._inhibited;
    }

    get unfocusInhibited() {
        return this._unfocusInhibited;
    }

    setTheme(theme) {
        this._theme = theme;
        if (!this._layer)
            return;
        this._trail.setTheme(theme);
        this._arrow.setTheme(theme);
        for (const e of this._effects)
            e.destroy();
        this._effects.clear();
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
        if (!this._unfocusInhibited) {
            this._seat.inhibit_unfocus(); // first: clients keep pointer focus
            this._unfocusInhibited = true;
        }
        if (!this._inhibited) {
            this._tracker.inhibit_cursor_visibility();
            this._inhibited = true;
        }
        this._onChange?.();
    }

    // Immediate on every end/abort path. The cosmetic fade runs while still active in
    // Overlay.finish(), never as an untracked animation after control was released.
    release() {
        // The real cursor comes back first, whatever happens below.
        if (this._inhibited) {
            this._inhibited = false;
            try {
                this._tracker.uninhibit_cursor_visibility();
            } catch (e) {
                console.error(`pcpilot: could not restore the cursor: ${e.message}`);
            }
        }
        if (this._unfocusInhibited) {
            this._unfocusInhibited = false;
            try {
                this._seat.uninhibit_unfocus();
            } catch (e) {
                console.error(`pcpilot: could not restore pointer unfocus: ${e.message}`);
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
        this._arrow.visible = false;
    }

    // Idempotent.
    destroy() {
        this.release();
        if (this._layer && this._layerDestroyId)
            this._layer.disconnect(this._layerDestroyId);
        this._layerDestroyId = 0;
        this._arrow?.destroy();
        this._trail?.destroy();
        this._forgetActors();
    }

    // Follow the pointer (called on pointer motion and by the motion engine).
    sync(trail = true) {
        if (!this._engaged || !this._layer)
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
        if (!this._engaged || !this._layer)
            return;
        const [x, y] = global.get_pointer();
        const size = Math.round(30 * this._scale);
        const ring = new St.Widget({
            reactive: false, width: size, height: size,
            style: `border: ${Math.round(2 * this._scale)}px solid ${withAlpha(this._theme.ripple, 0.9)};` +
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
        if (!this._engaged || !this._layer)
            return;
        const [x, y] = global.get_pointer();
        const icon = new St.Icon({
            reactive: false,
            icon_name: dir > 0 ? 'pan-down-symbolic' : 'pan-up-symbolic',
            icon_size: 16,
            style: `color: ${withAlpha(this._theme.ripple, 0.95)};`,
            opacity: 230,
        });
        const off = 22 * this._scale;
        this._effect(icon, x + off, y + off * dir, {
            translation_y: 10 * dir * this._scale, opacity: 0,
            duration: HINT_MS, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }
}
