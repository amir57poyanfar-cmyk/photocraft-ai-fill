"""PhotoCraft control-socket client, written against the real protocol.

Verified against ``photocraft-cli serve`` (headless) and the desktop app's
``--control <PORT>`` server:

* Line-delimited JSON, one object per line, ``\\n`` terminated.
* Request:  ``{"id": 1, "method": "auth", "params": {"token": "..."}}``
* Response: ``{"id": 1, "ok": true, "result": {...}}``
  or       ``{"id": 1, "ok": false, "error": "message"}``

Method surface used here (all confirmed present via ``methods``):

    auth, methods, session.list
    doc.inspect  -> hasSelection, selectionBounds [x,y,w,h], width, height, layers
    doc.render   -> {"base64": "...", "mime": "image/png"}
    doc.place    -> {"layer": id, "bounds": [l,t,r,b], "scale": %, "smartObject": bool}
    engine.execute -> run an engine command (layer.translate, layer.setProps, ...)

Two behaviours of the real server that shape this client:

1. ``doc.place`` **centres** the image on the canvas and ignores x/y/name
   params. Exact placement is done afterwards with ``layer.translate``.
2. ``doc.place`` reads the patch **from disk**, and the server only reads
   inside its ``--automation-read-root``. Patch files therefore live in a
   configurable work dir that must be inside that root.

The desktop GUI is a different server
-------------------------------------
``photocraft.exe --control`` answers **no** ``doc.*`` method at all. Measured on
the real binary, not assumed. Its surface is ``engine.execute``, ``ui.inspect``,
``ui.screenshot``, ``app.open``, ``app.save``, ``ui.menu.list`` and friends.

So this client adapts, in this order:

* **State** - ``doc.inspect``, else ``engine.execute document.inspect``. Both
  return the same payload, so selection tracking works on either host.
* **Capture** - ``doc.render``, else ``edit.copyMerged`` + the Windows
  clipboard. ``copyMerged`` honours the active selection and returns its exact
  bounds, so the clipboard image is already the crop. This is better than
  cropping ``ui.screenshot`` by viewport geometry: no zoom/pan math, no window
  chrome, no DPI scaling to get wrong.
* **Place** - ``doc.place``, else the patch goes to the clipboard and
  ``edit.paste`` inserts it as a new layer. ``edit.paste`` reports the layer id
  and a **centred** offset, so ``layer.translate`` still runs afterwards to put
  it at the selection origin.
"""

from __future__ import annotations

import base64
import binascii
import io
import itertools
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from config import Config, get_config

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 512 * 1024 * 1024
RECV_CHUNK = 256 * 1024


# --------------------------------------------------------------------- errors
class PhotocraftError(RuntimeError):
    """Base class for every failure from this module."""


class ConnectionLost(PhotocraftError):
    """The socket died or was never up."""


class AuthError(PhotocraftError):
    """PhotoCraft rejected the control token."""


class RemoteError(PhotocraftError):
    """PhotoCraft answered ``ok: false`` with an error string."""

    def __init__(self, method: str, message: str) -> None:
        self.method = method
        self.message = message
        super().__init__(f"{method}: {message}")


class NoDocumentError(RemoteError):
    """No document is open in PhotoCraft."""


class ProtocolError(PhotocraftError):
    """The peer sent something that is not a valid JSON-RPC line."""


def _is_unknown_method(exc: RemoteError) -> bool:
    """True when the server rejected the *name*, not the request.

    The two PhotoCraft servers answer ``unknown method `x`; did you mean ...``
    for a name they do not have. That is the signal to try the other surface
    rather than to give up.
    """
    return "unknown method" in exc.message.lower()


# ------------------------------------------------------------------ selection
@dataclass(frozen=True)
class Selection:
    """A rectangular selection in document pixels."""

    x: int
    y: int
    width: int
    height: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}"

    @classmethod
    def from_bounds(cls, bounds: Any) -> "Selection | None":
        """Parse PhotoCraft's ``selectionBounds``: ``[x, y, width, height]``."""
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            return None
        try:
            x, y, w, h = (int(round(float(v))) for v in bounds)
        except (TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        return cls(x, y, w, h)

    def padded(self, padding: int, canvas: tuple[int, int]) -> "Selection":
        """Grow the selection by ``padding``, clamped to the canvas."""
        if padding <= 0:
            return self
        cw, ch = canvas
        left = max(0, self.x - padding)
        top = max(0, self.y - padding)
        right = min(cw, self.x + self.width + padding)
        bottom = min(ch, self.y + self.height + padding)
        return Selection(left, top, max(1, right - left), max(1, bottom - top))


@dataclass
class DocumentState:
    """The parts of ``doc.inspect`` the companion needs."""

    width: int
    height: int
    name: str = "Untitled"
    has_selection: bool = False
    selection: Selection | None = None
    active_layer: int | None = None
    layers: list[dict[str, Any]] | None = None
    raw: dict[str, Any] | None = None

    @property
    def canvas(self) -> tuple[int, int]:
        return (self.width, self.height)

    @classmethod
    def from_inspect(cls, data: dict[str, Any]) -> "DocumentState":
        selection = Selection.from_bounds(data.get("selectionBounds"))
        return cls(
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            name=str(data.get("name") or "Untitled"),
            has_selection=bool(data.get("hasSelection")) and selection is not None,
            selection=selection,
            active_layer=data.get("activeLayer"),
            layers=list(data.get("layers") or []),
            raw=data,
        )


# --------------------------------------------------------------------- client
class PhotocraftClient:
    """Thread-safe, auto-reconnecting client for the PhotoCraft control port."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self._sock: socket.socket | None = None
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._state: DocumentState | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------ properties
    @property
    def connected(self) -> bool:
        return self._sock is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def state(self) -> DocumentState | None:
        return self._state

    # ------------------------------------------------------------ connection
    def connect(self, force: bool = False) -> None:
        with self._lock:
            if self._sock is not None and not force:
                return
            self._close_socket()
            host, port = self.config.address
            try:
                sock = socket.create_connection((host, port), timeout=self.config.connect_timeout)
            except OSError as exc:
                self._last_error = f"connect failed: {exc}"
                raise ConnectionLost(f"Cannot reach PhotoCraft at {host}:{port}: {exc}") from exc

            sock.settimeout(self.config.response_timeout)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:  # pragma: no cover
                pass

            self._sock = sock
            self._buffer.clear()
            try:
                self._handshake()
            except Exception:
                self._close_socket()
                raise
            self._last_error = None

    def disconnect(self) -> None:
        with self._lock:
            self._close_socket()

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        self._buffer.clear()
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _handshake(self) -> None:
        token = self.config.control_token
        if not token:
            raise AuthError("No control token configured.")
        try:
            result = self._request("auth", {"token": token}, retry=False)
        except RemoteError as exc:
            raise AuthError(f"PhotoCraft rejected the control token: {exc.message}") from exc
        except ConnectionLost as exc:
            raise AuthError(f"Connection dropped during auth: {exc}") from exc
        if isinstance(result, dict) and result.get("authenticated") is False:
            raise AuthError("PhotoCraft refused the control token.")
        log.info("Authenticated with PhotoCraft at %s", self.config.address_label)

    # --------------------------------------------------------------- framing
    def _recv_message(self) -> dict[str, Any]:
        """Read one newline-terminated JSON object, accumulating partial chunks.

        A high-resolution render is a multi-megabyte base64 string that arrives
        in hundreds of TCP segments. Nothing is parsed until a full newline has
        landed, so a partial read can never surface as a JSON decode error.
        """
        sock = self._sock
        if sock is None:
            raise ConnectionLost("Not connected.")

        while True:
            newline = self._buffer.find(b"\n")
            if newline != -1:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                if not line.strip():
                    continue
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError as exc:
                    raise ProtocolError(f"Malformed JSON from PhotoCraft: {exc}") from exc
                if not isinstance(message, dict):
                    raise ProtocolError(f"Expected a JSON object, got {type(message).__name__}.")
                return message

            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise ProtocolError("PhotoCraft response exceeded the maximum message size.")

            try:
                chunk = sock.recv(RECV_CHUNK)
            except socket.timeout as exc:
                raise ConnectionLost(
                    f"Timed out after {self.config.response_timeout:.0f}s waiting for PhotoCraft."
                ) from exc
            except OSError as exc:
                raise ConnectionLost(f"Socket error while reading: {exc}") from exc

            if not chunk:
                raise ConnectionLost("PhotoCraft closed the connection.")
            self._buffer.extend(chunk)

    def _send(self, payload: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionLost("Not connected.")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            sock.sendall(data)
        except OSError as exc:
            raise ConnectionLost(f"Socket error while sending: {exc}") from exc

    # -------------------------------------------------------------- requests
    def _request(self, method: str, params: dict[str, Any] | None = None, *, retry: bool = True) -> Any:
        """Send one call and return its ``result``.

        ``retry`` reconnects and replays once when the socket dropped. Only
        idempotent methods should retry: replaying ``doc.place`` would add the
        layer twice.
        """
        with self._lock:
            attempt = 0
            while True:
                attempt += 1
                try:
                    if self._sock is None:
                        self.connect()
                    request_id = next(self._ids)
                    self._send({"id": request_id, "method": method, "params": params or {}})
                    return self._await_result(request_id, method)
                except ConnectionLost as exc:
                    self._last_error = str(exc)
                    self._close_socket()
                    if not retry or attempt >= 2:
                        raise
                    log.warning("%s failed (%s); reconnecting", method, exc)
                    time.sleep(0.25)

    def _await_result(self, request_id: int, method: str) -> Any:
        while True:
            message = self._recv_message()

            if message.get("ok") is False:
                error = str(message.get("error") or "unknown error")
                if "no document open" in error.lower():
                    raise NoDocumentError(method, error)
                raise RemoteError(method, error)

            if "id" in message and message["id"] != request_id:
                log.debug("Skipping stale response id=%s (waiting %s)", message["id"], request_id)
                continue
            if "id" not in message and "result" not in message:
                log.debug("Ignoring unsolicited frame: %s", list(message)[:5])
                continue
            return message.get("result")

    # ------------------------------------------------------- document state
    def get_document_state(self) -> DocumentState:
        """Canvas size, active layer and selection bounds.

        Two servers exist and they do not share a surface:

        * the headless ``photocraft-cli serve`` answers ``doc.inspect``;
        * the desktop GUI answers **no** ``doc.*`` method at all. Its state
          comes from the ``document.inspect`` engine command.

        Try the capability-scoped method first, then the engine command, so the
        same client drives either host.
        """
        result = self._inspect_payload()
        if not isinstance(result, dict):
            raise ProtocolError("document inspect returned an unexpected payload.")
        state = DocumentState.from_inspect(result)
        self._state = state
        return state

    def _inspect_payload(self) -> Any:
        """``doc.inspect``, or ``engine.execute document.inspect`` on the GUI."""
        try:
            return self._request("doc.inspect", {}, retry=True)
        except NoDocumentError:
            raise
        except RemoteError as exc:
            if not _is_unknown_method(exc):
                raise
            log.debug("doc.inspect unsupported (%s); using the engine command", exc.message)
        try:
            return self.run_command("document.inspect")
        except NoDocumentError:
            raise
        except RemoteError as exc:
            if _is_unknown_method(exc):
                raise RemoteError(
                    "doc.inspect",
                    "this PhotoCraft build exposes neither `doc.inspect` nor the "
                    "`document.inspect` engine command, so the canvas cannot be read",
                ) from exc
            raise

    #: Backwards-compatible alias.
    inspect_document = get_document_state

    def render_canvas(self, fmt: str = "png") -> Image.Image:
        """The composited canvas as an image, on either host.

        ``doc.render`` on the CLI server. The desktop GUI has no render method,
        so it falls back to ``edit.copyMerged`` plus the clipboard. That
        command copies the **merged composite**, and when there is an active
        selection it copies exactly that region, so the result is already the
        crop the caller wanted.
        """
        try:
            result = self._request("doc.render", {"format": fmt}, retry=True)
        except NoDocumentError:
            raise
        except RemoteError as exc:
            if not _is_unknown_method(exc):
                raise
            log.debug("doc.render unsupported (%s); using edit.copyMerged", exc.message)
            return self._render_via_clipboard()

        payload = _extract_base64(result)
        if payload is None:
            raise ProtocolError("doc.render returned no image data.")
        try:
            raw = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ProtocolError(f"doc.render returned invalid base64: {exc}") from exc
        if not raw:
            raise ProtocolError("doc.render returned an empty image.")
        try:
            image = Image.open(io.BytesIO(raw))
            image.load()
        except Exception as exc:
            raise ProtocolError(f"doc.render returned unreadable image data: {exc}") from exc
        return image.convert("RGBA") if image.mode not in ("RGB", "RGBA") else image

    def _render_via_clipboard(self) -> Image.Image:
        """Capture through ``edit.copyMerged`` and the Windows clipboard."""
        try:
            import clipboard_image
        except ImportError as exc:  # pragma: no cover - module ships with this project
            raise RemoteError(
                "edit.copyMerged",
                f"the clipboard fallback needs clipboard_image.py: {exc}",
            ) from exc

        try:
            copied = self.run_command("edit.copyMerged")
        except NoDocumentError:
            raise
        except RemoteError as exc:
            raise RemoteError(
                "edit.copyMerged",
                f"this build exposes neither `doc.render` nor `edit.copyMerged`, "
                f"so the canvas cannot be read: {exc.message}",
            ) from exc

        try:
            image = clipboard_image.read_image()
        except clipboard_image.ClipboardError as exc:
            raise RemoteError("edit.copyMerged", str(exc)) from exc
        if image is None:
            raise RemoteError(
                "edit.copyMerged",
                "the copy succeeded but the clipboard held no image",
            )

        bounds = copied.get("bounds") if isinstance(copied, dict) else None
        log.debug("Captured %sx%s via clipboard, bounds=%s", image.width, image.height, bounds)
        return image.convert("RGBA") if image.mode not in ("RGB", "RGBA") else image

    # ------------------------------------------------------------- selection
    def get_selection(self) -> Selection | None:
        """Current rectangular selection, or None when nothing is selected."""
        state = self.get_document_state()
        return state.selection if state.has_selection else None

    def crop_selection(self, canvas_image: Image.Image, selection: Selection | None) -> Image.Image:
        """Crop the selected region out of a rendered canvas."""
        if selection is None:
            return canvas_image.copy()
        left, top, right, bottom = selection.box
        left = max(0, min(left, canvas_image.width))
        top = max(0, min(top, canvas_image.height))
        right = max(left + 1, min(right, canvas_image.width))
        bottom = max(top + 1, min(bottom, canvas_image.height))
        return canvas_image.crop((left, top, right, bottom))

    # -------------------------------------------------------------- placing
    def place_in_canvas(
        self,
        image_patch: Image.Image,
        rect: Selection | None = None,
        *,
        name: str | None = None,
        align_to_rect: bool = True,
    ) -> dict[str, Any]:
        """Place a generated patch on the canvas, at ``rect`` when given.

        PhotoCraft centres a placed image, so after ``doc.place`` the new layer
        is translated by the difference between where it landed and where it
        belongs. The patch file is always removed afterwards.
        """
        work_dir = self.config.work_dir()
        stamp = f"{int(time.time() * 1000)}_{next(self._ids)}"
        patch_path = work_dir / f"aifill_{stamp}.png"
        try:
            image_patch.save(patch_path, format="PNG")

            # retry=False: a replayed place would add the layer twice.
            try:
                placed = self._request("doc.place", {"path": str(patch_path)}, retry=False)
            except NoDocumentError:
                raise
            except RemoteError as exc:
                if not _is_unknown_method(exc):
                    raise
                log.debug("doc.place unsupported (%s); using the clipboard", exc.message)
                placed = self._place_via_clipboard(image_patch)
            if not isinstance(placed, dict):
                raise ProtocolError("doc.place returned an unexpected payload.")

            layer_id = placed.get("layer")
            bounds = placed.get("bounds")

            if align_to_rect and rect is not None and isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                # bounds are [left, top, right, bottom] in canvas pixels.
                dx = int(round(rect.x - float(bounds[0])))
                dy = int(round(rect.y - float(bounds[1])))
                if dx or dy:
                    self._request(
                        "engine.execute",
                        {"command": "layer.translate", "params": {"layer": layer_id, "dx": dx, "dy": dy}},
                        retry=False,
                    )
                    # Record where the layer actually ended up, so callers are
                    # not left holding the pre-translate centred position.
                    placed["finalBounds"] = [
                        bounds[0] + dx, bounds[1] + dy, bounds[2] + dx, bounds[3] + dy
                    ]

            if name:
                try:
                    self._request(
                        "engine.execute",
                        {"command": "layer.setProps", "params": {"layer": layer_id, "name": name}},
                        retry=False,
                    )
                except PhotocraftError as exc:
                    log.debug("Could not rename placed layer: %s", exc)

            return placed
        finally:
            _cleanup_path(patch_path)

    def _place_via_clipboard(self, image_patch: Image.Image) -> dict[str, Any]:
        """Insert the patch through the clipboard and ``edit.paste``.

        ``edit.paste`` reports ``{"layer": id, "offset": [x, y]}`` where the
        offset is the **centred** position, matching ``doc.place``'s behaviour.
        The caller's translate step therefore still applies. The clipboard is
        cleared afterwards so a large image is not left resident.
        """
        try:
            import clipboard_image
        except ImportError as exc:  # pragma: no cover - module ships with this project
            raise RemoteError(
                "edit.paste",
                f"the clipboard fallback needs clipboard_image.py: {exc}",
            ) from exc

        try:
            clipboard_image.write_image(image_patch)
        except clipboard_image.ClipboardError as exc:
            raise RemoteError("edit.paste", str(exc)) from exc

        try:
            pasted = self.run_command("edit.paste")
        except NoDocumentError:
            raise
        except RemoteError as exc:
            raise RemoteError(
                "edit.paste",
                f"this build exposes neither `doc.place` nor `edit.paste`, so the "
                f"patch cannot be inserted: {exc.message}",
            ) from exc
        finally:
            # Never leave the generated image sitting on the user's clipboard.
            try:
                clipboard_image.clear_clipboard()
            except clipboard_image.ClipboardError as exc:
                log.debug("Could not clear the clipboard: %s", exc)

        if not isinstance(pasted, dict):
            raise ProtocolError("edit.paste returned an unexpected payload.")
        if not pasted.get("pasted", True) and pasted.get("layer") is None:
            raise RemoteError(
                "edit.paste",
                "the clipboard held no image, so nothing was pasted",
            )

        layer_id = pasted.get("layer")
        offset = pasted.get("offset")
        # `edit.paste`'s reported offset is not trustworthy: with an active
        # selection PhotoCraft pastes into the selection and still reports
        # [0, 0]. Read the layer's real bounds instead, so the caller's
        # translate step is computed from where it actually landed.
        bounds = self._layer_bounds(layer_id)
        if bounds is None and isinstance(offset, (list, tuple)) and len(offset) == 2:
            left, top = int(offset[0]), int(offset[1])
            bounds = [left, top, left + image_patch.width, top + image_patch.height]
        log.debug("Pasted layer %s, reported offset %s, real bounds %s", layer_id, offset, bounds)
        return {"layer": layer_id, "bounds": bounds, "via": "clipboard"}

    def _layer_bounds(self, layer_id: Any) -> list[int] | None:
        """Real bounds of ``layer_id`` as ``[left, top, right, bottom]``.

        ``document.inspect`` reports each layer's bounds as ``[x, y, w, h]``,
        unlike ``doc.place`` which reports ``[l, t, r, b]``. Converting here
        keeps the caller working in one convention.
        """
        if layer_id is None:
            return None
        try:
            payload = self.run_command("document.inspect")
        except PhotocraftError as exc:
            log.debug("Could not inspect the pasted layer: %s", exc)
            return None
        if not isinstance(payload, dict):
            return None
        for layer in payload.get("layers") or []:
            if not isinstance(layer, dict) or layer.get("id") != layer_id:
                continue
            box = layer.get("bounds")
            if isinstance(box, (list, tuple)) and len(box) == 4:
                x, y, w, h = (int(round(float(v))) for v in box)
                return [x, y, x + w, y + h]
        return None

    # ----------------------------------------------------------- convenience
    def run_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        """Run an engine command through ``engine.execute``."""
        return self._request("engine.execute", {"command": command, "params": params or {}}, retry=True)

    def deselect(self) -> None:
        try:
            self.run_command("select.deselect")
        except PhotocraftError as exc:
            log.debug("deselect failed: %s", exc)

    def ping(self) -> bool:
        """Cheap liveness probe; False instead of raising."""
        try:
            self._request("session.list", {}, retry=True)
            return True
        except PhotocraftError:
            return False

    def wait_until_ready(self, timeout: float = 10.0, interval: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.connect()
                return True
            except PhotocraftError:
                time.sleep(interval)
        return False

    def __enter__(self) -> "PhotocraftClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.disconnect()


# -------------------------------------------------------------------- helpers
def _extract_base64(result: Any) -> str | None:
    """Find the base64 image string in a ``doc.render`` result.

    The real server returns ``{"base64": "...", "mime": "image/png"}``; the
    other spellings are accepted so a build change does not break the bridge.
    """
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return None
    for key in ("base64", "image", "data", "png", "image_base64", "imageBase64"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = result.get("result")
    if isinstance(nested, (dict, str)):
        return _extract_base64(nested)
    return None


def _cleanup_path(path: Path) -> None:
    """Best-effort removal that never masks the original error."""
    try:
        if path.is_dir():
            for child in path.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            path.rmdir()
        elif path.exists():
            path.unlink()
    except OSError as exc:  # pragma: no cover - Windows file locks
        log.debug("Could not remove temp path %s: %s", path, exc)
