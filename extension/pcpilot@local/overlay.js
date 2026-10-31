// Red border on every monitor + "X está controlando seu PC" pill + end-of-session toast.
// Everything is non-reactive (clicks fall through to the windows below) and lives in
// top chrome, above all windows including fullscreen ones.

import Cairo from 'gi://cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {AICursor} from './cursor.js';

const RED = '#ff453a';
const BORDER_PX = 8;
const GLOW_PX = 36;
const GLOW_ALPHA = 0.5;
const PULSE_MS = 1400;
const TOAST_MS = 2500;
const PAINT_TIMEOUT_MS = 1500; // the daemon waits 2s for the ack

function esc(s) {
    return GLib.markup_escape_text(String(s), -1);
}

// Soft red glow fading inwards from the border (St does not paint inset box-shadows on
// background-less widgets). Static: repainted only when resized.
const Glow = GObject.registerClass(
class PCPilotGlow extends St.DrawingArea {
    vfunc_repaint() {
        const cr = this.get_context();
        const [w, h] = this.get_surface_size();
        const g = GLOW_PX * St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const strip = (x0, y0, x1, y1, rx, ry, rw, rh) => {
            const grad = new Cairo.LinearGradient(x0, y0, x1, y1);
            grad.addColorStopRGBA(0, 1, 0.27, 0.23, GLOW_ALPHA);
            grad.addColorStopRGBA(1, 1, 0.27, 0.23, 0);
            cr.setSource(grad);
            cr.rectangle(rx, ry, rw, rh);
            cr.fill();
        };
        strip(0, 0, 0, g, 0, 0, w, g); // top
        strip(0, h, 0, h - g, 0, h - g, w, g); // bottom
        strip(0, 0, g, 0, 0, 0, g, h); // left
        strip(w, 0, w - g, 0, w - g, 0, g, h); // right
        cr.$dispose?.();
    }
});

export class Overlay {
    // onCursorChange: called whenever the AI cursor is engaged/released.
    constructor({onCursorChange = null} = {}) {
        this._active = false;
        this._label = 'IA';
        this._hotkey = 'Ctrl+Alt+Esc';
        this._status = '';
        this._captureHidden = 0;
        this._pulseId = 0;
        this._toastId = 0;
        this._pulseHigh = true;

        this._group = new St.Widget({name: 'pcpilotOverlay', reactive: false, visible: false});
        // Anyone may destroy our actors (the shell disposes the whole stage on logout, while
        // the daemon link can still report a loss): from then on the overlay is inert.
        this._group.connect('destroy', () => this._forget());
        Main.layoutManager.addTopChrome(this._group);

        this._frames = [];
        this._toast = new St.Label({
            reactive: false,
            visible: false,
            style: `background-color: rgba(20,20,22,0.92); color: #fff; border: 1px solid ${RED};` +
                'border-radius: 999px; padding: 8px 18px; font-size: 14px;',
        });
        this._toastBox = new St.Widget({
            reactive: false,
            layout_manager: new Clutter.BinLayout(),
        });
        this._toast.set({
            x_expand: true, y_expand: true,
            x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.START,
        });
        this._toastBox.add_child(this._toast);
        this._toastBox.set_style('padding-top: 14px;');

        this._pillDot = new St.Label({reactive: false, text: '●', style: `font-size: 14px; color: ${RED};`});
        this._pillText = new St.Label({reactive: false, style: 'font-size: 14px; color: #fff;'});
        const pillLine = new St.BoxLayout({reactive: false, style: 'spacing: 6px;'});
        pillLine.add_child(this._pillDot);
        pillLine.add_child(this._pillText);
        this._pillStatus = new St.Label({
            reactive: false,
            visible: false,
            style: 'font-size: 12px; color: rgba(255,255,255,0.7);',
            x_align: Clutter.ActorAlign.CENTER,
        });
        this._pill = new St.BoxLayout({
            reactive: false,
            orientation: Clutter.Orientation.VERTICAL,
            style: `background-color: rgba(20,20,22,0.92); color: #fff; border: 1px solid ${RED};` +
                'border-radius: 999px; padding: 8px 18px;',
        });
        this._pill.add_child(pillLine);
        this._pill.add_child(this._pillStatus);

        // The AI cursor lives in the overlay group: hidden from screenshots with it.
        this._cursorLayer = new St.Widget({reactive: false});
        this._group.add_child(this._cursorLayer);
        this._cursor = new AICursor(this._cursorLayer, onCursorChange);

        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._build());
        this._build();
    }

    get cursor() {
        return this._cursor;
    }

    // Show the AI cursor again (e.g. an op after a wrapup faded it) while a session is up.
    engageCursor() {
        if (this._active)
            this._cursor?.engage();
    }

    // Idempotent; also safe after the shell destroyed our actors itself.
    destroy() {
        this._cursor?.destroy(); // gives the real cursor back first
        this._cursor = null;
        const group = this._group;
        this._forget();
        group?.destroy();
    }

    // Drops every actor reference, timer and signal (the actors are gone or going).
    _forget() {
        this._cursor?.release();
        this._active = false;
        if (this._pulseId)
            GLib.source_remove(this._pulseId);
        this._pulseId = 0;
        if (this._toastId)
            GLib.source_remove(this._toastId);
        this._toastId = 0;
        if (this._monitorsId)
            Main.layoutManager.disconnect(this._monitorsId);
        this._monitorsId = 0;
        this._group = null;
        this._frames = [];
    }

    // Session overlay is up (true even while hidden for a screenshot capture).
    get active() {
        return this._active && this._group !== null;
    }

    get visible() {
        return this._group?.visible ?? false;
    }

    _build() {
        if (!this._group)
            return;
        for (const f of this._frames) {
            if (this._pill.get_parent() === f.inner)
                f.inner.remove_child(this._pill);
            f.frame.destroy();
        }
        this._frames = [];
        if (this._toastBox.get_parent())
            this._group.remove_child(this._toastBox);

        const monitors = Main.layoutManager.monitors;
        const primary = Main.layoutManager.primaryMonitor ?? monitors[0];
        for (const m of monitors) {
            const frame = new St.Widget({
                reactive: false,
                x: m.x, y: m.y, width: m.width, height: m.height,
                style: `border: ${BORDER_PX}px solid ${RED};`,
                layout_manager: new Clutter.BinLayout(),
            });
            // Separate glow layer so the pulse only fades the soft inner shadow.
            const glow = new Glow({reactive: false, x_expand: true, y_expand: true});
            const inner = new St.Widget({
                reactive: false,
                x_expand: true, y_expand: true,
                layout_manager: new Clutter.BinLayout(),
                style: 'padding-top: 4px;',
            });
            frame.add_child(glow);
            frame.add_child(inner);
            frame.visible = this._active;
            this._group.add_child(frame);
            this._frames.push({frame, glow, inner});
            if (m === primary) {
                inner.add_child(this._pill);
                this._pill.set({
                    x_expand: true, y_expand: true,
                    x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.START,
                });
            }
        }
        if (primary) {
            this._toastBox.set({x: primary.x, y: primary.y, width: primary.width, height: primary.height});
            this._group.add_child(this._toastBox);
        }
        this._group.set_child_above_sibling(this._cursorLayer, null);
        this._sync();
    }

    _sync() {
        if (!this._group)
            return; // destroyed (e.g. a capture finishing after disable)
        const name = esc(this._label);
        this._pillText.clutter_text.set_markup(
            `<b>${name} está controlando seu PC</b>` +
            ` · <span alpha="80%">${esc(this._hotkey)} para retomar</span>`);
        this._pillStatus.text = this._status;
        this._pillStatus.visible = this._status !== '';
        for (const f of this._frames)
            f.frame.visible = this._active;
        this._group.visible = this._captureHidden === 0 && (this._active || this._toast.visible);
    }

    // Shows the overlay; resolves once a frame containing it has been painted.
    show(label, hotkey) {
        if (!this._group)
            return Promise.resolve(false);
        if (this._toastId) {
            GLib.source_remove(this._toastId);
            this._toastId = 0;
        }
        this._toast.visible = false;
        this._active = true;
        this._label = label || 'IA';
        this._hotkey = hotkey || 'Ctrl+Alt+Esc';
        this._status = '';
        this._captureHidden = 0;
        this._sync();
        this._raise();
        this._startPulse();
        this._cursor.engage();
        return this._waitPainted();
    }

    hide(toastText = null) {
        // Real cursor back immediately, on every end path, before anything else.
        this._cursor?.release();
        if (!this._group)
            return;
        this._active = false;
        this._status = '';
        this._stopPulse();
        if (toastText) {
            this._toast.text = toastText;
            this._toast.visible = true;
            if (this._toastId)
                GLib.source_remove(this._toastId);
            this._toastId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TOAST_MS, () => {
                this._toastId = 0;
                if (this._group)
                    this._toast.visible = false;
                this._sync();
                return GLib.SOURCE_REMOVE;
            });
            this._raise();
        }
        this._sync();
    }

    setStatus(text) {
        if (!this._group)
            return;
        this._status = String(text ?? '').slice(0, 200);
        this._sync();
    }

    // Screenshot support: hide for the duration of a capture (nestable).
    async withHidden(fn) {
        this._captureHidden++;
        this._sync();
        try {
            return await fn();
        } finally {
            this._captureHidden--;
            this._sync();
        }
    }

    _raise() {
        const parent = this._group?.get_parent();
        parent?.set_child_above_sibling(this._group, null);
    }

    _startPulse() {
        if (this._pulseId || !this._group)
            return;
        this._pulseId = GLib.timeout_add(GLib.PRIORITY_LOW, PULSE_MS, () => {
            this._pulseHigh = !this._pulseHigh;
            for (const f of this._frames) {
                f.glow.ease({
                    opacity: this._pulseHigh ? 255 : 140,
                    duration: PULSE_MS - 100,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                });
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopPulse() {
        if (this._pulseId)
            GLib.source_remove(this._pulseId);
        this._pulseId = 0;
        if (!this._group)
            return;
        for (const f of this._frames) {
            f.glow.remove_all_transitions();
            f.glow.opacity = 255;
        }
    }

    // Resolves after the next stage paint (or a short timeout).
    async waitPaint() {
        await this._waitPainted();
    }

    _waitPainted() {
        return new Promise(resolve => {
            let paintId = 0, timeoutId = 0;
            // Only an actual paint confirms; a timeout means "not shown".
            const finish = painted => {
                if (paintId)
                    global.stage.disconnect(paintId);
                if (timeoutId)
                    GLib.source_remove(timeoutId);
                paintId = timeoutId = 0;
                resolve(painted && this._group?.visible === true && this._group.is_mapped() &&
                    this._frames.length > 0 && this._frames.every(f => f.frame.is_mapped()));
            };
            paintId = global.stage.connect('after-paint', () => finish(true));
            timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, PAINT_TIMEOUT_MS, () => {
                timeoutId = 0;
                finish(false);
                return GLib.SOURCE_REMOVE;
            });
            global.stage.queue_redraw();
        });
    }
}
