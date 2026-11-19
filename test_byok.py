"""Universal BYOK engine tests.

No network: ``_http_json`` and ``_download`` are patched, and the two SDK
clients are replaced with fakes. What is checked:

* provider detection from every key prefix, every known URL, and an unknown
  relay URL;
* the OpenAI-shaped path: ``/images/edits`` first, then the multimodal chat
  route when the relay does not implement edits;
* OpenRouter's ``modalities`` payload and ``message.images[]`` reply shape;
* Gemini, Hugging Face, Replicate and fal adapters;
* the exact server error text reaching the caller, prefixed with provider and
  status;
* ``test_connection`` for success and for each failure shape;
* the patch being resized to the exact selection size.

The point of most of these is the contract the user asked for: never a silent
fallback to a different generator, always the server's own words.
"""

from __future__ import annotations

import base64
import io
import json
import unittest
import urllib.error
from unittest import mock

from PIL import Image

import ai_service
from ai_service import (
    AIError,
    AIService,
    MissingAPIKeyError,
    NO_KEY_MESSAGE,
    ProviderHTTPError,
    _clean_translation,
    _extract_chat_image,
    _multipart,
    _server_message,
    build_edit_instruction,
    build_fill_prompt,
)
from config import (
    Config,
    PROVIDER_CUSTOM,
    PROVIDER_FAL,
    PROVIDER_GEMINI,
    PROVIDER_HF,
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    PROVIDER_REPLICATE,
    default_base_url,
    default_model,
    detect_provider,
)

PERSIAN = "متن های داخلش رو پاک کن"
ENGLISH = "Remove all text and lettering cleanly."


# ------------------------------------------------------------------ fixtures
def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def b64_image(size=(64, 48)) -> str:
    return base64.b64encode(png_bytes(Image.new("RGB", size, (10, 120, 200)))).decode("ascii")


def _temp_config_path() -> str:
    """A throwaway config file, so saving never touches the real config.json."""
    import tempfile
    from pathlib import Path
    return str(Path(tempfile.mkdtemp()) / "config.json")


def make_config(**overrides) -> Config:
    base = dict(
        api_key="", base_url="", model_name="",
        temp_dir=r"E:\ai-scatch\.tmp",
        response_timeout=5.0,
        config_path=_temp_config_path(),
    )
    base.update(overrides)
    return Config(**base)


def http_error(status: int, body: dict | str) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode() if isinstance(body, dict) else str(body).encode()
    return urllib.error.HTTPError(
        "https://example.test/x", status, "err", {}, io.BytesIO(raw)
    )


def fake_urlopen(payload=None, *, status=None, body=None, calls=None):
    """A urlopen replacement returning JSON, or raising an HTTP error."""
    def _open(request, timeout=None):
        if calls is not None:
            calls.append(request)
        if status is not None:
            raise http_error(status, body if body is not None else {"error": {"message": "boom"}})
        raw = json.dumps(payload).encode()
        response = mock.MagicMock()
        response.read.return_value = raw
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        return response
    return _open


# ------------------------------------------------------------------ detection
class TestProviderDetection(unittest.TestCase):
    def test_key_prefixes(self):
        cases = {
            "AIzaSyABC": PROVIDER_GEMINI,
            "sk-or-v1-abc": PROVIDER_OPENROUTER,
            "sk-proj-abc": PROVIDER_OPENAI,
            "hf_abc": PROVIDER_HF,
            "r8_abc": PROVIDER_REPLICATE,
            "fal_key:secret": PROVIDER_FAL,
            "something-else": PROVIDER_CUSTOM,
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(detect_provider(key, ""), expected)

    def test_sk_or_is_not_read_as_openai(self):
        # The ordering bug this guards: "sk-" is a prefix of "sk-or-".
        self.assertEqual(detect_provider("sk-or-v1-x", ""), PROVIDER_OPENROUTER)
        self.assertEqual(detect_provider("sk-proj-x", ""), PROVIDER_OPENAI)

    def test_known_base_urls(self):
        cases = {
            "https://generativelanguage.googleapis.com/v1beta": PROVIDER_GEMINI,
            "https://api-inference.huggingface.co": PROVIDER_HF,
            "https://api.replicate.com/v1": PROVIDER_REPLICATE,
            "https://fal.run": PROVIDER_FAL,
            "https://fal.ai/api": PROVIDER_FAL,
            "https://openrouter.ai/api/v1": PROVIDER_OPENROUTER,
            "https://api.openai.com/v1": PROVIDER_OPENAI,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_provider("", url), expected)

    def test_unknown_url_is_custom(self):
        self.assertEqual(detect_provider("", "https://my-relay.example.com/v1"), PROVIDER_CUSTOM)
        self.assertEqual(detect_provider("", "http://localhost:1234/v1"), PROVIDER_CUSTOM)

    def test_explicit_url_beats_key_prefix(self):
        # The user typed the endpoint, so it wins over what the key looks like.
        self.assertEqual(
            detect_provider("sk-abc", "https://my-relay.example.com/v1"), PROVIDER_CUSTOM
        )

    def test_nothing_configured_is_custom(self):
        self.assertEqual(detect_provider("", ""), PROVIDER_CUSTOM)

    def test_defaults_per_provider(self):
        self.assertEqual(default_model(PROVIDER_GEMINI), "gemini-2.5-flash-image")
        self.assertEqual(default_model(PROVIDER_OPENAI), "gpt-image-1")
        self.assertEqual(default_model(PROVIDER_REPLICATE), "black-forest-labs/flux-kontext-pro")
        self.assertEqual(default_model(PROVIDER_FAL), "fal-ai/flux-pro/kontext")
        self.assertEqual(default_base_url(PROVIDER_OPENROUTER), "https://openrouter.ai/api/v1")

    def test_config_resolves_the_three_fields(self):
        cfg = make_config(api_key="sk-or-v1-abc")
        self.assertEqual(cfg.active_provider, PROVIDER_OPENROUTER)
        self.assertEqual(cfg.active_base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(cfg.active_model, "google/gemini-2.5-flash-image-preview")

    def test_explicit_model_and_url_win(self):
        cfg = make_config(api_key="sk-abc", base_url="https://relay.test/v1", model_name="my-model")
        self.assertEqual(cfg.active_base_url, "https://relay.test/v1")
        self.assertEqual(cfg.active_model, "my-model")


class TestNoKey(unittest.TestCase):
    def test_no_key_no_url_raises_the_exact_message(self):
        service = AIService(make_config())
        with self.assertRaises(MissingAPIKeyError) as ctx:
            service.generative_fill(Image.new("RGB", (8, 8)), "add a hat")
        self.assertEqual(str(ctx.exception), NO_KEY_MESSAGE)

    def test_no_key_is_not_available(self):
        self.assertFalse(AIService(make_config()).available)

    def test_endpoint_alone_is_available(self):
        # Local servers often need no key.
        self.assertTrue(AIService(make_config(base_url="http://localhost:1234/v1")).available)

    def test_there_is_no_keyless_fallback(self):
        # The old free Pollinations path must be gone.
        self.assertFalse(hasattr(ai_service, "_pollinations_fill"))
        self.assertFalse(hasattr(AIService(make_config()), "_pollinations_fill"))


# --------------------------------------------------------------- error text
class TestErrorSurfacing(unittest.TestCase):
    def test_server_message_shapes(self):
        cases = [
            ({"error": {"message": "model not found"}}, "model not found"),
            ({"error": "bad key"}, "bad key"),
            ({"detail": "not authenticated"}, "not authenticated"),
            ({"message": "quota exceeded"}, "quota exceeded"),
            ({"error": {"detail": "nested detail"}}, "nested detail"),
            ({"detail": [{"msg": "list form"}]}, "list form"),
            ("plain text", "plain text"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(_server_message(payload), expected)

    def test_http_error_carries_provider_and_status(self):
        exc = ProviderHTTPError("openai", 404, "no such route")
        self.assertEqual(str(exc), "[openai / HTTP 404] no such route")
        self.assertTrue(exc.route_missing)

    def test_401_is_not_a_missing_route(self):
        self.assertFalse(ProviderHTTPError("openai", 401, "bad key").route_missing)

    def test_raw_server_text_reaches_the_caller(self):
        cfg = make_config(api_key="sk-test", base_url="https://relay.test/v1", model_name="m")
        service = AIService(cfg)
        body = {"error": {"message": "Model 'm' does not exist on this relay"}}
        with mock.patch.object(
            ai_service.urllib.request, "urlopen", side_effect=fake_urlopen(status=500, body=body)
        ):
            with self.assertRaises(ProviderHTTPError) as ctx:
                service.generative_fill(Image.new("RGB", (8, 8)), "add a hat", max_retries=0)
        self.assertIn("Model 'm' does not exist on this relay", str(ctx.exception))
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_401_becomes_a_missing_key_error(self):
        cfg = make_config(api_key="sk-test", base_url="https://relay.test/v1", model_name="m")
        service = AIService(cfg)
        with mock.patch.object(
            ai_service.urllib.request, "urlopen",
            side_effect=fake_urlopen(status=401, body={"error": {"message": "invalid api key"}}),
        ):
            with self.assertRaises(MissingAPIKeyError) as ctx:
                service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertIn("invalid api key", str(ctx.exception))

    def test_non_json_200_is_reported(self):
        cfg = make_config(api_key="sk-test", base_url="https://relay.test/v1", model_name="m")
        service = AIService(cfg)
        response = mock.MagicMock()
        response.read.return_value = b"<html>gateway error</html>"
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        with mock.patch.object(ai_service.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(AIError) as ctx:
                service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertIn("did not return JSON", str(ctx.exception))


# ------------------------------------------------------- openai / relay path
class TestOpenAIPath(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(api_key="sk-test", base_url="https://relay.test/v1", model_name="gpt-image-1")
        self.service = AIService(self.cfg)

    def test_images_edits_success(self):
        payload = {"data": [{"b64_json": b64_image()}]}
        with mock.patch.object(ai_service, "_http_json", return_value=payload) as http:
            result = self.service.generative_fill(Image.new("RGB", (64, 48)), "remove the sign", max_retries=0)
        url = http.call_args.args[0]
        self.assertTrue(url.endswith("/images/edits"))
        self.assertEqual(result.image.size, (64, 48))
        # A relay URL is custom_openai_compatible by definition.
        self.assertEqual(result.provider, PROVIDER_CUSTOM)

    def test_official_openai_url_reports_openai(self):
        cfg = make_config(api_key="sk-test", base_url="https://api.openai.com/v1", model_name="gpt-image-1")
        service = AIService(cfg)
        payload = {"data": [{"b64_json": b64_image()}]}
        with mock.patch.object(ai_service, "_http_json", return_value=payload):
            result = service.generative_fill(Image.new("RGB", (16, 16)), "x", max_retries=0)
        self.assertEqual(result.provider, PROVIDER_OPENAI)

    def test_images_edits_sends_multipart_with_the_png(self):
        payload = {"data": [{"b64_json": b64_image()}]}
        with mock.patch.object(ai_service, "_http_json", return_value=payload) as http:
            self.service.generative_fill(Image.new("RGB", (20, 10)), "remove the sign", max_retries=0)
        body = http.call_args.kwargs["data"]
        headers = http.call_args.kwargs["headers"]
        self.assertIn(b'name="image"; filename="selection.png"', body)
        self.assertIn(b'name="prompt"', body)
        self.assertIn(b'name="model"', body)
        self.assertIn("multipart/form-data", headers["Content-Type"])
        self.assertEqual(headers["Authorization"], "Bearer sk-test")

    def test_404_on_edits_falls_through_to_chat(self):
        """The relay case: no /images/edits, image model via chat completions."""
        chat = {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + b64_image()}}]}}]}
        calls = []

        def http(url, **kwargs):
            calls.append(url)
            if url.endswith("/images/edits"):
                raise ProviderHTTPError("openai", 404, "not found")
            return chat

        with mock.patch.object(ai_service, "_http_json", side_effect=http):
            result = self.service.generative_fill(Image.new("RGB", (64, 48)), "remove the sign", max_retries=0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].endswith("/images/edits"))
        self.assertTrue(calls[1].endswith("/chat/completions"))
        self.assertEqual(result.image.size, (64, 48))
        self.assertTrue(any("chat/completions" in n for n in result.notes))

    def test_400_on_edits_also_falls_through(self):
        chat = {"choices": [{"message": {"content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64_image()}}]}}]}

        def http(url, **kwargs):
            if url.endswith("/images/edits"):
                raise ProviderHTTPError("openai", 400, "unsupported parameter")
            return chat

        with mock.patch.object(ai_service, "_http_json", side_effect=http):
            result = self.service.generative_fill(Image.new("RGB", (16, 16)), "x", max_retries=0)
        self.assertEqual(result.image.size, (16, 16))

    def test_500_on_edits_does_not_fall_through(self):
        """A server error is not a missing route: report it, do not retry elsewhere."""
        calls = []

        def http(url, **kwargs):
            calls.append(url)
            raise ProviderHTTPError("openai", 500, "upstream exploded")

        with mock.patch.object(ai_service, "_http_json", side_effect=http):
            with self.assertRaises(ProviderHTTPError) as ctx:
                self.service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertEqual(len(calls), 1)
        self.assertIn("upstream exploded", str(ctx.exception))

    def test_chat_payload_sends_a_data_uri(self):
        chat = {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + b64_image()}}]}}]}
        captured = {}

        def http(url, **kwargs):
            if url.endswith("/chat/completions"):
                captured["payload"] = json.loads(kwargs["data"].decode())
                return chat
            raise ProviderHTTPError("openai", 404, "nope")

        with mock.patch.object(ai_service, "_http_json", side_effect=http):
            self.service.generative_fill(Image.new("RGB", (8, 8)), "make it blue", max_retries=0)
        content = captured["payload"]["messages"][0]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn("make it blue", content[0]["text"])

    def test_no_key_sends_no_authorization_header(self):
        cfg = make_config(base_url="http://localhost:1234/v1", model_name="local")
        service = AIService(cfg)
        payload = {"data": [{"b64_json": b64_image()}]}
        with mock.patch.object(ai_service, "_http_json", return_value=payload) as http:
            service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertNotIn("Authorization", http.call_args.kwargs["headers"])


# ------------------------------------------------------------------ openrouter
class TestOpenRouter(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(api_key="sk-or-v1-abc")
        self.service = AIService(self.cfg)

    def test_sends_modalities_and_image_url(self):
        reply = {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + b64_image()}}]}}]}
        captured = {}

        def http(url, **kwargs):
            captured["url"] = url
            captured["payload"] = json.loads(kwargs["data"].decode())
            captured["headers"] = kwargs["headers"]
            return reply

        with mock.patch.object(ai_service, "_http_json", side_effect=http):
            result = self.service.generative_fill(Image.new("RGB", (32, 24)), "add a hat", max_retries=0)
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(captured["payload"]["modalities"], ["image", "text"])
        self.assertEqual(captured["payload"]["model"], "google/gemini-2.5-flash-image-preview")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-or-v1-abc")
        self.assertEqual(result.image.size, (32, 24))

    def test_openrouter_error_is_surfaced(self):
        with mock.patch.object(
            ai_service.urllib.request, "urlopen",
            side_effect=fake_urlopen(status=402, body={"error": {"message": "Insufficient credits"}}),
        ):
            with self.assertRaises(ProviderHTTPError) as ctx:
                self.service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertIn("Insufficient credits", str(ctx.exception))


# ---------------------------------------------------------------------- gemini
class FakeGeminiResponse:
    def __init__(self, data: bytes | None = None, text: str = "", finish: str = "STOP"):
        part = mock.MagicMock()
        part.inline_data = mock.MagicMock(data=data) if data else None
        part.text = text
        candidate = mock.MagicMock()
        candidate.content.parts = [part]
        candidate.finish_reason = finish
        self.candidates = [candidate]
        self.prompt_feedback = None


class TestGemini(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(api_key="AIzaSyTEST")
        self.service = AIService(self.cfg)

    def test_extracts_the_inline_image(self):
        response = FakeGeminiResponse(data=png_bytes(Image.new("RGB", (40, 30))))
        client = mock.MagicMock()
        client.models.generate_content.return_value = response
        with mock.patch.object(self.service, "_get_client", return_value=client):
            result = self.service.generative_fill(Image.new("RGB", (40, 30)), "remove the text", max_retries=0)
        self.assertEqual(result.image.size, (40, 30))
        self.assertEqual(result.provider, PROVIDER_GEMINI)
        call = client.models.generate_content.call_args
        self.assertEqual(call.kwargs["model"], "gemini-2.5-flash-image")

    def test_text_only_reply_is_an_error_not_an_image(self):
        response = FakeGeminiResponse(text="I cannot edit that image.")
        client = mock.MagicMock()
        client.models.generate_content.return_value = response
        with mock.patch.object(self.service, "_get_client", return_value=client):
            with self.assertRaises(AIError) as ctx:
                self.service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertIn("I cannot edit that image", str(ctx.exception))

    def test_safety_finish_reason_raises_safety(self):
        response = FakeGeminiResponse(data=None, finish="SAFETY")
        client = mock.MagicMock()
        client.models.generate_content.return_value = response
        with mock.patch.object(self.service, "_get_client", return_value=client):
            with self.assertRaises(ai_service.SafetyBlockedError):
                self.service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)


# --------------------------------------------------------------- huggingface
class FakeHFClient:
    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls = []

    def image_to_image(self, image, prompt=None, model=None, **kwargs):
        self.calls.append({"model": model, "prompt": prompt, "size": image.size})
        if self.error:
            raise self.error
        return self.output

    def whoami(self):
        return {"name": "tester"}


class TestHuggingFace(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(api_key="hf_test")
        self.service = AIService(self.cfg)

    def test_calls_image_to_image_with_the_default_model(self):
        fake = FakeHFClient(output=Image.new("RGB", (100, 80)))
        with mock.patch.object(self.service, "_get_hf_client", return_value=fake):
            result = self.service.generative_fill(Image.new("RGB", (50, 40)), "remove the text", max_retries=0)
        self.assertEqual(fake.calls[0]["model"], "black-forest-labs/FLUX.1-Kontext-dev")
        self.assertEqual(result.image.size, (50, 40))  # resized back

    def test_hf_error_is_surfaced(self):
        fake = FakeHFClient(error=RuntimeError("402 Client Error: Payment Required"))
        with mock.patch.object(self.service, "_get_hf_client", return_value=fake):
            with self.assertRaises(AIError) as ctx:
                self.service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertIn("Payment Required", str(ctx.exception))


# ------------------------------------------------------- replicate and fal
class TestReplicate(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(api_key="r8_test")
        self.service = AIService(self.cfg)

    def test_creates_then_polls_until_succeeded(self):
        created = {"status": "starting", "urls": {"get": "https://api.replicate.com/v1/predictions/1"}}
        done = {"status": "succeeded", "output": ["https://cdn.test/out.png"]}
        calls = []

        def http(url, **kwargs):
            calls.append(url)
            return created if len(calls) == 1 else done

        with mock.patch.object(ai_service, "_http_json", side_effect=http), \
             mock.patch.object(ai_service, "_download", return_value=png_bytes(Image.new("RGB", (64, 48)))), \
             mock.patch.object(ai_service.time, "sleep"):
            result = self.service.generative_fill(Image.new("RGB", (64, 48)), "remove the text", max_retries=0)
        self.assertTrue(calls[0].endswith("/models/black-forest-labs/flux-kontext-pro/predictions"))
        self.assertEqual(result.image.size, (64, 48))

    def test_failed_prediction_reports_the_error(self):
        created = {"status": "failed", "error": "NSFW content detected", "urls": {"get": "https://x/1"}}
        with mock.patch.object(ai_service, "_http_json", return_value=created):
            with self.assertRaises(AIError) as ctx:
                self.service.generative_fill(Image.new("RGB", (8, 8)), "x", max_retries=0)
        self.assertIn("NSFW content detected", str(ctx.exception))


class TestFal(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(api_key="fal_key:secret")
        self.service = AIService(self.cfg)

    def test_posts_to_the_model_route_with_key_auth(self):
        reply = {"images": [{"url": "https://cdn.test/out.png"}]}
        captured = {}

        def http(url, **kwargs):
            captured["url"] = url
            captured["payload"] = json.loads(kwargs["data"].decode())
            captured["headers"] = kwargs["headers"]
            return reply

        with mock.patch.object(ai_service, "_http_json", side_effect=http), \
             mock.patch.object(ai_service, "_download", return_value=png_bytes(Image.new("RGB", (30, 20)))):
            result = self.service.generative_fill(Image.new("RGB", (30, 20)), "remove the text", max_retries=0)
        self.assertEqual(captured["url"], "https://fal.run/fal-ai/flux-pro/kontext")
        self.assertEqual(captured["headers"]["Authorization"], "Key fal_key:secret")
        self.assertTrue(captured["payload"]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(result.image.size, (30, 20))


# ------------------------------------------------------------ reply parsing
class TestChatImageExtraction(unittest.TestCase):
    def test_openrouter_images_array(self):
        reply = {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + b64_image()}}]}}]}
        self.assertEqual(_extract_chat_image(reply, "openrouter").size, (64, 48))

    def test_content_parts_with_image_url(self):
        reply = {"choices": [{"message": {"content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64_image()}}]}}]}
        self.assertEqual(_extract_chat_image(reply, "custom").size, (64, 48))

    def test_content_part_with_b64_json(self):
        reply = {"choices": [{"message": {"content": [{"b64_json": b64_image()}]}}]}
        self.assertEqual(_extract_chat_image(reply, "custom").size, (64, 48))

    def test_plain_data_uri_content(self):
        reply = {"choices": [{"message": {"content": "data:image/png;base64," + b64_image()}}]}
        self.assertEqual(_extract_chat_image(reply, "custom").size, (64, 48))

    def test_text_only_reply_names_the_server_message(self):
        reply = {"choices": [{"message": {"content": "I can't help with that request."}}]}
        with self.assertRaises(AIError) as ctx:
            _extract_chat_image(reply, "custom")
        self.assertIn("I can't help with that request", str(ctx.exception))

    def test_no_choices_reports_the_error_field(self):
        with self.assertRaises(AIError) as ctx:
            _extract_chat_image({"error": {"message": "model overloaded"}}, "custom")
        self.assertIn("model overloaded", str(ctx.exception))


# ------------------------------------------------------------ test_connection
class TestConnectionProbe(unittest.TestCase):
    def test_no_config(self):
        ok, message = AIService(make_config()).test_connection()
        self.assertFalse(ok)
        self.assertEqual(message, NO_KEY_MESSAGE)

    def test_openai_style_success(self):
        service = AIService(make_config(api_key="sk-test", base_url="https://relay.test/v1", model_name="m"))
        with mock.patch.object(ai_service, "_http_json", return_value={"data": [{"id": "a"}, {"id": "b"}]}):
            ok, message = service.test_connection()
        self.assertTrue(ok)
        self.assertIn("2 models visible", message)
        self.assertIn("m", message)

    def test_openai_style_failure_keeps_the_server_text(self):
        service = AIService(make_config(api_key="sk-test", base_url="https://relay.test/v1", model_name="m"))
        with mock.patch.object(
            ai_service, "_http_json",
            side_effect=ProviderHTTPError("custom_openai_compatible", 401, "invalid key"),
        ):
            ok, message = service.test_connection()
        self.assertFalse(ok)
        self.assertIn("invalid key", message)
        self.assertIn("HTTP 401", message)

    def test_gemini_success(self):
        service = AIService(make_config(api_key="AIzaSyTEST"))
        model = mock.MagicMock()
        model.name = "models/gemini-2.5-flash-image"
        client = mock.MagicMock()
        client.models.list.return_value = [model]
        with mock.patch.object(service, "_get_client", return_value=client):
            ok, message = service.test_connection()
        self.assertTrue(ok)
        self.assertIn("gemini-2.5-flash-image", message)

    def test_gemini_missing_model_is_reported(self):
        service = AIService(make_config(api_key="AIzaSyTEST"))
        model = mock.MagicMock()
        model.name = "models/gemini-1.5-pro"
        client = mock.MagicMock()
        client.models.list.return_value = [model]
        with mock.patch.object(service, "_get_client", return_value=client):
            ok, message = service.test_connection()
        self.assertFalse(ok)
        self.assertIn("not available", message)

    def test_huggingface_success(self):
        service = AIService(make_config(api_key="hf_test"))
        with mock.patch.object(service, "_get_hf_client", return_value=FakeHFClient()):
            ok, message = service.test_connection()
        self.assertTrue(ok)
        self.assertIn("tester", message)

    def test_huggingface_failure(self):
        service = AIService(make_config(api_key="hf_test"))
        fake = FakeHFClient()
        fake.whoami = mock.MagicMock(side_effect=RuntimeError("401 Unauthorized"))
        with mock.patch.object(service, "_get_hf_client", return_value=fake):
            ok, message = service.test_connection()
        self.assertFalse(ok)
        self.assertIn("401", message)

    def test_probe_never_raises(self):
        service = AIService(make_config(api_key="sk-test", base_url="https://relay.test/v1"))
        with mock.patch.object(ai_service, "_http_json", side_effect=RuntimeError("socket closed")):
            ok, message = service.test_connection()
        self.assertFalse(ok)
        self.assertIn("socket closed", message)


# ------------------------------------------------------------------- prompts
class TestPrompts(unittest.TestCase):
    def test_selection_prompt_keeps_the_logical_persian(self):
        prompt = build_fill_prompt(PERSIAN, is_selection=True)
        self.assertIn(PERSIAN, prompt)
        self.assertIn("Persian", prompt)

    def test_selection_prompt_demands_matching_dimensions(self):
        prompt = build_fill_prompt("remove the sign", is_selection=True)
        self.assertIn("SAME pixel dimensions", prompt)
        self.assertIn("blends into the surrounding canvas", prompt)

    def test_full_canvas_prompt(self):
        prompt = build_fill_prompt("make it blue", is_selection=False)
        self.assertIn("make it blue", prompt)
        self.assertIn("same aspect ratio", prompt)

    def test_edit_instruction_is_short_and_imperative(self):
        instruction = build_edit_instruction("remove the sign", is_selection=True)
        self.assertTrue(instruction.startswith("remove the sign"))
        self.assertIn("blends in seamlessly", instruction)

    def test_translation_cleanup(self):
        cases = [
            ('  "English: Remove all text."  ', "Remove all text."),
            ("Translation: 'Erase the letters'", "Erase the letters."),
            ("Remove the text", "Remove the text."),
            ("\u201cClear the area\u201d", "Clear the area."),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_clean_translation(raw), expected)


# ------------------------------------------------------------------- resize
class TestResize(unittest.TestCase):
    def test_odd_model_output_is_resized_to_the_selection(self):
        cfg = make_config(api_key="sk-test", base_url="https://relay.test/v1", model_name="m")
        service = AIService(cfg)
        payload = {"data": [{"b64_json": b64_image((1024, 1024))}]}
        with mock.patch.object(ai_service, "_http_json", return_value=payload):
            result = service.generative_fill(Image.new("RGB", (300, 200)), "x", max_retries=0)
        self.assertEqual(result.image.size, (300, 200))
        self.assertTrue(any("Resized" in n for n in result.notes))

    def test_matching_size_is_left_alone(self):
        cfg = make_config(api_key="sk-test", base_url="https://relay.test/v1", model_name="m")
        service = AIService(cfg)
        payload = {"data": [{"b64_json": b64_image((300, 200))}]}
        with mock.patch.object(ai_service, "_http_json", return_value=payload):
            result = service.generative_fill(Image.new("RGB", (300, 200)), "x", max_retries=0)
        self.assertFalse(any("Resized" in n for n in result.notes))


class TestMultipart(unittest.TestCase):
    def test_body_has_fields_and_file(self):
        body, content_type = _multipart({"model": "m", "prompt": "p"}, "image", "selection.png", b"PNGDATA")
        self.assertIn(b'name="model"', body)
        self.assertIn(b"m", body)
        self.assertIn(b'filename="selection.png"', body)
        self.assertIn(b"PNGDATA", body)
        self.assertIn("boundary=", content_type)


if __name__ == "__main__":
    unittest.main(verbosity=2)
