"""PhotoCraft Generative Fill - Firefly-style contextual task bar.

The window has three states, driven by what PhotoCraft is doing:

    BAR     full prompt bar, docked bottom-centre of the PhotoCraft window
    PILL    a small "AI Fill" chip in the corner, out of the way
    HIDDEN  withdrawn entirely (PhotoCraft minimised or another app in front)

Transitions are decided by :func:`winwindow.decide_mode`, which is pure and
unit-tested. This module only gathers the facts and applies the result.

It never steals focus from PhotoCraft: showing is done with
``SW_SHOWNOACTIVATE`` rather than Tk's ``deiconify``.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from pathlib import Path
from typing import Any

import customtkinter as ctk
from PIL import Image

import winwindow
import rtl
from ai_service import (
    AIError,
    AIService,
    MissingAPIKeyError,
    NO_KEY_MESSAGE,
    SafetyBlockedError,
)
from config import (
    PROVIDER_LABELS,
    Config,
    default_base_url,
    default_model,
    detect_provider,
    get_config,
    is_valid_token,
    new_control_token,
)
from photocraft_client import (
    AuthError,
    ConnectionLost,
    DocumentState,
    NoDocumentError,
    PhotocraftClient,
    PhotocraftError,
    ProtocolError,
    RemoteError,
    Selection,
    _cleanup_path,
)

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ palette
# Cyber-neon / glass-dark. The original names are kept as aliases because the
# test suite (and the rest of the app) reads them directly.
BG = "#0D0F17"          # deep obsidian
SURFACE = "#131622"     # raised glass panel
SURFACE_HI = "#1D2133"  # hover / active glass
BORDER = "#262B40"      # layered inner border
TEXT = "#EAF0FF"
MUTED = "#8A93B2"
ACCENT = "#00F5D4"      # cyan
ACCENT_HOVER = "#7B2CBF"  # electric violet
OK = "#00F5D4"
WARN = "#FFB020"
BAD = "#FF007F"         # magenta

# The three neon stops used by the breathing border and the processing wave.
NEON_CYAN = "#00F5D4"
NEON_VIOLET = "#7B2CBF"
NEON_MAGENTA = "#FF007F"
NEON_STOPS = (NEON_CYAN, NEON_VIOLET, NEON_MAGENTA)

ANIM_MS = 16            # ~60fps

PLACEHOLDER = "Describe what to generate / replace..."
TRACK_MS = 220
POLL_MS = 1200
MIN_BAR_WIDTH = 640
#: The bar must never grow past the screen. A long error string used to stretch
#: the window and push the gear and collapse buttons off the right edge.
MAX_BAR_WIDTH = 920
SCREEN_MARGIN = 80
#: Two lines of wrapped status text; beyond that it is elided.
STATUS_WRAP_LINES = 2


def max_bar_width(screen_width: int) -> int:
    """Widest the bar may be: the configured cap, or the screen minus a margin."""
    return max(MIN_BAR_WIDTH, min(screen_width - SCREEN_MARGIN, MAX_BAR_WIDTH))


# --------------------------------------------------------------- neon helpers
def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(c))) for c in rgb)


def _mix(a: str, b: str, t: float) -> str:
    """Linear blend of two hex colours; t=0 -> a, t=1 -> b."""
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    return _rgb_to_hex((ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t))


def neon_at(phase: float) -> str:
    """A colour on the cyan -> violet -> magenta -> cyan loop.

    ``phase`` is any float; one full loop is 1.0. Pure function so it is easy
    to test and cannot drift with frame timing.
    """
    phase %= 1.0
    scaled = phase * len(NEON_STOPS)
    index = int(scaled)
    frac = scaled - index
    return _mix(NEON_STOPS[index % len(NEON_STOPS)], NEON_STOPS[(index + 1) % len(NEON_STOPS)], frac)


def ease_out(t: float) -> float:
    """Cubic ease-out for expand/collapse interpolation."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


# -------------------------------------------------------- clipboard / entries
# Windows virtual keycodes. These are layout-independent: on a Persian (FA)
# keyboard Ctrl+V arrives as ``Control-ر``, so matching on ``event.keysym``
# can never work. The physical keycode is still 86.
VK_C = 67
VK_X = 88
VK_V = 86
VK_A = 65

# ``bind_class`` registers on the whole Tk interpreter, not per toplevel, so
# the class binding must be installed exactly once for the entire process.
_CLIPBOARD_CLASS_BOUND = False


def _selection_bounds(widget: Any) -> tuple[Any, Any] | None:
    """Return the entry's selected range, or None when nothing is selected."""
    try:
        return widget.index("sel.first"), widget.index("sel.last")
    except Exception:
        return None


def clipboard_copy(widget: Any) -> bool:
    bounds = _selection_bounds(widget)
    if bounds is None:
        return False
    try:
        text = widget.get(*bounds)
        widget.clipboard_clear()
        widget.clipboard_append(text)
        return True
    except Exception:
        return False


def clipboard_cut(widget: Any) -> bool:
    if not clipboard_copy(widget):
        return False
    bounds = _selection_bounds(widget)
    if bounds is None:
        return False
    try:
        widget.delete(*bounds)
        return True
    except Exception:
        return False


def _is_ctk_entry(widget: Any) -> bool:
    """True when ``widget`` is a customtkinter entry wrapper, not a raw tk.Entry."""
    return hasattr(widget, "_entry") and hasattr(widget, "_deactivate_placeholder")


def _clear_placeholder(widget: Any) -> None:
    """Remove CustomTkinter's placeholder state before writing real text.

    CustomTkinter 6 stores the placeholder as literal text in the raw
    ``_entry``. Writing to ``_entry`` directly (as the keycode handler does)
    therefore appends after the placeholder unless its state is cleared first.
    """
    if _is_ctk_entry(widget):
        try:
            widget._deactivate_placeholder()
        except Exception:  # pragma: no cover - version without the helper
            pass


def clipboard_paste(widget: Any) -> bool:
    """Paste clipboard text into an entry, replacing any active selection.

    Works for both a raw ``tk.Entry`` and a ``CTkEntry`` wrapper. For the
    latter it clears the placeholder state and writes through the wrapper's
    own ``delete``/``insert`` so no placeholder text can survive the paste.
    """
    if widget is None:
        return False
    try:
        text = widget.clipboard_get()
    except Exception:
        return False

    # A CTkEntry wrapper owns the placeholder logic: clear it, then replace
    # the whole content through the wrapper so get() returns exactly the text.
    if _is_ctk_entry(widget):
        _clear_placeholder(widget)
        try:
            widget.delete(0, "end")
            widget.insert(0, text)
            return True
        except Exception:  # pragma: no cover - widget torn down
            return False

    # Raw tk.Entry: replace only the active selection, keep the cursor otherwise.
    bounds = _selection_bounds(widget)
    if bounds is not None:
        try:
            widget.delete(*bounds)
        except Exception:  # pragma: no cover - selection vanished mid-call
            pass
    try:
        widget.insert("insert", text)
        return True
    except Exception:  # pragma: no cover - widget torn down
        return False


def select_all(widget: Any) -> None:
    try:
        widget.select_range(0, "end")
        widget.icursor("end")
    except Exception:  # pragma: no cover - widget torn down
        pass


def handle_clipboard_key(event: Any, widget: Any | None = None) -> str | None:
    """Dispatch a Ctrl+<key> press by physical keycode.

    Returns ``"break"`` when the key was handled so Tk stops propagating it,
    or None for an unrelated key. The keysym is deliberately ignored.
    """
    target = widget if widget is not None else getattr(event, "widget", None)
    if target is None:
        return None
    code = getattr(event, "keycode", 0)
    if code == VK_V:
        clipboard_paste(target)
        return "break"
    if code == VK_C:
        clipboard_copy(target)
        return "break"
    if code == VK_X:
        clipboard_cut(target)
        return "break"
    if code == VK_A:
        select_all(target)
        return "break"
    return None


def _paste_and_notify(widget: Any, on_change: Any = None) -> None:
    clipboard_paste(widget)
    if on_change is not None:
        try:
            on_change()
        except Exception:  # pragma: no cover - callback on dead widget
            pass


def _class_shift_insert(event: Any) -> str:
    """Class-level Shift+Insert paste for any tk.Entry in the app."""
    clipboard_paste(getattr(event, "widget", None))
    return "break"


def _make_entry_menu(widget: Any, on_change: Any = None,
                     menu_factory: Any = None) -> Any:
    """Build the dark-neon right-click menu for an entry."""
    factory = menu_factory or tk.Menu
    menu = factory(
        widget, tearoff=0, bg=SURFACE, fg=TEXT,
        activebackground=NEON_VIOLET, activeforeground="#ffffff",
        bd=1, relief="flat",
    )

    def run(action: Any) -> None:
        action(widget)
        if on_change is not None:
            try:
                on_change()
            except Exception:  # pragma: no cover
                pass

    menu.add_command(label="Paste", command=lambda: run(clipboard_paste))
    menu.add_command(label="Copy", command=lambda: run(clipboard_copy))
    menu.add_command(label="Cut", command=lambda: run(clipboard_cut))
    menu.add_separator()
    menu.add_command(label="Select All", command=lambda: run(lambda w: select_all(w)))
    return menu


def _popup_menu(event: Any, menu: Any) -> None:
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        try:
            menu.grab_release()
        except Exception:  # pragma: no cover
            pass


def _focus_entry(entry: Any) -> None:
    """Focus an entry immediately, even inside a borderless floating window."""
    try:
        entry.focus_force()
    except Exception:  # pragma: no cover
        pass
    inner = getattr(entry, "_entry", None)
    target = inner if inner is not None else entry
    try:
        target.focus_set()
    except Exception:  # pragma: no cover
        pass


def wire_entry_clipboard(entry: Any, root: Any = None, on_change: Any = None,
                         menu_factory: Any = None) -> Any:
    """Install layout-independent clipboard shortcuts on a CTkEntry.

    Covers physical Ctrl+C/X/V/A, Shift+Insert, click-to-focus, and a
    right-click context menu. Returns the menu so callers keep it alive.
    """
    global _CLIPBOARD_CLASS_BOUND
    inner = getattr(entry, "_entry", None)
    target = inner if inner is not None else entry
    # Paste/copy/cut operate on the CTkEntry wrapper so its placeholder state
    # is honoured; event bindings still go on the raw tk.Entry.
    text_widget = entry if inner is not None else target
    toplevel = root if root is not None else entry.winfo_toplevel()

    # One class binding for the whole interpreter covers every tk.Entry,
    # including entries created later. The per-widget binding below is
    # belt-and-braces for customtkinter versions that wrap the entry
    # differently. A handled key returns "break", so the two never both fire.
    if not _CLIPBOARD_CLASS_BOUND:
        try:
            toplevel.bind_class("Entry", "<Control-KeyPress>", handle_clipboard_key, add="+")
            toplevel.bind_class("Entry", "<Shift-Insert>", _class_shift_insert, add="+")
            _CLIPBOARD_CLASS_BOUND = True
        except Exception:  # pragma: no cover - fake root in a test
            pass

    def on_key(event: Any) -> str | None:
        result = handle_clipboard_key(event, text_widget)
        if result == "break" and on_change is not None:
            try:
                on_change()
            except Exception:  # pragma: no cover
                pass
        return result

    target.bind("<Control-KeyPress>", on_key, add="+")

    def on_shift_insert(_event: Any) -> str:
        _paste_and_notify(text_widget, on_change)
        return "break"

    target.bind("<Shift-Insert>", on_shift_insert, add="+")

    # The toolbar is borderless: an explicit focus is needed on click.
    entry.bind("<Button-1>", lambda _e: _focus_entry(entry), add="+")
    target.bind("<Button-1>", lambda _e: _focus_entry(entry), add="+")

    menu = _make_entry_menu(text_widget, on_change, menu_factory)
    for widget in (entry, target):
        widget.bind("<Button-3>", lambda e, m=menu: _popup_menu(e, m), add="+")
    return menu


class NeonBorder:
    """Animates a CTkFrame's native border through the neon gradient.

    Using the frame's own ``border_color`` means the glow is drawn by
    customtkinter's rounded-rectangle canvas: no overlay widget, no click
    interception, and no effect on geometry. ``attach`` accepts any widget that
    exposes ``configure(border_color=...)``, so the same controller drives both
    the expanded shell and the collapsed pill.
    """

    def __init__(self, master: Any, speed: float = 0.006) -> None:
        self.master = master
        self.speed = speed
        self.widgets: list[Any] = []
        self._phase = 0.0
        self._running = False
        self._after_id: str | None = None
        # If the master is destroyed without stop() being called, cancel the
        # pending frame anyway so no callback fires against a dead widget.
        try:
            self.master.bind("<Destroy>", lambda _e: self.stop(), add="+")
        except Exception:  # pragma: no cover - non-widget master in a test
            pass

    def attach(self, widget: Any) -> None:
        if widget not in self.widgets:
            self.widgets.append(widget)

    def _paint(self, color: str) -> None:
        for widget in self.widgets:
            try:
                widget.configure(border_color=color)
            except Exception:  # pragma: no cover - widget torn down mid-frame
                pass

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_id:
            try:
                self.master.after_cancel(self._after_id)
            except Exception:  # pragma: no cover
                pass
            self._after_id = None

    def _tick(self) -> None:
        if not self._running:
            return
        self._phase = (self._phase + self.speed) % 1.0
        self._paint(neon_at(self._phase))
        self._after_id = self.master.after(ANIM_MS, self._tick)


class NeonWave(tk.Canvas):
    """A glowing laser sweep used in place of the static progress bar.

    Drawn on its own canvas below the buttons; it cannot affect layout.
    """

    def __init__(self, master: Any, height: int = 4) -> None:
        super().__init__(master, highlightthickness=0, bd=0, bg=BG, height=height, name="neon_wave")
        self._phase = 0.0
        self._running = False
        self._after_id: str | None = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Destroy>", lambda _e: self.stop(), add="+")

    def _draw(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        self.delete("all")
        # A dim track plus a bright moving segment with a soft tail.
        self.create_line(0, h / 2, w, h / 2, fill=SURFACE_HI, width=max(2, h - 1))
        span = max(40, w // 3)
        head = int(self._phase * (w + span)) - span
        tail = max(0, head - span)
        for i in range(span):
            x = head - i
            if 0 <= x <= w:
                t = 1 - i / span
                color = _mix(NEON_VIOLET, neon_at(self._phase), t)
                self.create_line(x, h / 2, x + 1, h / 2, fill=color, width=max(2, h - 1))

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:  # pragma: no cover
                pass
            self._after_id = None

    def _tick(self) -> None:
        if not self._running:
            return
        self._phase = (self._phase + 0.012) % 1.0
        self._draw()
        self._after_id = self.after(ANIM_MS, self._tick)


class SettingsDialog(ctk.CTkToplevel):
    """Settings popup: universal API key, endpoint, model, then the socket."""

    def __init__(self, app: "FillToolbar") -> None:
        super().__init__(app)
        self.app = app
        self.cfg = app.config_obj

        self.title("Generative Fill - Settings")
        self.geometry("480x760")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        try:
            self.attributes("-topmost", True)
        except Exception:  # pragma: no cover
            pass
        self.protocol("WM_DELETE_WINDOW", self._close)
        # Take focus on top of the bar and keep it. Without the grab the dialog
        # opened behind the toolbar and the first click landed on the bar.
        self.after(60, self._take_focus)
        self._build()

    def _take_focus(self) -> None:
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
            self.api_key_entry.focus_set()
        except Exception:  # pragma: no cover
            log.debug("settings focus failed", exc_info=True)

    def _close(self) -> None:
        self._closing_dialog = True
        self._stop_test_animation()
        try:
            self.grab_release()
        except Exception:  # pragma: no cover
            pass
        neon = getattr(self, "_neon", None)
        if neon is not None:
            neon.stop()
        self.app._settings = None
        self.destroy()

    def _build(self) -> None:
        # The outer frame owns the neon border and the pinned action bar. All
        # fields live in a scrollable child, so a long config can never push
        # the Test / Save / Cancel buttons off the bottom of the dialog.
        outer = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=18, border_width=2, border_color=NEON_VIOLET
        )
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        # Breathing neon border on the dialog itself.
        self._neon = NeonBorder(self, speed=0.004)
        self._neon.attach(outer)
        self._neon.start()

        root = ctk.CTkScrollableFrame(outer, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=4, pady=(6, 0))

        head = ctk.CTkLabel(
            root, text="⚡  GENERATIVE FILL  ·  SETTINGS",
            text_color=NEON_CYAN, font=ctk.CTkFont(size=15, weight="bold"), anchor="w",
        )
        head.pack(fill="x", padx=18, pady=(18, 2))
        ctk.CTkLabel(
            root, text="Bring your own key. Any provider, any relay.",
            text_color=MUTED, font=ctk.CTkFont(size=10), anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 8))

        def label(text: str, pad_top: int = 8) -> None:
            ctk.CTkLabel(
                root, text=text, text_color=MUTED, font=ctk.CTkFont(size=10), anchor="w"
            ).pack(fill="x", padx=18, pady=(pad_top, 3))

        def entry(**kwargs: Any) -> ctk.CTkEntry:
            widget = ctk.CTkEntry(
                root, fg_color=BG, border_color=BORDER, text_color=TEXT,
                font=ctk.CTkFont(size=11), corner_radius=8, **kwargs,
            )
            widget.pack(fill="x", padx=18)
            # Glowing focus border.
            widget.bind("<FocusIn>", lambda _e, w=widget: w.configure(border_color=NEON_CYAN), add="+")
            widget.bind("<FocusOut>", lambda _e, w=widget: w.configure(border_color=BORDER), add="+")
            return widget

        label("API Key", 0)
        # One-click paste row: the key field plus a compact neon Paste button.
        key_row = ctk.CTkFrame(root, fg_color="transparent")
        key_row.pack(fill="x", padx=18)
        self.api_key_entry = ctk.CTkEntry(
            key_row, fg_color=BG, border_color=BORDER, text_color=TEXT,
            font=ctk.CTkFont(size=11), corner_radius=8,
            placeholder_text="paste any key: AIza..., sk-..., sk-or-..., hf_..., r8_...",
            show="•",
        )
        self.api_key_entry.pack(side="left", fill="x", expand=True)
        self.api_key_entry.bind("<FocusIn>", lambda _e: self.api_key_entry.configure(border_color=NEON_CYAN), add="+")
        self.api_key_entry.bind("<FocusOut>", lambda _e: self.api_key_entry.configure(border_color=BORDER), add="+")
        self.paste_key_btn = ctk.CTkButton(
            key_row, text="Paste", width=56, height=28, corner_radius=8,
            fg_color=NEON_VIOLET, hover_color=NEON_MAGENTA, text_color="#ffffff",
            font=ctk.CTkFont(size=10, weight="bold"),
            command=lambda: _paste_and_notify(self.api_key_entry, self._refresh_provider),
        )
        self.paste_key_btn.pack(side="right", padx=(6, 0))
        if self.cfg.api_key:
            self.api_key_entry.insert(0, self.cfg.api_key)
        self.api_key_entry.bind("<KeyRelease>", lambda _e: self._refresh_provider())

        label("Base URL (optional)")
        self.base_url_entry = entry(placeholder_text="blank = auto-detect, or https://.../v1")
        if self.cfg.base_url:
            self.base_url_entry.insert(0, self.cfg.base_url)
        self.base_url_entry.bind("<KeyRelease>", lambda _e: self._refresh_provider())

        label("Model (optional)")
        self.model_entry = entry(placeholder_text="blank = provider default, or exact model ID")
        if self.cfg.model_name:
            self.model_entry.insert(0, self.cfg.model_name)
        self.model_entry.bind("<KeyRelease>", lambda _e: self._refresh_provider())

        # Clipboard support on all three Settings inputs. Keep references to
        # the menus so Tk does not garbage-collect them.
        self._entry_menus = [
            wire_entry_clipboard(self.api_key_entry, root, self._refresh_provider),
            wire_entry_clipboard(self.base_url_entry, root, self._refresh_provider),
            wire_entry_clipboard(self.model_entry, root, self._refresh_provider),
        ]

        # A clear "Detected Provider & Endpoint" status card.
        status_card = ctk.CTkFrame(
            root, fg_color=BG, corner_radius=10, border_width=1, border_color=BORDER
        )
        status_card.pack(fill="x", padx=18, pady=(12, 0))
        ctk.CTkLabel(
            status_card, text="DETECTED PROVIDER & ENDPOINT",
            text_color=MUTED, font=ctk.CTkFont(size=8, weight="bold"), anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 2))
        self.provider_badge = ctk.CTkLabel(
            status_card, text="", text_color=NEON_CYAN,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=SURFACE_HI, corner_radius=10, padx=10, pady=3, anchor="w",
        )
        self.provider_badge.pack(fill="x", padx=12, pady=(0, 4))
        self.provider_hint = ctk.CTkLabel(
            status_card, text=self._provider_hint(), text_color=TEXT,
            font=ctk.CTkFont(size=9), anchor="w", wraplength=400, justify="left",
        )
        self.provider_hint.pack(fill="x", padx=12, pady=(0, 10))
        self._refresh_provider()

        # A clear Test / Validation feedback box.
        feedback = ctk.CTkFrame(
            root, fg_color=BG, corner_radius=10, border_width=1, border_color=BORDER
        )
        feedback.pack(fill="x", padx=18, pady=(10, 0))
        ctk.CTkLabel(
            feedback, text="TEST / VALIDATION FEEDBACK",
            text_color=MUTED, font=ctk.CTkFont(size=8, weight="bold"), anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 2))
        self.test_result = ctk.CTkLabel(
            feedback, text="Press Test API to validate your key.", text_color=MUTED,
            font=ctk.CTkFont(size=10), anchor="w", wraplength=400, justify="left",
        )
        self.test_result.pack(fill="x", padx=12, pady=(0, 10))

        net = ctk.CTkFrame(root, fg_color="transparent")
        net.pack(fill="x", pady=(12, 0))

        host_wrap = ctk.CTkFrame(net, fg_color="transparent")
        host_wrap.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkLabel(host_wrap, text="Host", text_color=TEXT, font=ctk.CTkFont(size=10), anchor="w").pack(fill="x", pady=(0, 3))
        self.host_entry = ctk.CTkEntry(host_wrap, fg_color=SURFACE, border_color=BORDER, text_color=TEXT, font=ctk.CTkFont(size=11))
        self.host_entry.pack(fill="x")
        self.host_entry.insert(0, self.cfg.photocraft_host)

        port_wrap = ctk.CTkFrame(net, fg_color="transparent", width=100)
        port_wrap.pack(side="left")
        ctk.CTkLabel(port_wrap, text="Control port", text_color=TEXT, font=ctk.CTkFont(size=10), anchor="w").pack(fill="x", pady=(0, 3))
        self.port_entry = ctk.CTkEntry(port_wrap, width=100, fg_color=SURFACE, border_color=BORDER, text_color=TEXT, font=ctk.CTkFont(size=11))
        self.port_entry.pack(fill="x")
        self.port_entry.insert(0, str(self.cfg.photocraft_port))

        label("Control token (64 hex)")
        token_row = ctk.CTkFrame(root, fg_color="transparent")
        token_row.pack(fill="x")
        self.token_entry = ctk.CTkEntry(
            token_row, show="•", fg_color=SURFACE, border_color=BORDER,
            text_color=TEXT, font=ctk.CTkFont(size=10),
        )
        self.token_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.token_entry.insert(0, self.cfg.control_token)
        ctk.CTkButton(
            token_row, text="gen", width=46, height=26, fg_color=SURFACE_HI,
            hover_color=BORDER, text_color=MUTED, font=ctk.CTkFont(size=10),
            command=self._generate_token,
        ).pack(side="left")

        label("Context padding around selection (px)")
        self.padding_entry = entry()
        self.padding_entry.insert(0, str(self.cfg.context_padding))

        self.full_context_var = ctk.BooleanVar(value=bool(self.cfg.send_full_context))
        ctk.CTkCheckBox(
            root, text="Send full canvas as context", variable=self.full_context_var,
            text_color=TEXT, font=ctk.CTkFont(size=10), fg_color=ACCENT,
            hover_color=ACCENT_HOVER, border_color=BORDER,
            checkbox_width=16, checkbox_height=16,
        ).pack(anchor="w", pady=(10, 0))

        self.auto_popup_var = ctk.BooleanVar(value=bool(self.cfg.auto_popup))
        ctk.CTkCheckBox(
            root, text="Auto-popup on selection (Firefly style)", variable=self.auto_popup_var,
            text_color=TEXT, font=ctk.CTkFont(size=10), fg_color=ACCENT,
            hover_color=ACCENT_HOVER, border_color=BORDER,
            checkbox_width=16, checkbox_height=16,
        ).pack(anchor="w", pady=(8, 0))

        self.message = ctk.CTkLabel(
            root, text="", text_color=MUTED, font=ctk.CTkFont(size=9), anchor="w", wraplength=360
        )
        self.message.pack(fill="x", pady=(8, 6))

        # Pinned action bar: always visible regardless of scroll position.
        actions = ctk.CTkFrame(outer, fg_color="transparent")
        actions.pack(fill="x", padx=14, pady=(4, 12))
        self.test_btn = ctk.CTkButton(
            actions, text="Test API", height=34, corner_radius=8,
            fg_color="transparent", border_width=1, border_color=NEON_CYAN,
            hover_color=SURFACE_HI, text_color=NEON_CYAN,
            font=ctk.CTkFont(size=11, weight="bold"), command=self._test,
        )
        self.test_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.save_btn = ctk.CTkButton(
            actions, text="Save", height=34, corner_radius=8,
            fg_color=NEON_CYAN, hover_color="#00C9AC", text_color="#04121A",
            font=ctk.CTkFont(size=11, weight="bold"), command=self._save,
        )
        self.save_btn.pack(side="left", fill="x", expand=True, padx=4)
        self.cancel_btn = ctk.CTkButton(
            actions, text="Cancel", height=34, corner_radius=8,
            fg_color=SURFACE_HI, hover_color=BORDER, text_color=TEXT,
            font=ctk.CTkFont(size=11), command=self._close,
        )
        self.cancel_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        # Backwards-compatible alias used by older callers and tests.
        self.test_button = self.test_btn

    def _note(self, text: str, color: str = MUTED) -> None:
        self.message.configure(text=text, text_color=color)

    def _provider_hint(self) -> str:
        """Show what the key and endpoint currently resolve to, live."""
        key = self.api_key_entry.get().strip() if hasattr(self, "api_key_entry") else self.cfg.api_key
        url = self.base_url_entry.get().strip() if hasattr(self, "base_url_entry") else self.cfg.base_url
        model = self.model_entry.get().strip() if hasattr(self, "model_entry") else self.cfg.model_name
        if not key and not url:
            return "No key yet. Paste any provider's key, or an endpoint URL for a local server."
        provider = detect_provider(key, url)
        resolved_model = model or default_model(provider) or "(enter a model)"
        endpoint = url or default_base_url(provider) or "provider SDK"
        return (
            f"Detected: {PROVIDER_LABELS.get(provider, provider)}\n"
            f"Endpoint: {endpoint}\n"
            f"Model: {resolved_model}"
        )

    def _refresh_provider(self) -> None:
        self.provider_hint.configure(text=self._provider_hint())
        key = self.api_key_entry.get().strip() if hasattr(self, "api_key_entry") else self.cfg.api_key
        url = self.base_url_entry.get().strip() if hasattr(self, "base_url_entry") else self.cfg.base_url
        provider = detect_provider(key, url)
        label = PROVIDER_LABELS.get(provider, provider)
        if hasattr(self, "provider_badge"):
            self.provider_badge.configure(text=f"  {label.upper()}  ")

    def _test(self) -> None:
        """Probe the key/endpoint as typed, without saving it."""
        key = self.api_key_entry.get().strip()
        url = self.base_url_entry.get().strip()
        model = self.model_entry.get().strip()
        if not key and not url:
            self._test_note(NO_KEY_MESSAGE, BAD)
            return

        probe = Config(
            api_key=key, base_url=url, model_name=model,
            response_timeout=min(self.cfg.response_timeout, 60.0),
        )
        self._test_note("Testing...", MUTED)
        self.test_button.configure(state="disabled")
        self._test_anim_after = None
        self._animate_test_button()
        threading.Thread(
            target=self._test_worker, args=(probe,), name="pc-test-api", daemon=True
        ).start()

    def _animate_test_button(self, i: int = 0) -> None:
        """Cycle the Test button through the neon stops while probing."""
        if getattr(self, "_closing_dialog", False):
            return
        if self.test_button.cget("state") == "normal":
            self._test_anim_after = None
            return
        try:
            # Animate the outline, not the fill: the Test button is a
            # transparent neon-outline style and must stay that way.
            self.test_btn.configure(border_color=neon_at((i * 0.08) % 1.0))
        except Exception:  # pragma: no cover - dialog closed
            return
        self._test_anim_after = self.after(ANIM_MS * 3, self._animate_test_button, i + 1)

    def _stop_test_animation(self) -> None:
        after = getattr(self, "_test_anim_after", None)
        if after:
            try:
                self.after_cancel(after)
            except Exception:  # pragma: no cover
                pass
        self._test_anim_after = None

    def _test_worker(self, probe: Config) -> None:
        try:
            ok, message = AIService(probe).test_connection()
        except Exception as exc:  # noqa: BLE001 - never let a probe kill the dialog
            ok, message = False, f"{type(exc).__name__}: {exc}"
        self.app._post("test_result", (ok, message))

    def _test_note(self, text: str, color: str = MUTED) -> None:
        self._stop_test_animation()
        try:
            self.test_btn.configure(state="normal", fg_color="transparent", border_color=NEON_CYAN)
            self.test_result.configure(text=text, text_color=color)
        except Exception:  # pragma: no cover - dialog already closed
            pass

    def show_test_result(self, ok: bool, message: str) -> None:
        self._test_note(("OK  " if ok else "Failed  ") + message, OK if ok else BAD)

    def _generate_token(self) -> None:
        self.token_entry.delete(0, "end")
        self.token_entry.insert(0, new_control_token())
        self._note("New token generated. Save, then relaunch PhotoCraft.", WARN)

    def _save(self) -> None:
        token = self.token_entry.get().strip()
        if not is_valid_token(token):
            self._note("Control token must be exactly 64 hex characters.", BAD)
            return
        try:
            port = int(self.port_entry.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self._note("Control port must be between 1 and 65535.", BAD)
            return
        try:
            padding = int(self.padding_entry.get().strip() or "0")
        except ValueError:
            self._note("Padding must be a whole number.", BAD)
            return

        # One key, one endpoint, one model: no per-provider filing needed.
        key = self.api_key_entry.get().strip()
        url = self.base_url_entry.get().strip()
        model = self.model_entry.get().strip()

        try:
            self.cfg.update(
                api_key=key,
                base_url=url,
                model_name=model,
                photocraft_host=self.host_entry.get().strip() or "127.0.0.1",
                photocraft_port=port,
                control_token=token,
                context_padding=padding,
                send_full_context=bool(self.full_context_var.get()),
                auto_popup=bool(self.auto_popup_var.get()),
            )
        except (OSError, ValueError) as exc:
            self._note(f"Could not save: {exc}", BAD)
            return

        self.app.on_settings_saved()
        self._note(f"Saved. Using {self.cfg.provider_label}. Reconnecting...", OK)
        self.after(700, self._close)


class FillToolbar(ctk.CTk):
    """The contextual task bar."""

    def __init__(self, config: Config | None = None, core_pid: int = 0) -> None:
        super().__init__()
        self.config_obj = config or get_config()
        self.client = PhotocraftClient(self.config_obj)
        self.ai = AIService(self.config_obj)

        self.core_pid = int(core_pid or 0)
        self._photo_hwnd: int | None = None
        self._mode: str | None = None
        self._busy = False
        self._closing = False
        self._settings: SettingsDialog | None = None
        self._state: DocumentState | None = None
        self._drag_origin: tuple[int, int] | None = None
        self._manual_offset: tuple[int, int] | None = None
        # Set when the user opens the bar by hand (pill click or gear). Sticky
        # until they collapse it: a background poll must never fold a bar the
        # user just opened.
        self._user_expanded = False
        self._ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        # Neon animation controllers. They drive the frames' native border
        # colour, so they never intercept a click or change the bar geometry.
        self._neon = NeonBorder(self)
        self._wave: NeonWave | None = None
        self._glow_states: list[dict[str, Any]] = []

        winwindow.set_dpi_aware()

        self._build_window()
        self._build_bar()
        self._build_pill()
        self._neon.attach(self.shell)
        self._neon.attach(self.pill)
        self._neon.start()
        self._pump_queue()
        self._start_workers()
        self.after(TRACK_MS, self._track)

    # ------------------------------------------------------------- window
    def _build_window(self) -> None:
        ctk.set_appearance_mode("dark")
        self.title("PhotoCraft Generative Fill")
        self.overrideredirect(True)
        self.configure(fg_color=BG)
        try:
            self.attributes("-topmost", bool(self.config_obj.always_on_top))
        except Exception:  # pragma: no cover
            pass
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- bar
    def _build_bar(self) -> None:
        self.shell = ctk.CTkFrame(self, fg_color=BG, corner_radius=16, border_width=2, border_color=NEON_CYAN)

        row = ctk.CTkFrame(self.shell, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=8)

        self.grip = ctk.CTkLabel(
            row, text="⠿", text_color="#5a5a5a", font=ctk.CTkFont(size=15), width=16, cursor="fleur"
        )
        self.grip.pack(side="left", padx=(0, 8))
        for widget in (self.grip, self.shell, row):
            self._make_draggable(widget)

        self.status_dot = ctk.CTkLabel(row, text="●", text_color=BAD, font=ctk.CTkFont(size=13), width=14)
        self.status_dot.pack(side="left")

        self.badge = ctk.CTkLabel(
            row, text="Disconnected", text_color=MUTED,
            font=ctk.CTkFont(size=11, weight="bold"), anchor="w",
            justify="left", width=132,
        )
        self.badge.pack(side="left", padx=(4, 10))

        self.pin_btn = ctk.CTkButton(
            row, text="📌", width=28, height=28, corner_radius=8,
            fg_color=SURFACE if self.config_obj.always_on_top else "transparent",
            hover_color=SURFACE_HI,
            text_color=TEXT if self.config_obj.always_on_top else MUTED,
            font=ctk.CTkFont(size=12), command=self._toggle_pin,
        )
        self.pin_btn.pack(side="right", padx=(6, 0))

        self.settings_btn = ctk.CTkButton(
            row, text="⚙", width=28, height=28, corner_radius=8,
            fg_color="transparent", hover_color=SURFACE_HI, text_color=MUTED,
            font=ctk.CTkFont(size=13), command=self._open_settings,
        )
        self.settings_btn.pack(side="right", padx=(4, 0))

        self.collapse_btn = ctk.CTkButton(
            row, text="–", width=28, height=28, corner_radius=8,
            fg_color="transparent", hover_color=SURFACE_HI, text_color=MUTED,
            font=ctk.CTkFont(size=14, weight="bold"), command=self._collapse_to_pill,
        )
        self.collapse_btn.pack(side="right", padx=(4, 0))

        self.generate_btn = ctk.CTkButton(
            row, text="Generative Fill", width=132, height=32, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
            font=ctk.CTkFont(size=11, weight="bold"), command=self._on_generate,
        )
        self.generate_btn.pack(side="right", padx=(6, 4))

        self.prompt_entry = ctk.CTkEntry(
            row, placeholder_text=PLACEHOLDER, height=32, corner_radius=8,
            fg_color=SURFACE, border_color=BORDER, text_color=TEXT,
            placeholder_text_color=MUTED, font=ctk.CTkFont(size=11),
        )
        self.prompt_entry.pack(side="left", fill="x", expand=True, padx=(0, 2))
        self.prompt_entry.bind("<Return>", lambda _e: self._on_generate())
        self.prompt_entry.bind("<KP_Enter>", lambda _e: self._on_generate())
        # Tk already renders Persian right-to-left with joined letters, so the
        # entry holds the logical string untouched. Only the alignment follows
        # the text: a Persian prompt reads better flush right.
        self.prompt_entry.bind("<KeyRelease>", self._on_prompt_changed)
        self.prompt_entry.bind("<<Paste>>", lambda _e: self.after(1, self._on_prompt_changed))
        self._prompt_menu = wire_entry_clipboard(self.prompt_entry, self, self._on_prompt_changed)

        # Hover micro-interactions: a neon fade on the primary actions.
        self._add_glow(self.generate_btn, ACCENT, NEON_MAGENTA)
        self._add_glow(self.settings_btn, BG, NEON_VIOLET)
        self._add_glow(self.collapse_btn, BG, NEON_VIOLET)
        self._add_glow(self.pin_btn, BG, NEON_CYAN)

        # The static progress bar is replaced by a neon laser sweep while a
        # generation is running. Keep ``progress`` as an alias so any external
        # caller written against the old attribute still works.
        self.wave = NeonWave(self.shell, height=4)
        self.progress = self.wave

        self.status_line = ctk.CTkLabel(
            self.shell, text="", text_color=MUTED, font=ctk.CTkFont(size=9), anchor="w",
            justify="left",
        )

    # --------------------------------------------------------------- pill
    def _build_pill(self) -> None:
        self.pill = ctk.CTkFrame(self, fg_color=BG, corner_radius=16, border_width=2, border_color=NEON_CYAN)
        self.pill_btn = ctk.CTkButton(
            self.pill, text="✨ AI Fill", width=96, height=30, corner_radius=12,
            fg_color=SURFACE, hover_color=SURFACE_HI, text_color=TEXT,
            font=ctk.CTkFont(size=11, weight="bold"), command=self._expand_from_pill,
        )
        self.pill_btn.pack(padx=4, pady=4)
        self._make_draggable(self.pill_btn)
        self._add_glow(self.pill_btn, SURFACE, NEON_CYAN)

    def _expand_from_pill(self) -> None:
        self._user_expanded = True
        self._apply_mode(winwindow.BAR, force=True)
        self.prompt_entry.focus_set()

    def _collapse_to_pill(self) -> None:
        """The only way a manually opened bar goes back to a pill."""
        self._user_expanded = False
        self._manual_offset = None
        self._apply_mode(winwindow.PILL, force=True)

    # ---------------------------------------------------------------- hover
    def _add_glow(self, button: Any, base: str, glow: str = ACCENT, steps: int = 6) -> None:
        """Fade a button's fill toward ``glow`` on hover, back to ``base`` off.

        CustomTkinter snaps between ``fg_color`` and ``hover_color``; this
        interpolates instead so the transition reads as a glow rather than a
        flicker. The animation is cancelled if the pointer leaves mid-fade.
        """
        state: dict[str, Any] = {"after": None}
        # Register the state so the toolbar can cancel every pending fade on
        # close, even when the pointer is mid-transition.
        self._glow_states.append(state)

        def cancel() -> None:
            if state["after"]:
                try:
                    self.after_cancel(state["after"])
                except Exception:  # pragma: no cover
                    pass
                state["after"] = None

        def fade(from_color: str, to_color: str, i: int = 1) -> None:
            if i > steps:
                state["after"] = None
                return
            try:
                button.configure(fg_color=_mix(from_color, to_color, ease_out(i / steps)))
            except Exception:  # pragma: no cover - widget torn down
                return
            state["after"] = self.after(ANIM_MS, fade, from_color, to_color, i + 1)

        def on_enter(_event: Any) -> None:
            cancel()
            fade(base, glow)

        def on_leave(_event: Any) -> None:
            cancel()
            fade(glow, base)

        for widget in (button, getattr(button, "_canvas", None)):
            if widget is None:
                continue
            widget.bind("<Enter>", on_enter, add="+")
            widget.bind("<Leave>", on_leave, add="+")

    # ------------------------------------------------------------- dragging
    def _make_draggable(self, widget: Any) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, event: Any) -> None:
        self._drag_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event: Any) -> None:
        if self._drag_origin is None:
            return
        dx, dy = self._drag_origin
        x, y = event.x_root - dx, event.y_root - dy
        self.geometry(f"+{x}+{y}")
        # Remember the user's placement so tracking does not fight them.
        self._manual_offset = (x, y)

    # ---------------------------------------------------------------- modes
    def _bar_max_width(self) -> int:
        try:
            screen = self.winfo_screenwidth()
        except Exception:  # pragma: no cover - headless
            screen = MAX_BAR_WIDTH + SCREEN_MARGIN
        return max_bar_width(screen)

    def _measure(self, mode: str) -> tuple[int, int]:
        if mode == winwindow.PILL:
            return 104, 40
        self.update_idletasks()
        # Clamp to the screen: the requested width grows with the longest status
        # line, and an unbounded bar pushed its own buttons off the edge.
        width = max(MIN_BAR_WIDTH, self.shell.winfo_reqwidth())
        width = min(width, self._bar_max_width())
        height = self.shell.winfo_reqheight()
        return width, height

    def _apply_mode(self, mode: str, force: bool = False) -> None:
        if mode == self._mode and not force:
            self._reposition()
            return

        previous = self._mode
        self._mode = mode

        if mode == winwindow.HIDDEN:
            if previous is not None:
                self.withdraw()
            return

        if mode == winwindow.PILL:
            self.shell.pack_forget()
            self.pill.pack(fill="both", expand=True)
        else:
            self.pill.pack_forget()
            self.shell.pack(fill="both", expand=True)

        width, height = self._measure(mode)
        # ``force`` is used by callers that need the final geometry immediately
        # (the pill click, tests, programmatic state changes). Animate only the
        # organic state-machine transitions.
        if not force and previous is not None and previous != winwindow.HIDDEN and previous != mode:
            self._animate_size(width, height)
        else:
            self.geometry(f"{width}x{height}")
        self._reposition()
        if previous is None or previous == winwindow.HIDDEN:
            self._show_without_focus()

    def _animate_size(self, target_w: int, target_h: int, steps: int = 10) -> None:
        """Ease the window from its current size to the target size.

        The final geometry is applied unconditionally at the end, so a dropped
        frame can never leave the bar at an intermediate size.
        """
        try:
            start_w = max(1, self.winfo_width())
            start_h = max(1, self.winfo_height())
        except Exception:  # pragma: no cover - headless
            self.geometry(f"{target_w}x{target_h}")
            return

        if (start_w, start_h) == (target_w, target_h):
            self.geometry(f"{target_w}x{target_h}")
            return

        self._stop_size_animation()

        def step(i: int) -> None:
            if self._closing:
                self._size_after = None
                return
            t = ease_out(i / steps)
            w = int(start_w + (target_w - start_w) * t)
            h = int(start_h + (target_h - start_h) * t)
            try:
                self.geometry(f"{w}x{h}")
            except Exception:  # pragma: no cover
                return
            if i < steps:
                self._size_after = self.after(ANIM_MS, step, i + 1)
            else:
                self._size_after = None
                self.geometry(f"{target_w}x{target_h}")
                self._reposition()

        step(1)

    def _stop_size_animation(self) -> None:
        after = getattr(self, "_size_after", None)
        if after:
            try:
                self.after_cancel(after)
            except Exception:  # pragma: no cover
                pass
        self._size_after = None

    def _reposition(self) -> None:
        if self._mode in (None, winwindow.HIDDEN):
            return
        width, height = self._measure(self._mode)

        # A user-dragged position wins until the window is re-anchored.
        if self._manual_offset is not None:
            x, y = self._manual_offset
        else:
            rect = self._photo_rect()
            if rect is None:
                return
            x, y = winwindow.anchor_for(self._mode, rect, width, height)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _show_without_focus(self) -> None:
        """Map the window without stealing focus from PhotoCraft."""
        try:
            self.deiconify()
        except Exception:  # pragma: no cover
            pass
        if winwindow.IS_WINDOWS:
            try:
                hwnd = winwindow.toplevel_hwnd(self.winfo_id())
                winwindow.show_without_activating(hwnd)
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------ tracking
    def _photo_rect(self) -> winwindow.Rect | None:
        """Current PhotoCraft window rectangle, re-resolving the handle if needed."""
        if not self.core_pid:
            return None
        if self._photo_hwnd and not winwindow.is_window_visible(self._photo_hwnd):
            self._photo_hwnd = None
        if not self._photo_hwnd:
            self._photo_hwnd = winwindow.find_main_window(self.core_pid)
        if not self._photo_hwnd:
            return None
        return winwindow.get_window_rect(self._photo_hwnd)

    def _is_typing(self) -> bool:
        try:
            return self.focus_get() is self.prompt_entry
        except Exception:
            return False

    def _track(self) -> None:
        """Poll PhotoCraft's window and apply the contextual state machine."""
        if self._closing:
            return
        try:
            self._track_once()
        except Exception:  # pragma: no cover - never kill the loop
            log.debug("track failed: %s", traceback.format_exc())
        if not self._closing:
            self.after(TRACK_MS, self._track)

    def _track_once(self) -> None:
        if not self.core_pid:
            # Standalone run (no launcher): behave like a normal floating bar.
            self._manual_offset = None
            self._apply_mode(winwindow.BAR)
            return

        photo_running = winwindow.pid_is_alive(self.core_pid)
        rect = self._photo_rect()
        minimized = bool(self._photo_hwnd and winwindow.is_minimized(self._photo_hwnd))

        # Match on process id, not window handle: the Settings dialog is a
        # second top-level window in our own process, and a handle comparison
        # read it as "focus left PhotoCraft".
        photo_foreground = winwindow.foreground_is_one_of([self.core_pid])
        our_foreground = winwindow.foreground_is_one_of([os.getpid()])

        settings_open = self._settings is not None and self._settings.winfo_exists()
        has_selection = bool(self._state and self._state.has_selection)

        mode = winwindow.decide_mode(
            photo_running=photo_running,
            photo_minimized=minimized,
            photo_foreground=photo_foreground,
            our_foreground=our_foreground,
            settings_open=settings_open,
            typing=self._is_typing(),
            auto_popup=bool(self.config_obj.auto_popup),
            has_selection=has_selection,
            user_expanded=self._user_expanded,
        )

        if not photo_running:
            self._on_close()
            return

        # Re-anchor only when the shape actually changes. Clearing the offset on
        # every poll threw away the user's drag each time the bar re-rendered.
        if self._mode != mode:
            self._manual_offset = None
        self._apply_mode(mode)

    # ---------------------------------------------------------------- pin
    def _toggle_pin(self) -> None:
        pinned = not bool(self.config_obj.always_on_top)
        self.config_obj.update(always_on_top=pinned)
        try:
            self.attributes("-topmost", pinned)
        except Exception:  # pragma: no cover
            pass
        self.pin_btn.configure(
            fg_color=SURFACE if pinned else "transparent",
            text_color=TEXT if pinned else MUTED,
        )
        self._set_status("Pinned on top." if pinned else "Unpinned.")

    # ------------------------------------------------------------ settings
    def _open_settings(self) -> None:
        # Opening the gear is a deliberate act: keep the bar open from now on.
        self._user_expanded = True
        if self._mode != winwindow.BAR:
            self._apply_mode(winwindow.BAR, force=True)
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.lift()
            self._settings.focus_force()
            return
        self._settings = SettingsDialog(self)

    def on_settings_saved(self) -> None:
        self.ai.reset()
        self.client.disconnect()
        self._set_status("Settings saved. Reconnecting...", OK)
        self._start_connect()

    # -------------------------------------------------------------- queue
    def _post(self, kind: str, payload: Any = None) -> None:
        self._ui_queue.put((kind, payload))

    def _pump_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        if not self._closing:
            self.after(70, self._pump_queue)

    def _handle_event(self, kind: str, payload: Any) -> None:
        if kind == "connection":
            connected, detail = payload
            self._render_connection(connected, detail)
        elif kind == "state":
            self._render_state(payload)
        elif kind == "status":
            message, color = payload
            self._set_status(message, color)
        elif kind == "busy":
            self._set_busy(bool(payload))
        elif kind == "done":
            self._set_busy(False)
            self._set_status(payload, OK)
        elif kind == "error":
            self._set_busy(False)
            self._set_status(payload, BAD)
        elif kind == "test_result":
            ok, message = payload
            if self._settings is not None:
                self._settings.show_test_result(bool(ok), str(message))

    # ------------------------------------------------------------ rendering
    def _fit(self) -> None:
        if self._mode in (None, winwindow.HIDDEN):
            return
        width, height = self._measure(self._mode)
        self.geometry(f"{width}x{height}")
        self._reposition()

    def _status_wraplength(self) -> int:
        """Pixels the status text may occupy before it wraps.

        Measured from the clamped bar width, not the requested width, so a long
        message wraps inside the window instead of widening it.
        """
        width = min(max(MIN_BAR_WIDTH, self.shell.winfo_reqwidth()), self._bar_max_width())
        return max(160, width - 40)

    def _set_status(self, message: str, color: str = MUTED) -> None:
        message = self._elide_status(message)
        self.status_line.configure(
            text=message, text_color=color, wraplength=self._status_wraplength()
        )
        visible = bool(message)
        if visible and not self.status_line.winfo_ismapped():
            # A little more left inset than right keeps the first glyph clear
            # of the rounded border; anchor/justify keep it flush-left.
            self.status_line.pack(fill="x", padx=(14, 12), pady=(0, 7))
            self._fit()
        elif not visible and self.status_line.winfo_ismapped():
            self.status_line.pack_forget()
            self._fit()

    def _elide_status(self, message: str) -> str:
        """Keep a long error to roughly two wrapped lines.

        The full text still goes to the log; the bar shows the head of it plus an
        ellipsis so the message stays readable without growing the window.
        """
        message = (message or "").strip()
        if not message:
            return ""
        limit = max(80, (self._status_wraplength() // 5) * STATUS_WRAP_LINES)
        if len(message) <= limit:
            return message
        log.info("status (truncated in bar): %s", message)
        return message[: limit - 1].rstrip() + "\u2026"

    def _render_connection(self, connected: bool, detail: str | None = None) -> None:
        self.status_dot.configure(text_color=OK if connected else BAD)
        if not connected:
            self.badge.configure(text="Disconnected", text_color=MUTED)
            self._state = None
        if detail:
            self._set_status(detail, MUTED if connected else BAD)

    def _render_state(self, state: DocumentState | None) -> None:
        self._state = state
        if state is None:
            self.badge.configure(text="No document", text_color=MUTED)
            return
        if state.has_selection and state.selection is not None:
            self.badge.configure(text=f"Selection: {state.selection.label}", text_color=ACCENT)
        else:
            self.badge.configure(text="Full Canvas", text_color=TEXT)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.generate_btn.configure(text="Generating...", state="disabled", fg_color=NEON_VIOLET)
            self.wave.pack(fill="x", side="bottom")
            self.wave.start()
            self._pulse_badge()
        else:
            self.generate_btn.configure(text="Generative Fill", state="normal", fg_color=ACCENT)
            self.wave.stop()
            self.wave.pack_forget()
            self._stop_badge_pulse()
        self._fit()

    def _pulse_badge(self) -> None:
        """Pulse the status badge while a generation is running."""
        if not self._busy:
            return
        self._badge_phase = getattr(self, "_badge_phase", 0.0) + 0.12
        color = neon_at(self._badge_phase % 1.0)
        try:
            self.badge.configure(text_color=color)
            self.status_dot.configure(text_color=color)
        except Exception:  # pragma: no cover - widget torn down
            pass
        self._badge_after = self.after(ANIM_MS * 3, self._pulse_badge)

    def _stop_badge_pulse(self) -> None:
        after = getattr(self, "_badge_after", None)
        if after:
            try:
                self.after_cancel(after)
            except Exception:  # pragma: no cover
                pass
        self._badge_after = None

    # ------------------------------------------------------------ workers
    def _start_workers(self) -> None:
        threading.Thread(target=self._monitor_loop, name="pc-monitor", daemon=True).start()
        self._start_connect()

    def _start_connect(self) -> None:
        threading.Thread(target=self._connect_worker, name="pc-connect", daemon=True).start()

    def _connect_worker(self) -> None:
        try:
            self.client.connect(force=True)
            self._post("connection", (True, "Connected to PhotoCraft."))
            self._post("state", self.client.get_document_state())
        except AuthError as exc:
            self._post("connection", (False, f"Auth failed: {exc}"))
        except NoDocumentError:
            self._post("connection", (True, "Connected. No document open."))
            self._post("state", None)
        except PhotocraftError as exc:
            self._post("connection", (False, f"Not connected: {exc}"))
        except Exception as exc:  # pragma: no cover
            log.debug("connect worker: %s", traceback.format_exc())
            self._post("connection", (False, f"Not connected: {exc}"))

    def _monitor_loop(self) -> None:
        while not self._closing:
            threading.Event().wait(POLL_MS / 1000.0)
            if self._closing or self._busy:
                continue
            was_connected = self.client.connected
            try:
                if not self.client.connected:
                    self.client.connect()
                    self._post("connection", (True, "Reconnected to PhotoCraft."))
                self._post("state", self.client.get_document_state())
            except NoDocumentError:
                self._post("connection", (True, "Connected. No document open."))
                self._post("state", None)
            except PhotocraftError as exc:
                self.client.disconnect()
                if was_connected:
                    self._post("connection", (False, f"Connection lost: {exc}"))
            except Exception:  # pragma: no cover
                log.debug("monitor: %s", traceback.format_exc())

    # ------------------------------------------------------------ generate
    def _on_prompt_changed(self, _event: Any = None) -> None:
        """Right-align the prompt when it is mostly Persian, left otherwise.

        The text itself is never reordered. Tk performs BiDi and glyph shaping,
        so the logical string the user typed is the correct thing to display
        and the correct thing to send to the model.
        """
        try:
            text = self.prompt_entry.get()
        except Exception:  # pragma: no cover - widget torn down
            return
        want = "right" if rtl.is_rtl_dominant(text) else "left"
        try:
            if str(self.prompt_entry.cget("justify")) != want:
                self.prompt_entry.configure(justify=want)
        except Exception:  # pragma: no cover - justify unsupported
            pass

    def _on_generate(self) -> None:
        if self._busy:
            return
        # The logical prompt, exactly as typed. Persian must reach Gemini in
        # logical order; reversing it for display would corrupt the request.
        prompt = rtl.logical_prompt(self.prompt_entry.get())
        if not prompt:
            self._set_status("Type what to generate first.", WARN)
            self.prompt_entry.focus_set()
            return
        if not self.ai.available:
            self._set_status(NO_KEY_MESSAGE, WARN)
            self._open_settings()
            return

        self._set_busy(True)
        self._set_status("Reading selection from PhotoCraft...")
        threading.Thread(target=self._generate_worker, args=(prompt,), name="pc-fill", daemon=True).start()

    def _generate_worker(self, prompt: str) -> None:
        try:
            self.client.connect()

            state = self.client.get_document_state()
            self._post("state", state)
            if state.width <= 0 or state.height <= 0:
                raise ProtocolError("Document has no usable canvas size.")

            selection = state.selection if state.has_selection else None

            self._post("status", ("Rendering canvas...", MUTED))
            canvas = self.client.render_canvas()

            if selection is not None:
                padded = selection.padded(self.config_obj.context_padding, state.canvas)
                source = self.client.crop_selection(canvas, padded)
                target = selection
                region_label = f"selection {selection.label}"
            else:
                source = canvas
                target = Selection(0, 0, state.width, state.height)
                region_label = "full canvas"

            self._post("status", (f"Sending {region_label} to {self.config_obj.provider_label}...", MUTED))
            context = canvas if (self.config_obj.send_full_context and selection is not None) else None

            result = self.ai.generative_fill(
                source, prompt,
                is_selection=selection is not None,
                context_image=context,
            )

            patch = result.image
            if selection is not None:
                padded = selection.padded(self.config_obj.context_padding, state.canvas)
                if patch.size != source.size:
                    patch = patch.resize(source.size, Image.LANCZOS)
                if source.size != (selection.width, selection.height):
                    ox = selection.x - padded.x
                    oy = selection.y - padded.y
                    patch = patch.crop((ox, oy, ox + selection.width, oy + selection.height))

            self._post("status", ("Placing layer...", MUTED))
            placed = self.client.place_in_canvas(
                patch, target,
                name=f"AI Fill - {prompt[:40]}",
                align_to_rect=selection is not None,
            )

            layer_id = placed.get("layer")
            self._post("state", self.client.get_document_state())
            self._post("done", f"Placed layer {layer_id} · {region_label} · {result.elapsed_s:.1f}s")
        except MissingAPIKeyError as exc:
            self._post("error", str(exc))
        except SafetyBlockedError as exc:
            self._post("error", f"Blocked by safety filters: {exc}")
        except AuthError as exc:
            self._post("error", f"PhotoCraft auth failed: {exc}")
        except NoDocumentError:
            self._post("error", "No document is open in PhotoCraft.")
        except RemoteError as exc:
            self._post("error", f"PhotoCraft rejected the call: {exc.message}")
        except ProtocolError as exc:
            self._post("error", f"Protocol problem: {exc}")
        except ConnectionLost as exc:
            self._post("connection", (False, str(exc)))
            self._post("error", f"Lost PhotoCraft connection: {exc}")
        except AIError as exc:
            self._post("error", f"Gemini error: {exc}")
        except Exception as exc:  # pragma: no cover
            log.error("generate failed: %s", traceback.format_exc())
            self._post("error", f"Unexpected error: {exc}")

    # ---------------------------------------------------------------- close
    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._neon.stop()
        self._stop_badge_pulse()
        self._stop_size_animation()
        for state in self._glow_states:
            after = state.get("after")
            if after:
                try:
                    self.after_cancel(after)
                except Exception:  # pragma: no cover
                    pass
                state["after"] = None
        if self.wave is not None:
            self.wave.stop()
        try:
            self.client.disconnect()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PhotoCraft Generative Fill toolbar")
    parser.add_argument("--core-pid", type=int, default=0, help="PID of photocraft_core.exe to track")
    parser.add_argument("--port", type=int, default=0, help="Override the control port")
    parser.add_argument("--standalone", action="store_true", help="Run without window tracking")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args(argv)

    config = get_config()
    if args.port:
        config.photocraft_port = args.port

    app = FillToolbar(config, core_pid=0 if args.standalone else args.core_pid)
    app.mainloop()


if __name__ == "__main__":
    main()
