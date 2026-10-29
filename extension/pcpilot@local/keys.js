// xdotool-style key names -> Clutter keyvals.

import Clutter from 'gi://Clutter';

import {E_BAD_ARGS, OpError} from './protocol.js';

const ALIASES = {
    enter: 'Return', return: 'Return', ret: 'Return',
    esc: 'Escape', escape: 'Escape',
    backspace: 'BackSpace', bksp: 'BackSpace',
    del: 'Delete', delete: 'Delete',
    ins: 'Insert', insert: 'Insert',
    pgup: 'Page_Up', pageup: 'Page_Up', page_up: 'Page_Up', prior: 'Page_Up',
    pgdn: 'Page_Down', pagedown: 'Page_Down', page_down: 'Page_Down', next: 'Page_Down',
    space: 'space', spc: 'space',
    tab: 'Tab',
    up: 'Up', down: 'Down', left: 'Left', right: 'Right',
    home: 'Home', end: 'End',
    menu: 'Menu', print: 'Print', printscreen: 'Print',
    capslock: 'Caps_Lock', caps_lock: 'Caps_Lock',
    ctrl: 'Control_L', control: 'Control_L', control_l: 'Control_L', control_r: 'Control_R',
    shift: 'Shift_L', shift_l: 'Shift_L', shift_r: 'Shift_R',
    alt: 'Alt_L', alt_l: 'Alt_L', alt_r: 'Alt_R',
    super: 'Super_L', super_l: 'Super_L', super_r: 'Super_R',
    meta: 'Super_L', win: 'Super_L', cmd: 'Super_L', logo: 'Super_L',
    altgr: 'ISO_Level3_Shift',
    plus: 'plus', minus: 'minus',
};

export const MODIFIER_KEYVALS = {
    ctrl: Clutter.KEY_Control_L,
    shift: Clutter.KEY_Shift_L,
    alt: Clutter.KEY_Alt_L,
    super: Clutter.KEY_Super_L,
};

let lowerMap = null;

function lowerKeyMap() {
    if (!lowerMap) {
        lowerMap = new Map();
        for (const k of Object.keys(Clutter)) {
            if (!k.startsWith('KEY_'))
                continue;
            const name = k.slice(4).toLowerCase();
            // Prefer the first (canonical-case) spelling if several differ only by case.
            if (!lowerMap.has(name))
                lowerMap.set(name, Clutter[k]);
        }
    }
    return lowerMap;
}

export function charToKeyval(ch) {
    if (ch === '\n' || ch === '\r')
        return Clutter.KEY_Return;
    if (ch === '\t')
        return Clutter.KEY_Tab;
    return Clutter.unicode_to_keysym(ch.codePointAt(0));
}

// One key name -> keyval. Single characters map to their unicode keyval as typed;
// lowercaseLetter is used inside chords ("ctrl+T" means ctrl+t, like xdotool).
export function nameToKeyval(name, lowercaseLetter = false) {
    if ([...name].length === 1) {
        let ch = name;
        if (lowercaseLetter && ch.toLowerCase() !== ch.toUpperCase())
            ch = ch.toLowerCase();
        return charToKeyval(ch);
    }
    const alias = ALIASES[name.toLowerCase()];
    if (alias !== undefined)
        return Clutter[`KEY_${alias}`];
    const exact = Clutter[`KEY_${name}`];
    if (typeof exact === 'number')
        return exact;
    const lower = lowerKeyMap().get(name.toLowerCase());
    if (lower !== undefined)
        return lower;
    throw new OpError(E_BAD_ARGS, `unknown key name: ${name}`);
}

// "ctrl+shift+t" -> [Control_L, Shift_L, t]; the last element is the key, the rest are held.
export function parseChord(chord) {
    if (typeof chord !== 'string' || chord === '')
        throw new OpError(E_BAD_ARGS, 'key chord must be a non-empty string');
    let parts;
    if (chord === '+') {
        parts = ['+'];
    } else if (chord.endsWith('++')) {
        parts = [...chord.slice(0, -2).split('+'), '+'];
    } else {
        parts = chord.split('+');
    }
    if (parts.some(p => p === ''))
        throw new OpError(E_BAD_ARGS, `bad key chord: ${chord}`);
    const chorded = parts.length > 1;
    return parts.map(p => nameToKeyval(p.trim() || p, chorded));
}
