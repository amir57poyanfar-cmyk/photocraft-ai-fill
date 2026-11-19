"""Settings modal tests: three fields, live provider readout, Test API.

Real Tk widgets, so these check the dialog the user actually sees rather than a
mock of it. The dialog is built on top of a real toolbar, which is how it runs
in the app.
"""

from __future__ import annotations

import unittest
from unittest import mock

import ui
from config import (
    Config,
    PROVIDER_FAL,
    PROVIDER_GEMINI,
    PROVIDER_HF,
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    PROVIDER_REPLICATE,
)


def _temp_config_path() -> str:
    """A throwaway config file, so saving never touches the real config.json."""
    import tempfile
    from pathlib import Path
    return str(Path(tempfile.mkdtemp()) / "config.json")


def make_config(**overrides) -> Config:
    base = dict(
        api_key="", base_url="", model_name="",
        control_token="a" * 64,
        temp_dir=r"E:\ai-scatch\.tmp",
        config_path=_temp_config_path(),
    )
    base.update(overrides)
    return Config(**base)


class TestSettingsDialog(unittest.TestCase):
    def setUp(self):
        self.app = ui.FillToolbar(make_config())
        self.app.update()
        self.dialog = ui.SettingsDialog(self.app)
        self.app._settings = self.dialog
        self.dialog.update()

    def tearDown(self):
        try:
            self.dialog.destroy()
        except Exception:
            pass
        try:
            self.app.destroy()
        except Exception:
            pass

    def _type(self, entry, text: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, text)
        self.dialog._refresh_provider()
        self.dialog.update()

    def test_has_the_three_fields(self):
        self.assertTrue(hasattr(self.dialog, "api_key_entry"))
        self.assertTrue(hasattr(self.dialog, "base_url_entry"))
        self.assertTrue(hasattr(self.dialog, "model_entry"))

    def test_has_a_test_button(self):
        self.assertEqual(self.dialog.test_button.cget("text"), "Test API")

    def test_key_is_masked(self):
        # customtkinter 6 does not expose `show` on the wrapper, and applies it
        # to the inner Tk entry only once the field has content. So type a key
        # and check what the widget would actually render.
        self.dialog.api_key_entry.insert(0, "SECRETKEY")
        self.dialog.update()
        self.assertEqual(self.dialog.api_key_entry._entry.cget("show"), "•")

    def test_placeholders_explain_the_fields(self):
        self.assertIn("paste any key", self.dialog.api_key_entry.cget("placeholder_text"))
        self.assertIn("auto-detect", self.dialog.base_url_entry.cget("placeholder_text"))
        self.assertIn("default", self.dialog.model_entry.cget("placeholder_text"))

    def test_hint_with_nothing_configured(self):
        self.assertIn("No key yet", self.dialog.provider_hint.cget("text"))

    def test_hint_detects_each_provider(self):
        cases = {
            "AIzaSyABC": "Google Gemini",
            "sk-proj-abc": "OpenAI",
            "sk-or-v1-abc": "OpenRouter",
            "hf_abc": "Hugging Face",
            "r8_abc": "Replicate",
            "fal_a:b": "fal.ai",
            "mystery-key": "Custom",
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self._type(self.dialog.api_key_entry, key)
                self.assertIn(expected, self.dialog.provider_hint.cget("text"))

    def test_hint_shows_the_resolved_endpoint_and_model(self):
        self._type(self.dialog.api_key_entry, "sk-or-v1-abc")
        hint = self.dialog.provider_hint.cget("text")
        self.assertIn("https://openrouter.ai/api/v1", hint)
        self.assertIn("google/gemini-2.5-flash-image-preview", hint)

    def test_custom_url_overrides_the_key_prefix(self):
        self._type(self.dialog.api_key_entry, "sk-abc")
        self._type(self.dialog.base_url_entry, "https://relay.example.com/v1")
        hint = self.dialog.provider_hint.cget("text")
        self.assertIn("Custom", hint)
        self.assertIn("https://relay.example.com/v1", hint)

    def test_model_override_is_shown(self):
        self._type(self.dialog.api_key_entry, "sk-abc")
        self._type(self.dialog.model_entry, "my-custom-model")
        self.assertIn("my-custom-model", self.dialog.provider_hint.cget("text"))

    def test_test_without_any_config_does_not_launch_a_probe(self):
        with mock.patch.object(ui.threading, "Thread") as thread:
            self.dialog._test()
        thread.assert_not_called()
        self.assertIn("No API key configured", self.dialog.test_result.cget("text"))

    def test_test_launches_a_probe_with_the_typed_values(self):
        self._type(self.dialog.api_key_entry, "sk-test")
        self._type(self.dialog.base_url_entry, "https://relay.test/v1")
        self._type(self.dialog.model_entry, "m")
        captured = {}

        def fake_thread(target=None, args=(), **kwargs):
            captured["probe"] = args[0]
            return mock.MagicMock()

        with mock.patch.object(ui.threading, "Thread", side_effect=fake_thread):
            self.dialog._test()
        probe = captured["probe"]
        self.assertEqual(probe.api_key, "sk-test")
        self.assertEqual(probe.base_url, "https://relay.test/v1")
        self.assertEqual(probe.model_name, "m")

    def test_test_probe_does_not_save(self):
        self._type(self.dialog.api_key_entry, "sk-unsaved")
        with mock.patch.object(ui.threading, "Thread", return_value=mock.MagicMock()):
            self.dialog._test()
        self.assertEqual(self.app.config_obj.api_key, "")

    def test_success_result_is_shown_green(self):
        self.dialog.show_test_result(True, "OpenAI key works. Model: gpt-image-1")
        self.assertIn("OpenAI key works", self.dialog.test_result.cget("text"))
        self.assertEqual(self.dialog.test_result.cget("text_color"), ui.OK)

    def test_failure_result_shows_the_server_text(self):
        self.dialog.show_test_result(False, "[openai / HTTP 401] invalid api key")
        self.assertIn("invalid api key", self.dialog.test_result.cget("text"))
        self.assertEqual(self.dialog.test_result.cget("text_color"), ui.BAD)

    def test_test_result_is_wrapped(self):
        self.dialog.show_test_result(False, "x" * 400)
        self.assertGreater(int(self.dialog.test_result.cget("wraplength")), 0)

    def test_save_persists_all_three_fields(self):
        self._type(self.dialog.api_key_entry, "r8_test")
        self._type(self.dialog.base_url_entry, "https://api.replicate.com/v1")
        self._type(self.dialog.model_entry, "owner/model")
        self.dialog._save()
        cfg = self.app.config_obj
        self.assertEqual(cfg.api_key, "r8_test")
        self.assertEqual(cfg.base_url, "https://api.replicate.com/v1")
        self.assertEqual(cfg.model_name, "owner/model")
        self.assertEqual(cfg.active_provider, PROVIDER_REPLICATE)

    def test_save_clears_the_fields_when_emptied(self):
        self._type(self.dialog.api_key_entry, "sk-x")
        self.dialog._save()
        self.assertEqual(self.app.config_obj.api_key, "sk-x")
        self._type(self.dialog.api_key_entry, "")
        self.dialog._save()
        self.assertEqual(self.app.config_obj.api_key, "")

    def test_save_rejects_a_bad_token(self):
        self.dialog.token_entry.delete(0, "end")
        self.dialog.token_entry.insert(0, "short")
        self.dialog._save()
        self.assertIn("64 hex", self.dialog.message.cget("text"))


class TestToolbarWithNoKey(unittest.TestCase):
    """The bar must say what to do, not silently generate something."""

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

    def test_generate_without_a_key_opens_settings(self):
        self.app.prompt_entry.insert(0, "remove the text")
        with mock.patch.object(self.app, "_open_settings") as opened:
            self.app._on_generate()
        opened.assert_called_once()
        self.assertIn("No API key configured", self.app.status_line.cget("text"))

    def test_generate_without_a_key_does_not_start_a_worker(self):
        self.app.prompt_entry.insert(0, "remove the text")
        with mock.patch.object(ui.threading, "Thread") as thread, \
             mock.patch.object(self.app, "_open_settings"):
            self.app._on_generate()
        thread.assert_not_called()

    def test_empty_prompt_is_rejected_first(self):
        with mock.patch.object(self.app, "_open_settings") as opened:
            self.app._on_generate()
        opened.assert_not_called()
        self.assertIn("Type what to generate", self.app.status_line.cget("text"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
