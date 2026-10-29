// PCPilot UI: connects to pcpilotd as role "ui", shows the "AI is in control" overlay
// while a session is active and executes screenshot / input / window ops for it.
//
// Authorization model: every state event starts a new generation. A request is bound at
// receipt to (generation, session token, connection epoch) and only runs if all three are
// still current and the overlay had been confirmed painted when it arrived. Going inactive
// flushes the queue. Ops run one at a time; an op that times out is cancelled and the
// queue waits for it to finish; one that ignores cancellation drops the connection
// (the daemon then ends the session) and keeps the queue blocked until it settles.

import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {DaemonClient} from './client.js';
import {Input, Token} from './input.js';
import {Ops} from './ops.js';
import {Overlay} from './overlay.js';
import {E_FAILED, E_INACTIVE, OpError, err, ok, toastText} from './protocol.js';

// Below the daemon's 20s ui_timeout. Ops validate their own worst case below this.
const OP_TIMEOUT_MS = 18000;
// After cancelling a timed-out op, how long it may take to wind down.
const CANCEL_GRACE_MS = 2000;

function settleWithin(promise, ms) {
    return new Promise(resolve => {
        let id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            id = 0;
            resolve({settled: false});
            return GLib.SOURCE_REMOVE;
        });
        const done = outcome => {
            if (id)
                GLib.source_remove(id);
            id = 0;
            resolve({settled: true, ...outcome});
        };
        promise.then(value => done({value}), error => done({error}));
    });
}

export default class PCPilotExtension extends Extension {
    enable() {
        this._overlay = new Overlay({onCursorChange: () => this._writeTestState()});
        this._input = new Input();
        this._ops = new Ops({overlay: this._overlay, input: this._input});
        this._gen = 0;
        this._active = false; // overlay confirmed painted for the current generation
        this._session = null; // daemon's session number, echoed in state_ack and pong
        this._sessionUp = false; // a session was started (overlay shown or showing)
        this._token = new Token();
        this._token.cancel();
        this._queue = [];
        this._pumping = false;
        this._hotCornersId = 0;
        this._client = new DaemonClient({
            onMessage: (msg, epoch) => this._onMessage(msg, epoch),
            onDisconnected: () => this._setInactive('connection_lost'),
        });
        this._client.start();
    }

    disable() {
        this._client.stop();
        this._client = null;
        this._setInactive(null);
        this._ops = null;
        this._input.destroy();
        this._input = null;
        this._overlay.destroy();
        this._overlay = null;
    }

    _onMessage(msg, epoch) {
        if (msg.event === 'ping') {
            // Health lease: answered right here on the main loop, never behind the op
            // queue, and only while this UI has the overlay up for exactly that session.
            if (this._active && msg.session === this._session && this._overlay?.active)
                this._client?.send({event: 'pong', session: msg.session}, epoch);
            return;
        }
        if (msg.event === 'state') {
            if (msg.active === true) {
                this._setActive(msg, epoch).catch(e => {
                    console.error(`pcpilot: activation failed: ${e.message}\n${e.stack}`);
                    this._setInactive(null);
                });
            } else {
                this._setInactive(msg.reason ?? null);
            }
            return;
        }
        if (msg.id !== undefined && typeof msg.op === 'string') {
            this._queue.push({
                msg, epoch,
                gen: this._gen,
                token: this._token,
                // Requests name their session; one for any other session is stale.
                authorized: this._active &&
                    (msg.session === undefined || msg.session === this._session),
            });
            this._pump();
        }
        // Anything else (stray replies, unknown events) is ignored.
    }

    // Hot corners are pressure barriers: injected absolute motion starting at or crossing
    // the top-left corner (the headless pointer starts at 0,0) opens the overview, and
    // every following click/key silently lands there. The AI never needs them, so they
    // are suspended while a session is active.
    _suspendHotCorners() {
        if (this._hotCornersId)
            return;
        const off = () => Main.layoutManager.hotCorners.forEach(c => c?.setBarrierSize(0));
        this._hotCornersId = Main.layoutManager.connect('hot-corners-changed', off);
        off();
    }

    _restoreHotCorners() {
        if (!this._hotCornersId)
            return;
        Main.layoutManager.disconnect(this._hotCornersId);
        this._hotCornersId = 0;
        const size = Main.layoutManager.panelBox.height;
        Main.layoutManager.hotCorners.forEach(c => c?.setBarrierSize(size));
    }

    async _setActive(msg, epoch) {
        const gen = ++this._gen;
        this._active = false;
        this._session = msg.session ?? null;
        this._token.cancel();
        this._flush();
        this._input.releaseAll();
        const token = new Token();
        this._token = token;
        this._sessionUp = true;
        this._suspendHotCorners();
        const shown = await this._overlay.show(msg.label, msg.hotkey);
        if (gen !== this._gen || !this._client || this._client.epoch !== epoch)
            return; // superseded while waiting for the paint
        if (!shown) {
            // Never acknowledge control that the user cannot see.
            console.warn('pcpilot: overlay was not painted; not acknowledging');
            this._setInactive(null);
            return;
        }
        this._active = true;
        try {
            this._ops.session.snapshot(); // baseline for `new` windows and the wrapup
        } catch (e) {
            console.error(`pcpilot: session snapshot failed: ${e.message}`);
        }
        this._writeTestState();
        const ack = {event: 'state_ack', active: true};
        if (this._session !== null)
            ack.session = this._session;
        this._client.send(ack, epoch);
    }

    _setInactive(reason) {
        const wasUp = this._sessionUp;
        this._gen++;
        this._active = false;
        this._session = null;
        this._sessionUp = false;
        // Every step runs even if an earlier one throws; the user's cursor comes first.
        const steps = [
            () => this._overlay?.cursor?.release(),
            () => this._token.cancel(), // stops glides, animations and a wrapup in flight
            () => this._flush(),
            () => this._input?.releaseAll(),
            () => this._restoreHotCorners(),
            () => this._overlay?.hide(wasUp && reason !== null ? toastText(reason) : null),
            () => this._ops?.session?.clear(),
        ];
        for (const step of steps) {
            try {
                step();
            } catch (e) {
                console.error(`pcpilot: cleanup step failed: ${e.message}\n${e.stack}`);
            }
        }
        this._writeTestState();
    }

    // Test-only introspection (PCPILOT_TEST_HOOKS=1 in the shell's environment, set by the
    // headless test harness, never in a real session): cursor/session state as JSON in
    // $XDG_RUNTIME_DIR/pcpilot/test-state.json, written on every state change.
    _writeTestState() {
        if (GLib.getenv('PCPILOT_TEST_HOOKS') !== '1')
            return;
        try {
            const dir = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'pcpilot']);
            GLib.mkdir_with_parents(dir, 0o700);
            const tracker = global.backend.get_cursor_tracker();
            GLib.file_set_contents(GLib.build_filenamev([dir, 'test-state.json']), JSON.stringify({
                active: this._active,
                session: this._session,
                cursor_inhibited: this._overlay?.cursor?.inhibited ?? false,
                cursor_engaged: this._overlay?.cursor?.engaged ?? false,
                real_cursor_visible: tracker.get_pointer_visible(),
                hot_corners_suspended: this._hotCornersId !== 0,
                time: GLib.get_monotonic_time(),
            }));
        } catch (e) {
            console.warn(`pcpilot: test state: ${e.message}`);
        }
    }

    // Refuse everything still queued (it belongs to a session that is over).
    _flush() {
        const stale = this._queue.splice(0);
        for (const e of stale)
            this._reply(e, err(e.msg.id, E_INACTIVE, 'session ended'));
    }

    _reply(entry, msg) {
        this._client?.send(msg, entry.epoch);
    }

    _current(entry) {
        return entry.authorized && this._active && entry.gen === this._gen &&
            !entry.token.cancelled && entry.token === this._token &&
            this._client?.epoch === entry.epoch;
    }

    async _pump() {
        if (this._pumping)
            return;
        this._pumping = true;
        try {
            while (this._queue.length > 0) {
                // eslint-disable-next-line no-await-in-loop
                await this._run(this._queue.shift());
            }
        } finally {
            this._pumping = false;
        }
    }

    async _run(entry) {
        const {id, op} = entry.msg;
        if (!this._current(entry)) {
            this._reply(entry, err(id, E_INACTIVE, 'no active session'));
            return;
        }
        const opToken = entry.token.child();
        const work = Promise.resolve().then(() => this._ops.run(op, entry.msg.args ?? {}, opToken));
        let outcome = await settleWithin(work, OP_TIMEOUT_MS);
        if (!outcome.settled) {
            opToken.cancel();
            outcome = await settleWithin(work, CANCEL_GRACE_MS);
            if (!outcome.settled) {
                // Cannot stop it: end control rather than let it act behind the next op.
                console.error(`pcpilot: ${op} ignored cancellation; dropping the session`);
                this._setInactive('ui_error');
                this._client?.reconnect();
                await work.catch(() => {});
            }
            this._reply(entry, err(id, E_FAILED, `${op} timed out`));
            return;
        }
        if (outcome.error !== undefined) {
            const e = outcome.error;
            if (e instanceof OpError) {
                this._reply(entry, err(id, e.code, e.message));
            } else {
                console.error(`pcpilot: ${op} failed: ${e?.message}\n${e?.stack}`);
                this._reply(entry, err(id, E_FAILED, String(e?.message ?? e)));
            }
            return;
        }
        this._reply(entry, opToken.cancelled
            ? err(id, E_INACTIVE, 'session ended')
            : ok(id, outcome.value ?? {}));
    }
}
