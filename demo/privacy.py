"""Keep this tool's own windows out of screen shares.

The whole design is a private assist — a voice in your ear, no bot in the call,
nothing rendered for anyone else. That falls apart the moment you share your
screen and your own console is sitting there with the answers in it.

Windows has a documented API for this: `SetWindowDisplayAffinity` with
`WDA_EXCLUDEFROMCAPTURE` (Windows 10 2004+). The window stays completely normal
on your monitor and is *absent* from anything capturing the screen — not
blacked out, not blurred, simply not present. Password managers and banking apps
use it for the same reason.

Verified rather than assumed: a magenta test window measured 80,000 matching
pixels in a screenshot before the call and 0 after.

What it does NOT do, and you should know before relying on it:

- It protects windows, not your microphone. Nothing here stops spoken answers
  leaking into the call if `TTS_DEVICE` points at your speakers.
- It cannot help against a phone camera pointed at your screen, or someone
  looking over your shoulder.
- Windows Terminal draws in its own window, so hiding "the console" may hide
  nothing. Classic conhost works. `check_setup --hide` reports which you have.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
# Deliberately not WDA_MONITOR: that renders the window as a black rectangle in
# the capture, which is more conspicuous than leaving it visible.
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def _user32():
    user32 = ctypes.windll.user32
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    return user32


def exclude_window(hwnd: int) -> bool:
    """Hide one window from screen capture. False if the OS refused."""
    if not hwnd:
        return False
    try:
        return bool(_user32().SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False


def restore_window(hwnd: int) -> bool:
    """Undo `exclude_window`."""
    if not hwnd:
        return False
    try:
        return bool(_user32().SetWindowDisplayAffinity(hwnd, WDA_NONE))
    except Exception:
        return False


def console_window() -> int:
    """HWND of the console this process is attached to, or 0."""
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return 0


def console_is_classic(hwnd: int) -> bool:
    """True for classic conhost, False for Windows Terminal.

    Terminal hosts the session in its own window and leaves this one hidden, so
    excluding it from capture protects nothing — the visible text lives in a
    window this process does not own. Worth reporting rather than silently
    claiming the console is hidden.
    """
    if not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def hide_console() -> tuple[bool, str]:
    """Try to keep this console out of screen shares.

    Returns (hidden, explanation).
    """
    hwnd = console_window()
    if not hwnd:
        return False, "no console window attached to this process"
    if not console_is_classic(hwnd):
        return False, (
            "running under Windows Terminal, whose window this process does not "
            "own — the console cannot be hidden from here. Use a classic console "
            "(conhost) if you need it hidden, or keep it off the shared screen."
        )
    if exclude_window(hwnd):
        return True, "console hidden from screen capture"
    return False, "the OS refused; needs Windows 10 version 2004 or newer"
