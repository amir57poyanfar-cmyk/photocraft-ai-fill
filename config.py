"""Configuration for the PhotoCraft Generative Fill companion.

Resolution order (first hit wins):

    1. ``config.json`` (written by the Settings popup)
    2. ``.env`` / process environment
    3. built-in defaults

The control token is a 64 character hex string, matching PhotoCraft's
``--control-token`` flag.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional at import time
    load_dotenv = None  # type: ignore[assignment]


APP_DIR = Path(__file__).resolve().parent
ENV_PATH = APP_DIR / ".env"
CONFIG_PATH = APP_DIR / "config.json"

#: PhotoCraft's ``--automation-read-root`` must include this folder, because
#: ``doc.place`` reads the patch from disk. Keeping it next to the app makes
#: that easy to arrange.
TEMP_DIR = APP_DIR / ".tmp"

TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"
DEFAULT_IMAGEN_MODEL = "imagen-4.0-generate-001"

#: Hugging Face image-editing models, tried in order. These are open-weights
#: instruction editors, so a free HF token can drive them without billing.
DEFAULT_HF_IMAGE_MODELS = (
    "black-forest-labs/FLUX.1-Kontext-dev",
    "Qwen/Qwen-Image-Edit",
    "timbrooks/instruct-pix2pix",
)

#: Free text models used only to turn a Persian prompt into English.
DEFAULT_HF_TEXT_MODELS = (
    "Qwen/Qwen2.5-7B-Instruct",
    "meta-llama/Llama-3.1-8B-Instruct",
)

#: Hugging Face tokens are prefixed; that prefix is how the provider is picked.
HF_TOKEN_PREFIX = "hf_"

PROVIDER_GEMINI = "gemini"
PROVIDER_OPENAI = "openai"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_HF = "huggingface"
PROVIDER_REPLICATE = "replicate"
PROVIDER_FAL = "fal"
#: Anything else: a relay, aggregator, or self-hosted gateway that speaks the
#: OpenAI HTTP shape. The most common real case, so it is the fallback.
PROVIDER_CUSTOM = "custom_openai_compatible"

PROVIDERS = (
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    PROVIDER_OPENROUTER,
    PROVIDER_HF,
    PROVIDER_REPLICATE,
    PROVIDER_FAL,
    PROVIDER_CUSTOM,
)

#: ``provider -> (base_url, default_model)``. ``None`` means "the official SDK
#: handles it" (Gemini, Hugging Face) rather than a raw HTTP base.
PROVIDER_DEFAULTS: dict[str, tuple[str | None, str]] = {
    PROVIDER_GEMINI: (None, "gemini-2.5-flash-image"),
    PROVIDER_OPENAI: ("https://api.openai.com/v1", "gpt-image-1"),
    PROVIDER_OPENROUTER: ("https://openrouter.ai/api/v1", "google/gemini-2.5-flash-image-preview"),
    PROVIDER_HF: (None, "black-forest-labs/FLUX.1-Kontext-dev"),
    PROVIDER_REPLICATE: ("https://api.replicate.com/v1", "black-forest-labs/flux-kontext-pro"),
    PROVIDER_FAL: ("https://fal.run", "fal-ai/flux-pro/kontext"),
    PROVIDER_CUSTOM: ("", ""),
}

#: Human-facing names for the Settings modal and the status bar.
PROVIDER_LABELS: dict[str, str] = {
    PROVIDER_GEMINI: "Google Gemini",
    PROVIDER_OPENAI: "OpenAI",
    PROVIDER_OPENROUTER: "OpenRouter",
    PROVIDER_HF: "Hugging Face",
    PROVIDER_REPLICATE: "Replicate",
    PROVIDER_FAL: "fal.ai",
    PROVIDER_CUSTOM: "Custom (OpenAI-compatible)",
}

#: Substrings that identify a provider from an explicit base URL. Checked in
#: order, so the more specific hosts come first.
_URL_HINTS: tuple[tuple[str, str], ...] = (
    ("generativelanguage.googleapis.com", PROVIDER_GEMINI),
    ("huggingface", PROVIDER_HF),
    ("replicate.com", PROVIDER_REPLICATE),
    ("fal.run", PROVIDER_FAL),
    ("fal.ai", PROVIDER_FAL),
    ("openrouter.ai", PROVIDER_OPENROUTER),
    ("api.openai.com", PROVIDER_OPENAI),
)

#: ``api_key prefix -> provider``. Checked in order: ``sk-or-`` must be tested
#: before ``sk-`` or every OpenRouter key would be read as an OpenAI key.
_KEY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("AIza", PROVIDER_GEMINI),
    ("sk-or-", PROVIDER_OPENROUTER),
    ("sk-", PROVIDER_OPENAI),
    ("hf_", PROVIDER_HF),
    ("r8_", PROVIDER_REPLICATE),
    ("fal_", PROVIDER_FAL),
)


def detect_provider(api_key: str, base_url: str = "") -> str:
    """Work out which backend a key and/or endpoint means.

    An explicit ``base_url`` is the strongest signal: the user typed it, so it
    wins. Its host decides the provider when it is a known one, otherwise the
    endpoint is assumed to speak the OpenAI HTTP shape, which is what relays,
    aggregators (One-API, New-API, AvalAI) and self-hosted gateways expose.

    With no URL, the key's prefix decides. An unrecognised key is still usable:
    it is treated as a custom OpenAI-compatible endpoint rather than rejected.
    """
    api_key = str(api_key or "").strip()
    base_url = str(base_url or "").strip()

    if base_url:
        lowered = base_url.lower()
        for needle, provider in _URL_HINTS:
            if needle in lowered:
                return provider
        return PROVIDER_CUSTOM

    if api_key:
        for prefix, provider in _KEY_PREFIXES:
            if api_key.startswith(prefix):
                return provider
        # fal keys are ``key_id:key_secret``.
        if ":" in api_key:
            return PROVIDER_FAL
        return PROVIDER_CUSTOM

    return PROVIDER_CUSTOM


def default_base_url(provider: str) -> str:
    return PROVIDER_DEFAULTS.get(provider, ("", ""))[0] or ""


def default_model(provider: str) -> str:
    return PROVIDER_DEFAULTS.get(provider, ("", ""))[1] or ""


def looks_like_hf_token(key: str) -> bool:
    """True when ``key`` is a Hugging Face token rather than a Gemini key."""
    return str(key or "").strip().startswith(HF_TOKEN_PREFIX)


def _load_env_file() -> None:
    if load_dotenv is None or not ENV_PATH.exists():
        return
    load_dotenv(dotenv_path=ENV_PATH, override=False)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_port(value: Any, fallback: int = DEFAULT_PORT) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return port if 1 <= port <= 65535 else fallback


def _coerce_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_models(value: Any, fallback: tuple[str, ...]) -> list[str]:
    """Accept a list, a comma-separated string, or nothing at all."""
    if isinstance(value, (list, tuple)):
        models = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str) and value.strip():
        models = [part.strip() for part in value.split(",") if part.strip()]
    else:
        models = []
    return models or list(fallback)


def _mask(key: str) -> str:
    """Show enough of a key to recognise it, never enough to use it."""
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def new_control_token() -> str:
    return secrets.token_hex(32)


def is_valid_token(token: str) -> bool:
    return bool(TOKEN_RE.match(token or ""))


@dataclass
class Config:
    #: Universal BYOK fields. One key, one optional endpoint, one optional model.
    api_key: str = ""
    base_url: str = ""
    model_name: str = ""
    #: Legacy fields, kept so older config.json files still load. They are
    #: folded into ``api_key`` on load and cleared on save.
    gemini_api_key: str = ""
    hf_token: str = ""
    photocraft_host: str = DEFAULT_HOST
    photocraft_port: int = DEFAULT_PORT
    control_token: str = ""
    image_model: str = DEFAULT_IMAGE_MODEL
    imagen_model: str = DEFAULT_IMAGEN_MODEL
    #: Ordered model lists for the Hugging Face path.
    hf_image_models: list[str] = field(default_factory=lambda: list(DEFAULT_HF_IMAGE_MODELS))
    hf_text_models: list[str] = field(default_factory=lambda: list(DEFAULT_HF_TEXT_MODELS))
    #: Pixels of surrounding context added around the selection when cropping.
    context_padding: int = 32
    #: Send the full canvas alongside the crop so the model sees surroundings.
    send_full_context: bool = True
    connect_timeout: float = 4.0
    response_timeout: float = 180.0
    always_on_top: bool = True
    #: Firefly behaviour: pop the bar open when a selection appears.
    auto_popup: bool = True
    temp_dir: str = str(TEMP_DIR)
    #: Where this config was loaded from, so ``save()`` writes back to the same
    #: file. Tests point this at a temp file; without it they would overwrite
    #: the user's real config.json.
    config_path: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, config_path: Path | None = None) -> "Config":
        _load_env_file()
        stored = _read_json(config_path or CONFIG_PATH)

        def pick(settings_key: str, env_key: str, default: Any) -> Any:
            if settings_key in stored and stored[settings_key] not in (None, ""):
                return stored[settings_key]
            env_value = os.getenv(env_key)
            if env_value not in (None, ""):
                return env_value
            return default

        api_key = str(pick("api_key", "PHOTOCRAFT_API_KEY", "")).strip()
        if not api_key:
            api_key = str(os.getenv("OPENAI_API_KEY", "")).strip()

        # Legacy configs stored the key under a provider-specific field. Adopt
        # whichever is present so an existing setup keeps working untouched.
        legacy_gemini = str(pick("gemini_api_key", "GEMINI_API_KEY", "")).strip()
        if not legacy_gemini:
            legacy_gemini = str(os.getenv("GOOGLE_API_KEY", "")).strip()
        legacy_hf = str(pick("hf_token", "HF_TOKEN", "")).strip()
        if not legacy_hf:
            legacy_hf = str(os.getenv("HUGGINGFACE_TOKEN", "")).strip()
        # An older build wrote an hf_ token into gemini_api_key; put it back.
        if not legacy_hf and looks_like_hf_token(legacy_gemini):
            legacy_hf, legacy_gemini = legacy_gemini, ""
        if not api_key:
            api_key = legacy_hf or legacy_gemini

        base_url = str(pick("base_url", "PHOTOCRAFT_BASE_URL", "")).strip()
        model_name = str(pick("model_name", "PHOTOCRAFT_MODEL_NAME", "")).strip()

        token = str(pick("control_token", "PHOTOCRAFT_CONTROL_TOKEN", "")).strip()
        if not is_valid_token(token):
            token = new_control_token()

        try:
            padding = int(str(pick("context_padding", "PHOTOCRAFT_CONTEXT_PADDING", 32)).strip())
        except (TypeError, ValueError):
            padding = 32

        cfg = cls(
            api_key=api_key,
            base_url=base_url,
            model_name=model_name,
            gemini_api_key=legacy_gemini,
            hf_token=legacy_hf,
            hf_image_models=_coerce_models(
                pick("hf_image_models", "PHOTOCRAFT_HF_IMAGE_MODELS", None), DEFAULT_HF_IMAGE_MODELS
            ),
            hf_text_models=_coerce_models(
                pick("hf_text_models", "PHOTOCRAFT_HF_TEXT_MODELS", None), DEFAULT_HF_TEXT_MODELS
            ),
            photocraft_host=str(pick("photocraft_host", "PHOTOCRAFT_HOST", DEFAULT_HOST)).strip() or DEFAULT_HOST,
            photocraft_port=_coerce_port(pick("photocraft_port", "PHOTOCRAFT_PORT", DEFAULT_PORT)),
            control_token=token,
            image_model=str(pick("image_model", "GEMINI_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)).strip()
            or DEFAULT_IMAGE_MODEL,
            imagen_model=str(pick("imagen_model", "GEMINI_IMAGEN_MODEL", DEFAULT_IMAGEN_MODEL)).strip()
            or DEFAULT_IMAGEN_MODEL,
            context_padding=max(0, min(padding, 512)),
            send_full_context=_coerce_bool(
                pick("send_full_context", "PHOTOCRAFT_SEND_FULL_CONTEXT", True), True
            ),
            connect_timeout=float(pick("connect_timeout", "PHOTOCRAFT_CONNECT_TIMEOUT", 4.0)),
            response_timeout=float(pick("response_timeout", "PHOTOCRAFT_RESPONSE_TIMEOUT", 180.0)),
            always_on_top=_coerce_bool(pick("always_on_top", "PHOTOCRAFT_ALWAYS_ON_TOP", True), True),
            auto_popup=_coerce_bool(pick("auto_popup", "PHOTOCRAFT_AUTO_POPUP", True), True),
            temp_dir=str(pick("temp_dir", "PHOTOCRAFT_TEMP_DIR", str(TEMP_DIR))),
            config_path=str(config_path or CONFIG_PATH),
        )
        if not (config_path or CONFIG_PATH).exists():
            cfg.save()
        return cfg

    # ----------------------------------------------------------------- saving
    def save(self, path: Path | None = None) -> Path:
        # Write back to the file this config came from, not a hardcoded path.
        target = path or Path(self.config_path or CONFIG_PATH)
        payload = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "hf_image_models": list(self.hf_image_models),
            "hf_text_models": list(self.hf_text_models),
            "photocraft_host": self.photocraft_host,
            "photocraft_port": self.photocraft_port,
            "control_token": self.control_token,
            "image_model": self.image_model,
            "imagen_model": self.imagen_model,
            "context_padding": self.context_padding,
            "send_full_context": self.send_full_context,
            "connect_timeout": self.connect_timeout,
            "response_timeout": self.response_timeout,
            "always_on_top": self.always_on_top,
            "auto_popup": self.auto_popup,
            "temp_dir": self.temp_dir,
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target

    def update(self, **changes: Any) -> None:
        with self._lock:
            if "photocraft_port" in changes:
                changes["photocraft_port"] = _coerce_port(changes["photocraft_port"], self.photocraft_port)
            if "control_token" in changes:
                token = str(changes["control_token"]).strip()
                if not is_valid_token(token):
                    raise ValueError("Control token must be exactly 64 hex characters.")
                changes["control_token"] = token
            for key in ("api_key", "base_url", "model_name"):
                if key in changes:
                    changes[key] = str(changes[key] or "").strip()
            # A new key supersedes the legacy fields it was migrated from.
            if changes.get("api_key"):
                changes.setdefault("gemini_api_key", "")
                changes.setdefault("hf_token", "")
            for key in ("hf_image_models", "hf_text_models"):
                if key in changes:
                    changes[key] = _coerce_models(changes[key], DEFAULT_HF_IMAGE_MODELS)
            if "context_padding" in changes:
                try:
                    changes["context_padding"] = max(0, min(int(changes["context_padding"]), 512))
                except (TypeError, ValueError):
                    changes.pop("context_padding")
            for key, value in changes.items():
                if hasattr(self, key) and not key.startswith("_"):
                    setattr(self, key, value)
            self.save()

    # ---------------------------------------------------------------- helpers
    @property
    def address(self) -> tuple[str, int]:
        return (self.photocraft_host, self.photocraft_port)

    @property
    def address_label(self) -> str:
        return f"{self.photocraft_host}:{self.photocraft_port}"

    @property
    def has_api_key(self) -> bool:
        """True when a key is configured. Without one, nothing can be sent."""
        return bool(self.api_key)

    @property
    def active_provider(self) -> str:
        """Which backend the configured key and endpoint mean.

        Derived, never stored: ``detect_provider`` reads the key prefix and the
        endpoint. There is no silent substitution, so what Settings shows is
        exactly what will be called.
        """
        return detect_provider(self.api_key, self.base_url)

    @property
    def active_api_key(self) -> str:
        return self.api_key

    @property
    def active_base_url(self) -> str:
        """The endpoint to call: the user's, or the provider's default."""
        return self.base_url or default_base_url(self.active_provider)

    @property
    def active_model(self) -> str:
        """The model to call: the user's, or the provider's default."""
        return self.model_name or default_model(self.active_provider)

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.active_provider, self.active_provider)

    @property
    def is_free_mode(self) -> bool:
        """Kept for compatibility. There is no keyless backend any more: an
        unconfigured engine reports a clear error instead of generating
        something unrelated."""
        return False

    @property
    def masked_api_key(self) -> str:
        return _mask(self.api_key)

    @property
    def masked_hf_token(self) -> str:
        return _mask(self.hf_token)

    @property
    def masked_gemini_key(self) -> str:
        return _mask(self.gemini_api_key)

    def set_api_key(self, key: str) -> str:
        """Store a key and return the provider it now resolves to."""
        self.api_key = str(key or "").strip()
        return self.active_provider

    def work_dir(self) -> Path:
        """Directory for patch files; must be readable by PhotoCraft automation."""
        path = Path(self.temp_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_lock", None)
        data.pop("config_path", None)
        return data


_cache: Config | None = None


def get_config(reload: bool = False) -> Config:
    global _cache
    if _cache is None or reload:
        _cache = Config.load()
    return _cache
