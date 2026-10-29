// JSON-lines client of the pcpilotd socket, with reconnect backoff.
// The extension is only ever a client: it exposes nothing itself.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {DEFAULT_SOCKET, MAX_LINE, PROTOCOL_VERSION} from './protocol.js';

Gio._promisify(Gio.SocketClient.prototype, 'connect_async');
Gio._promisify(Gio.DataInputStream.prototype, 'read_line_async');
Gio._promisify(Gio.OutputStream.prototype, 'write_all_async');

const BACKOFF_MIN_MS = 500;
const BACKOFF_MAX_MS = 5000;

export class DaemonClient {
    // onMessage(msg, epoch): every incoming object except the reply to our hello; epoch
    // identifies the connection it came on (replies must go back on that same one).
    // onConnected() / onDisconnected(): link state (after hello succeeds / on loss).
    constructor({onMessage, onConnected, onDisconnected}) {
        this._onMessage = onMessage;
        this._onConnected = onConnected;
        this._onDisconnected = onDisconnected;
        this._path = GLib.getenv('PCPILOT_SOCKET') || DEFAULT_SOCKET;
        this._backoff = BACKOFF_MIN_MS;
        this._retryId = 0;
        this._conn = null;
        this._cancellable = null;
        this._writeChain = Promise.resolve();
        this._encoder = new TextEncoder();
        this._decoder = new TextDecoder();
        this._stopped = true;
        this._loggedFailure = false;
        this._connected = false;
        this._epoch = 0;
    }

    start() {
        this._stopped = false;
        this._connect();
    }

    stop() {
        this._stopped = true;
        if (this._retryId) {
            GLib.source_remove(this._retryId);
            this._retryId = 0;
        }
        this._teardown(false);
    }

    get connected() {
        return this._connected;
    }

    // Identifies the current connection; changes on every (re)connect.
    get epoch() {
        return this._epoch;
    }

    // Drop the current connection (the daemon then ends any session) and reconnect.
    reconnect() {
        if (this._conn)
            this._lost(this._conn);
    }

    // Sends on the current connection; with `epoch`, only if it is still that connection.
    send(msg, epoch = null) {
        if (epoch !== null && epoch !== this._epoch)
            return;
        const conn = this._conn;
        const cancellable = this._cancellable;
        if (!conn)
            return;
        const bytes = this._encoder.encode(`${JSON.stringify(msg)}\n`);
        const out = conn.get_output_stream();
        this._writeChain = this._writeChain.then(async () => {
            if (conn !== this._conn)
                return;
            try {
                await out.write_all_async(bytes, GLib.PRIORITY_DEFAULT, cancellable);
            } catch (e) {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    console.warn(`pcpilot: write failed: ${e.message}`);
                this._lost(conn);
            }
        });
    }

    async _connect() {
        this._retryId = 0;
        if (this._stopped)
            return;
        const cancellable = new Gio.Cancellable();
        this._cancellable = cancellable;
        let conn;
        try {
            const client = new Gio.SocketClient();
            conn = await client.connect_async(
                new Gio.UnixSocketAddress({path: this._path}), cancellable);
        } catch (e) {
            if (this._stopped || cancellable.is_cancelled())
                return;
            if (!this._loggedFailure) {
                console.log(`pcpilot: cannot connect to ${this._path}: ${e.message} (retrying)`);
                this._loggedFailure = true;
            }
            this._scheduleRetry();
            return;
        }
        if (this._stopped || cancellable.is_cancelled()) {
            conn.close(null);
            return;
        }
        this._conn = conn;
        this._epoch++;
        this._writeChain = Promise.resolve();
        const input = new Gio.DataInputStream({
            base_stream: conn.get_input_stream(),
            close_base_stream: false,
        });
        this.send({op: 'hello', id: 1, args: {role: 'ui', version: PROTOCOL_VERSION}});
        this._readLoop(conn, input, cancellable, this._epoch);
    }

    async _readLoop(conn, input, cancellable, epoch) {
        let helloDone = false;
        try {
            for (;;) {
                // eslint-disable-next-line no-await-in-loop
                const [line, len] = await input.read_line_async(GLib.PRIORITY_DEFAULT, cancellable);
                if (line === null)
                    break; // EOF
                if (len > MAX_LINE) {
                    console.warn('pcpilot: line too long, dropping connection');
                    break;
                }
                let msg;
                try {
                    msg = JSON.parse(this._decoder.decode(line));
                } catch {
                    console.warn('pcpilot: bad JSON from daemon');
                    continue;
                }
                if (!msg || typeof msg !== 'object')
                    continue;
                if (!helloDone && msg.id === 1 && 'ok' in msg && !('op' in msg)) {
                    helloDone = true;
                    if (!msg.ok) {
                        console.warn(`pcpilot: hello refused: ${JSON.stringify(msg.error)}`);
                        break;
                    }
                    this._backoff = BACKOFF_MIN_MS;
                    this._loggedFailure = false;
                    this._connected = true;
                    console.log(`pcpilot: connected to ${this._path}`);
                    this._onConnected?.();
                    continue;
                }
                try {
                    if (conn !== this._conn)
                    break;
                this._onMessage(msg, epoch);
                } catch (e) {
                    console.error(`pcpilot: handler error: ${e.message}\n${e.stack}`);
                }
            }
        } catch (e) {
            if (cancellable.is_cancelled())
                return;
            console.warn(`pcpilot: read failed: ${e.message}`);
        }
        this._lost(conn);
    }

    _lost(conn) {
        if (conn !== this._conn)
            return;
        this._teardown(true);
        this._scheduleRetry();
    }

    _teardown(notify) {
        const wasConnected = this._connected;
        this._connected = false;
        this._cancellable?.cancel();
        this._cancellable = null;
        if (this._conn) {
            this._conn.close_async(GLib.PRIORITY_DEFAULT, null, (c, res) => {
                try {
                    c.close_finish(res);
                } catch {}
            });
            this._conn = null;
            this._epoch++;
        }
        if (notify && wasConnected)
            this._onDisconnected?.();
    }

    _scheduleRetry() {
        if (this._stopped || this._retryId)
            return;
        const delay = this._backoff;
        this._backoff = Math.min(this._backoff * 2, BACKOFF_MAX_MS);
        this._retryId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
            this._retryId = 0;
            this._connect();
            return GLib.SOURCE_REMOVE;
        });
    }
}
