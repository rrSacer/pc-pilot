// Mirror of pcpilot/protocol.py (codes and reasons the UI uses).

export const PROTOCOL_VERSION = 1;
export const DEFAULT_SOCKET = '/run/pcpilot/pcpilot.sock';
export const MAX_LINE = 1 << 20;

export const E_BAD_ARGS = 'bad_args';
export const E_UNKNOWN_OP = 'unknown_op';
export const E_INACTIVE = 'inactive';
export const E_NOT_FOUND = 'not_found';
export const E_FAILED = 'failed';
export const E_UNAUTHORIZED = 'unauthorized';
export const E_BUSY = 'busy';

export const R_USER_HOTKEY = 'user_hotkey';
export const R_USER_RELEASE = 'user_release';
export const R_IDLE_TIMEOUT = 'idle_timeout';
export const R_MAX_DURATION = 'max_duration';
export const R_UI_DISCONNECTED = 'ui_disconnected';
export const R_UI_TIMEOUT = 'ui_timeout';
export const R_AGENT_REQUEST = 'agent_request';
export const R_AGENT_DISCONNECTED = 'agent_disconnected';
export const R_SHUTDOWN = 'shutdown';
export const R_ERROR = 'error';
export const R_INPUT_ERROR = 'input_error';

// Toast text per session end reason ("Controle devolvido a você — <motivo>").
// Unknown reasons get the generic toast without a motive.
export const TOAST_PREFIX = 'Controle devolvido a você';
export const REASON_TEXT = {
    [R_USER_HOTKEY]: 'atalho de retomada',
    [R_USER_RELEASE]: 'liberado pelo comando pcpilot release',
    [R_UI_TIMEOUT]: 'a interface demorou a responder',
    [R_ERROR]: 'erro interno do serviço',
    [R_INPUT_ERROR]: 'nenhum teclado disponível para o atalho de retomada',
    [R_IDLE_TIMEOUT]: 'IA ociosa',
    [R_MAX_DURATION]: 'tempo máximo da sessão',
    [R_UI_DISCONNECTED]: 'interface desconectada',
    [R_AGENT_REQUEST]: 'a IA terminou',
    [R_AGENT_DISCONNECTED]: 'a IA desconectou',
    [R_SHUTDOWN]: 'serviço encerrado',
    connection_lost: 'conexão com o serviço perdida',
};

export function toastText(reason) {
    const why = REASON_TEXT[reason];
    return why ? `${TOAST_PREFIX} — ${why}` : TOAST_PREFIX;
}

export class OpError extends Error {
    constructor(code, message = '') {
        super(message || code);
        this.code = code;
    }
}

export function ok(id, result = {}) {
    return {id, ok: true, result};
}

export function err(id, code, message = '') {
    return {id, ok: false, error: {code, message}};
}
