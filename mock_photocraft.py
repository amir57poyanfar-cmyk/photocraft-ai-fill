"""Mock PhotoCraft control server speaking the REAL verified protocol.

Mirrors ``photocraft-cli serve``:

    response envelope: {"id": n, "ok": true, "result": ...}
                       {"id": n, "ok": false, "error": "message"}
    doc.inspect  -> hasSelection, selectionBounds [x,y,w,h], width, height, layers
    doc.render   -> {"base64": "...", "mime": "image/png"}
    doc.place    -> centres the image, returns {"layer": id, "bounds": [l,t,r,b], ...}
    engine.execute -> layer.translate / layer.setProps / select.rect / select.deselect

The render payload is deliberately dribbled out in small writes so the client's
newline-accumulation logic is exercised on every test run.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

DEFAULT_TOKEN = "a" * 64


class MockPhotocraft:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str = DEFAULT_TOKEN,
        width: int = 1200,
        height: int = 800,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.width = width
        self.height = height

        self.selection: list[int] | None = None
        self.layers: list[dict[str, Any]] = [
            {"id": 2, "name": "Background", "bounds": [0, 0, width, height], "kind": "Pixel", "selected": True}
        ]
        self.active_layer = 2
        self.placed: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._chunk_size = 4096
        self._connections: set[socket.socket] = set()
        self._conn_lock = threading.Lock()
        self._next_layer = 3

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "MockPhotocraft":
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(8)
        self._server.settimeout(0.5)
        self._thread = threading.Thread(target=self._accept_loop, name="mock-accept", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._conn_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "MockPhotocraft":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ---------------------------------------------------------------- server
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._server.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            with self._conn_lock:
                self._connections.add(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buffer = bytearray()
        authed = False
        try:
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    idx = buffer.find(b"\n")
                    line = bytes(buffer[:idx])
                    del buffer[: idx + 1]
                    if not line.strip():
                        continue
                    try:
                        request = json.loads(line)
                    except ValueError:
                        self._send(conn, {"id": None, "ok": False, "error": "parse error"})
                        continue
                    self.requests.append(request)
                    response, authed = self._dispatch(request, authed)
                    self._send(conn, response)
        except OSError:
            pass
        finally:
            with self._conn_lock:
                self._connections.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, conn: socket.socket, payload: dict[str, Any]) -> None:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        for start in range(0, len(data), self._chunk_size):
            conn.sendall(data[start : start + self._chunk_size])
            if len(data) > self._chunk_size:
                time.sleep(0.001)

    def _canvas(self) -> Image.Image:
        image = Image.new("RGB", (self.width, self.height), (28, 34, 46))
        draw = ImageDraw.Draw(image)
        draw.rectangle([40, 40, self.width - 40, self.height - 40], outline=(80, 130, 240), width=3)
        draw.text((60, 60), "MOCK PHOTOCRAFT", fill=(230, 235, 245))
        if self.selection:
            x, y, w, h = self.selection
            draw.rectangle([x, y, x + w, y + h], outline=(61, 220, 132), width=2)
        return image

    def _dispatch(self, request: dict[str, Any], authed: bool) -> tuple[dict[str, Any], bool]:
        rid = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        def ok(result: Any) -> dict[str, Any]:
            return {"id": rid, "ok": True, "result": result}

        def err(message: str) -> dict[str, Any]:
            return {"id": rid, "ok": False, "error": message}

        if method == "auth":
            if params.get("token") == self.token:
                return ok({"authenticated": True}), True
            return err("invalid control token"), False

        if not authed:
            return err("not authenticated"), False

        if method == "methods":
            return ok(["engine.execute", "session.list", "doc.inspect", "doc.render", "doc.place", "methods"]), authed

        if method == "session.list":
            return ok({"active": 0, "documents": [{"index": 0, "name": "mock.pcraft"}]}), authed

        if method == "doc.inspect":
            return ok(
                {
                    "name": "mock.pcraft",
                    "width": self.width,
                    "height": self.height,
                    "activeLayer": self.active_layer,
                    "hasSelection": self.selection is not None,
                    "selectionBounds": list(self.selection) if self.selection else None,
                    "layers": [dict(L) for L in self.layers],
                }
            ), authed

        if method == "doc.render":
            buffer = io.BytesIO()
            self._canvas().save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            return ok({"base64": encoded, "mime": "image/png"}), authed

        if method == "doc.place":
            path = Path(str(params.get("path", "")))
            if not path.is_file():
                return err(f"cannot read {path}"), authed
            try:
                patch = Image.open(path)
                patch.load()
            except Exception as exc:
                return err(f"not an image: {exc}"), authed

            pw, ph = patch.size
            # Real server centres the placed image.
            left = (self.width - pw) / 2.0
            top = (self.height - ph) / 2.0
            layer_id = self._next_layer
            self._next_layer += 1
            record = {
                "layer": layer_id,
                "bounds": [left, top, left + pw, top + ph],
                "scale": 100.0,
                "smartObject": True,
                "frame": None,
            }
            self.layers.insert(
                0,
                {
                    "id": layer_id,
                    "name": path.stem,
                    "bounds": [int(left), int(top), pw, ph],
                    "kind": "Smart Object",
                    "selected": True,
                },
            )
            self.active_layer = layer_id
            self.placed.append({**record, "path": str(path), "size": [pw, ph]})
            return ok(record), authed

        if method == "engine.execute":
            command = params.get("command")
            cparams = params.get("params") or {}
            if command == "layer.translate":
                target = cparams.get("layer", self.active_layer)
                dx = int(cparams.get("dx", 0))
                dy = int(cparams.get("dy", 0))
                for L in self.layers:
                    if L["id"] == target:
                        b = L["bounds"]
                        L["bounds"] = [b[0] + dx, b[1] + dy, b[2], b[3]]
                        for rec in self.placed:
                            if rec["layer"] == target:
                                rb = rec["bounds"]
                                rec["bounds"] = [rb[0] + dx, rb[1] + dy, rb[2] + dx, rb[3] + dy]
                        break
                return ok(None), authed
            if command == "layer.setProps":
                target = cparams.get("layer", self.active_layer)
                for L in self.layers:
                    if L["id"] == target and "name" in cparams:
                        L["name"] = cparams["name"]
                return ok(None), authed
            if command == "select.rect":
                self.selection = [
                    int(cparams.get("x", 0)),
                    int(cparams.get("y", 0)),
                    int(cparams.get("width", 1)),
                    int(cparams.get("height", 1)),
                ]
                return ok(None), authed
            if command == "select.deselect":
                self.selection = None
                return ok(None), authed
            return err(f"unknown command `{command}`"), authed

        return err(f"unknown method `{method}` (try `methods`)"), authed


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock PhotoCraft control server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=800)
    args = ap.parse_args()

    server = MockPhotocraft(args.host, args.port, args.token, args.width, args.height).start()
    print(f"Mock PhotoCraft on {args.host}:{args.port} (token {args.token[:8]}...)")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
