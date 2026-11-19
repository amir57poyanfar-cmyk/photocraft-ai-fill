"""Headless tests for the clipboard paste fix in ui.py.

No Tk window is created: the entry is a small fake exposing the tk.Entry
methods the helpers use, and key events are plain objects with a physical
``keycode``. This is what makes the tests safe to run on a desktop without
popping the neon toolbar onto the screen.

Run:  python test_clipboard_paste.py
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import ui


class FakeEntry:
    """Minimal tk.Entry stand-in with a real selection model."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.cursor = len(text)
        self.sel: tuple[int, int] | None = None
        self.clipboard: str = ""

    # -- tk.Entry text API -------------------------------------------------
    def get(self, first: object = 0, last: object = "end") -> str:
        if first == 0 and last == "end":
            return self.text
        if first == "sel.first" and last == "sel.last" and self.sel:
            return self.text[self.sel[0]:self.sel[1]]
        return self.text[int(first):int(last)]

    def insert(self, index: object, text: str) -> None:
        pos = self.cursor if index == "insert" else int(index)
        self.text = self.text[:pos] + text + self.text[pos:]
        self.cursor = pos + len(text)
        self.sel = None

    def delete(self, first: object, last: object | None = None) -> None:
        if last is None:
            first, last = int(first), int(first) + 1
        else:
            first = self.cursor if first == "insert" else int(first)
            last = self.cursor if last == "insert" else int(last)
        self.text = self.text[:first] + self.text[last:]
        self.cursor = first
        self.sel = None

    def index(self, what: str) -> int:
        if what == "sel.first":
            if not self.sel:
                raise ValueError("no selection")
            return self.sel[0]
        if what == "sel.last":
            if not self.sel:
                raise ValueError("no selection")
            return self.sel[1]
        raise ValueError(what)

    def select_range(self, first: int, last: str) -> None:
        self.sel = (first, len(self.text))

    def icursor(self, index: object) -> None:
        self.cursor = len(self.text)

    # -- clipboard API -----------------------------------------------------
    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, text: str) -> None:
        self.clipboard += text

    def clipboard_get(self) -> str:
        if not self.clipboard:
            raise ValueError("CLIPBOARD empty")
        return self.clipboard


def key(code: int, keysym: str = "unknown") -> SimpleNamespace:
    """A key event. ``keysym`` is deliberately wrong for the FA case."""
    return SimpleNamespace(keycode=code, keysym=keysym)


class TestPhysicalKeycodes(unittest.TestCase):
    def test_ctrl_v_keycode_86_pastes_even_with_persian_keysym(self):
        entry = FakeEntry("hello")
        entry.cursor = 5
        entry.clipboard = " متن "
        result = ui.handle_clipboard_key(key(86, "و"), entry)
        self.assertEqual(result, "break")
        self.assertEqual(entry.text, "hello متن ")

    def test_ctrl_v_replaces_selected_text(self):
        entry = FakeEntry("selected")
        entry.sel = (0, 8)
        entry.clipboard = "NEW"
        ui.handle_clipboard_key(key(86), entry)
        self.assertEqual(entry.text, "NEW")

    def test_ctrl_c_keycode_67_copies_selection(self):
        entry = FakeEntry("abcdef")
        entry.sel = (1, 4)
        ui.handle_clipboard_key(key(67), entry)
        self.assertEqual(entry.clipboard, "bcd")
        self.assertEqual(entry.text, "abcdef")

    def test_ctrl_x_keycode_88_cuts_selection(self):
        entry = FakeEntry("abcdef")
        entry.sel = (1, 4)
        ui.handle_clipboard_key(key(88), entry)
        self.assertEqual(entry.clipboard, "bcd")
        self.assertEqual(entry.text, "aef")

    def test_ctrl_a_keycode_65_selects_all(self):
        entry = FakeEntry("abcdef")
        ui.handle_clipboard_key(key(65), entry)
        self.assertEqual(entry.sel, (0, 6))
        self.assertEqual(entry.cursor, 6)

    def test_unrelated_key_is_not_handled(self):
        entry = FakeEntry("abc")
        self.assertIsNone(ui.handle_clipboard_key(key(66), entry))
        self.assertEqual(entry.text, "abc")

    def test_paste_with_empty_clipboard_is_a_noop(self):
        entry = FakeEntry("abc")
        entry.cursor = 3
        self.assertTrue(ui.clipboard_paste(entry) is False)
        self.assertEqual(entry.text, "abc")

    def test_event_widget_fallback(self):
        entry = FakeEntry("x")
        entry.clipboard = "Y"
        event = SimpleNamespace(keycode=86, widget=entry)
        self.assertEqual(ui.handle_clipboard_key(event), "break")
        self.assertEqual(entry.text, "xY")


class TestContextMenu(unittest.TestCase):
    def test_menu_has_paste_copy_cut_and_select_all(self):
        class FakeMenu:
            def __init__(self, *a, **kw):
                self.commands = []
                self.separators = 0

            def add_command(self, label, command):
                self.commands.append(label)

            def add_separator(self):
                self.separators += 1

        entry = FakeEntry("abc")
        menu = ui._make_entry_menu(entry, menu_factory=FakeMenu)
        self.assertEqual(menu.commands, ["Paste", "Copy", "Cut", "Select All"])
        self.assertEqual(menu.separators, 1)

    def test_menu_paste_command_uses_clipboard(self):
        class FakeMenu:
            def __init__(self, *a, **kw):
                self.commands = {}

            def add_command(self, label, command):
                self.commands[label] = command

            def add_separator(self):
                pass

        entry = FakeEntry("abc")
        entry.clipboard = "XYZ"
        menu = ui._make_entry_menu(entry, menu_factory=FakeMenu)
        menu.commands["Paste"]()
        self.assertEqual(entry.text, "abcXYZ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
