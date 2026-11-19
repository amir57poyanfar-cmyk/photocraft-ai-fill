"""End-to-end tests against the mock, speaking the verified protocol.

Run:  python -m unittest -v test_bridge.py
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import config as config_module
from config import Config, is_valid_token, new_control_token
from mock_photocraft import DEFAULT_TOKEN, MockPhotocraft
from photocraft_client import (
    AuthError,
    ConnectionLost,
    NoDocumentError,
    PhotocraftClient,
    ProtocolError,
    RemoteError,
    Selection,
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(port: int, token: str = DEFAULT_TOKEN, tmp: str | None = None) -> Config:
    return Config(
        api_key="",
        photocraft_host="127.0.0.1",
        photocraft_port=port,
        control_token=token,
        connect_timeout=2.0,
        response_timeout=20.0,
        context_padding=0,
        temp_dir=tmp or tempfile.mkdtemp(prefix="aifill_test_"),
    )


class TestConfig(unittest.TestCase):
    def test_token_generation_is_64_hex(self):
        token = new_control_token()
        self.assertEqual(len(token), 64)
        self.assertTrue(is_valid_token(token))
        self.assertNotEqual(token, new_control_token())

    def test_invalid_tokens_rejected(self):
        for bad in ("", "abc", "z" * 64, "a" * 63, "a" * 65):
            self.assertFalse(is_valid_token(bad), bad)

    def test_update_validates_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = make_config(8765)
            with mock.patch.object(config_module, "CONFIG_PATH", path):
                cfg.update(photocraft_port=99999)
                self.assertEqual(cfg.photocraft_port, 8765)
                cfg.update(photocraft_port="9000")
                self.assertEqual(cfg.photocraft_port, 9000)
                with self.assertRaises(ValueError):
                    cfg.update(control_token="nope")
                cfg.update(context_padding=9999)
                self.assertEqual(cfg.context_padding, 512, "padding is clamped")
                self.assertTrue(path.exists())
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["photocraft_port"], 9000)


class TestSelection(unittest.TestCase):
    def test_parse_bounds(self):
        sel = Selection.from_bounds([100, 50, 300, 200])
        self.assertEqual(sel.box, (100, 50, 400, 250))
        self.assertEqual(sel.label, "300x200")

    def test_reject_bad_bounds(self):
        for bad in (None, [], [1, 2, 3], [0, 0, 0, 10], "x"):
            self.assertIsNone(Selection.from_bounds(bad), bad)

    def test_padding_clamped_to_canvas(self):
        sel = Selection(10, 10, 50, 50)
        padded = sel.padded(32, (100, 100))
        self.assertEqual(padded.box, (0, 0, 92, 92))
        self.assertEqual(Selection(0, 0, 10, 10).padded(0, (100, 100)).box, (0, 0, 10, 10))


class TestClientAgainstMock(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.server = MockPhotocraft(port=self.port).start()
        self.tmp = tempfile.mkdtemp(prefix="aifill_test_")
        self.client = PhotocraftClient(make_config(self.port, tmp=self.tmp))
        time.sleep(0.1)

    def tearDown(self):
        self.client.disconnect()
        self.server.stop()

    def test_connect_and_auth(self):
        self.client.connect()
        self.assertTrue(self.client.connected)
        self.assertEqual(self.server.requests[0]["method"], "auth")
        self.assertEqual(self.server.requests[0]["params"]["token"], DEFAULT_TOKEN)

    def test_bad_token_raises_auth_error(self):
        client = PhotocraftClient(make_config(self.port, token="b" * 64))
        with self.assertRaises(AuthError):
            client.connect()
        self.assertFalse(client.connected)

    def test_get_document_state(self):
        self.client.connect()
        state = self.client.get_document_state()
        self.assertEqual(state.width, 1200)
        self.assertEqual(state.height, 800)
        self.assertFalse(state.has_selection)
        self.assertIsNone(state.selection)

    def test_selection_detected(self):
        self.server.selection = [100, 50, 300, 200]
        self.client.connect()
        state = self.client.get_document_state()
        self.assertTrue(state.has_selection)
        self.assertEqual(state.selection.box, (100, 50, 400, 250))

    def test_render_canvas_multichunk(self):
        self.client.connect()
        image = self.client.render_canvas()
        self.assertEqual(image.size, (1200, 800))

    def test_crop_selection(self):
        self.client.connect()
        canvas = self.client.render_canvas()
        crop = self.client.crop_selection(canvas, Selection(100, 50, 300, 200))
        self.assertEqual(crop.size, (300, 200))

    def test_crop_full_canvas_when_no_selection(self):
        self.client.connect()
        canvas = self.client.render_canvas()
        crop = self.client.crop_selection(canvas, None)
        self.assertEqual(crop.size, canvas.size)

    def test_place_aligns_to_selection_exactly(self):
        """doc.place centres; the client must translate it to the selection."""
        self.client.connect()
        patch = Image.new("RGBA", (300, 200), (255, 0, 0, 255))
        rect = Selection(100, 50, 300, 200)
        result = self.client.place_in_canvas(patch, rect, name="AI Fill")

        self.assertEqual(result["layer"], self.server.placed[0]["layer"])
        # Centred would be (450, 300); it must end up at (100, 50).
        bounds = self.server.placed[0]["bounds"]
        self.assertAlmostEqual(bounds[0], 100.0, places=0)
        self.assertAlmostEqual(bounds[1], 50.0, places=0)

        layer = next(L for L in self.server.layers if L["id"] == result["layer"])
        self.assertEqual(layer["name"], "AI Fill")

    def test_place_cleans_patch_file(self):
        self.client.connect()
        patch = Image.new("RGBA", (64, 64), (0, 255, 0, 255))
        self.client.place_in_canvas(patch, Selection(10, 10, 64, 64))
        placed_path = Path(self.server.placed[0]["path"])
        self.assertFalse(placed_path.exists(), "patch file must be deleted after placing")

    def test_no_document_maps_to_no_document_error(self):
        class NoDoc(MockPhotocraft):
            def _dispatch(self, request, authed):
                if request.get("method") == "doc.inspect":
                    return {"id": request.get("id"), "ok": False, "error": "no document open"}, authed
                return super()._dispatch(request, authed)

        port = free_port()
        server = NoDoc(port=port).start()
        client = PhotocraftClient(make_config(port, tmp=self.tmp))
        try:
            client.connect()
            with self.assertRaises(NoDocumentError):
                client.get_document_state()
        finally:
            client.disconnect()
            server.stop()

    def test_unknown_method_maps_to_remote_error(self):
        self.client.connect()
        with self.assertRaises(RemoteError):
            self.client._request("doc.nope", {})

    def test_unknown_method_error_names_the_method(self):
        """The status bar has to say *which* call the server rejected."""
        self.client.connect()
        with self.assertRaises(RemoteError) as ctx:
            self.client._request("ui.screenshot", {})
        self.assertEqual(ctx.exception.method, "ui.screenshot")
        self.assertIn("ui.screenshot", str(ctx.exception))

    def test_reconnect_after_server_restart(self):
        self.client.connect()
        self.assertTrue(self.client.connected)

        self.server.stop()
        time.sleep(0.2)
        with self.assertRaises(ConnectionLost):
            self.client.get_document_state()
        self.assertFalse(self.client.connected)

        self.server = MockPhotocraft(port=self.port).start()
        time.sleep(0.3)
        state = self.client.get_document_state()
        self.assertEqual(state.width, 1200)
        self.assertEqual(self.server.requests[0]["method"], "auth")

    def test_ping_false_when_down(self):
        self.client.connect()
        self.assertTrue(self.client.ping())
        self.server.stop()
        time.sleep(0.2)
        self.assertFalse(self.client.ping())


class TestFraming(unittest.TestCase):
    def test_partial_line_is_not_parsed_early(self):
        """A 3 MB base64 payload split into 1 KB writes must reassemble once."""
        port = free_port()
        payload = json.dumps({"id": 2, "ok": True, "result": {"base64": "A" * 3_000_000}}).encode() + b"\n"

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            conn.recv(65536)
            conn.sendall(json.dumps({"id": 1, "ok": True, "result": {"authenticated": True}}).encode() + b"\n")
            conn.recv(65536)
            for i in range(0, len(payload), 1024):
                conn.sendall(payload[i : i + 1024])
            time.sleep(0.3)
            conn.close()
            server.close()

        threading.Thread(target=serve, daemon=True).start()
        time.sleep(0.1)

        client = PhotocraftClient(make_config(port))
        try:
            client.connect()
            result = client._request("doc.render", {"format": "png"})
            self.assertEqual(len(result["base64"]), 3_000_000)
        finally:
            client.disconnect()

    def test_malformed_json_raises_protocol_error(self):
        port = free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            conn.recv(65536)
            conn.sendall(json.dumps({"id": 1, "ok": True, "result": {"authenticated": True}}).encode() + b"\n")
            conn.recv(65536)
            conn.sendall(b'{"id": 2, "ok": true, "result": {broken\n')
            time.sleep(0.2)
            conn.close()
            server.close()

        threading.Thread(target=serve, daemon=True).start()
        time.sleep(0.1)

        client = PhotocraftClient(make_config(port))
        try:
            client.connect()
            with self.assertRaises(ProtocolError):
                client._request("doc.render", {"format": "png"})
        finally:
            client.disconnect()


if __name__ == "__main__":
    unittest.main(verbosity=2)
