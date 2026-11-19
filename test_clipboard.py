"""Clipboard image transport tests.

These exercise the real Windows clipboard, because the failure modes that
matter (a locked clipboard, a DIB with a doubled header, a PNG format that is
registered but empty) only appear against the real thing.
"""

from __future__ import annotations

import unittest

from PIL import Image

import clipboard_image


class TestRoundTrip(unittest.TestCase):
    def test_write_then_read_returns_the_same_pixels(self):
        original = Image.new("RGB", (64, 48), (12, 200, 90))
        clipboard_image.write_image(original)
        back = clipboard_image.read_image()
        self.assertIsNotNone(back)
        self.assertEqual(back.size, (64, 48))
        # JPEG-free path, so the corner pixel must match exactly.
        self.assertEqual(back.convert("RGB").getpixel((0, 0)), (12, 200, 90))

    def test_read_returns_none_when_clipboard_holds_no_image(self):
        clipboard_image.clear_clipboard()
        self.assertIsNone(clipboard_image.read_image())

    def test_clear_empties_the_clipboard(self):
        clipboard_image.write_image(Image.new("RGB", (8, 8), (1, 2, 3)))
        clipboard_image.clear_clipboard()
        self.assertIsNone(clipboard_image.read_image())

    def test_write_rejects_none(self):
        with self.assertRaises(clipboard_image.ClipboardError):
            clipboard_image.write_image(None)


class TestSizes(unittest.TestCase):
    def test_non_square_size_survives(self):
        # A transposed width/height is the classic DIB header bug.
        clipboard_image.write_image(Image.new("RGB", (300, 200), (255, 0, 0)))
        back = clipboard_image.read_image()
        self.assertEqual(back.size, (300, 200))

    def test_large_image_survives(self):
        clipboard_image.write_image(Image.new("RGB", (800, 600), (240, 240, 240)))
        back = clipboard_image.read_image()
        self.assertEqual(back.size, (800, 600))

    def test_one_pixel_image_survives(self):
        clipboard_image.write_image(Image.new("RGB", (1, 1), (7, 8, 9)))
        back = clipboard_image.read_image()
        self.assertEqual(back.size, (1, 1))


class TestClipboardIsReleased(unittest.TestCase):
    def test_clipboard_is_not_left_locked_after_write(self):
        clipboard_image.write_image(Image.new("RGB", (16, 16), (0, 0, 0)))
        # A second operation would raise if the handle had been left open.
        clipboard_image.write_image(Image.new("RGB", (16, 16), (0, 0, 0)))
        self.assertIsNotNone(clipboard_image.read_image())

    def test_clipboard_is_not_left_locked_after_read(self):
        clipboard_image.write_image(Image.new("RGB", (16, 16), (0, 0, 0)))
        clipboard_image.read_image()
        clipboard_image.read_image()
        self.assertIsNotNone(clipboard_image.read_image())

    def test_clipboard_is_not_left_locked_after_clear(self):
        clipboard_image.clear_clipboard()
        clipboard_image.clear_clipboard()
        clipboard_image.clear_clipboard()


class TestAlpha(unittest.TestCase):
    def test_rgba_input_is_accepted(self):
        clipboard_image.write_image(Image.new("RGBA", (32, 32), (10, 20, 30, 128)))
        back = clipboard_image.read_image()
        self.assertEqual(back.size, (32, 32))


if __name__ == "__main__":
    unittest.main(verbosity=2)
