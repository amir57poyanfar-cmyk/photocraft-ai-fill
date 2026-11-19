"""Universal BYOK image-editing engine.

One entry point, ``generative_fill(image, prompt, is_selection)``, that sends a
cropped selection to whichever provider the user's key and endpoint mean, and
returns the edited image or the server's own error text.

Design rules, in order of importance:

1. **No silent substitution.** If the configured provider fails, the failure is
   reported. Nothing quietly redirects to a different generator, because a
   plausible-looking image from the wrong model is worse than an error.
2. **The server's words, not ours.** Every HTTP failure is surfaced as
   ``[Provider / HTTP 404] <exact server message>`` so a wrong model name or a
   dead relay is diagnosable from the bar without opening a log.
3. **The patch fits.** Whatever comes back is resized to the exact selection
   size, so it drops into PhotoCraft pixel-for-pixel.

Adapters
--------
``gemini``          official SDK, ``generateContent`` with the image inline
``openai``          ``/images/edits`` multipart, then ``/chat/completions``
``custom_openai_compatible``  same two attempts; the relay case
``openrouter``      ``/chat/completions`` with ``modalities: [image, text]``
``huggingface``     ``InferenceClient.image_to_image``
``replicate``       predictions API with polling
``fal``             ``fal.run/<model>`` synchronous call

The OpenAI-shaped providers try ``/images/edits`` first because that is the
documented image-editing route. Relays and aggregators frequently do not
implement it and expose image models through chat completions instead, so a
404/405/400 there falls through to the multimodal chat call rather than failing.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from config import (
    PROVIDER_CUSTOM,
    PROVIDER_FAL,
    PROVIDER_GEMINI,
    PROVIDER_HF,
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    PROVIDER_REPLICATE,
    Config,
    get_config,
)
from rtl import has_rtl as _has_rtl

log = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types as genai_types
except Exception as _import_error:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    _GENAI_IMPORT_ERROR: Exception | None = _import_error
else:
    _GENAI_IMPORT_ERROR = None

try:
    from huggingface_hub import InferenceClient
except Exception as _hf_import_error:  # pragma: no cover
    InferenceClient = None  # type: ignore[assignment]
    _HF_IMPORT_ERROR: Exception | None = _hf_import_error
else:
    _HF_IMPORT_ERROR = None


#: Sent on every outbound request. Some relays reject the default Python UA.
USER_AGENT = "PhotoCraftGenerativeFill/1.0 (+desktop companion)"

RETRYABLE_MARKERS = (
    "429", "500", "502", "503", "504", "deadline", "timeout",
    "temporarily", "unavailable", "overloaded", "resource_exhausted", "internal",
)

#: Statuses that mean "this route does not exist here, try the other one".
ROUTE_MISSING_STATUSES = (400, 404, 405, 415, 422, 501)

#: Instruction used to turn a Persian prompt into an English editing command.
TRANSLATION_SYSTEM_PROMPT = (
    "You translate photo-editing instructions into English for an image model.\n"
    "Reply with the English instruction only: one sentence, no preamble, no quotes, "
    "no explanation.\n"
    "Keep it a concrete visual editing command. Name what to remove, add or change, "
    "and how the result should blend with its surroundings.\n"
    "Never answer the instruction, never describe the reply, never add commentary."
)

NO_KEY_MESSAGE = "No API key configured. Open Settings (⚙) and enter any API key or endpoint."


class AIError(RuntimeError):
    """Any failure from the generation service."""


class MissingAPIKeyError(AIError):
    """No API key configured."""


class SafetyBlockedError(AIError):
    """The provider refused on safety grounds."""


class ProviderHTTPError(AIError):
    """A provider answered with an HTTP error.

    Carries the status and the server's own message separately so callers can
    show ``[Provider / HTTP n] message`` without re-parsing a string.
    """

    def __init__(self, provider: str, status: int, message: str, url: str = "") -> None:
        self.provider = provider
        self.status = int(status)
        self.server_message = (message or "").strip()
        self.url = url
        super().__init__(f"[{provider} / HTTP {status}] {self.server_message or 'no message'}")

    @property
    def route_missing(self) -> bool:
        return self.status in ROUTE_MISSING_STATUSES


@dataclass
class FillResult:
    image: Image.Image
    model: str
    prompt: str
    used_reference: bool = True
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    provider: str = ""
    #: The English instruction actually sent, when it differs from ``prompt``.
    translated_prompt: str = ""
    #: Set instead of raising when the caller wants the error on the bar.
    error: str = ""


#: Backwards-compatible alias.
AIResult = FillResult


# --------------------------------------------------------------------- prompts
def build_fill_prompt(prompt: str, *, is_selection: bool, context_note: str = "") -> str:
    """Compose the instruction sent alongside the image.

    ``prompt`` must be the **logical** text the user typed. Persian is passed
    through unchanged: reversing it for display would hand the model mirrored
    text. A Persian instruction is answered in kind, so the reply language is
    pinned to the instruction's own language.
    """
    prompt = prompt.strip()
    persian = _has_rtl(prompt)
    language_rule = (
        "- The instruction is written in Persian. Interpret it as Persian and act on "
        "its meaning; do not transliterate it or treat it as an image description.\n"
        if persian
        else ""
    )
    if is_selection:
        core = (
            "You are an inpainting engine inside a desktop photo editor. The image you "
            "receive is a crop of a selected region of a larger canvas.\n\n"
            f"Instruction: {prompt}\n\n"
            "Rules:\n"
            "- Return the SAME framing and the SAME pixel dimensions as the input crop.\n"
            "- Fill the whole crop; do not letterbox, pad, crop further, or change aspect ratio.\n"
            "- Match the surrounding lighting, colour temperature, grain, perspective and "
            "focus so the result blends into the surrounding canvas without a visible seam.\n"
            "- Where the instruction replaces something, remove the original completely "
            "rather than layering over it.\n"
            "- No text, captions, watermarks, borders or UI chrome unless explicitly asked for.\n"
            f"{language_rule}"
            "- The output is an image. Do not render the instruction text into the image."
        )
    else:
        core = (
            "You are an image editing engine inside a desktop photo editor.\n\n"
            f"Instruction: {prompt}\n\n"
            "Rules:\n"
            "- Return the full edited image at the same aspect ratio and dimensions as the input.\n"
            "- Keep everything the instruction did not mention untouched.\n"
            "- No text, captions, watermarks or borders unless explicitly asked for.\n"
            f"{language_rule}"
            "- The output is an image. Do not render the instruction text into the image."
        )
    if context_note:
        core += f"\n- {context_note}"
    return core


def build_edit_instruction(prompt: str, *, is_selection: bool) -> str:
    """The short imperative handed to chat-style and open-weights editors.

    Long system-style prompts make these models restyle the whole crop, so this
    stays one clear command with the constraint that actually matters.
    """
    prompt = prompt.strip()
    if is_selection:
        return (
            f"{prompt}. Edit only this region, keep the same framing and composition, "
            "and match the surrounding colours, lighting and texture so the result "
            "blends in seamlessly. Do not add text, borders or watermarks."
        )
    return (
        f"{prompt}. Keep the same framing and composition, and leave everything not "
        "mentioned unchanged. Do not add text, borders or watermarks."
    )


#: Kept for callers written against the previous name.
build_hf_edit_prompt = build_edit_instruction


# ---------------------------------------------------------------------- engine
class AIService:
    """Sends a selection to the configured provider and returns the edit."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self._client: Any = None
        self._client_key: str | None = None
        self._hf_client: Any = None
        self._hf_key: str | None = None

    # ------------------------------------------------------------- plumbing
    @property
    def provider(self) -> str:
        return self.config.active_provider

    @property
    def available(self) -> bool:
        """True when there is something to call: a key, an endpoint, or both.

        A bare endpoint is enough because local servers (LM Studio, Ollama, a
        self-hosted gateway) often need no key at all.
        """
        return bool(self.config.api_key or self.config.base_url)

    def reset(self) -> None:
        self._client = None
        self._client_key = None
        self._hf_client = None
        self._hf_key = None

    def _require_key(self) -> str:
        key = self.config.api_key
        if not key and not self.config.base_url:
            raise MissingAPIKeyError(NO_KEY_MESSAGE)
        return key

    def _auth_headers(self, override: str | None = None) -> dict[str, str]:
        """Authorization header, omitted entirely when there is no key."""
        if override:
            return {"Authorization": override}
        key = self.config.api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _bearer(self) -> str:
        """``Bearer <key>`` for probes, or empty when there is no key."""
        key = self.config.api_key
        return f"Bearer {key}" if key else ""

    def _fal_auth(self) -> str:
        key = self.config.api_key
        return f"Key {key}" if key else ""

    def _get_client(self) -> Any:
        """The Gemini client."""
        key = self._require_key()
        if genai is None:
            raise AIError(
                "The google-genai package is not installed. Run: pip install -r requirements.txt"
                + (f" ({_GENAI_IMPORT_ERROR})" if _GENAI_IMPORT_ERROR else "")
            )
        if self._client is None or self._client_key != key:
            self._client = genai.Client(api_key=key)
            self._client_key = key
        return self._client

    def _get_hf_client(self) -> Any:
        """The Hugging Face client. One instance serves every model."""
        key = self._require_key()
        if InferenceClient is None:
            raise AIError(
                "The huggingface_hub package is not installed. "
                "Run: pip install -r requirements.txt"
                + (f" ({_HF_IMPORT_ERROR})" if _HF_IMPORT_ERROR else "")
            )
        if self._hf_client is None or self._hf_key != key:
            self._hf_client = InferenceClient(token=key)
            self._hf_key = key
        return self._hf_client

    # ------------------------------------------------------------ public API
    def generative_fill(
        self,
        image: Image.Image,
        prompt: str,
        is_selection: bool = True,
        *,
        context_image: Image.Image | None = None,
        max_retries: int = 2,
    ) -> FillResult:
        """Edit ``image`` per ``prompt`` and return the new image.

        Raises ``MissingAPIKeyError`` with no key, and ``ProviderHTTPError``
        carrying the server's message on a failed call. There is no fallback to
        a different provider: a wrong-provider image would be a silent lie.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise AIError("Prompt is empty.")
        if image is None:
            raise AIError("No source image supplied.")

        self._require_key()
        started = time.monotonic()
        provider = self.provider

        if provider == PROVIDER_GEMINI:
            result = self._gemini_fill(
                image, prompt, is_selection=is_selection,
                context_image=context_image, max_retries=max_retries,
            )
        elif provider == PROVIDER_HF:
            result = self._hf_fill(image, prompt, is_selection=is_selection, max_retries=max_retries)
        elif provider == PROVIDER_REPLICATE:
            result = self._replicate_fill(image, prompt, is_selection=is_selection, max_retries=max_retries)
        elif provider == PROVIDER_FAL:
            result = self._fal_fill(image, prompt, is_selection=is_selection, max_retries=max_retries)
        elif provider == PROVIDER_OPENROUTER:
            result = self._openrouter_fill(image, prompt, is_selection=is_selection, max_retries=max_retries)
        else:  # openai and custom_openai_compatible
            result = self._openai_fill(image, prompt, is_selection=is_selection, max_retries=max_retries)

        # Pixel-exact alignment: the patch is pasted at the selection origin, so
        # a model returning 1024x1024 for a 300x200 crop must be resized back.
        if result.image.size != image.size:
            result.notes.append(
                f"Resized {result.image.width}x{result.image.height} -> "
                f"{image.width}x{image.height}"
            )
            result.image = result.image.resize(image.size, Image.LANCZOS)

        result.elapsed_s = time.monotonic() - started
        result.provider = provider
        return result

    # --------------------------------------------------------------- gemini
    def _gemini_fill(
        self,
        image: Image.Image,
        prompt: str,
        *,
        is_selection: bool,
        context_image: Image.Image | None,
        max_retries: int,
    ) -> FillResult:
        context_note = ""
        if is_selection and context_image is not None:
            context_note = (
                "A second image is the full canvas for context only; use it to judge "
                "surrounding colour and lighting, but return only the crop."
            )
        instruction = build_fill_prompt(prompt, is_selection=is_selection, context_note=context_note)
        client = self._get_client()
        model = self.config.active_model

        contents: list[Any] = [instruction, image]
        if context_image is not None:
            contents.append(context_image)

        def call() -> Any:
            return client.models.generate_content(
                model=model, contents=contents, config=self._build_gemini_config()
            )

        response = self._with_retries(call, max_retries=max_retries, label=f"gemini {model}")
        return FillResult(
            image=self._extract_gemini_image(response),
            model=model,
            prompt=prompt,
            used_reference=True,
        )

    def _build_gemini_config(self) -> Any:
        if genai_types is None:
            return None
        for kwargs in (
            {"response_modalities": ["TEXT", "IMAGE"]},
            {"response_modalities": ["IMAGE"]},
            {},
        ):
            try:
                return genai_types.GenerateContentConfig(**kwargs)
            except (TypeError, ValueError):
                continue
        return None

    def _extract_gemini_image(self, response: Any) -> Image.Image:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            blocked = getattr(response, "prompt_feedback", None)
            if blocked is not None:
                raise SafetyBlockedError(f"Request was blocked: {blocked}")
            raise AIError("Gemini returned no candidates.")

        candidate = candidates[0]
        finish_reason = str(getattr(candidate, "finish_reason", "") or "")
        if "SAFETY" in finish_reason.upper() or "PROHIBITED" in finish_reason.upper():
            raise SafetyBlockedError(f"Gemini blocked the result ({finish_reason}).")

        content = getattr(candidate, "content", None)
        parts = list(getattr(content, "parts", None) or []) if content is not None else []
        if not parts:
            parts = list(getattr(response, "parts", None) or [])

        text_bits: list[str] = []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None:
                data = getattr(inline, "data", None)
                if data:
                    return _bytes_to_image(data)
                if isinstance(inline, dict) and inline.get("data"):
                    return _bytes_to_image(inline["data"])
            text = getattr(part, "text", None)
            if text:
                text_bits.append(str(text))

        detail = " ".join(t.strip() for t in text_bits if t and t.strip())
        if detail:
            raise AIError(f"Gemini replied without an image: {detail[:400]}")
        raise AIError("Gemini returned no image data.")

    # ----------------------------------------------------- openai / relays
    def _openai_fill(
        self,
        image: Image.Image,
        prompt: str,
        *,
        is_selection: bool,
        max_retries: int,
    ) -> FillResult:
        """``/images/edits`` first, then the multimodal chat route.

        Relays and aggregators often expose image models only through
        ``/chat/completions``. A missing route there is expected, not an error,
        so it falls through instead of surfacing a 404 the user cannot act on.
        """
        base = self._base_url()
        model = self.config.active_model
        instruction = build_edit_instruction(prompt, is_selection=is_selection)
        notes: list[str] = []

        try:
            data = self._openai_images_edit(base, model, image, instruction, max_retries=max_retries)
            return FillResult(
                image=_bytes_to_image(data), model=model, prompt=prompt,
                used_reference=True, notes=notes,
            )
        except ProviderHTTPError as exc:
            if not exc.route_missing:
                raise
            notes.append(f"/images/edits unavailable ({exc.status}); used /chat/completions")
            log.info("images/edits returned %s; trying chat completions", exc.status)
        except AIError as exc:
            # A 200 with an unreadable body is also worth one chat attempt.
            notes.append(f"/images/edits returned no image ({exc}); used /chat/completions")
            log.info("images/edits unusable (%s); trying chat completions", exc)

        payload = self._chat_payload(model, image, instruction, modalities=False)
        response = self._post_json(
            f"{base}/chat/completions", payload, max_retries=max_retries, label="chat/completions"
        )
        return FillResult(
            image=_extract_chat_image(response, self.provider),
            model=model, prompt=prompt, used_reference=True, notes=notes,
        )

    def _openai_images_edit(
        self,
        base: str,
        model: str,
        image: Image.Image,
        instruction: str,
        *,
        max_retries: int,
    ) -> bytes:
        fields = {
            "model": model,
            "prompt": instruction,
            "n": "1",
            "size": f"{image.width}x{image.height}",
            "response_format": "b64_json",
        }
        body, content_type = _multipart(fields, "image", "selection.png", _png_bytes(image))

        def call() -> Any:
            return _http_json(
                f"{base}/images/edits", data=body, method="POST",
                headers={"Content-Type": content_type, **self._auth_headers()},
                timeout=self.config.response_timeout, provider=self.provider,
            )

        response = self._with_retries(call, max_retries=max_retries, label="images/edits")
        return _extract_images_edit_payload(response, self.provider)

    def _chat_payload(
        self, model: str, image: Image.Image, instruction: str, *, modalities: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {
                            "type": "image_url",
                            "image_url": {"url": _data_uri(image)},
                        },
                    ],
                }
            ],
        }
        if modalities:
            payload["modalities"] = ["image", "text"]
        return payload

    # ---------------------------------------------------------- openrouter
    def _openrouter_fill(
        self,
        image: Image.Image,
        prompt: str,
        *,
        is_selection: bool,
        max_retries: int,
    ) -> FillResult:
        base = self._base_url()
        model = self.config.active_model
        instruction = build_edit_instruction(prompt, is_selection=is_selection)
        payload = self._chat_payload(model, image, instruction, modalities=True)
        response = self._post_json(
            f"{base}/chat/completions", payload, max_retries=max_retries, label="openrouter chat"
        )
        return FillResult(
            image=_extract_chat_image(response, self.provider),
            model=model, prompt=prompt, used_reference=True,
        )

    # -------------------------------------------------------- hugging face
    def _hf_fill(
        self,
        image: Image.Image,
        prompt: str,
        *,
        is_selection: bool,
        max_retries: int,
    ) -> FillResult:
        client = self._get_hf_client()
        model = self.config.active_model
        instruction = build_edit_instruction(prompt, is_selection=is_selection)

        def call() -> Any:
            return client.image_to_image(image, prompt=instruction, model=model)

        output = self._with_retries(call, max_retries=max_retries, label=f"huggingface {model}")
        return FillResult(
            image=_coerce_image(output), model=model, prompt=prompt, used_reference=True
        )

    # ----------------------------------------------------------- replicate
    def _replicate_fill(
        self,
        image: Image.Image,
        prompt: str,
        *,
        is_selection: bool,
        max_retries: int,
    ) -> FillResult:
        """Create a prediction, then poll until it finishes.

        Replicate's model endpoint accepts ``{version}`` or ``{owner}/{name}``;
        the latter is what the model id looks like, so it is used directly.
        """
        base = self._base_url()
        model = self.config.active_model
        instruction = build_edit_instruction(prompt, is_selection=is_selection)
        payload = {
            "input": {
                "prompt": instruction,
                "input_image": _data_uri(image),
                "aspect_ratio": "match_input_image",
            }
        }

        created = self._post_json(
            f"{base}/models/{model}/predictions", payload,
            max_retries=max_retries, label="replicate create",
        )
        url = _dig(created, "urls", "get") or _dig(created, "urls", "poll")
        if not url:
            raise AIError(f"Replicate returned no polling URL: {json.dumps(created)[:300]}")

        deadline = time.monotonic() + self.config.response_timeout
        while time.monotonic() < deadline:
            status = str(_dig(created, "status") or "").lower()
            if status in ("succeeded", "successful"):
                return FillResult(
                    image=_image_from_url_or_b64(_first_output(created), self.provider),
                    model=model, prompt=prompt, used_reference=True,
                )
            if status in ("failed", "canceled", "cancelled"):
                detail = _dig(created, "error") or status
                raise AIError(f"[replicate] prediction {status}: {detail}")
            time.sleep(2)
            created = _http_json(
                url, method="GET",
                headers=self._auth_headers(),
                timeout=60, provider=self.provider,
            )

        raise AIError("[replicate] timed out waiting for the prediction to finish")

    # ----------------------------------------------------------------- fal
    def _fal_fill(
        self,
        image: Image.Image,
        prompt: str,
        *,
        is_selection: bool,
        max_retries: int,
    ) -> FillResult:
        base = self._base_url().rstrip("/")
        model = self.config.active_model
        instruction = build_edit_instruction(prompt, is_selection=is_selection)
        payload = {"prompt": instruction, "image_url": _data_uri(image)}

        # fal accepts either "Key <key>" or a bearer token depending on surface.
        response = self._post_json(
            f"{base}/{model.lstrip('/')}", payload,
            max_retries=max_retries, label="fal run",
            auth_header=self._fal_auth(),
        )
        return FillResult(
            image=_image_from_url_or_b64(_first_output(response), self.provider),
            model=model, prompt=prompt, used_reference=True,
        )

    # ------------------------------------------------------------- helpers
    def _base_url(self) -> str:
        base = self.config.active_base_url
        if not base:
            raise AIError(
                f"No endpoint configured for {self.provider_label}. "
                "Enter a Base URL in Settings (⚙)."
            )
        return base.rstrip("/")

    @property
    def provider_label(self) -> str:
        return self.config.provider_label

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        max_retries: int,
        label: str,
        auth_header: str | None = None,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", **self._auth_headers(auth_header)}
        if self.provider == PROVIDER_OPENROUTER:
            # OpenRouter attributes traffic with these; harmless elsewhere.
            headers["HTTP-Referer"] = "https://perchai.app"
            headers["X-Title"] = "PhotoCraft Generative Fill"

        def call() -> Any:
            return _http_json(
                url, data=body, method="POST", headers=headers,
                timeout=self.config.response_timeout, provider=self.provider,
            )

        return self._with_retries(call, max_retries=max_retries, label=label)

    # --------------------------------------------------------------- retries
    def _with_retries(self, call: Any, *, max_retries: int, label: str) -> Any:
        """Run ``call``, retrying only genuinely transient failures.

        A provider HTTP error is re-raised as-is: the server already said what
        went wrong, and retrying a 401 or a 404 just delays the message.
        """
        attempt = 0
        last: Exception | None = None
        while attempt <= max_retries:
            attempt += 1
            try:
                return call()
            except ProviderHTTPError as exc:
                if exc.status in (401, 403):
                    raise MissingAPIKeyError(
                        f"[{exc.provider} / HTTP {exc.status}] {exc.server_message}"
                    ) from exc
                if not _is_retryable(str(exc.status)) or attempt > max_retries:
                    raise
                last = exc
            except Exception as exc:  # SDKs raise a wide range of types
                last = exc
                message = str(exc)
                if _is_auth_error(message):
                    raise MissingAPIKeyError(f"{label} rejected the API key: {message[:300]}") from exc
                if _is_safety_error(message):
                    raise SafetyBlockedError(f"{label} was blocked: {message[:300]}") from exc
                if attempt > max_retries or not _is_retryable(message):
                    raise AIError(f"{label} failed: {message[:400]}") from exc

            delay = min(2 ** attempt, 8) + random.uniform(0, 0.4)
            log.warning("%s attempt %s failed (%s); retrying in %.1fs", label, attempt, last, delay)
            time.sleep(delay)
        raise AIError(f"{label} failed: {last}")

    # ------------------------------------------------------ connection probe
    def test_connection(self) -> tuple[bool, str]:
        """Check the key and endpoint without spending an image generation.

        Returns ``(ok, message)``. The message names the provider and, on
        failure, repeats the server's own words so the user can fix the key,
        URL or model from Settings.
        """
        provider = self.provider
        label = self.provider_label
        if not self.config.api_key and not self.config.base_url:
            return False, NO_KEY_MESSAGE
        if not self.config.api_key and provider in (PROVIDER_GEMINI, PROVIDER_HF):
            return False, NO_KEY_MESSAGE

        try:
            if provider == PROVIDER_GEMINI:
                return self._probe_gemini()
            if provider == PROVIDER_HF:
                return self._probe_huggingface()
            if provider == PROVIDER_REPLICATE:
                return self._probe_http(f"{self._base_url()}/models/{self.config.active_model}",
                                        self._bearer())
            if provider == PROVIDER_FAL:
                # fal has no cheap listing route; a HEAD on the model is enough.
                return self._probe_http(f"{self._base_url()}/{self.config.active_model.lstrip('/')}",
                                        self._fal_auth(), method="GET", allow_missing=True)
            return self._probe_http(f"{self._base_url()}/models", self._bearer())
        except MissingAPIKeyError as exc:
            return False, str(exc)
        except ProviderHTTPError as exc:
            return False, str(exc)
        except AIError as exc:
            return False, f"[{label}] {exc}"
        except Exception as exc:  # noqa: BLE001 - a probe must never crash the dialog
            return False, f"[{label}] {type(exc).__name__}: {exc}"

    def _probe_gemini(self) -> tuple[bool, str]:
        if genai is None:
            return False, "The google-genai package is not installed."
        client = self._get_client()
        try:
            models = list(client.models.list())
        except Exception as exc:  # noqa: BLE001
            return False, f"[gemini] {_server_message(str(exc))}"
        wanted = self.config.active_model
        names = {getattr(m, "name", "") for m in models}
        short = {n.split("/")[-1] for n in names}
        if short and wanted not in short:
            return False, (
                f"[gemini] Key works, but model '{wanted}' is not available to it. "
                f"Try one of: {', '.join(sorted(short)[:4])}"
            )
        return True, f"Gemini key works ({len(models)} models visible). Model: {wanted}"

    def _probe_huggingface(self) -> tuple[bool, str]:
        if InferenceClient is None:
            return False, "The huggingface_hub package is not installed."
        client = self._get_hf_client()
        try:
            who = client.whoami()
        except Exception as exc:  # noqa: BLE001
            return False, f"[huggingface] {_server_message(str(exc))}"
        name = ""
        if isinstance(who, dict):
            name = who.get("name") or who.get("user") or ""
        return True, f"Hugging Face token works{' as ' + name if name else ''}. Model: {self.config.active_model}"

    def _probe_http(
        self, url: str, auth: str, *, method: str = "GET", allow_missing: bool = False
    ) -> tuple[bool, str]:
        try:
            body = _http_json(
                url, method=method, headers={"Authorization": auth},
                timeout=30, provider=self.provider,
            )
        except ProviderHTTPError as exc:
            if allow_missing and exc.route_missing:
                return True, (
                    f"{self.provider_label} key accepted (no listing route to confirm, "
                    f"HTTP {exc.status} on probe). Model: {self.config.active_model}"
                )
            return False, str(exc)

        count = ""
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list):
                count = f" ({len(data)} models visible)"
        return True, f"{self.provider_label} key works{count}. Model: {self.config.active_model}"


# -------------------------------------------------------------------- helpers
def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    (image if image.mode in ("RGB", "RGBA") else image.convert("RGB")).save(buffer, "PNG")
    return buffer.getvalue()


def _data_uri(image: Image.Image) -> str:
    return "data:image/png;base64," + base64.b64encode(_png_bytes(image)).decode("ascii")


def _multipart(
    fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes
) -> tuple[bytes, str]:
    """Build a multipart/form-data body and its Content-Type."""
    boundary = "----PhotoCraftFill" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8")
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _http_json(
    url: str,
    *,
    data: bytes | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 60,
    provider: str = "",
) -> Any:
    """Make a request and parse the JSON reply, surfacing the server's error."""
    request = urllib.request.Request(
        url, data=data, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ProviderHTTPError(provider or _host(url), exc.code, _read_error_body(exc), url) from exc
    except urllib.error.URLError as exc:
        raise AIError(f"Cannot reach {_host(url)}: {exc.reason}") from exc
    except Exception as exc:
        raise AIError(f"Request to {_host(url)} failed: {exc}") from exc

    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        # Some relays answer 200 with an HTML error page.
        snippet = raw[:300].decode("utf-8", "replace")
        raise AIError(f"{_host(url)} did not return JSON: {snippet}") from None


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    """Pull the most useful message out of an error response."""
    try:
        raw = exc.read()
    except Exception:  # pragma: no cover
        return str(exc.reason)
    text = raw[:2000].decode("utf-8", "replace").strip()
    if not text:
        return str(exc.reason)
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:400]
    return _server_message(parsed) or text[:400]


def _server_message(payload: Any) -> str:
    """Find the human-readable message in a provider's error JSON.

    Every provider nests it differently: ``{"error": {"message": ...}}``,
    ``{"error": "..."}``, ``{"detail": "..."}``, ``{"message": ...}``.
    """
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""

    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "error", "msg"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()

    for key in ("detail", "message", "msg", "error_description", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                nested = _server_message(first)
                if nested:
                    return nested
            if isinstance(first, str):
                return first.strip()
    return ""


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc or url
    except Exception:  # pragma: no cover
        return url


def _dig(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if current is None:
            return None
    return current


def _first_output(payload: Any) -> Any:
    """The first image reference in a provider's output payload."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        # A leaf image reference: a URL or inline base64.
        for key in ("url", "b64_json", "b64", "image_url", "image_base64"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return payload if key != "url" else value
            if isinstance(value, dict) and isinstance(value.get("url"), str):
                return value
        for key in ("output", "images", "image", "data", "result"):
            if key in payload:
                found = _first_output(payload[key])
                if found:
                    return found
        return None
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = _first_output(item)
            if found:
                return found
    return None


def _extract_images_edit_payload(response: Any, provider: str) -> bytes:
    """Read ``/images/edits`` output: b64 JSON or a URL."""
    items = _dig(response, "data") if isinstance(response, dict) else None
    if not isinstance(items, list) or not items:
        raise AIError(f"[{provider}] /images/edits returned no image data: {json.dumps(response)[:300]}")
    first = items[0]
    if isinstance(first, dict):
        b64 = first.get("b64_json") or first.get("b64") or first.get("image_base64")
        if b64:
            return _b64_to_bytes(b64)
        url = first.get("url")
        if url:
            return _download(url, provider)
    if isinstance(first, str):
        return _b64_to_bytes(first) if not first.startswith("http") else _download(first, provider)
    raise AIError(f"[{provider}] /images/edits returned an unusable item: {str(first)[:200]}")


def _extract_chat_image(response: Any, provider: str) -> Image.Image:
    """Find an image in a chat completion, however the relay shaped it.

    Handles ``message.images[]`` (OpenRouter), ``content`` parts with
    ``image_url``, inline base64 data URIs, and a plain URL string.
    """
    choices = _dig(response, "choices")
    if not isinstance(choices, list) or not choices:
        message = _server_message(response)
        raise AIError(
            f"[{provider}] response contained no choices"
            + (f": {message}" if message else f": {json.dumps(response)[:300]}")
        )

    message = _dig(choices[0], "message") or {}
    candidates: list[Any] = []

    images = message.get("images") if isinstance(message, dict) else None
    if isinstance(images, list):
        candidates.extend(images)

    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("image_url", "output_image", "image"):
                    candidates.append(part.get("image_url") or part.get("image") or part)
                elif "b64_json" in part:
                    candidates.append(part)
            elif isinstance(part, str) and part.startswith("data:image"):
                candidates.append(part)
    elif isinstance(content, str) and content.startswith("data:image"):
        candidates.append(content)

    for candidate in candidates:
        try:
            return _image_from_candidate(candidate, provider)
        except AIError:
            continue

    text = content if isinstance(content, str) else ""
    raise AIError(
        f"[{provider}] the reply had no image"
        + (f". Server said: {text[:300]}" if text.strip() else "")
    )


def _image_from_candidate(candidate: Any, provider: str) -> Image.Image:
    if isinstance(candidate, str):
        if candidate.startswith("data:image"):
            return _bytes_to_image(_b64_to_bytes(candidate.split(",", 1)[-1]))
        if candidate.startswith("http"):
            return _bytes_to_image(_download(candidate, provider))
        return _bytes_to_image(_b64_to_bytes(candidate))
    if isinstance(candidate, dict):
        url = candidate.get("url")
        if isinstance(url, dict):
            url = url.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return _bytes_to_image(_download(url, provider))
        if isinstance(url, str) and url.startswith("data:image"):
            return _bytes_to_image(_b64_to_bytes(url.split(",", 1)[-1]))
        # OpenRouter wraps it: {"image_url": {"url": "data:image/png;base64,..."}}
        nested = candidate.get("image_url")
        if isinstance(nested, dict) and isinstance(nested.get("url"), str):
            return _image_from_candidate(nested["url"], provider)
        if isinstance(nested, str):
            return _image_from_candidate(nested, provider)
        for key in ("b64_json", "b64", "data", "image_base64"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return _bytes_to_image(_b64_to_bytes(value))
    raise AIError(f"[{provider}] unusable image entry: {str(candidate)[:160]}")


def _image_from_url_or_b64(value: Any, provider: str) -> Image.Image:
    if not value:
        raise AIError(f"[{provider}] the response contained no image.")
    return _image_from_candidate(value, provider)


def _download(url: str, provider: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ProviderHTTPError(provider, exc.code, _read_error_body(exc), url) from exc
    except Exception as exc:
        raise AIError(f"[{provider}] could not download the result image: {exc}") from exc


def _b64_to_bytes(data: str) -> bytes:
    data = (data or "").strip()
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        return base64.b64decode(data)
    except Exception as exc:
        raise AIError(f"Image payload was not valid base64: {exc}") from exc


def _coerce_image(output: Any) -> Image.Image:
    """Turn whatever an image model returned into a PIL image."""
    if isinstance(output, Image.Image):
        output.load()
        return output
    if isinstance(output, (list, tuple)):
        if not output:
            raise AIError("the model returned an empty list")
        return _coerce_image(output[0])
    if isinstance(output, (bytes, bytearray)):
        return _bytes_to_image(output)
    if isinstance(output, str):
        return _bytes_to_image(output)
    raise AIError(f"the model returned {type(output).__name__}, not an image")


def _bytes_to_image(data: Any) -> Image.Image:
    if isinstance(data, str):
        data = _b64_to_bytes(data)
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AIError("Image payload was empty.")
    try:
        image = Image.open(io.BytesIO(bytes(data)))
        image.load()
    except Exception as exc:
        raise AIError(f"Returned payload is not a readable image: {exc}") from exc
    return image


def _extract_chat_text(response: Any) -> str:
    """Pull the assistant text out of a chat completion response."""
    choices = _dig(response, "choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = _dig(choices[0], "message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits = []
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                bits.append(str(part["text"]))
            elif isinstance(part, str):
                bits.append(part)
        return " ".join(bits)
    return str(content or "")


def _clean_translation(text: str) -> str:
    """Strip the wrappers models like to add around a translation."""
    text = (text or "").strip()
    if not text:
        return ""

    prefixes = ("english:", "translation:", "instruction:", "output:", "result:")
    quotes = (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u00ab", "\u00bb"), ("`", "`"))
    for _ in range(4):
        before = text
        for prefix in prefixes:
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
        for left, right in quotes:
            if len(text) > 1 and text.startswith(left) and text.endswith(right):
                text = text[1:-1].strip()
        if text == before:
            break

    text = text.splitlines()[0].strip() if text else ""
    if text and not text.endswith((".", "!", "?")):
        text = text.rstrip(",;:") + "."
    return text


def _is_retryable(message: str) -> bool:
    lowered = str(message).lower()
    return any(marker in lowered for marker in RETRYABLE_MARKERS)


def _is_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "api key not valid", "api_key_invalid", "unauthenticated",
            "permission denied", "401", "403", "invalid token",
            "token is not valid", "authorization",
        )
    )


def _is_safety_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in ("safety", "blocked", "prohibited", "recitation"))


_service: AIService | None = None


def get_ai_service(config: Config | None = None) -> AIService:
    global _service
    if _service is None:
        _service = AIService(config)
    return _service
