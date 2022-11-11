import os

if 'WAYLAND_DISPLAY' in os.environ:
    from .keyboardcontrol_wayland import KeyboardCapture, KeyboardEmulation # pylint: disable=unused-import
else:
    from .keyboardcontrol_x11 import KeyboardCapture, KeyboardEmulation # pylint: disable=unused-import
