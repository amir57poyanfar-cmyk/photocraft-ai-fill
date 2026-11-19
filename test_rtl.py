"""Persian / RTL handling tests.

The central claim these tests lock in: Tk on Windows already renders logical
Persian correctly, so the toolbar must keep the logical string and must **not**
apply ``get_display`` to it. Applying BiDi reordering is the bug, not the fix.
"""

from __future__ import annotations

import unittest

import rtl

PERSIAN = "متن های داخلش رو پاک کن"
MIRRORED = "نک کاپ ور شلخاد یاه نتم"


class TestDetection(unittest.TestCase):
    def test_persian_is_detected(self):
        self.assertTrue(rtl.has_rtl(PERSIAN))

    def test_english_is_not_rtl(self):
        self.assertFalse(rtl.has_rtl("remove all the text"))
        self.assertFalse(rtl.has_rtl(""))
        self.assertFalse(rtl.has_rtl(None))

    def test_mixed_text_with_persian_is_detected(self):
        self.assertTrue(rtl.has_rtl("remove the متن"))

    def test_persian_is_rtl_dominant(self):
        self.assertTrue(rtl.is_rtl_dominant(PERSIAN))

    def test_english_is_not_rtl_dominant(self):
        self.assertFalse(rtl.is_rtl_dominant("remove all the text"))

    def test_one_stray_persian_word_does_not_flip_a_long_english_prompt(self):
        self.assertFalse(rtl.is_rtl_dominant("remove all the text and the متن here"))

    def test_empty_is_not_rtl_dominant(self):
        self.assertFalse(rtl.is_rtl_dominant(""))
        self.assertFalse(rtl.is_rtl_dominant(None))


class TestLogicalIsPreserved(unittest.TestCase):
    """The prompt the model receives must be the logical string, untouched."""

    def test_to_visual_rtl_is_identity(self):
        # Tk does BiDi itself; reordering here would double-reverse the text.
        self.assertEqual(rtl.to_visual_rtl(PERSIAN), PERSIAN)

    def test_to_visual_rtl_does_not_produce_the_mirrored_form(self):
        self.assertNotEqual(rtl.to_visual_rtl(PERSIAN), MIRRORED)

    def test_logical_prompt_keeps_character_order(self):
        self.assertEqual(rtl.logical_prompt(PERSIAN), PERSIAN)

    def test_logical_prompt_keeps_mixed_order(self):
        text = "remove the متن from this area"
        self.assertEqual(rtl.logical_prompt(text), text)

    def test_logical_prompt_strips_surrounding_space(self):
        self.assertEqual(rtl.logical_prompt(f"  {PERSIAN}  "), PERSIAN)

    def test_logical_prompt_collapses_internal_runs(self):
        self.assertEqual(rtl.logical_prompt("a    b\tc"), "a b c")

    def test_logical_prompt_keeps_non_breaking_space_as_space(self):
        self.assertEqual(rtl.logical_prompt("a\u00A0b"), "a b")

    def test_logical_prompt_handles_none_and_empty(self):
        self.assertEqual(rtl.logical_prompt(None), "")
        self.assertEqual(rtl.logical_prompt(""), "")
        self.assertEqual(rtl.logical_prompt("   "), "")

    def test_persian_letters_survive_normalisation(self):
        out = rtl.logical_prompt(f"  {PERSIAN}  ")
        self.assertEqual(len(out), len(PERSIAN))
        self.assertTrue(rtl.has_rtl(out))


class TestBidiReorder(unittest.TestCase):
    """bidi_reorder is for BiDi-unaware surfaces only, and must be correct there."""

    def test_reorder_reverses_persian(self):
        self.assertEqual(rtl.bidi_reorder(PERSIAN), MIRRORED)

    def test_reorder_leaves_english_alone(self):
        self.assertEqual(rtl.bidi_reorder("hello world"), "hello world")

    def test_reorder_is_available(self):
        # python-bidi is a declared dependency; if it were missing the helper
        # would silently return its input and this would fail.
        self.assertNotEqual(rtl.bidi_reorder(PERSIAN), PERSIAN)


class TestPromptBuilding(unittest.TestCase):
    """ai_service must carry the logical Persian prompt into the instruction."""

    def setUp(self):
        from ai_service import build_fill_prompt
        self.build = build_fill_prompt

    def test_persian_prompt_appears_logically(self):
        out = self.build(PERSIAN, is_selection=True)
        self.assertIn(PERSIAN, out)

    def test_persian_prompt_is_not_mirrored(self):
        out = self.build(PERSIAN, is_selection=True)
        self.assertNotIn(MIRRORED, out)

    def test_persian_instruction_gets_a_language_rule(self):
        out = self.build(PERSIAN, is_selection=True)
        self.assertIn("written in Persian", out)

    def test_english_instruction_gets_no_persian_rule(self):
        out = self.build("remove the text", is_selection=True)
        self.assertNotIn("written in Persian", out)

    def test_remove_text_instruction_is_carried_verbatim(self):
        # "متن های داخلش رو پاک کن" = remove the text inside it
        out = self.build(PERSIAN, is_selection=True)
        self.assertIn("Instruction: " + PERSIAN, out)

    def test_full_canvas_prompt_also_keeps_persian(self):
        out = self.build(PERSIAN, is_selection=False)
        self.assertIn(PERSIAN, out)
        self.assertNotIn(MIRRORED, out)

    def test_output_is_an_image_rule_present(self):
        out = self.build(PERSIAN, is_selection=True)
        self.assertIn("Do not render the instruction text into the image", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
