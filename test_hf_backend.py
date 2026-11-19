"""Hugging Face adapter tests.

The multi-model fallback chain and the Persian translation step are gone: the
engine now calls exactly the model the user configured and reports the server's
error if that model fails. What is kept here is the adapter itself, plus the
legacy-config migration, which still has to work for existing installs.

No network: the HF client is replaced with a fake.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import ai_service
from ai_service import AIError, AIService, MissingAPIKeyError, NO_KEY_MESSAGE
from config import Config, PROVIDER_HF


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _temp_config_path() -> str:
    """A throwaway config file, so saving never touches the real config.json."""
    import tempfile
    from pathlib import Path
    return str(Path(tempfile.mkdtemp()) / "config.json")


def make_config(**overrides) -> Config:
    base = dict(
        api_key="hf_" + "a" * 30,
        base_url="",
        model_name="",
        temp_dir=r"E:\ai-scatch\.tmp",
        response_timeout=5.0,
        config_path=_temp_config_path(),
    )
    base.update(overrides)
    return Config(**base)


class FakeHFClient:
    """Stands in for ``InferenceClient``."""

    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls: list[dict] = []

    def image_to_image(self, image, prompt=None, model=None, **kwargs):
        self.calls.append({"model": model, "prompt": prompt, "size": image.size})
        if self.error:
            raise self.error
        return self.output

    def whoami(self):
        return {"name": "tester"}


def install_fake(service: AIService, client) -> None:
    """Install a fake client so ``_get_hf_client`` does not replace it."""
    service._hf_client = client
    service._hf_key = service.config.api_key


class TestProviderSelection(unittest.TestCase):
    def test_hf_token_selects_hugging_face(self):
        self.assertEqual(make_config().active_provider, PROVIDER_HF)

    def test_default_model(self):
        self.assertEqual(
            make_config().active_model, "black-forest-labs/FLUX.1-Kontext-dev"
        )

    def test_explicit_model_wins(self):
        self.assertEqual(
            make_config(model_name="Qwen/Qwen-Image-Edit").active_model,
            "Qwen/Qwen-Image-Edit",
        )

    def test_label_is_human_readable(self):
        self.assertEqual(make_config().provider_label, "Hugging Face")

    def test_no_key_means_no_api_key(self):
        self.assertFalse(make_config(api_key="").has_api_key)


class TestLegacyConfigMigration(unittest.TestCase):
    """An existing install's config.json must keep working untouched."""

    def _load(self, payload: dict) -> Config:
        directory = Path(tempfile.mkdtemp())
        path = directory / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return Config.load(path)

    def test_legacy_hf_token_is_adopted(self):
        cfg = self._load({"hf_token": "hf_legacytoken"})
        self.assertEqual(cfg.api_key, "hf_legacytoken")
        self.assertEqual(cfg.active_provider, PROVIDER_HF)

    def test_legacy_gemini_key_is_adopted(self):
        cfg = self._load({"gemini_api_key": "AIzaLegacy"})
        self.assertEqual(cfg.api_key, "AIzaLegacy")
        self.assertEqual(cfg.active_provider, "gemini")

    def test_hf_token_written_into_gemini_field_is_recovered(self):
        # An older build stored the hf_ token under gemini_api_key.
        cfg = self._load({"gemini_api_key": "hf_misfiled"})
        self.assertEqual(cfg.api_key, "hf_misfiled")
        self.assertEqual(cfg.active_provider, PROVIDER_HF)

    def test_new_fields_win_over_legacy(self):
        cfg = self._load({"api_key": "sk-newkey", "gemini_api_key": "AIzaOld"})
        self.assertEqual(cfg.api_key, "sk-newkey")
        self.assertEqual(cfg.active_provider, "openai")

    def test_save_writes_the_new_fields(self):
        cfg = self._load({"hf_token": "hf_legacytoken"})
        path = cfg.save()
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "hf_legacytoken")
        self.assertIn("base_url", saved)
        self.assertIn("model_name", saved)


class TestHFFill(unittest.TestCase):
    def setUp(self):
        self.service = AIService(make_config())

    def test_calls_image_to_image_with_the_configured_model(self):
        fake = FakeHFClient(output=Image.new("RGB", (300, 200)))
        install_fake(self.service, fake)
        result = self.service.generative_fill(
            Image.new("RGB", (300, 200)), "remove the text", max_retries=0
        )
        self.assertEqual(fake.calls[0]["model"], "black-forest-labs/FLUX.1-Kontext-dev")
        self.assertEqual(result.image.size, (300, 200))
        self.assertEqual(result.provider, PROVIDER_HF)

    def test_instruction_is_the_english_command(self):
        fake = FakeHFClient(output=Image.new("RGB", (10, 10)))
        install_fake(self.service, fake)
        self.service.generative_fill(Image.new("RGB", (10, 10)), "remove the text", max_retries=0)
        self.assertIn("remove the text", fake.calls[0]["prompt"])
        self.assertIn("blends in seamlessly", fake.calls[0]["prompt"])

    def test_persian_prompt_reaches_the_model_logically(self):
        fake = FakeHFClient(output=Image.new("RGB", (10, 10)))
        install_fake(self.service, fake)
        persian = "متن های داخلش رو پاک کن"
        self.service.generative_fill(Image.new("RGB", (10, 10)), persian, max_retries=0)
        self.assertIn(persian, fake.calls[0]["prompt"])

    def test_error_is_surfaced_with_the_server_text(self):
        fake = FakeHFClient(error=RuntimeError("402 Client Error: Payment Required"))
        install_fake(self.service, fake)
        with self.assertRaises(AIError) as ctx:
            self.service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertIn("Payment Required", str(ctx.exception))

    def test_no_token_raises_the_clear_message(self):
        service = AIService(make_config(api_key=""))
        with self.assertRaises(MissingAPIKeyError) as ctx:
            service.generative_fill(Image.new("RGB", (8, 8)), "x")
        self.assertEqual(str(ctx.exception), NO_KEY_MESSAGE)

    def test_available_reflects_the_token(self):
        self.assertTrue(self.service.available)
        self.assertFalse(AIService(make_config(api_key="")).available)


class TestDimensionMatching(unittest.TestCase):
    def setUp(self):
        self.service = AIService(make_config())

    def test_wrong_size_output_is_resized_to_the_crop(self):
        install_fake(self.service, FakeHFClient(output=Image.new("RGB", (1024, 1024))))
        result = self.service.generative_fill(
            Image.new("RGB", (300, 200)), "remove the text", max_retries=0
        )
        self.assertEqual(result.image.size, (300, 200))
        self.assertTrue(any("Resized" in n for n in result.notes))

    def test_tall_crop_is_preserved(self):
        install_fake(self.service, FakeHFClient(output=Image.new("RGB", (512, 512))))
        result = self.service.generative_fill(
            Image.new("RGB", (120, 400)), "remove the text", max_retries=0
        )
        self.assertEqual(result.image.size, (120, 400))

    def test_correct_size_output_is_untouched(self):
        install_fake(self.service, FakeHFClient(output=Image.new("RGB", (300, 200))))
        result = self.service.generative_fill(
            Image.new("RGB", (300, 200)), "remove the text", max_retries=0
        )
        self.assertFalse(any("Resized" in n for n in result.notes))


class TestHelpers(unittest.TestCase):
    def test_coerce_image_accepts_pil(self):
        image = Image.new("RGB", (4, 4))
        self.assertIs(ai_service._coerce_image(image), image)

    def test_coerce_image_accepts_bytes(self):
        data = png_bytes(Image.new("RGB", (6, 5)))
        self.assertEqual(ai_service._coerce_image(data).size, (6, 5))

    def test_coerce_image_accepts_a_list(self):
        data = png_bytes(Image.new("RGB", (6, 5)))
        self.assertEqual(ai_service._coerce_image([data]).size, (6, 5))

    def test_coerce_image_rejects_junk(self):
        with self.assertRaises(AIError):
            ai_service._coerce_image(b"not an image")

    def test_extract_chat_text_from_object(self):
        response = {"choices": [{"message": {"content": "hello"}}]}
        self.assertEqual(ai_service._extract_chat_text(response), "hello")

    def test_extract_chat_text_from_parts(self):
        response = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
        self.assertEqual(ai_service._extract_chat_text(response), "a b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
