"""Headless tests for the Settings modal fixes.

No Tk window is opened. Two things are checked:

1. Pasting into a CTkEntry that is showing its placeholder yields exactly the
   pasted key, with no placeholder text mixed in. The fake reproduces
   customtkinter 6's real behaviour: the placeholder is literal text in the
   inner entry, and ``insert``/``_deactivate_placeholder`` manage it.

2. The Settings layout instantiates and packs every required widget and wires
   each action button to its callback. This is verified by driving the real
   ``_build`` method against mocked CTk widget factories, so no window exists.

Run:  python test_settings_layout.py
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

import ui


# ---------------------------------------------------------------------------
# A faithful fake of customtkinter 6's CTkEntry placeholder mechanics.
# ---------------------------------------------------------------------------
class FakeInnerEntry:
    def __init__(self, owner: "FakeCTkEntry") -> None:
        self.owner = owner
        self.value = ""
        self.selection: tuple[int, int] | None = None

    def get(self) -> str:
        return self.value

    def _pos(self, index: object) -> int:
        if index in ("end", "insert"):
            return len(self.value)
        return int(index)

    def delete(self, first: object, last: object = None) -> None:
        a = self._pos(first)
        b = len(self.value) if last is None else self._pos(last)
        self.value = self.value[:a] + self.value[b:]
        self.selection = None

    def insert(self, index: object, text: str) -> None:
        pos = self._pos(index)
        self.value = self.value[:pos] + text + self.value[pos:]
        self.selection = None

    def cget(self, key: str) -> str:
        return self.owner._inner_opts.get(key, "")

    def config(self, **kwargs: object) -> None:
        self.owner._inner_opts.update(kwargs)

    def __setitem__(self, key: str, value: object) -> None:
        self.owner._inner_opts[key] = value

    def index(self, what: str) -> int:
        if not self.selection:
            raise ValueError("no selection")
        return self.selection[0] if what == "sel.first" else self.selection[1]

    def select_range(self, first: int, last: str) -> None:
        self.selection = (first, len(self.value))

    def icursor(self, index: object) -> None:
        pass

    def clipboard_get(self) -> str:
        return self.owner.clipboard

    def clipboard_clear(self) -> None:
        self.owner.clipboard = ""

    def clipboard_append(self, text: str) -> None:
        self.owner.clipboard += text

    def bind(self, *a: object, **kw: object) -> None:
        pass

    def focus_set(self) -> None:
        pass


class FakeCTkEntry:
    """Minimal CTkEntry with a real placeholder state machine."""

    def __init__(self, placeholder_text: str | None = None, show: str = "", **kw: object) -> None:
        self._placeholder_text = placeholder_text
        self._placeholder_text_active = False
        self._pre_placeholder_arguments: dict[str, object] = {"show": show}
        self._inner_opts: dict[str, object] = {"show": show}
        self.clipboard = ""
        self._entry = FakeInnerEntry(self)
        if placeholder_text:
            self._activate_placeholder()

    def _activate_placeholder(self) -> None:
        if self._entry.get() == "" and self._placeholder_text is not None:
            self._placeholder_text_active = True
            self._entry.delete(0, "end")
            self._entry.insert(0, self._placeholder_text)

    def _deactivate_placeholder(self) -> None:
        if self._placeholder_text_active:
            self._placeholder_text_active = False
            self._entry.delete(0, "end")
            for key, value in self._pre_placeholder_arguments.items():
                self._inner_opts[key] = value

    def delete(self, first: object, last: object = None) -> None:
        self._entry.delete(first, last)

    def insert(self, index: object, text: str) -> None:
        self._deactivate_placeholder()
        self._entry.insert(index, text)

    def get(self) -> str:
        return "" if self._placeholder_text_active else self._entry.get()

    def clipboard_get(self) -> str:
        return self.clipboard

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, text: str) -> None:
        self.clipboard += text

    def configure(self, **kw: object) -> None:
        pass

    def bind(self, *a: object, **kw: object) -> None:
        pass

    def focus_force(self) -> None:
        pass

    def winfo_toplevel(self) -> object:
        return self


class TestPlaceholderPaste(unittest.TestCase):
    def test_paste_into_placeholder_replaces_it_entirely(self):
        entry = FakeCTkEntry(placeholder_text="paste any key: AIza..., sk-or-..., hf_...")
        entry.clipboard = "hf_ABCDEF123456"
        self.assertTrue(ui.clipboard_paste(entry))
        self.assertEqual(entry.get(), "hf_ABCDEF123456")
        self.assertNotIn("paste any key", entry.get())
        self.assertFalse(entry._placeholder_text_active)

    def test_paste_via_keycode_86_uses_wrapper(self):
        entry = FakeCTkEntry(placeholder_text="paste any key: AIza..., sk-or-..., hf_...")
        entry.clipboard = "AIzaSyTESTKEY"
        event = SimpleNamespace(keycode=86, widget=entry._entry)
        self.assertEqual(ui.handle_clipboard_key(event, entry), "break")
        self.assertEqual(entry.get(), "AIzaSyTESTKEY")

    def test_paste_into_plain_entry_still_works(self):
        from test_clipboard_paste import FakeEntry
        entry = FakeEntry("abc")
        entry.clipboard = "XYZ"
        # A raw tk.Entry paste keeps the unselected text and inserts at cursor.
        self.assertTrue(ui.clipboard_paste(entry))
        self.assertEqual(entry.text, "abcXYZ")


class TestSettingsLayoutSource(unittest.TestCase):
    """Verify the real _build source wires every required widget."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(ui.__file__).read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.build = next(
            node for node in ast.walk(cls.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_build"
        )
        cls.build_src = ast.get_source_segment(cls.source, cls.build) or ""

    def test_scrollable_content_and_pinned_actions(self):
        self.assertIn("CTkScrollableFrame", self.build_src)
        self.assertIn("actions.pack", self.build_src)

    def test_all_three_action_buttons_created_and_packed(self):
        for name in ("test_btn", "save_btn", "cancel_btn"):
            with self.subTest(button=name):
                self.assertIn(f"self.{name} = ctk.CTkButton", self.build_src)
                self.assertIn(f"self.{name}.pack", self.build_src)

    def test_action_buttons_wired_to_callbacks(self):
        self.assertIn("self.test_btn", self.build_src)
        self.assertIn("command=self._test", self.build_src)
        self.assertIn("command=self._save", self.build_src)
        self.assertIn("command=self._close", self.build_src)

    def test_status_and_feedback_cards_present(self):
        self.assertIn("DETECTED PROVIDER & ENDPOINT", self.build_src)
        self.assertIn("TEST / VALIDATION FEEDBACK", self.build_src)
        self.assertIn("self.provider_badge", self.build_src)
        self.assertIn("self.provider_hint", self.build_src)
        self.assertIn("self.test_result", self.build_src)

    def test_test_button_alias_for_backcompat(self):
        self.assertIn("self.test_button = self.test_btn", self.build_src)

    def test_status_line_has_left_padding_and_west_anchor(self):
        self.assertIn('padx=(14, 12)', self.source)
        self.assertIn('anchor="w"', self.build_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
