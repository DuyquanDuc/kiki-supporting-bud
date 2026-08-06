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

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000


class Overlay:
    """Owns a Toplevel. Every method must be called on the tkinter thread."""

    def __init__(self, root: tk.Tk, enabled: bool = True):
        self._root = root
        self._hide_job: str | None = None
        self._enabled = enabled
        if not enabled:
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

        self._meta = tk.Label(
            frame, text="", bg=_BG, fg=_MUTED, font=("Consolas", 9),
            wraplength=config.OVERLAY_WIDTH - 40, justify="left", anchor="w",
        )
        self._meta.pack(fill="x", pady=(10, 0))

        self._win.update_idletasks()
        self._make_click_through()

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

    def _place(self) -> None:
        self._win.update_idletasks()
        width = config.OVERLAY_WIDTH
        height = self._win.winfo_reqheight()
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = screen_w - width - config.OVERLAY_MARGIN
        y = screen_h - height - config.OVERLAY_MARGIN - 60
        self._win.geometry(f"{width}x{height}+{x}+{y}")

    def show(self, title: str, body: str = "", meta: str = "", accent: bool = False) -> None:
        if not self._enabled:
            return
        self._title.configure(text=title, fg=_ACCENT if accent else _FG)
        self._body.configure(text=body)
        self._body.pack_configure(pady=(6, 0) if body else (0, 0))
        self._meta.configure(text=meta)
        self._place()
        self._win.deiconify()
        self._win.lift()

        if self._hide_job is not None:
            self._root.after_cancel(self._hide_job)
        self._hide_job = self._root.after(
            int(config.OVERLAY_SECONDS * 1000), self.hide
        )

    def hide(self) -> None:
        self._hide_job = None
        if self._win is not None:
            self._win.withdraw()
