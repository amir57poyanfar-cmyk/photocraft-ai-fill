"""Toolbar width tests: the bar must never grow past the screen.

The bug this guards: a long error string widened the status label, the label
widened the bar, and the bar pushed its own Settings gear and collapse buttons
off the right edge of the screen. The window is real (Tk is available on this
machine), so the measurement is the actual geometry, not a mock.
"""

from __future__ import annotations

import unittest

import ui
from config import Config


def _temp_config_path() -> str:
    """A throwaway config file, so saving never touches the real config.json."""
    import tempfile
    from pathlib import Path
    return str(Path(tempfile.mkdtemp()) / "config.json")


def make_config() -> Config:
    return Config(
        api_key="", base_url="", model_name="",
        control_token="a" * 64,
        temp_dir=r"E:\ai-scatch\.tmp",
        config_path=_temp_config_path(),
    )


LONG_ERROR = (
    "Every Hugging Face image model failed. Tried: black-forest-labs/FLUX.1-Kontext-dev, "
    "Qwen/Qwen-Image-Edit, timbrooks/instruct-pix2pix. Details: "
    "black-forest-labs/FLUX.1-Kontext-dev edit failed: 402 Client Error: Payment Required "
    "for url: https://router.huggingface.co/hf-inference/models/black-forest-labs/"
    "FLUX.1-Kontext-dev; Qwen/Qwen-Image-Edit edit failed: 402 Client Error: Payment "
    "Required; timbrooks/instruct-pix2pix edit failed: 402 Client Error: Payment Required"
)


class TestMaxBarWidth(unittest.TestCase):
    def test_capped_on_a_wide_screen(self):
        self.assertEqual(ui.max_bar_width(1920), ui.MAX_BAR_WIDTH)

    def test_shrinks_on_a_narrow_screen(self):
        self.assertEqual(ui.max_bar_width(800), 800 - ui.SCREEN_MARGIN)

    def test_never_below_the_minimum(self):
        self.assertGreaterEqual(ui.max_bar_width(400), ui.MIN_BAR_WIDTH)

    def test_always_leaves_a_margin(self):
        for screen in (1920, 1600, 1366, 1280, 1024, 900):
            self.assertLessEqual(ui.max_bar_width(screen), max(screen - ui.SCREEN_MARGIN, ui.MIN_BAR_WIDTH))


class TestToolbarWidth(unittest.TestCase):
    """Real window geometry, so the assertion is about pixels."""

    def setUp(self):
        self.app = ui.FillToolbar(make_config())
        self.app.update()
        self.app._apply_mode(ui.winwindow.BAR, force=True)
        self.app.update()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass

    def _width(self) -> int:
        self.app.update_idletasks()
        return self.app.winfo_width()

    def test_long_error_does_not_exceed_the_cap(self):
        self.app._set_status(LONG_ERROR, ui.BAD)
        self.app.update()
        self.assertLessEqual(self._width(), ui.max_bar_width(self.app.winfo_screenwidth()))

    def test_long_error_does_not_widen_the_bar(self):
        self.app._set_status("Ready.", ui.OK)
        self.app.update()
        before = self._width()

        self.app._set_status(LONG_ERROR, ui.BAD)
        self.app.update()
        self.assertLessEqual(self._width(), before)

    def test_buttons_stay_inside_the_window(self):
        self.app._set_status(LONG_ERROR, ui.BAD)
        self.app.update()
        self.app.update_idletasks()
        width = self._width()
        for name, button in (
            ("settings", self.app.settings_btn),
            ("collapse", self.app.collapse_btn),
            ("generate", self.app.generate_btn),
        ):
            right = button.winfo_x() + button.winfo_width()
            self.assertLessEqual(
                right, width, f"{name} button extends past the window edge ({right} > {width})"
            )
            self.assertGreater(button.winfo_width(), 1, f"{name} button has no width")

    def test_status_text_is_elided(self):
        self.app._set_status(LONG_ERROR, ui.BAD)
        self.app.update()
        shown = self.app.status_line.cget("text")
        self.assertLess(len(shown), len(LONG_ERROR))
        self.assertTrue(shown.endswith("\u2026"))

    def test_short_status_is_not_elided(self):
        self.app._set_status("Rendering canvas...", ui.MUTED)
        self.app.update()
        self.assertEqual(self.app.status_line.cget("text"), "Rendering canvas...")

    def test_wraplength_is_bounded(self):
        self.app._set_status(LONG_ERROR, ui.BAD)
        self.app.update()
        wraplength = int(self.app.status_line.cget("wraplength"))
        self.assertGreater(wraplength, 0)
        self.assertLessEqual(wraplength, ui.max_bar_width(self.app.winfo_screenwidth()))

    def test_pill_is_still_small(self):
        self.app._apply_mode(ui.winwindow.PILL, force=True)
        self.app.update()
        self.assertLess(self._width(), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
