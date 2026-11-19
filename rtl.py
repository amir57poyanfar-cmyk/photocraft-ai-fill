"""Persian/Arabic text handling for the Generative Fill toolbar.

The important finding, measured rather than assumed: **Tk on Windows already
does BiDi reordering and Arabic/Persian shaping itself.** A ``CTkEntry`` or
``Label`` given the logical string ``"متن های داخلش رو پاک کن"`` renders it
correctly, right-to-left, with connected letter forms.

Applying ``bidi.algorithm.get_display()`` to that string is what *breaks* it:
it reverses the character order a second time, producing the mirrored
``"نک کاپ ور شلخاد یاه نتم"`` that looks like a rendering bug. ``arabic_reshaper``
is not needed either, and would double-shape already-shaped glyphs.

So the rule for this project is: **keep the logical string everywhere.** The
widget displays it correctly, and the model receives the true logical text.
``to_visual_rtl`` therefore returns its input unchanged; it exists so callers
have one named place to express the intent, and so the reasoning is recorded
next to the code rather than lost.

``bidi_reorder`` is kept for surfaces that genuinely have no BiDi support
(plain-text logs, a console, a naive HTML render). It is deliberately *not*
used on any Tk widget.
"""

from __future__ import annotations

import re

__all__ = [
    "RTL_CHARS",
    "bidi_reorder",
    "has_rtl",
    "is_rtl_dominant",
    "logical_prompt",
    "to_visual_rtl",
]

#: Arabic, Arabic Supplement/Extended, Hebrew, and the Arabic presentation forms.
RTL_CHARS = re.compile(
    r"[\u0590-\u05FF"  # Hebrew
    r"\u0600-\u06FF"  # Arabic
    r"\u0700-\u074F"  # Syriac
    r"\u0750-\u077F"  # Arabic Supplement
    r"\u08A0-\u08FF"  # Arabic Extended-A
    r"\uFB50-\uFDFF"  # Arabic Presentation Forms-A
    r"\uFE70-\uFEFF]"  # Arabic Presentation Forms-B
)

#: Latin letters and digits, used to decide the dominant direction.
_LTR_CHARS = re.compile(r"[A-Za-z0-9]")


def has_rtl(text: str | None) -> bool:
    """True when ``text`` contains at least one Persian/Arabic/Hebrew letter."""
    return bool(text) and RTL_CHARS.search(text) is not None


def is_rtl_dominant(text: str | None) -> bool:
    """True when the text is mostly RTL.

    Mixed prompts like ``"remove the متن"`` stay left-aligned; a Persian
    sentence gets right alignment. Counting letters rather than checking for
    presence avoids flipping the box for one stray word.
    """
    if not text:
        return False
    rtl = len(RTL_CHARS.findall(text))
    if not rtl:
        return False
    ltr = len(_LTR_CHARS.findall(text))
    return rtl >= ltr


def to_visual_rtl(text: str) -> str:
    """Return ``text`` ready for a Tk widget: unchanged.

    See the module docstring. Tk performs BiDi reordering and glyph shaping on
    Windows, so the logical string is already the correct thing to display.
    Reversing it here is the bug, not the fix.

    Kept as a named function so the intent is explicit at every call site and
    so this decision is documented where someone would otherwise "fix" it.
    """
    return text


def bidi_reorder(text: str) -> str:
    """Reorder logical text into visual order for a BiDi-unaware surface.

    Only for plain-text output such as a log line or a console. Never for a Tk
    widget. Falls back to the input when ``python-bidi`` is unavailable.
    """
    if not has_rtl(text):
        return text
    try:
        from bidi.algorithm import get_display
    except ImportError:  # pragma: no cover - dependency is declared
        return text
    return get_display(text)


def logical_prompt(text: str | None) -> str:
    """Normalise a prompt for the model without touching its character order.

    Strips surrounding whitespace and collapses internal runs of whitespace,
    which is what a text field tends to accumulate. The logical order, the
    Persian letters and any mixed English are left exactly as typed, because
    that is what Gemini needs to read.
    """
    if not text:
        return ""
    return re.sub(r"[ \t\u00A0]+", " ", text).strip()
