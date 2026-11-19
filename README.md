<div align="center">

# ✨ PhotoCraft AI Generative Fill

**Cyber-Neon Floating Toolbar · Universal BYOK AI Engine**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Windows](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)](https://www.microsoft.com/)
[![CustomTkinter](https://img.shields.io/badge/UI-CustomTkinter-1f1f1f?logo=python&logoColor=white)](https://github.com/TomSchimansky/CustomTkinter)
[![BYOK AI](https://img.shields.io/badge/AI-Universal%20BYOK-FF007F)](#-bring-your-own-api)
[![License: MIT](https://img.shields.io/badge/License-MIT-00F5D4)](LICENSE)

A Photoshop-Firefly-style generative fill companion that lives on top of
[PhotoCraft](https://github.com/microsoft/photocraft). Select a region on the
canvas, describe what belongs there, and the edited pixels land back as a new
layer at the exact coordinates.

```
        ╭──────────────────────────────────────────────────────────────╮
        │  ⠿   ●  Selection: 320×240                                   │
        │      [ Describe what to generate...              ]  ✨ Fill  ⚙ │
        ╰──────────────────────────────────────────────────────────────╯
            cyan ── violet ── magenta breathing neon border
```

</div>

---

## Overview

This project turns PhotoCraft into an AI-editing host without modifying the
editor itself. A tiny launcher wraps `photocraft.exe`, starts a console-less
floating toolbar alongside it, and bridges the two over a local JSON-RPC
socket. The toolbar is a contextual task bar, not a separate app window: it
appears when you make a selection, collapses to a pill otherwise, and gets out
of the way when PhotoCraft is minimised or unfocused.

Everything about the AI side is bring-your-own. Paste any key and/or endpoint
into Settings, press **Test API**, and the toolbar tells you immediately
whether it works — with the provider's raw error text if it doesn't. There are
no silent fallbacks to a different generator.

## ✨ Key Features

- **One-click native launcher.** Run `install.bat` once. Every normal way of
  launching PhotoCraft (Desktop shortcut, Start Menu, double-click, opening an
  image) now starts the toolbar too, with zero terminal windows. Fully
  reversible with `uninstall.bat`.
- **Firefly-style contextual bar.** Auto-pops bottom-centre over a selection,
  collapses to a tiny `✨ AI Fill` pill otherwise, tracks the PhotoCraft window
  by process, and never steals focus. Manual expand is sticky until you press
  `–`.
- **Cyber-neon UI.** Deep obsidian glass surfaces, a breathing cyan → violet →
  magenta border on the bar and pill, a laser-sweep animation during
  generation, eased expand/collapse, and glow-on-hover buttons — all at 60fps.
- **Universal BYOK engine.** One API key, one optional endpoint, one optional
  model. Supports **Google Gemini, OpenAI, OpenRouter, Hugging Face, Replicate,
  fal.ai**, and any **custom OpenAI-compatible relay** (One-API, New-API,
  AvalAI, local LM Studio / Ollama, …).
- **Exact-error reporting.** Failures surface as `[provider / HTTP status]
  server message`. No keyless backend, no substitute model, no pretending a
  text-to-image result is an edit.
- **Full RTL / Persian support.** Type Persian prompts naturally; Tk renders
  them correctly and the *logical* string is sent to the model. Right-alignment
  kicks in automatically for RTL text.
- **Clipboard + engine bridge.** Works against both the headless
  `photocraft-cli serve` server (`doc.*` methods) and the desktop GUI's
  `--control` server (`engine.execute` fallback). Capture uses
  `edit.copyMerged` (which honours the selection); placement uses
  `edit.paste` followed by a real-bounds `layer.translate` for pixel-exact
  coordinates.
- **Resilient transport.** Line-delimited JSON-RPC with a byte-accumulating
  `recv` (no truncation on multi-megabyte base64), auto-reconnect with one
  replay, and temp-file cleanup in `finally` blocks.

## Installation

### Requirements

- Windows 10/11
- Python 3.10+ (the installer creates a private `.venv`)
- PhotoCraft installed at `E:\PhotoCraft`
- An API key from any supported provider

### One-time install

```bat
cd E:\ai-scatch
install.bat
```

The installer:

1. Renames `E:\PhotoCraft\photocraft.exe` → `photocraft_core.exe`, keeping a
   safety copy. Re-running is a no-op if the core binary already exists.
2. Creates `.venv` and installs the dependencies plus PyInstaller.
3. Compiles `launcher.py` into a `--onefile --noconsole` `photocraft.exe` and
   puts it where the original was, preserving the icon.

From then on, launch PhotoCraft however you normally do. To undo:

```bat
uninstall.bat
```

That restores the original executable and removes build artifacts. Your
`config.json` and `.venv` are left alone.

### Run without installing

```bat
run.bat
```

Start PhotoCraft separately with its control server:

```bat
cd E:\PhotoCraft
photocraft_core.exe --control 8765 --control-token <64-hex> ^
  --automation-read-root E:\ai-scatch\.tmp --automation-write-root E:\ai-scatch\.tmp
```

## ⚙ Configuration

Click the **⚙** gear on the bar (or the pill) to open Settings. The dialog
matches the neon theme and stays open until **Save** or **Cancel**.

| Field | What to paste |
| --- | --- |
| **API Key** | Any key: `AIza…`, `sk-…`, `sk-or-…`, `hf_…`, `r8_…`, `fal_…`, `key:secret`, or a relay's own token |
| **Base URL** | Blank to auto-detect, or an endpoint such as `https://…/v1` (needed for local servers, which need no key) |
| **Model** | Blank for the provider default, or an exact model ID |

A live badge shows the detected provider as you type. Press **Test API** to
probe it immediately — the button cycles through the neon stops while it
works, then shows either a green success or the exact server error. Nothing is
sent to a provider until you press **Generate** or **Test API**.

The control port and 64-hex token are generated by the launcher and written to
`config.json`, which the toolbar reads, so the two always agree. `config.json`
is git-ignored and never leaves your machine.

## 🔑 Bring your own API

`detect_provider(api_key, base_url)` decides what to call. An explicit Base URL
always wins over the key prefix, because you typed it. `sk-or-` is checked
before `sk-`, so an OpenRouter key is never read as OpenAI.

| Signal | Provider | Default model |
| --- | --- | --- |
| `AIza` | Google Gemini | `gemini-2.5-flash-image` |
| `sk-or-` | OpenRouter | `google/gemini-2.5-flash-image-preview` |
| `sk-` | OpenAI | `gpt-image-1` |
| `hf_` | Hugging Face | `black-forest-labs/FLUX.1-Kontext-dev` |
| `r8_` | Replicate | `black-forest-labs/flux-kontext-pro` |
| `fal_` or `key:secret` | fal.ai | `fal-ai/flux-pro/kontext` |
| Any other key | Custom OpenAI-compatible | your Model field |
| Known host in URL | That provider | as above |
| Any other URL | Custom OpenAI-compatible | your Model field |

### How each provider is called

| Provider | Route |
| --- | --- |
| Gemini | Official SDK, `generateContent` with the crop inline |
| OpenAI / custom | `POST {base}/images/edits` (multipart), then `POST {base}/chat/completions` |
| OpenRouter | `POST /chat/completions` with `modalities: ["image", "text"]` |
| Hugging Face | `InferenceClient.image_to_image` |
| Replicate | `POST /models/{model}/predictions`, then poll to completion |
| fal.ai | `POST {base}/{model}` with `Authorization: Key …` |

The OpenAI-shaped path tries `/images/edits` first because that is the
documented editing route. Relays often don't implement it and expose image
models through chat completions instead, so a **404/405/400/415/422/501**
falls through to the multimodal chat call. **401/403** (auth) and **5xx** are
reported as-is — retrying elsewhere would only hide the real problem.

Whatever comes back is resized to the exact selection `(width, height)` so the
patch lands pixel-aligned.

### No silent substitutions

There is no keyless backend and no fallback to a different generator. If the
configured provider fails, you see the server's own words:

```
[openai / HTTP 404] The model `gpt-image-1` does not exist
```

With nothing configured the bar says exactly:

```
No API key configured. Open Settings (⚙) and enter any API key or endpoint.
```

### Persian prompts

The logical Persian string is sent to the model, never a reordered one. Tk on
Windows already performs BiDi reordering and glyph shaping, so
`rtl.to_visual_rtl()` is deliberately an identity function — reversing the text
would hand the model mirrored input. `RTL`-dominant prompts right-align in the
entry automatically. Gemini and chat-style models read Persian directly; the
prompt adds a rule telling them to act on the meaning rather than describe the
image.

Older configs with `gemini_api_key` or `hf_token` keep working: the key is
adopted into `api_key` on load and the legacy fields are cleared on save.

## 🏗 Architecture

```
 PhotoCraft ──document.inspect──► selection bounds + canvas size
            ──edit.copyMerged──► selection pixels on the clipboard
                                  │
                                  ▼
                        crop selection (+ context padding)
                                  │
                                  ▼
                  Universal BYOK engine (ai_service.py)
                  Gemini / OpenAI / OpenRouter / HF / Replicate / fal
                                  │
                                  ▼
              ──edit.paste────►  new layer
              ──layer.translate► moved to the exact selection origin
```

| File | Purpose |
| --- | --- |
| `config.py` | Universal `api_key` / `base_url` / `model_name`, provider detection, token validation, legacy migration |
| `photocraft_client.py` | Socket client: auth, framing, `doc.*` with `engine.execute` fallback, clipboard capture/place, reconnect |
| `ai_service.py` | BYOK adapters, exact error propagation, retry and resize-to-crop |
| `ui.py` | Neon floating toolbar, animations, Settings dialog, window tracking |
| `rtl.py` | RTL detection and logical-prompt helpers |
| `clipboard_image.py` | PNG-preferring clipboard read/write with guaranteed handle release |
| `winwindow.py` | Win32 window tracking by process PID |
| `launcher.py` | Wrapper that starts PhotoCraft core + the silent toolbar and monitors both |
| `install.bat` / `uninstall.bat` | One-time native install and clean reversal |
| `run.bat` | Development launch without installing |
| `mock_photocraft.py` | Mock server speaking the verified protocol |
| `test_*.py` | Unit, pipeline, UI-state and integration tests |

### Protocol notes

The client was written against both real servers, not assumptions:

- Line-delimited JSON-RPC: `{"id":n,"ok":true,"result":…}` /
  `{"id":n,"ok":false,"error":"…"}`. `recv` accumulates bytes until a full
  `\n`, so a 3 MB base64 render split across writes is never parsed early.
- `photocraft-cli serve` exposes `doc.inspect`, `doc.render`, `doc.place`.
  The desktop GUI's `--control` server does **not** — it only answers
  `engine.execute` / `engine.commands`.
- On the desktop GUI, state comes from `document.inspect`, capture from
  `edit.copyMerged` (which honours the selection), and placement from
  `edit.paste` + a real-bounds `layer.translate`. `edit.paste` reports
  `offset [0,0]` even when it pasted into the selection, so the client reads
  the layer's actual bounds after pasting.
- Patch files are written into `.tmp/`, placed, then removed in `finally`,
  including on failure. Place/translate calls are marked `retry=False` so a
  reconnect can never double-place.

## 🧪 Testing

```bat
.venv\Scripts\python.exe -m unittest test_window test_launcher test_bridge ^
    test_rtl test_clipboard test_desktop_fallback test_hf_backend ^
    test_byok test_toolbar_width test_settings_dialog
.venv\Scripts\python.exe test_ai_errors.py
.venv\Scripts\python.exe test_pipeline.py
```

280 tests, no network. `_http_json`, `_download` and both SDK clients are
faked, so the suite is fast and deterministic. Test configs point
`config_path` at a temp file, so a test that saves can never overwrite the
real `config.json`.

> **Note:** `test_ui_states.py` and `smoke_ui.py` open real Tk windows and are
> meant for interactive desktop verification, not headless/CI runs. The neon
> animation loops use `after(16, …)`; every job is stored and cancelled on
> close via `stop()` / `<Destroy>` handlers.

## 🔧 Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `No API key configured…` | Open ⚙ and paste a key or a base URL. Press **Test API**. |
| `[provider / HTTP 402] Payment Required` | The provider needs billing. HF's free tier does not cover paid image models; use a Gemini key or a relay with credit. |
| `Auth failed` / `401` | Wrong key for that endpoint, or a relay that needs its own token. Check the Base URL. |
| Relay returns `404` on `/images/edits` | Normal. The client falls through to `/chat/completions`; set the exact Model ID if it still fails. |
| Bar doesn't appear | Auto-popup may be off (Settings), or PhotoCraft is minimised / unfocused. Click the pill to expand manually. |
| Bar collapses while typing | It shouldn't — manual expand is sticky. If it does, press the pill or gear to re-expand and report it. |
| `Not connected` | Check the port/token in Settings match the `--control` arguments PhotoCraft was started with. |
| Persian text looks mirrored | It shouldn't on Windows. Do not pre-reverse the text; the logical string is correct. |

## License

[MIT](LICENSE) — see the LICENSE file for details.
