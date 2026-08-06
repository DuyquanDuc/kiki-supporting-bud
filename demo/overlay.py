"""A private always-on-top card. Click-through, never takes focus.

Even with speech as the primary output, the overlay earns its place: it holds
the detail after the sentence has finished playing, and it shows the latency
number that the whole design exists to defend.
"""

from __future__ import annotations

import ctypes
import tkinter as tk

from . import config

_BG = "#111318"
_FG = "#f4f5f7"
_MUTED = "#8a90a0"
_ACCENT = "#4da3ff"
_CODE_BG = "#0a0c10"
_CODE_FG = "#d7e3f4"

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000


def _split_read_block(body: str) -> tuple[str, str]:
    """(prose, code). Splits on the --- marker; code is stripped of fences."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == config.READ_MARKER:
            prose = "\n".join(lines[:index]).strip()
            code = [
                l for l in lines[index + 1:] if not l.strip().startswith("```")
            ]
            return prose, "\n".join(code).strip("\n")
    return body, ""


class Overlay:
    """Owns a Toplevel. Every method must be called on the tkinter thread."""

    def __init__(self, root: tk.Tk, mode: str = "read"):
        self._root = root
        self._hide_job: str | None = None
        self._mode = mode
        self._enabled = mode != "off"
        if not self._enabled:
            # Build nothing. tkinter still runs — it owns the main loop that
            # keeps the hotkeys alive — but there is no window to ever show.
            self._win = None
            return

        self._win = tk.Toplevel(root)
        self._win.withdraw()
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.94)
        self._win.configure(bg=_BG)

        frame = tk.Frame(self._win, bg=_BG, padx=20, pady=16)
        frame.pack(fill="both", expand=True)

        self._title = tk.Label(
            frame, text="", bg=_BG, fg=_FG, font=("Segoe UI Semibold", 15),
            wraplength=config.OVERLAY_WIDTH - 40, justify="left", anchor="w",
        )
        self._title.pack(fill="x")

        self._body = tk.Label(
            frame, text="", bg=_BG, fg=_FG, font=("Segoe UI", 11),
            wraplength=config.OVERLAY_WIDTH - 40, justify="left", anchor="w",
        )
        self._body.pack(fill="x", pady=(6, 0))

        # Code goes here instead: monospace, never wrapped. Wrapped code is
        # unreadable, so the window widens rather than folding lines.
        self._code = tk.Label(
            frame, text="", bg=_CODE_BG, fg=_CODE_FG, font=("Consolas", 11),
            justify="left", anchor="w", padx=12, pady=10,
        )

        self._meta = tk.Label(
            frame, text="", bg=_BG, fg=_MUTED, font=("Consolas", 9),
            wraplength=config.OVERLAY_WIDTH - 40, justify="left", anchor="w",
        )
        self._meta.pack(fill="x", pady=(10, 0))

        self._win.update_idletasks()
        self._make_click_through()
        if config.HIDE_FROM_CAPTURE:
            self._hide_from_capture()

    def _hide_from_capture(self) -> None:
        """Keep the card out of screen shares. Silent if the OS refuses."""
        from . import privacy

        hwnd = self._win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        privacy.exclude_window(parent or hwnd)

    def _make_click_through(self) -> None:
        """Windows-only: let clicks pass to the meeting window underneath."""
        try:
            hwnd = self._win.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            target = parent or hwnd
            style = ctypes.windll.user32.GetWindowLongW(target, _GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                target,
                _GWL_EXSTYLE,
                style | _WS_EX_TRANSPARENT | _WS_EX_LAYERED | _WS_EX_NOACTIVATE,
            )
        except Exception:
            pass  # non-Windows or blocked: overlay still works, just clickable

    def _place(self, width: int | None = None) -> None:
        self._win.update_idletasks()
        width = width or config.OVERLAY_WIDTH
        # Never taller than the screen, or the top of a long block is off-screen
        # and unreadable.
        height = min(
            self._win.winfo_reqheight(),
            self._root.winfo_screenheight() - config.OVERLAY_MARGIN * 2 - 60,
        )
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = screen_w - width - config.OVERLAY_MARGIN
        y = screen_h - height - config.OVERLAY_MARGIN - 60
        self._win.geometry(f"{width}x{height}+{x}+{y}")

    def show(self, title: str, body: str = "", meta: str = "", accent: bool = False) -> None:
        if not self._enabled:
            return

        prose, code = _split_read_block(body)
        # In "read" mode the card is for things you must look at. An answer with
        # nothing to read stays voice-only, which is what was asked for.
        if self._mode == "read" and not code:
            return

        width = config.OVERLAY_CODE_WIDTH if code else config.OVERLAY_WIDTH
        self._title.configure(text=title, fg=_ACCENT if accent else _FG)
        self._title.configure(wraplength=width - 40)
        self._body.configure(text=prose, wraplength=width - 40)
        self._body.pack_configure(pady=(6, 0) if prose else (0, 0))
        self._meta.configure(text=meta, wraplength=width - 40)

        if code:
            self._code.configure(text=code)
            self._code.pack(fill="x", pady=(12, 0), before=self._meta)
            seconds = min(
                config.OVERLAY_READ_MAX_SECONDS,
                config.OVERLAY_READ_BASE_SECONDS
                + config.OVERLAY_READ_PER_LINE * len(code.splitlines()),
            )
        else:
            self._code.pack_forget()
            seconds = config.OVERLAY_SECONDS

        self._place(width)
        self._win.deiconify()
        self._win.lift()

        if self._hide_job is not None:
            self._root.after_cancel(self._hide_job)
        self._hide_job = self._root.after(int(seconds * 1000), self.hide)

    def hide(self) -> None:
        self._hide_job = None
        if self._win is not None:
            self._win.withdraw()
