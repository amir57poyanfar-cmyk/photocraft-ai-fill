"""Client fallback tests for the desktop GUI surface.

The desktop build answers no ``doc.*`` method. These tests drive a server that
behaves like it: ``doc.*`` is rejected as an unknown method, ``document.inspect``
and ``edit.copyMerged`` / ``edit.paste`` work. That is the exact shape measured
on ``photocraft_core.exe``, so these tests fail if the fallback regresses.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

import clipboard_image
from config import Config
from photocraft_client import (
    NoDocumentError,
    PhotocraftClient,
    RemoteError,
)

TOKEN = "c" * 64


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DesktopLikeServer:
    """A server with the desktop GUI's method surface, not the CLI's.

    ``doc.*`` -> unknown method. ``engine.execute`` handles the commands the
    desktop build actually has.
    """

    def __init__(self, port: int, *, width: int = 800, height: int = 600) -> None:
        self.port = port
        self.width = width
        self.height = height
        self.selection: list[int] | None = None
        self.requests: list[dict] = []
        self.layers: list[dict] = []
        self.next_layer = 2
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The canvas the fake document holds, so copyMerged can put real pixels
        # on the clipboard the way PhotoCraft does.
        self.canvas = Image.new("RGB", (width, height), (240, 240, 240))

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "DesktopLikeServer":
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        stream = conn.makefile("rwb")
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                try:
                    request = json.loads(line.decode())
                except json.JSONDecodeError:
                    return
                self.requests.append(request)
                reply = self._dispatch(request)
                stream.write((json.dumps(reply) + "\n").encode())
                stream.flush()
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, request: dict) -> dict:
        rid = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        def ok(result):
            return {"id": rid, "ok": True, "result": result}

        def err(message):
            return {"id": rid, "ok": False, "error": message}

        if method == "auth":
            if params.get("token") == TOKEN:
                return ok({"authenticated": True})
            return err("authentication required")

        # The desktop build has no doc.* surface at all.
        if method.startswith("doc."):
            return err(f"unknown method `{method}`; did you mean `ui.inspect`?")

        if method == "engine.execute":
            return self._engine(params.get("command"), params.get("params") or {}, rid)

        return err(f"unknown method `{method}`")

    def _engine(self, command: str | None, params: dict, rid: int = 1) -> dict:
        def ok(result):
            return {"id": rid, "ok": True, "result": result}

        def err(message):
            return {"id": rid, "ok": False, "error": message}

        if command == "document.inspect":
            return ok({
                "width": self.width,
                "height": self.height,
                "hasSelection": self.selection is not None,
                "selectionBounds": self.selection,
                "activeLayer": self.next_layer - 1,
                "layers": self.layers,
            })

        if command == "edit.copyMerged":
            # PhotoCraft copies the selection when there is one, else the canvas.
            if self.selection:
                x, y, w, h = self.selection
                crop = self.canvas.crop((x, y, x + w, y + h))
                clipboard_image.write_image(crop)
                return ok({"bounds": [x, y, w, h]})
            clipboard_image.write_image(self.canvas)
            return ok({"bounds": [0, 0, self.width, self.height]})

        if command == "edit.paste":
            pasted = clipboard_image.read_image()
            if pasted is None:
                return ok({"pasted": False})
            layer_id = self.next_layer
            self.next_layer += 1
            # PhotoCraft's real behaviour, measured on photocraft_core.exe:
            # with an active selection it pastes into the selection and still
            # reports offset [0, 0]. Without one it centres the paste.
            if self.selection:
                x, y, w, h = self.selection
                left, top = x, y
                reported = [0, 0]
            else:
                left = (self.width - pasted.width) // 2
                top = (self.height - pasted.height) // 2
                reported = [left, top]
            self.layers.append({
                "id": layer_id,
                "name": f"Layer {layer_id - 1}",
                "bounds": [left, top, pasted.width, pasted.height],
            })
            return ok({"layer": layer_id, "offset": reported})

        if command == "layer.translate":
            layer_id = params.get("layer")
            dx = int(params.get("dx") or 0)
            dy = int(params.get("dy") or 0)
            for layer in self.layers:
                if layer["id"] == layer_id:
                    layer["bounds"][0] += dx
                    layer["bounds"][1] += dy
            return ok(None)

        if command == "layer.setProps":
            for layer in self.layers:
                if layer["id"] == params.get("layer"):
                    layer["name"] = params.get("name")
            return ok(None)

        return err(f"unknown command `{command}`")


def make_config(port: int, tmp: str) -> Config:
    return Config(
        photocraft_host="127.0.0.1",
        photocraft_port=port,
        control_token=TOKEN,
        connect_timeout=5.0,
        response_timeout=30.0,
        temp_dir=tmp,
    )


class TestDesktopFallbacks(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.server = DesktopLikeServer(port=self.port).start()
        self.tmp = tempfile.mkdtemp(prefix="aifill_desktop_")
        self.client = PhotocraftClient(make_config(self.port, self.tmp))
        self.client.connect()
        time.sleep(0.05)

    def tearDown(self):
        self.client.disconnect()
        self.server.stop()

    # ------------------------------------------------------------- state
    def test_state_falls_back_to_the_engine_command(self):
        state = self.client.get_document_state()
        self.assertEqual(state.width, 800)
        self.assertEqual(state.height, 600)
        self.assertFalse(state.has_selection)

    def test_state_reads_selection_bounds(self):
        self.server.selection = [100, 50, 300, 200]
        state = self.client.get_document_state()
        self.assertTrue(state.has_selection)
        self.assertEqual(state.selection.box, (100, 50, 400, 250))

    def test_state_tries_doc_inspect_first(self):
        self.client.get_document_state()
        methods = [r["method"] for r in self.server.requests]
        self.assertIn("doc.inspect", methods)
        self.assertIn("engine.execute", methods)

    # ----------------------------------------------------------- capture
    def test_render_falls_back_to_copy_merged(self):
        image = self.client.render_canvas()
        self.assertEqual(image.size, (800, 600))

    def test_render_returns_the_whole_canvas_without_a_selection(self):
        image = self.client.render_canvas()
        self.assertEqual(image.size, (self.server.width, self.server.height))

    def test_render_returns_the_selection_region(self):
        # copyMerged honours the selection, so the capture is already the crop.
        self.server.selection = [100, 50, 300, 200]
        image = self.client.render_canvas()
        self.assertEqual(image.size, (300, 200))

    def test_render_uses_copy_merged_not_a_screenshot(self):
        self.client.render_canvas()
        commands = [
            (r.get("params") or {}).get("command")
            for r in self.server.requests
            if r["method"] == "engine.execute"
        ]
        self.assertIn("edit.copyMerged", commands)
        self.assertNotIn("ui.screenshot", commands)

    def test_render_survives_an_empty_clipboard(self):
        clipboard_image.clear_clipboard()
        # copyMerged repopulates it, so this must still succeed.
        self.assertEqual(self.client.render_canvas().size, (800, 600))

    # ----------------------------------------------------------- place
    def test_place_falls_back_to_the_clipboard(self):
        patch = Image.new("RGB", (300, 200), (255, 0, 0))
        placed = self.client.place_in_canvas(patch)
        self.assertIn("layer", placed)
        self.assertEqual(placed["via"], "clipboard")

    def test_place_creates_a_layer(self):
        patch = Image.new("RGB", (300, 200), (255, 0, 0))
        placed = self.client.place_in_canvas(patch)
        self.assertEqual(len(self.server.layers), 1)
        self.assertEqual(self.server.layers[0]["id"], placed["layer"])

    def test_place_aligns_the_layer_to_the_selection(self):
        from photocraft_client import Selection

        patch = Image.new("RGB", (300, 200), (255, 0, 0))
        target = Selection(100, 50, 300, 200)
        self.client.place_in_canvas(patch, target)
        bounds = self.server.layers[0]["bounds"]
        # Paste centred it at (250, 200); translate must move it to (100, 50).
        self.assertEqual(bounds[0], 100)
        self.assertEqual(bounds[1], 50)

    def test_place_reports_bounds_in_doc_place_shape(self):
        patch = Image.new("RGB", (300, 200), (255, 0, 0))
        placed = self.client.place_in_canvas(patch)
        bounds = placed["bounds"]
        self.assertEqual(len(bounds), 4)
        self.assertEqual(bounds[2] - bounds[0], 300)
        self.assertEqual(bounds[3] - bounds[1], 200)

    def test_place_does_not_trust_the_reported_offset(self):
        """PhotoCraft reports offset [0,0] even when it pasted into a selection.

        Trusting that offset double-shifts the layer. The client must read the
        layer's real bounds instead.
        """
        from photocraft_client import Selection

        patch = Image.new("RGB", (300, 200), (255, 0, 0))
        target = Selection(100, 50, 300, 200)
        self.client.place_in_canvas(patch, target)
        bounds = self.server.layers[0]["bounds"]
        self.assertEqual((bounds[0], bounds[1]), (100, 50))
        self.assertEqual((bounds[2], bounds[3]), (300, 200))

    def test_place_converts_layer_bounds_from_xywh(self):
        """document.inspect reports [x, y, w, h]; doc.place reports [l,t,r,b]."""
        from photocraft_client import Selection

        patch = Image.new("RGB", (200, 150), (0, 0, 255))
        target = Selection(40, 30, 200, 150)
        placed = self.client.place_in_canvas(patch, target)
        # finalBounds must be the same convention as doc.place's bounds.
        self.assertEqual(placed["finalBounds"], [40, 30, 240, 180])

    def test_place_renames_the_layer(self):
        patch = Image.new("RGB", (100, 100), (0, 255, 0))
        self.client.place_in_canvas(patch, name="AI Fill - test")
        self.assertEqual(self.server.layers[0]["name"], "AI Fill - test")

    def test_place_clears_the_clipboard_afterwards(self):
        patch = Image.new("RGB", (100, 100), (0, 255, 0))
        self.client.place_in_canvas(patch)
        # The generated image must not be left on the user's clipboard.
        self.assertIsNone(clipboard_image.read_image())

    def test_place_cleans_up_the_patch_file(self):
        patch = Image.new("RGB", (100, 100), (0, 255, 0))
        self.client.place_in_canvas(patch)
        leftovers = list(Path(self.tmp).glob("aifill_*.png"))
        self.assertEqual(leftovers, [])

    def test_place_raises_when_nothing_was_pasted(self):
        patch = Image.new("RGB", (100, 100), (0, 255, 0))
        # A server that reports an empty paste must not look like success.
        original = self.server._engine

        def empty_paste(command, params, rid=1):
            if command == "edit.paste":
                return {"id": rid, "ok": True, "result": {"pasted": False}}
            return original(command, params, rid)

        self.server._engine = empty_paste
        with self.assertRaises(RemoteError):
            self.client.place_in_canvas(patch)


class TestNoDocumentStillRaises(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.server = DesktopLikeServer(port=self.port).start()
        self.tmp = tempfile.mkdtemp(prefix="aifill_nodoc_")
        self.client = PhotocraftClient(make_config(self.port, self.tmp))
        self.client.connect()

        def no_doc(command, params, rid=1):
            return {"id": rid, "ok": False,
                    "error": f"command `{command}` is not available right now: no document open"}

        self.server._engine = no_doc

    def tearDown(self):
        self.client.disconnect()
        self.server.stop()

    def test_state_raises_no_document(self):
        with self.assertRaises(NoDocumentError):
            self.client.get_document_state()


if __name__ == "__main__":
    unittest.main(verbosity=2)
