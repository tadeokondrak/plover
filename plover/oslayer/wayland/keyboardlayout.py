from xkbcommon import xkb

from plover.key_combo import add_modifiers_aliases, parse_key_combo
from plover.oslayer.xkeyboardcontrol import uchr_to_keysym


XKB_KEYCODE_OFFSET = 8
XKB_KEYCODE_MAX = 255

PLOVER_TAG = b'<PLVR>'
PLOVER_KEYMAP_TEMPLATE = (
b'''
xkb_keymap {
xkb_keycodes {
minimum = %u;
maximum = %u;
%s
};
xkb_types { include "complete" };
xkb_compatibility { include "complete" };
xkb_symbols {
%s
};
};
'''
)

XKB_ALIASES = (
    ('apostrophe', 'quoteright'),
    ('f11', 'l1'),
    ('f12', 'l2'),
    ('f13', 'l3'),
    ('f14', 'l4'),
    ('f15', 'l5'),
    ('f16', 'l6'),
    ('f17', 'l7'),
    ('f18', 'l8'),
    ('f19', 'l9'),
    ('f20', 'l10'),
    ('f21', 'r1'),
    ('f22', 'r2'),
    ('f23', 'r3'),
    ('f24', 'r4'),
    ('f25', 'r5'),
    ('f26', 'r6'),
    ('f27', 'r7'),
    ('f28', 'r8'),
    ('f29', 'r9'),
    ('f30', 'r10'),
    ('f31', 'r11'),
    ('f32', 'r12'),
    ('f33', 'r13'),
    ('f34', 'r14'),
    ('f35', 'r15'),
    ('grave', 'quoteleft'),
    ('henkan', 'henkan_mode'),
    ('kp_next', 'kp_page_down'),
    ('kp_page_up', 'kp_prior'),
    ('mae_koho', 'previouscandidate'),
    ('mode_switch', 'script_switch'),
    ('multiplecandidate', 'zen_koho'),
    ('next', 'page_down'),
    ('page_up', 'prior'),
)


class KeyComboLayout:

    def __init__(self, xkb_def_bytestring):
        '''Create a basic keyboard layout from a XKB keymap definition.'''
        # Ignore terminating null character.
        if xkb_def_bytestring.endswith(b'\x00'):
            xkb_def_bytestring = xkb_def_bytestring[:-1]
        keymap = xkb.Context().keymap_new_from_buffer(xkb_def_bytestring)
        modifiers = {
            keymap.mod_get_name(mod_index).lower(): 1 << mod_index
            for mod_index in range(keymap.num_mods())
        }
        for mod_name in ('alt', 'control', 'shift', 'super'):
            mods = modifiers.get(mod_name)
            if mods is not None:
                modifiers[mod_name + '_l'] = mods
                modifiers[mod_name + '_r'] = mods
        combo_from_keyname = {}
        keysym_level = {}
        for keycode in keymap:
            for level in range(keymap.num_levels_for_key(keycode, 0)):
                keysym = keymap.key_get_syms_by_level(keycode, 0, level)
                if len(keysym) != 1:
                    continue
                keysym = keysym[0]
                if not keysym:
                    # Ignore NoSymbol.
                    continue
                try:
                    keysym_name = xkb.keysym_get_name(keysym)
                except xkb.XKBInvalidKeysym:
                    continue
                if keysym_name.startswith('XF86'):
                    alias = keysym_name[4:].lower()
                    keysym_name = 'xf86_' + alias
                else:
                    alias = None
                    keysym_name = keysym_name.lower()
                if keysym_name in combo_from_keyname and level >= keysym_level.get(keysym, 0):
                    # Ignore if already available at a lower level.
                    continue
                assert keycode >= XKB_KEYCODE_OFFSET
                combo = (keycode - XKB_KEYCODE_OFFSET, modifiers.get(keysym_name, 0))
                combo_from_keyname[keysym_name] = combo
                if alias is not None:
                    combo_from_keyname[alias] = combo
                keysym_level[keysym] = level
        add_modifiers_aliases(combo_from_keyname)
        # Ensure all aliases for the same keysim are available.
        for alias_list in XKB_ALIASES:
            combo = next(filter(None, map(combo_from_keyname.get, alias_list)), None)
            if combo is not None:
                for alias in alias_list:
                    if alias not in combo_from_keyname:
                        combo_from_keyname[alias] = combo
        self._combo_from_keyname = combo_from_keyname

    def parse_key_combo(self, combo_string):
        return parse_key_combo(combo_string, self._combo_from_keyname.__getitem__)


class StringOutputLayout:

    def __init__(self):
        '''Create a custom layout for output strings.'''
        printable = {
            c
            for c in map(chr, range(XKB_KEYCODE_MAX))
            for c in (c.lower(), c.upper())
            if len(c) == 1 and c.isprintable()
        }
        # Note: we reserve the firt keycode for tagging the keymap
        # with our <PLVR> key and mapping the BackSpace keysym.
        max_mappings = XKB_KEYCODE_MAX - XKB_KEYCODE_OFFSET
        levels = 2
        char_to_combo = {}
        keymap = [[None] * levels for __ in range(max_mappings)]
        free_mappings = iter([
            (keycode, mod_level)
            for mod_level in range(levels)
            for keycode in range(1, max_mappings)
        ])
        for c in sorted(printable):
            keycode, mod_level = next(free_mappings)
            char_to_combo[c] = (keycode, 1 << mod_level >> 1)
            keymap[keycode][mod_level] = uchr_to_keysym(c)
        self._keymap = keymap
        self._char_to_combo = char_to_combo
        self._next_extra_mapping_index = 0
        self._extra_mappings = [[keycode, mod_level, None]
                                for keycode, mod_level in free_mappings]

    def string_to_combos(self, string):
        '''Return a tuple pair:
            - a boolean indicading if the keymap was updated
            - a list of `(keycode, modifiers)`
        '''
        combo_list = []
        updated = False
        for char in string:
            combo = self._char_to_combo.get(char)
            if combo is not None:
                combo_list.append(combo)
                continue
            extra_mapping = self._extra_mappings[self._next_extra_mapping_index]
            self._next_extra_mapping_index += 1
            self._next_extra_mapping_index %= len(self._extra_mappings)
            keycode, mod_level, old_char = extra_mapping
            if old_char is not None:
                del self._char_to_combo[old_char]
            extra_mapping[-1] = char
            self._keymap[keycode][mod_level] = uchr_to_keysym(char)
            self._char_to_combo[char] = combo = (keycode, 1 << mod_level >> 1)
            combo_list.append(combo)
            updated = True
        return updated, combo_list

    def to_xkb_def(self):
        '''Generate an XKB keymap definition for the layout.'''
        # Sway is more permissive than Xwayland on what an XKB keymap must
        # or must not include. We need to take care if we want to ensure
        # compatibility with both. See <https://github.com/atx/wtype/issues/1>
        keycodes_list = [b'%s = %u;' % (PLOVER_TAG, XKB_KEYCODE_OFFSET)]
        symbols_list = [b'key %s {[BackSpace]};' % PLOVER_TAG]
        for keycode, keysym_list in enumerate(self._keymap):
            keysym_list = [b'%#x' % keysym
                           for keysym in keysym_list
                           if keysym is not None]
            if not keysym_list:
                continue
            keycodes_list.append(b'<C%u> = %u;' % (keycode, XKB_KEYCODE_OFFSET + keycode))
            symbols_list.append(b'key <C%u> {[%s]};' % (keycode, b', '.join(keysym_list)))
        return PLOVER_KEYMAP_TEMPLATE % (
            XKB_KEYCODE_OFFSET, XKB_KEYCODE_OFFSET + len(self._keymap),
            b'\n'.join(keycodes_list),
            b'\n'.join(symbols_list),
        )
