"""Drag a box once over the shared-content area; reuse it forever.

Meeting layouts do not move, so this is a one-time cost that keeps every
capture cropped to the part of the screen that matters.
"""

from __future__ import annotations

import json
import tkinter as tk

from . import config

Region = dict  # {"left": int, "top": int, "width": int, "height": int}


def load() -> Region | None:
    if not config.REGION_FILE.exists():
        return None
    try:
        data = json.loads(config.REGION_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if all(k in data for k in ("left", "top", "width", "height")):
        return data
    return None


def save(region: Region) -> None:
    config.REGION_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.REGION_FILE.write_text(json.dumps(region, indent=2))


def select() -> Region | None:
    """Fullscreen drag-to-select. Returns None if cancelled with Escape.

    Single-monitor only: tkinter reports coordinates relative to the primary
    display, so a box dragged on a second monitor will capture the wrong area.
    """
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.28)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.title("Select the shared-content region")

    canvas = tk.Canvas(root, cursor="crosshair", bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        root.winfo_screenwidth() // 2,
        48,
        text="Drag a box around the shared content.  Esc to cancel.",
        fill="white",
        font=("Segoe UI", 16),
    )

    state: dict = {"x0": 0, "y0": 0, "rect": None, "result": None}

    def on_press(event: tk.Event) -> None:
        state["x0"], state["y0"] = event.x, event.y
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#4da3ff", width=2
        )

    def on_drag(event: tk.Event) -> None:
        if state["rect"] is not None:
            canvas.coords(state["rect"], state["x0"], state["y0"], event.x, event.y)

    def on_release(event: tk.Event) -> None:
        left, top = min(state["x0"], event.x), min(state["y0"], event.y)
        width, height = abs(event.x - state["x0"]), abs(event.y - state["y0"])
        if width < 40 or height < 40:
            return  # stray click, keep waiting for a real drag
        state["result"] = {"left": left, "top": top, "width": width, "height": height}
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", lambda _e: root.destroy())
    root.mainloop()

    result = state["result"]
    if result:
        save(result)
    return result


def load_or_select() -> Region | None:
    return load() or select()


if __name__ == "__main__":
    picked = select()
    print(f"Saved region: {picked}" if picked else "Cancelled.")
