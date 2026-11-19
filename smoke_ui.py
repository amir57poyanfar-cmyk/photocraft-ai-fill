"""Render the floating toolbar and capture it, against the mock server.

Run:  python smoke_ui.py
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

from PIL import ImageGrab

from config import Config
from mock_photocraft import DEFAULT_TOKEN, MockPhotocraft
from ui import FillToolbar


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def grab(widget, name: str) -> Path:
    widget.update_idletasks()
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    shot = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    out = Path(__file__).with_name(name)
    shot.save(out)
    print(f"SHOT  {out.name}  {shot.width}x{shot.height}")
    return out


def main() -> None:
    port = free_port()
    server = MockPhotocraft(port=port).start()
    cfg = Config(
        api_key="",
        photocraft_host="127.0.0.1",
        photocraft_port=port,
        control_token=DEFAULT_TOKEN,
        connect_timeout=2.0,
        response_timeout=10.0,
        context_padding=0,
        temp_dir=str(Path(__file__).with_name(".tmp")),
    )

    app = FillToolbar(cfg)
    app.update()

    deadline = time.time() + 8
    while time.time() < deadline:
        app.update()
        if app.status_dot.cget("text_color") == "#3ddc84":
            break
        time.sleep(0.05)
    time.sleep(0.4)
    app.update()

    print(f"BADGE   : {app.badge.cget('text')}")
    print(f"STATUS  : {app.status_line.cget('text')}")
    print(f"GEOMETRY: {app.winfo_width()}x{app.winfo_height()} at +{app.winfo_x()}+{app.winfo_y()}")
    grab(app, "shot_idle.png")

    # With a selection.
    server.selection = [120, 80, 420, 300]
    deadline = time.time() + 6
    while time.time() < deadline:
        app.update()
        if "Selection" in app.badge.cget("text"):
            break
        time.sleep(0.1)
    app.update()
    print(f"BADGE   : {app.badge.cget('text')}")
    grab(app, "shot_selection.png")

    # Busy state.
    app._set_busy(True)
    app._set_status("Sending selection 420x300 to Gemini...")
    app.update()
    time.sleep(0.4)
    app.update()
    print(f"BUSY    : {app.generate_btn.cget('text')} / progress mapped={bool(app.progress.winfo_ismapped())}")
    grab(app, "shot_busy.png")
    app._set_busy(False)
    app.update()

    # Settings dialog.
    app._open_settings()
    app.update()
    time.sleep(0.6)
    app.update()
    if app._settings is not None:
        app._settings.update_idletasks()
        grab(app._settings, "shot_settings.png")
        app._settings.destroy()
    app.update()

    app._closing = True
    app.destroy()
    server.stop()


if __name__ == "__main__":
    main()
