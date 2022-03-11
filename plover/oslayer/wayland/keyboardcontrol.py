"""Keyboard capture and control on Wayland.

This module provides an interface for capturing and emulating keyboard events
on Wayland compositors that support the 'virtual_keyboard_unstable_v1' and
'input_method_unstable_v2' protocols (that is, wlroots-based compositors
like Sway, as of January 2022).
"""

import os
import select
import threading
import time

from pywayland.client.display import Display
from pywayland.protocol.wayland.wl_seat import WlSeat

from plover.oslayer.xkeyboardcontrol import KEYCODE_TO_KEY

from .keyboardlayout import PLOVER_TAG, KeyComboLayout, StringOutputLayout
# Protocol modules generated from XML description files at build time.
from .input_method_unstable_v2 import ZwpInputMethodManagerV2
from .virtual_keyboard_unstable_v1 import ZwpVirtualKeyboardManagerV1


class KeyboardHandler:

    _INTERFACES = {
        interface.name: (nick, interface)
        for nick, interface in (
            ('seat', WlSeat),
            ('input_method', ZwpInputMethodManagerV2),
            ('virtual_keyboard', ZwpVirtualKeyboardManagerV1),
        )}

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._loop_thread = None
        self._pipe = None
        # Common for capture and emulation.
        self._display = None
        self._interface = None
        self._keyboard = None
        self._keymap = None
        self._replay_keyboard = None
        self._replay_layout = None
        # For capture only.
        self._refcount_capture = 0
        self._grabbed_keyboard = None
        self._input_method = None
        self._event_listeners = {
            'grab_key': set(),
            'grab_modifiers': set(),
        }
        # For emulation only.
        self._refcount_emulate = 0
        self._output_keyboard = None
        self._output_layout = None

    def _event_loop(self):
        with self._lock:
            readfds = (self._pipe[0], self._display.get_fd())
        while True:
            # Sleep until we get new data on the display connection,
            # or on the pipe used to signal the end of the loop.
            rlist, wlist, xlist = select.select(readfds, (), ())
            assert not wlist
            assert not xlist
            if self._pipe[0] in rlist:
                break
            # If we're here, rlist should contains
            # the display fd, process pending events.
            with self._lock:
                self._display.dispatch(block=True)
                self._display.flush()

    def __enter__(self):
        self._lock.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None and self._display is not None:
            self._display.flush()
        self._lock.__exit__(exc_type, exc_value, traceback)

    def _on_registry_global(self, obj, name, interface_name, interface_version):
        if interface_name not in self._INTERFACES:
            return
        nick, interface = self._INTERFACES[interface_name]
        self._interface[nick] = obj.bind(name, interface, interface_version)

    def _on_keymap(self, __keyboard, fmt, fd, size):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            keymap = os.read(fd, size)
            is_generated = PLOVER_TAG in keymap
            if is_generated or keymap == self._keymap:
                return
            self._replay_layout = KeyComboLayout(keymap)
            self._replay_keyboard.keymap(fmt, fd, size)
            self._keymap = keymap
        finally:
            os.close(fd)

    def _on_grab_key(self, __grabbed_keyboard, __serial, origtime, keycode, state):
        suppressed = False
        try:
            for cb in self._event_listeners['grab_key']:
                suppressed |= cb(origtime, keycode, state)
        finally:
            if not suppressed:
                self._replay_keyboard.key(origtime, keycode, state)

    def _on_grab_modifiers(self, __grabbed_keyboard, __serial, depressed, latched, locked, layout):
        suppressed = False
        try:
            for cb in self._event_listeners['grab_modifiers']:
                suppressed |= cb(depressed, latched, locked, layout)
        finally:
            if not suppressed:
                self._replay_keyboard.modifiers(depressed, latched, locked, layout)

    def _update_output_keymap(self):
        xkb_keymap = self._output_layout.to_xkb_def()
        fd = os.memfd_create('emulated_keymap.xkb')
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, xkb_keymap)
            self._output_keyboard.keymap(1, fd, len(xkb_keymap))
        finally:
            os.close(fd)

    def _ensure_interfaces(self, mode, interface_list):
        missing_interfaces = [
            interface_name
            for interface_name in interface_list
            if interface_name not in self._interface
        ]
        if missing_interfaces:
            missing_interfaces = ', '.join(f'\'{name}\'' for name in missing_interfaces)
            raise RuntimeError(f'Cannot {mode} keyboard events: your '
                               f'Wayland compositor does not support '
                               f'the following interfaces: '
                               f'{missing_interfaces}')

    def _setup_base(self):
        self._display = Display()
        self._display.connect()
        self._interface = {}
        reg = self._display.get_registry()
        reg.dispatcher['global'] = self._on_registry_global
        self._display.roundtrip()
        self._replay_keyboard = self._interface['virtual_keyboard'].create_virtual_keyboard(self._interface['seat'])
        self._keyboard = self._interface['seat'].get_keyboard()
        self._keyboard.dispatcher['keymap'] = self._on_keymap
        self._display.roundtrip()
        self._pipe = os.pipe()
        self._loop_thread = threading.Thread(target=self._event_loop)
        self._loop_thread.start()

    def _teardown_base(self):
        if self._loop_thread is not None:
            # Wake up the capture thread...
            os.write(self._pipe[1], b'quit')
            # ...and wait for it to terminate.
            self._loop_thread.join()
            self._loop_thread = None
            for fd in self._pipe:
                os.close(fd)
            self._pipe = None
        self._replay_keyboard = None
        self._replay_layout = None
        self._keymap = None
        if self._keyboard is not None:
            self._keyboard.release()
            self._keyboard = None
        while self._interface:
            self._interface.popitem()[1].release()
        self._interface = None
        if self._display is not None:
            self._display.disconnect()
            self._display = None

    def _setup_capture(self):
        self._ensure_interfaces('capture', ('seat', 'input_method', 'virtual_keyboard'))
        self._input_method = self._interface['input_method'].get_input_method(self._interface['seat'])
        self._grabbed_keyboard = self._input_method.grab_keyboard()
        self._grabbed_keyboard.dispatcher['key'] = self._on_grab_key
        self._grabbed_keyboard.dispatcher['modifiers'] = self._on_grab_modifiers

    def _teardown_capture(self):
        self._event_listeners['grab_key'].clear()
        self._event_listeners['grab_modifiers'].clear()
        if self._grabbed_keyboard is not None:
            self._grabbed_keyboard.destroy()
            self._grabbed_keyboard = None
        if self._input_method is not None:
            self._input_method.destroy()
            self._input_method = None

    def _setup_emulate(self):
        self._ensure_interfaces('emulate', ('seat', 'virtual_keyboard'))
        self._output_keyboard = self._interface['virtual_keyboard'].create_virtual_keyboard(self._interface['seat'])
        self._output_layout = StringOutputLayout()
        self._update_output_keymap()

    def _teardown_emulate(self):
        self._output_keyboard = None
        self._output_layout = None

    def incref(self, mode):
        if mode not in ('capture', 'emulate'):
            raise ValueError(mode)
        refattr = '_refcount_' + mode
        refcount = getattr(self, refattr) + 1
        assert refcount >= 1
        setattr(self, refattr, refcount)
        try:
            total_refcount = self._refcount_capture + self._refcount_emulate
            if total_refcount == 1:
                self._setup_base()
            if refcount == 1:
                getattr(self, '_setup_' + mode)()
        except:
            self.decref(mode)
            raise

    def decref(self, mode):
        if mode not in ('capture', 'emulate'):
            raise ValueError(mode)
        refattr = '_refcount_' + mode
        refcount = getattr(self, refattr) - 1
        assert refcount >= 0
        setattr(self, refattr, refcount)
        if refcount == 0:
            getattr(self, '_teardown_' + mode)()
        if self._refcount_capture + self._refcount_emulate == 0:
            self._teardown_base()

    def add_event_listener(self, event, callback):
        self._event_listeners[event].add(callback)

    def remove_event_listener(self, event, callback):
        self._event_listeners[event].discard(callback)

    def send_string(self, string):
        timestamp = time.thread_time_ns() // (10 ** 3)
        keymap_updated, combo_list = self._output_layout.string_to_combos(string)
        if keymap_updated:
            self._update_output_keymap()
        mods_state = 0
        for keycode, mods in combo_list:
            if mods != mods_state:
                self._output_keyboard.modifiers(mods_depressed=mods,
                                                mods_latched=0,
                                                mods_locked=0,
                                                group=0)
                mods_state = mods
            self._output_keyboard.key(timestamp, keycode, 1)
            self._output_keyboard.key(timestamp, keycode, 0)
        if mods_state:
            self._output_keyboard.modifiers(mods_depressed=0,
                                            mods_latched=0,
                                            mods_locked=0,
                                            group=0)

    def send_backspaces(self, count):
        timestamp = time.thread_time_ns() // (10 ** 3)
        for __ in range(count):
            self._output_keyboard.key(timestamp, 0, 1)
            self._output_keyboard.key(timestamp, 0, 0)

    def send_key_combination(self, combo_string):
        timestamp = time.thread_time_ns() // (10 ** 3)
        mods_state = 0
        for (keycode, mods), pressed in self._replay_layout.parse_key_combo(combo_string):
            self._replay_keyboard.key(timestamp, keycode, int(pressed))
            if mods:
                if pressed:
                    mods_state |= mods
                else:
                    mods_state &= ~mods
                self._replay_keyboard.modifiers(mods_depressed=mods_state,
                                                mods_latched=0,
                                                mods_locked=0,
                                                group=0)
        assert not mods_state


_keyboard = KeyboardHandler()


class KeyboardCapture:
    """Listen to keyboard press and release events.

    This uses the 'input_method_unstable_v2' protocol to grab the Wayland
    keyboard. This grab is global and unconditional, therefore a virtual
    keyboard input is also created (using the 'virtual_keyboard_unstable_v1'
    protocol) to forward events that do not need to be captured by Plover.
    Note that this grab will also capture events generated by the
    KeyboardEmulation class, those events need to be actively filtered out
    to avoid infinite feedback loops.
    """
    def __init__(self):
        self._started = False
        self._mod_state = 0
        self._grabbed_keyboard = None
        self._suppressed_keys = set()
        # Callbacks that receive keypresses.
        self.key_down = lambda key: None
        self.key_up = lambda key: None

    def start(self):
        """Connect to the Wayland compositor and start the event loop."""
        with _keyboard:
            _keyboard.add_event_listener('grab_key', self._on_grab_key)
            _keyboard.add_event_listener('grab_modifiers', self._on_grab_modifiers)
            _keyboard.incref('capture')
        self._started = True

    def cancel(self):
        """Cancel grabbing the keyboard and free resources."""
        if not self._started:
            return
        with _keyboard:
            _keyboard.decref('capture')
            _keyboard.remove_event_listener('grab_key', self._on_grab_key)
            _keyboard.remove_event_listener('grab_modifiers', self._on_grab_modifiers)
        self._started = False

    def _on_grab_key(self, __origtime, keycode, state):
        """Callback for when a new key event arrives."""
        key = KEYCODE_TO_KEY.get(keycode + 8)
        if key is None:
            # Unhandled, ignore and don't suppress.
            return False
        suppressed = key in self._suppressed_keys
        if state == 1:
            if self._mod_state:
                # Modifier(s) pressed, ignore.
                suppressed = False
            else:
                self.key_down(key)
        else:
            self.key_up(key)
        return suppressed

    def _on_grab_modifiers(self, depressed, latched, locked, __layout):
        """Callback for when the set of active modifiers changes."""
        # Note: ignore numlock state.
        self._mod_state = (depressed | latched | locked) & ~0x10
        return False

    def suppress_keyboard(self, keys=()):
        """Change the set of keys to capture."""
        self._suppressed_keys = set(keys)


class KeyboardEmulation:
    """Emulate keyboard events to send strings on Wayland.

    This emulation layer uses the 'virtual_keyboard_unstable_v1' protocol.
    Since the protocol allows using any XKB layout, a new layout is generated
    each time a string needs to be sent, containing just the needed symbols.
    This makes the emulation independent of the user’s current keyboard layout.
    To signal emulated events to KeyboardCapture, a special tag is inserted in
    generated XKB layouts.
    """
    def __init__(self):
        with _keyboard:
            _keyboard.incref('emulate')

    @staticmethod
    def send_string(string):
        """Emulate a complete string."""
        with _keyboard:
            _keyboard.send_string(string)

    @staticmethod
    def send_backspaces(count):
        """Emulate a sequence of backspaces."""
        with _keyboard:
            _keyboard.send_backspaces(count)

    @staticmethod
    def send_key_combination(combo_string):
        """Emulate a key combo."""
        with _keyboard:
            _keyboard.send_key_combination(combo_string)
