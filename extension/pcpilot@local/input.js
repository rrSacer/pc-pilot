// Clutter virtual pointer + keyboard. Tracks everything it holds down so it can all be
// released the moment the session ends.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {E_BAD_ARGS, E_INACTIVE, OpError} from './protocol.js';
import {onMonitor} from './windows.js';

export const BUTTONS = {
    left: Clutter.BUTTON_PRIMARY,
    middle: Clutter.BUTTON_MIDDLE,
    right: Clutter.BUTTON_SECONDARY,
};

// Cancellation token: one per session, plus a child per op (cancelled when the session
// ends or when that op times out). Ops check it between steps and before side effects.
export class Token {
    constructor() {
        this.cancelled = false;
        this._waiters = new Set();
        this._cancellable = null;
    }

    // A token cancelled together with this one (but cancellable on its own).
    child() {
        const t = new Token();
        if (this.cancelled) {
            t.cancel();
        } else {
            const onCancel = () => t.cancel();
            this._waiters.add(onCancel);
            t._waiters.add(() => this._waiters.delete(onCancel));
        }
        return t;
    }

    // Gio.Cancellable mirroring this token, for async GIO calls.
    get cancellable() {
        if (!this._cancellable) {
            this._cancellable = new Gio.Cancellable();
            if (this.cancelled)
                this._cancellable.cancel();
        }
        return this._cancellable;
    }

    cancel() {
        if (this.cancelled)
            return;
        this.cancelled = true;
        const waiters = [...this._waiters];
        this._waiters.clear();
        for (const w of waiters)
            w();
        this._cancellable?.cancel();
    }

    // Calls cb once on cancellation (immediately if already cancelled); returns a disposer.
    onCancel(cb) {
        if (this.cancelled) {
            cb();
            return () => {};
        }
        this._waiters.add(cb);
        return () => this._waiters.delete(cb);
    }

    check() {
        if (this.cancelled)
            throw new OpError(E_INACTIVE, 'session ended');
    }

    // Non-blocking sleep that rejects as soon as the token is cancelled.
    sleep(ms) {
        this.check();
        return new Promise((resolve, reject) => {
            let id = 0;
            const onCancel = () => {
                if (id)
                    GLib.source_remove(id);
                id = 0;
                reject(new OpError(E_INACTIVE, 'session ended'));
            };
            this._waiters.add(onCancel);
            const cb = () => {
                id = 0;
                this._waiters.delete(onCancel);
                resolve();
                return GLib.SOURCE_REMOVE;
            };
            id = ms > 0
                ? GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, cb)
                : GLib.idle_add(GLib.PRIORITY_DEFAULT, cb);
        });
    }
}

function now() {
    return GLib.get_monotonic_time();
}

export class Input {
    constructor() {
        const seat = global.stage.context.get_backend().get_default_seat();
        this._pointer = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        this._keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._buttons = new Set();
        this._keys = []; // keyvals currently held, in press order
    }

    destroy() {
        this.releaseAll();
        this._pointer = null;
        this._keyboard = null;
    }

    static checkPoint(x, y) {
        if (!Number.isFinite(x) || !Number.isFinite(y))
            throw new OpError(E_BAD_ARGS, 'x and y must be numbers');
        if (!onMonitor(x, y))
            throw new OpError(E_BAD_ARGS, `point ${x},${y} is outside the screen`);
    }

    move(x, y) {
        Input.checkPoint(x, y);
        this._pointer.notify_absolute_motion(now(), x, y);
    }

    position() {
        const [x, y] = global.get_pointer();
        return {x, y};
    }

    button(button, pressed) {
        if (!pressed && !this._buttons.has(button))
            return; // never send a release for something we are not holding
        this._pointer.notify_button(now(), button,
            pressed ? Clutter.ButtonState.PRESSED : Clutter.ButtonState.RELEASED);
        if (pressed)
            this._buttons.add(button);
        else
            this._buttons.delete(button);
    }

    scroll(direction) {
        this._pointer.notify_discrete_scroll(now(), direction, Clutter.ScrollSource.WHEEL);
    }

    key(keyval, pressed) {
        if (!pressed && !this._keys.includes(keyval))
            return;
        this._keyboard.notify_keyval(now(), keyval,
            pressed ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);
        if (pressed) {
            this._keys.push(keyval);
        } else {
            const i = this._keys.lastIndexOf(keyval);
            if (i >= 0)
                this._keys.splice(i, 1);
        }
    }

    tap(keyval) {
        this.key(keyval, true);
        this.key(keyval, false);
    }

    // Press keyvals in order, then release in reverse.
    chord(keyvals) {
        for (const k of keyvals)
            this.key(k, true);
        for (const k of [...keyvals].reverse())
            this.key(k, false);
    }

    releaseAll() {
        if (!this._keyboard)
            return;
        for (const k of [...this._keys].reverse())
            this.key(k, false);
        this._keys = [];
        for (const b of [...this._buttons])
            this.button(b, false);
        this._buttons.clear();
    }
}
