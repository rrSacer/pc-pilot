// A static, themed safety frame on every monitor, a pill and a return toast. Chrome
// stays above fullscreen windows; every actor is non-reactive so input passes through.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {AICursor} from './cursor.js';
import {animate} from './motion.js';
import {frameStrips, paintBadge, paintFrame} from './paint.js';
import {themeFor} from './theme.js';

const TOAST_MS = 2500;
const PAINT_TIMEOUT_MS = 1500; // daemon waits 2s for the ack
const FINISH_MS = 320;
const PILL_TOP_PX = 6; // inside the top bar: the pill covers the clock instead of cutting through it
const BADGE_PX = 24;

const FrameStrip = GObject.registerClass(
class PCPilotFrameStrip extends St.DrawingArea {
    _init(monitor, region, theme) {
        super._init({reactive: false, ...region});
        this._monitor = monitor;
        this._region = region;
        this._theme = theme;
    }

    setTheme(theme) {
        this._theme = theme;
        this.queue_repaint();
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const [width] = this.get_surface_size();
        const scale = this.width > 0 ? width / this.width : 1;
        paintFrame(cr, {
            width: this._monitor.width * scale, height: this._monitor.height * scale,
            ox: this._region.x * scale, oy: this._region.y * scale,
            theme: this._theme, scale,
        });
        cr.$dispose?.();
    }
});

const Badge = GObject.registerClass(
class PCPilotBadge extends St.DrawingArea {
    _init(theme, size = BADGE_PX) {
        super._init({reactive: false, width: size, height: size, y_align: Clutter.ActorAlign.CENTER});
        this._theme = theme;
    }

    setTheme(theme) {
        this._theme = theme;
        this.queue_repaint();
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const [w, h] = this.get_surface_size();
        paintBadge(cr, w / 2, h / 2, Math.min(w, h) / 2 - 1, this._theme);
        cr.$dispose?.();
    }
});

export class Overlay {
    constructor({onCursorChange = null} = {}) {
        this._active = false;
        this._label = 'IA';
        this._hotkey = 'Ctrl+Alt+Esc';
        this._status = '';
        this._theme = themeFor();
        this._captureHidden = 0;
        this._toastId = 0;
        this._finishing = false;
        this._finished = false;
        this._dimmed = false; // a wrapup has started fading the overlay out
        this._visualEpoch = 0;
        this._frames = [];

        this._group = new St.Widget({name: 'pcpilotOverlay', reactive: false, visible: false});
        this._group.connect('destroy', () => this._forget());
        Main.layoutManager.addTopChrome(this._group);

        this._pillBadge = new Badge(this._theme);
        this._pillText = new St.Label({reactive: false});
        this._pillStatus = new St.Label({reactive: false, visible: false});
        const lines = new St.BoxLayout({reactive: false, orientation: Clutter.Orientation.VERTICAL,
            style: 'spacing: 3px;', y_align: Clutter.ActorAlign.CENTER});
        lines.add_child(this._pillText);
        lines.add_child(this._pillStatus);
        this._pill = new St.BoxLayout({reactive: false, style: 'spacing: 12px;',
            x_expand: true, y_expand: true,
            x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.START});
        this._pill.add_child(this._pillBadge);
        this._pill.add_child(lines);
        this._pillBox = new St.Widget({reactive: false, layout_manager: new Clutter.BinLayout(),
            style: `padding-top: ${PILL_TOP_PX}px;`});
        this._pillBox.add_child(this._pill);

        this._toastBadge = new Badge(this._theme, 20);
        this._toastText = new St.Label({reactive: false, y_align: Clutter.ActorAlign.CENTER});
        this._toast = new St.BoxLayout({reactive: false, visible: false,
            x_expand: true, y_expand: true,
            x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.START});
        this._toast.add_child(this._toastBadge);
        this._toast.add_child(this._toastText);
        this._toastBox = new St.Widget({reactive: false, layout_manager: new Clutter.BinLayout(),
            style: `padding-top: ${PILL_TOP_PX}px;`});
        this._toastBox.add_child(this._toast);

        this._cursorLayer = new St.Widget({reactive: false});
        this._group.add_child(this._cursorLayer);
        this._cursor = new AICursor(this._cursorLayer, onCursorChange);
        this._applyTheme();
        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._build());
        this._build();
    }

    get cursor() { return this._cursor; }
    get themeId() { return this._theme.id; }
    get active() { return this._active && this._group !== null; }
    get visible() { return this._group?.visible ?? false; }

    _applyTheme() {
        const p = this._theme.pill;
        // A dark, slightly translucent capsule with the theme's tinted hairline and a soft
        // shadow, so it reads on light and dark content alike.
        const surface = `background-color: ${p.bg}; border: 1px solid ${p.border};` +
            'border-radius: 22px; box-shadow: 0 3px 14px rgba(0, 0, 0, 0.38);';
        this._pill.set_style(`${surface} padding: 7px 18px 7px 9px; spacing: 11px;`);
        this._toast.set_style(`${surface} padding: 6px 16px 6px 8px; spacing: 9px;`);
        this._pillText.set_style(`font-size: 14px; color: ${p.text};`);
        this._pillStatus.set_style(`font-size: 12px; color: ${p.muted}; max-width: 560px;`);
        this._toastText.set_style(`font-size: 14px; color: ${p.text};`);
        this._pillBadge.setTheme(this._theme);
        this._toastBadge.setTheme(this._theme);
        for (const f of this._frames)
            for (const strip of f.strips)
                strip.setTheme(this._theme);
        this._cursor.setTheme(this._theme);
    }

    // Ops call this before they run (idempotent and cheap when nothing was faded). After a
    // wrapup faded the overlay out, any further op in the same session brings the frame,
    // pill and AI cursor fully back first: nothing acts behind an invisible safety signal.
    engageCursor() {
        if (!this.active)
            return;
        if (this._dimmed) {
            this._visualEpoch++; // also stops a fade that is still running
            this._finishing = this._finished = this._dimmed = false;
            this._setOpacity(255);
            this._sync();
        }
        this._cursor?.engage();
    }

    destroy() {
        this._cursor?.destroy(); // real cursor first
        this._cursor = null;
        const group = this._group;
        this._forget();
        group?.destroy();
    }

    _forget() {
        this._cursor?.release();
        this._active = false;
        this._visualEpoch++;
        if (this._toastId)
            GLib.source_remove(this._toastId);
        this._toastId = 0;
        if (this._monitorsId)
            Main.layoutManager.disconnect(this._monitorsId);
        this._monitorsId = 0;
        this._group = null;
        this._frames = [];
    }

    _build() {
        if (!this._group)
            return;
        for (const f of this._frames)
            f.frame.destroy();
        this._frames = [];
        const monitors = Main.layoutManager.monitors;
        const primary = Main.layoutManager.primaryMonitor ?? monitors[0];
        for (const m of monitors) {
            const frame = new St.Widget({reactive: false, x: m.x, y: m.y,
                width: m.width, height: m.height});
            const strips = frameStrips(m.width, m.height).map(region => new FrameStrip(m, region, this._theme));
            for (const strip of strips)
                frame.add_child(strip);
            this._group.add_child(frame);
            this._frames.push({frame, strips});
        }
        for (const box of [this._pillBox, this._toastBox]) {
            if (!box.get_parent())
                this._group.add_child(box);
            if (primary)
                box.set({x: primary.x, y: primary.y, width: primary.width, height: primary.height});
            this._group.set_child_above_sibling(box, null);
        }
        this._group.set_child_above_sibling(this._cursorLayer, null);
        this._sync();
    }

    _sync() {
        if (!this._group)
            return;
        const esc = text => GLib.markup_escape_text(String(text), -1);
        this._pillText.clutter_text.set_markup(`<b>${esc(this._label)} está controlando seu PC</b>` +
            `  ·  <span alpha="80%">${esc(this._hotkey)} para retomar</span>`);
        this._pillStatus.text = this._status;
        this._pillStatus.visible = this._status !== '';
        for (const f of this._frames)
            f.frame.visible = this._active && !this._finished;
        const detailsVisible = this._captureHidden === 0;
        this._pillBox.visible = this._active && !this._finished && detailsVisible;
        this._toastBox.visible = this._toast.visible && detailsVisible;
        this._cursorLayer.visible = detailsVisible;
        this._group.visible = this._active || this._toast.visible;
    }

    show(label, hotkey, themeName) {
        if (!this._group)
            return Promise.resolve(false);
        if (this._toastId)
            GLib.source_remove(this._toastId);
        this._toastId = 0;
        this._toast.visible = false;
        this._active = true;
        this._visualEpoch++;
        this._finishing = this._finished = this._dimmed = false;
        this._setOpacity(255);
        this._label = String(label || 'IA').slice(0, 64);
        this._hotkey = hotkey || 'Ctrl+Alt+Esc';
        this._status = '';
        this._theme = themeFor(themeName);
        this._applyTheme();
        this._sync();
        this._cursor?.engage();
        this._raise();
        return this._waitPainted();
    }

    hide(toastText = null) {
        this._cursor?.release(); // show the real pointer before touching any other actor
        if (!this._group)
            return;
        this._visualEpoch++;
        this._active = this._finishing = this._finished = this._dimmed = false;
        this._setOpacity(255);
        this._status = '';
        if (this._toastId)
            GLib.source_remove(this._toastId);
        this._toastId = 0;
        this._toast.visible = Boolean(toastText);
        if (toastText) {
            this._toastText.text = toastText;
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

    _setOpacity(value) {
        if (!this._group)
            return;
        for (const actor of [this._pillBox, this._cursorLayer, ...this._frames.map(f => f.frame)])
            actor.opacity = value;
    }

    // Runs BEFORE session.end, while input is still owned. Cancellation never finishes a
    // cosmetic animation after the user takes over: cursor.release/hide are synchronous.
    async finish(token) {
        token.check();
        if (!this.active || this._finished)
            return;
        const epoch = ++this._visualEpoch;
        this._finishing = this._dimmed = true;
        try {
            await animate(FINISH_MS, ms => {
                token.check();
                if (epoch === this._visualEpoch)
                    this._setOpacity(Math.round(255 * (1 - ms / FINISH_MS)));
            }, token);
            token.check();
            if (epoch !== this._visualEpoch || !this.active)
                return;
            this._cursor?.release();
            this._finished = true;
            this._sync();
        } finally {
            if (epoch === this._visualEpoch)
                this._finishing = false;
        }
    }

    setStatus(text) {
        if (!this._group)
            return;
        this._status = String(text ?? '').slice(0, 200);
        this._sync();
    }

    // Shell.Screenshot renders the stage offscreen synchronously when invoked. Hide only
    // content-covering actors around that call, restore before returning to the main loop.
    // The static, thin monitor frame stays present both on screen and in the image.
    async withHidden(start) {
        let pending;
        this._captureHidden++;
        this._sync();
        try {
            pending = start();
        } finally {
            this._captureHidden--;
            this._sync();
        }
        return await pending;
    }

    _raise() {
        this._group?.get_parent()?.set_child_above_sibling(this._group, null);
    }

    async waitPaint() { await this._waitPainted(); }

    _waitPainted() {
        return new Promise(resolve => {
            let paintId = 0, timeoutId = 0;
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
