"""Regression test for the two live bugs the user hit.

Reported behaviour:
  1. A selection was active but the bar never auto-expanded.
  2. Clicking the pill expanded the bar, which then collapsed within
     milliseconds, so the gear and the prompt were unclickable.

Both are state-machine bugs, so this drives the real FillToolbar against the
mock server and asserts on the mode the tracking loop actually settles into.

Run:  python test_ui_states.py
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

from config import Config
from mock_photocraft import DEFAULT_TOKEN, MockPhotocraft
from ui import FillToolbar
import winwindow


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def settle(app: FillToolbar, seconds: float = 1.2) -> None:
    """Pump the Tk loop so the tracking timer actually fires."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.update()
        time.sleep(0.02)


def main() -> int:
    port = free_port()
    server = MockPhotocraft(port=port).start()
    cfg = Config(
        gemini_api_key="",
        photocraft_host="127.0.0.1",
        photocraft_port=port,
        control_token=DEFAULT_TOKEN,
        connect_timeout=2.0,
        response_timeout=10.0,
        context_padding=0,
        temp_dir=str(Path(__file__).with_name(".tmp")),
        auto_popup=True,
    )

    app = FillToolbar(cfg)
    # Tracking must see a live process or it closes the toolbar. Use our own
    # pid: it is genuinely alive, and the window handle is faked so the
    # geometry code has something to work with.
    app.core_pid = os.getpid()
    app._photo_hwnd = 1
    # A headless test process never owns the foreground window, so the real
    # check would (correctly) hide the bar. Focus ownership is an environment
    # property, not the thing under test: stub it as "PhotoCraft is focused".
    winwindow.foreground_is_one_of = lambda pids: True
    settle(app, 1.5)

    print("=== bug 1: no selection -> pill ===")
    server.selection = None
    settle(app, 1.0)
    # Force the tracking decision with the state the poll would have produced.
    app._state = None
    app._user_expanded = False
    mode = winwindow.decide_mode(
        photo_running=True, photo_minimized=False, photo_foreground=True,
        our_foreground=False, settings_open=False, typing=False,
        auto_popup=True, has_selection=False, user_expanded=False,
    )
    check("no selection collapses to pill", mode == winwindow.PILL, mode)

    print("\n=== bug 1b: selection present -> bar (auto-popup) ===")
    server.selection = [120, 80, 420, 300]
    settle(app, 1.6)
    check("poll picked up the selection", bool(app._state and app._state.has_selection),
          f"state={app._state}")
    mode = winwindow.decide_mode(
        photo_running=True, photo_minimized=False, photo_foreground=True,
        our_foreground=False, settings_open=False, typing=False,
        auto_popup=True, has_selection=bool(app._state and app._state.has_selection),
        user_expanded=False,
    )
    check("selection auto-expands to bar", mode == winwindow.BAR, mode)

    print("\n=== bug 2: clicking the pill must stay open ===")
    app._user_expanded = False
    app._expand_from_pill()
    settle(app, 0.3)
    check("pill click sets user_expanded", app._user_expanded is True)
    check("pill click shows the bar", app._mode == winwindow.BAR, str(app._mode))

    # This is the exact failure: the next poll has no selection and used to
    # fold the bar back to a pill.
    server.selection = None
    settle(app, 1.8)
    check("bar STAYS open after the poll drops the selection",
          app._mode == winwindow.BAR, str(app._mode))
    check("user_expanded is still sticky", app._user_expanded is True)

    print("\n=== bug 2b: gear opens on top and stays ===")
    app._open_settings()
    settle(app, 0.8)
    check("settings dialog exists", app._settings is not None and app._settings.winfo_exists())
    check("bar still open behind settings", app._mode == winwindow.BAR, str(app._mode))
    check("user_expanded survives opening settings", app._user_expanded is True)

    # Type into the key field the way a user would.
    if app._settings is not None:
        app._settings.api_key_entry.delete(0, "end")
        app._settings.api_key_entry.insert(0, "AIzaTESTKEY123")
        settle(app, 0.2)
        check("API key text lands in the field",
              app._settings.api_key_entry.get() == "AIzaTESTKEY123",
              app._settings.api_key_entry.get())
        # Poll again with the dialog up: it must not be torn down.
        settle(app, 1.4)
        check("settings survives a tracking poll",
              app._settings is not None and app._settings.winfo_exists())
        check("bar still open with settings up", app._mode == winwindow.BAR, str(app._mode))
        app._settings._close()
        settle(app, 0.4)
    check("bar still open after closing settings", app._mode == winwindow.BAR, str(app._mode))

    print("\n=== collapse is the only way back to a pill ===")
    app._collapse_to_pill()
    settle(app, 0.3)
    check("collapse clears user_expanded", app._user_expanded is False)
    check("collapse shows the pill", app._mode == winwindow.PILL, str(app._mode))

    print("\n=== minimized still hides, and returns as a bar ===")
    app._user_expanded = True
    mode = winwindow.decide_mode(
        photo_running=True, photo_minimized=True, photo_foreground=True,
        our_foreground=False, settings_open=False, typing=False,
        auto_popup=True, has_selection=False, user_expanded=True,
    )
    check("minimized hides even when manually expanded", mode == winwindow.HIDDEN, mode)
    mode = winwindow.decide_mode(
        photo_running=True, photo_minimized=False, photo_foreground=True,
        our_foreground=False, settings_open=False, typing=False,
        auto_popup=True, has_selection=False, user_expanded=True,
    )
    check("restores as a bar, not a pill", mode == winwindow.BAR, mode)

    app._closing = True
    app.destroy()
    server.stop()

    print("\n" + ("ALL UI STATE CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
