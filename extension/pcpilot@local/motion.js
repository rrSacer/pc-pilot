// Frame-clock driven animation: one Clutter.Timeline per animation, ticking with the
// compositor's frames (no timers when nothing moves). Cancelled with the session/op token:
// the timeline stops at once and the promise rejects with `inactive`.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';

import {E_INACTIVE, OpError} from './protocol.js';
import {clampToMonitors} from './windows.js';

// If the frame clock stalls (e.g. nothing gets painted), jump to the end this long after
// the planned duration instead of hanging until the op timeout.
const STALL_GRACE_MS = 500;

// Calls onFrame(elapsedMs) on every frame for `duration` ms, then onFrame(duration) once.
export function animate(duration, onFrame, token) {
    token.check();
    if (duration <= 0) {
        onFrame(0);
        return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
        const timeline = new Clutter.Timeline({duration, actor: global.stage});
        let stallId = 0, dispose = null, done = false;
        const finish = error => {
            if (done)
                return;
            done = true;
            dispose?.();
            if (stallId)
                GLib.source_remove(stallId);
            stallId = 0;
            timeline.stop();
            if (error) {
                reject(error);
                return;
            }
            try {
                onFrame(duration);
            } catch (e) {
                reject(e);
                return;
            }
            resolve();
        };
        timeline.connect('new-frame', () => {
            if (done)
                return;
            try {
                onFrame(Math.min(timeline.get_elapsed_time(), duration));
            } catch (e) {
                finish(e);
            }
        });
        timeline.connect('completed', () => finish(null));
        dispose = token.onCancel(() => finish(new OpError(E_INACTIVE, 'session ended')));
        stallId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, duration + STALL_GRACE_MS, () => {
            stallId = 0;
            finish(null);
            return GLib.SOURCE_REMOVE;
        });
        timeline.start();
    });
}

// Moves the virtual pointer along a planned path (see path.js).
export function glide(input, path, token, onPoint = null) {
    return animate(path.duration, ms => {
        // Overshoot/bow may poke past a screen edge or into a gap between monitors.
        const [x, y] = clampToMonitors(...path.at(ms));
        input.move(x, y);
        onPoint?.(x, y);
    }, token);
}
