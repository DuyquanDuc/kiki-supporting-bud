"""One private window holding the running history: what was heard, what was answered.

This replaces reading the terminal. The terminal cannot be protected — VS Code
and Windows Terminal own their own windows, so anything you read there is in the
screen share. This window is excluded from capture, so it is the only safe place
to put text you need to look at.

One window, opened once, appended to. Not a card per press: those were noise, and
a code answer that vanishes after nine seconds is no use anyway.

Two deliberate choices about focus:

- It never activates. Clicking or scrolling it does not pull focus away from the
  meeting app, so it cannot swallow a keystroke at a bad moment.
- It is NOT click-through, unlike the old card. Click-through would make it
  impossible to scroll back, and scrolling back is the entire point of keeping a
  history.
"""

from __future__ import annotations

import ctypes
import tkinter as tk

from . import config

_BG = "#0d1017"
_FG = "#e6e9ef"
_MUTED = "#7b8394"
_HEARD_THEM = "#9ecbff"
_HEARD_YOU = "#a5d6a7"
_ANSWER = "#ffd479"
# Auto-answers are a different KIND of thing from the ones you asked for: they
# were never spoken, and nobody chose to have them. Red so a glance tells you
# which lines you have already heard and which are just sitting there.
_AUTO = "#ff6b6b"
_CODE = "#d7e3f4"
_CODE_BG = "#161b24"

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_LAYERED = 0x00080000


class History:
    """A scrollback window. Every method must run on the tkinter thread."""

    def __init__(self, root: tk.Tk, enabled: bool = True):
        self._root = root
        self._enabled = enabled
        self._win: tk.Toplevel | None = None
        if not enabled:
            return

        self._win = tk.Toplevel(root)
        self._win.title("meeting support — private")
        self._win.attributes("-topmost", True)
        self._win.configure(bg=_BG)
        self._win.protocol("WM_DELETE_WINDOW", self.toggle)

        frame = tk.Frame(self._win, bg=_BG)
        frame.pack(fill="both", expand=True)

        scroll = tk.Scrollbar(frame, bg=_BG, troughcolor=_BG, width=10)
        scroll.pack(side="right", fill="y")

        self._text = tk.Text(
            frame, bg=_BG, fg=_FG, font=("Consolas", 10), wrap="word",
            relief="flat", padx=12, pady=10, yscrollcommand=scroll.set,
            insertwidth=0, highlightthickness=0, borderwidth=0,
        )
        self._text.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self._text.yview)

        self._text.tag_configure("them", foreground=_HEARD_THEM)
        self._text.tag_configure("you", foreground=_HEARD_YOU)
        self._text.tag_configure("answer", foreground=_ANSWER)
        self._text.tag_configure("auto", foreground=_AUTO)
        self._text.tag_configure("muted", foreground=_MUTED)
        self._text.tag_configure(
            "code", foreground=_CODE, background=_CODE_BG,
            font=("Consolas", 10), lmargin1=18, lmargin2=18,
        )
        self._text.configure(state="disabled")

        # --- the standing-request box ---------------------------------------
        # A meeting is not always a question to answer. Sometimes what is needed
        # is a translation, a calculation, or a specific framing, and that
        # cannot be inferred from the transcript — the user has to say so.
        self._ask_row = tk.Frame(self._win, bg=_BG)
        self._ask_row.pack(fill="x", side="bottom")
        self._ask_label = tk.Label(
            self._ask_row, text="ask:", bg=_BG, fg=_MUTED,
            font=("Consolas", 9), padx=8,
        )
        self._ask_label.pack(side="left")
        self._ask = tk.Entry(
            self._ask_row, bg=_CODE_BG, fg=_FG, font=("Consolas", 10),
            relief="flat", insertbackground=_FG, highlightthickness=1,
            highlightbackground=_CODE_BG, highlightcolor=_ANSWER,
        )
        self._ask.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=6)
        self._ask.bind("<Return>", self._commit_ask)
        self._ask.bind("<Escape>", self._clear_ask)
        # Plain text, read from the answer threads. Assignment is atomic under
        # the GIL, so no lock is needed for a single string swap.
        self._instruction = ""

        self._place()
        self._win.update_idletasks()
        self._no_activate()
        if config.HIDE_FROM_CAPTURE:
            self._hide_from_capture()
        self._visible = True

    # --- window plumbing ---------------------------------------------------

    def _place(self) -> None:
        width, height = config.HISTORY_WIDTH, config.HISTORY_HEIGHT
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = screen_w - width - config.OVERLAY_MARGIN
        y = screen_h - height - config.OVERLAY_MARGIN - 60
        self._win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    def _target_hwnd(self) -> int:
        hwnd = self._win.winfo_id()
        parent = ctypes.windll.user32.GetParent(hwnd)
        return parent or hwnd

    def _no_activate(self, on: bool = True) -> None:
        """Take clicks and scrolls without stealing focus from the meeting.

        Turned OFF briefly while the ask box is being typed into: a window that
        cannot be activated cannot receive keystrokes either, so the flag that
        protects the meeting is exactly the flag that would swallow the request.
        """
        try:
            target = self._target_hwnd()
            style = ctypes.windll.user32.GetWindowLongW(target, _GWL_EXSTYLE)
            if on:
                style |= _WS_EX_NOACTIVATE | _WS_EX_LAYERED
            else:
                style &= ~_WS_EX_NOACTIVATE
            ctypes.windll.user32.SetWindowLongW(target, _GWL_EXSTYLE, style)
        except Exception:
            pass  # non-Windows or blocked: the window still works

    # --- the ask box -------------------------------------------------------

    @property
    def instruction(self) -> str:
        """The standing request, or "". Read from the answer threads."""
        return getattr(self, "_instruction", "")

    def focus_ask(self) -> None:
        """Bring the window up and put the caret in the ask box."""
        if not self._enabled or self._win is None:
            return
        if not self._visible:
            self._win.deiconify()
            self._visible = True
        self._win.lift()
        self._no_activate(False)
        try:
            ctypes.windll.user32.SetForegroundWindow(self._target_hwnd())
        except Exception:
            pass
        self._ask.focus_force()
        self._ask.select_range(0, "end")
        self._ask_label.configure(fg=_ANSWER)

    def _release(self) -> None:
        """Give focus back to the meeting and re-arm the no-activate flag."""
        self._no_activate(True)
        self._ask_label.configure(fg=_ANSWER if self.instruction else _MUTED)
        self._win.lower()
        self._win.lift()

    def _commit_ask(self, _event=None) -> str:
        text = self._ask.get().strip()
        self._instruction = text
        if text:
            self._append(f"\nasking for: {text}\n", "muted")
        else:
            self._append("\nback to normal answers\n", "muted")
        self._release()
        return "break"

    def _clear_ask(self, _event=None) -> str:
        self._ask.delete(0, "end")
        return self._commit_ask()

    def _hide_from_capture(self) -> None:
        from . import privacy

        privacy.exclude_window(self._target_hwnd())

    def toggle(self) -> None:
        if not self._enabled or self._win is None:
            return
        if self._visible:
            self._win.withdraw()
        else:
            self._win.deiconify()
            self._win.lift()
        self._visible = not self._visible

    # --- content -----------------------------------------------------------

    def _append(self, text: str, tag: str) -> None:
        if not self._enabled or self._win is None:
            return
        # Only follow the tail if the user is already at the bottom; otherwise
        # they have scrolled back to read something and yanking them away is
        # worse than missing a line.
        at_bottom = self._text.yview()[1] > 0.999
        self._text.configure(state="normal")
        self._text.insert("end", text if text.endswith("\n") else text + "\n", tag)
        # Bound the buffer: a long meeting would otherwise grow without limit.
        excess = int(self._text.index("end-1c").split(".")[0]) - config.HISTORY_MAX_LINES
        if excess > 0:
            self._text.delete("1.0", f"{excess + 1}.0")
        self._text.configure(state="disabled")
        if at_bottom:
            self._text.see("end")

    def heard(self, label: str, text: str) -> None:
        self._append(f"{label}: {text}", "you" if label == "You" else "them")

    def image(self, path, caption: str = "") -> None:
        """Show a generated illustration inline.

        Inline rather than a link because this window is the only surface that
        is excluded from screen capture — opening the PNG in a viewer or a
        browser would put it straight into the share it was drawn during.

        A reference is kept on the widget: tkinter does not own PhotoImage, and
        an un-referenced one is garbage collected into a blank rectangle.
        """
        if not self._enabled or self._win is None:
            return
        try:
            from PIL import Image, ImageTk

            picture = Image.open(path)
            width = max(200, config.HISTORY_WIDTH - 60)
            if picture.width > width:
                height = round(picture.height * width / picture.width)
                picture = picture.resize((width, height), Image.LANCZOS)
            photo = ImageTk.PhotoImage(picture)
            if not hasattr(self, "_photos"):
                self._photos = []
            self._photos.append(photo)

            self._text.configure(state="normal")
            self._text.insert("end", "\n")
            self._text.image_create("end", image=photo)
            self._text.insert("end", f"\n{caption or path.name}\n", "muted")
            self._text.configure(state="disabled")
            self._text.see("end")
        except Exception as exc:
            self.note(f"could not show the illustration ({exc}); saved at {path}")

    def note(self, text: str) -> None:
        self._append(text, "muted")

    def answer(self, body: str, meta: str = "", auto: bool = False) -> None:
        """An answer, with any code block set apart and monospaced.

        `auto` marks one nobody pressed a button for. Coloured differently and
        given its own marker because it was never spoken — scrolling back, the
        eye needs to find the lines it has NOT already heard.
        """
        tag = "auto" if auto else "answer"
        marker = "~" if auto else ">"
        prose, code = _split(body)
        if prose:
            self._append(f"\n{marker} {prose}", tag)
        if code:
            self._append(code, "code")
        if meta:
            self._append(f"  {meta}\n", "muted")


def _split(body: str) -> tuple[str, str]:
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == config.READ_MARKER:
            prose = "\n".join(lines[:index]).strip()
            code = [l for l in lines[index + 1:] if not l.strip().startswith("```")]
            return prose, "\n".join(code).strip("\n")
    return body, ""
