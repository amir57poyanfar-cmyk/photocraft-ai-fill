"""Windows clipboard image transport, used by the desktop capture/place path.

The desktop PhotoCraft GUI has no ``doc.render`` and no ``doc.place``. What it
does have is ``edit.copyMerged`` and ``edit.paste``, which go through the
Windows clipboard. This module moves images across that boundary.

Two details that cost time to find and are easy to get wrong:

* PhotoCraft publishes the clipboard image under the **registered ``PNG``
  format** as well as ``CF_DIB``. Reading the PNG is lossless and avoids
  hand-building a DIB header.
* ``CF_DIB`` on the clipboard has **no** ``BITMAPFILEHEADER``. Prepending a
  14-byte BMP file header is the usual trick, but the DIB's own header must be
  kept as-is; synthesising a fresh 40-byte header on top of it corrupts the
  image.

Every clipboard handle is opened and closed inside a single function, and
``CloseClipboard`` runs in a ``finally`` so a failure cannot leave the clipboard
locked for the rest of the session.
"""

from __future__ import annotations

import io
import struct
import time

from PIL import Image

__all__ = [
    "ClipboardError",
    "clear_clipboard",
    "read_image",
    "write_image",
]


class ClipboardError(RuntimeError):
    """The clipboard could not be opened or read."""


def _win32clipboard():
    try:
        import win32clipboard
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ClipboardError(
            "pywin32 is required for the clipboard transport. "
            "Install it with: pip install pywin32"
        ) from exc
    return win32clipboard


def _open(retries: int = 6, delay: float = 0.05):
    """Open the clipboard, retrying while another process holds it."""
    win32clipboard = _win32clipboard()
    last: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            win32clipboard.OpenClipboard()
            return win32clipboard
        except Exception as exc:  # pywintypes.error when locked
            last = exc
            time.sleep(delay)
    raise ClipboardError(f"Could not open the clipboard: {last}")


def read_image() -> Image.Image | None:
    """Current clipboard image, or None when it holds no image.

    Prefers the registered ``PNG`` format, then falls back to ``CF_DIB``.
    """
    win32clipboard = _open()
    try:
        png_id = win32clipboard.RegisterClipboardFormat("PNG")
        if win32clipboard.IsClipboardFormatAvailable(png_id):
            data = win32clipboard.GetClipboardData(png_id)
            image = Image.open(io.BytesIO(data))
            image.load()
            return image

        cf_dib = 8  # win32con.CF_DIB, spelled out to keep the import minimal
        if win32clipboard.IsClipboardFormatAvailable(cf_dib):
            data = win32clipboard.GetClipboardData(cf_dib)
            if len(data) < 40:
                return None
            header_size = struct.unpack("<I", data[0:4])[0]
            width, height = struct.unpack("<ii", data[4:12])
            bit_count = struct.unpack("<H", data[14:16])[0]
            # Keep the DIB's own header, add only the BMP file header.
            file_header = struct.pack(
                "<2sIHHI", b"BM", 14 + len(data), 0, 0, 14 + header_size
            )
            image = Image.open(io.BytesIO(file_header + data))
            image.load()
            return image
        return None
    except ClipboardError:
        raise
    except Exception as exc:
        raise ClipboardError(f"Could not read the clipboard image: {exc}") from exc
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:  # pragma: no cover - already closed
            pass


def write_image(image: Image.Image) -> None:
    """Put ``image`` on the clipboard as ``CF_DIB`` so apps can paste it.

    ``CF_DIB`` is the format every Windows image app understands; PNG is added
    too when the platform accepts it, because it preserves alpha.
    """
    if image is None:
        raise ClipboardError("No image to write.")

    buffer = io.BytesIO()
    # BMP for the DIB payload; alpha is dropped because CF_DIB has no reliable
    # alpha channel and PhotoCraft composites the paste onto the canvas anyway.
    image.convert("RGB").save(buffer, "BMP")
    dib = buffer.getvalue()[14:]  # strip BITMAPFILEHEADER

    png_buffer = io.BytesIO()
    image.save(png_buffer, "PNG")
    png_bytes = png_buffer.getvalue()

    win32clipboard = _open()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(8, dib)  # CF_DIB
        try:
            png_id = win32clipboard.RegisterClipboardFormat("PNG")
            win32clipboard.SetClipboardData(png_id, png_bytes)
        except Exception:
            # PNG is a nicety; CF_DIB alone is enough for paste.
            pass
    except ClipboardError:
        raise
    except Exception as exc:
        raise ClipboardError(f"Could not write the clipboard image: {exc}") from exc
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:  # pragma: no cover - already closed
            pass


def clear_clipboard() -> None:
    """Empty the clipboard, releasing any large image it was holding."""
    win32clipboard = _open()
    try:
        win32clipboard.EmptyClipboard()
    except Exception as exc:
        raise ClipboardError(f"Could not clear the clipboard: {exc}") from exc
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:  # pragma: no cover - already closed
            pass
