"""Prove the desktop-compatible pipeline against a real server.

The desktop GUI answers engine commands, not doc.* RPC. This runs the whole
generative-fill loop using only engine commands:

    file.new -> select.rect -> document.inspect -> file.export.quickExport
    -> file.placeEmbedded(center) -> document.inspect

Run against photocraft-cli serve (same engine surface).
"""

from __future__ import annotations

import json
import socket
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8797
TOKEN = "a" * 64
WORK = r"E:\ai-scatch\.tmp"


def rpc(method: str, params: dict | None = None) -> dict:
    with socket.create_connection(("127.0.0.1", PORT), timeout=30) as sock:
        stream = sock.makefile("rwb")

        def send(rid: int, name: str, args: dict | None) -> dict:
            stream.write((json.dumps({"id": rid, "method": name, "params": args or {}}) + "\n").encode())
            stream.flush()
            line = stream.readline()
            if not line:
                raise OSError("server closed the connection")
            return json.loads(line.decode())

        send(1, "auth", {"token": TOKEN})
        return send(2, method, params)


def engine(command: str, params: dict | None = None) -> dict:
    reply = rpc("engine.execute", {"command": command, "params": params or {}})
    if not reply.get("ok"):
        raise RuntimeError(f"{command} failed: {reply.get('error')}")
    return reply.get("result")


failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


print("=== 1. new document ===")
engine("file.new", {"width": 800, "height": 600, "background": "white"})
check("file.new", True)

print("\n=== 2. rectangular selection ===")
engine("select.rect", {"x": 100, "y": 50, "width": 300, "height": 200})
state = engine("document.inspect")
check("hasSelection", state.get("hasSelection") is True, str(state.get("hasSelection")))
check("selectionBounds == [100,50,300,200]", state.get("selectionBounds") == [100, 50, 300, 200], str(state.get("selectionBounds")))

print("\n=== 3. export the composite (render path) ===")
canvas_path = WORK + r"\canvas.png"
engine("file.export.quickExport", {"path": canvas_path})
import os
check("canvas.png written", os.path.exists(canvas_path), f"{os.path.getsize(canvas_path) if os.path.exists(canvas_path) else 0} bytes")

from PIL import Image
img = Image.open(canvas_path)
check("canvas is 800x600", img.size == (800, 600), str(img.size))

print("\n=== 4. build a patch the size of the selection ===")
patch_path = WORK + r"\patch.png"
Image.new("RGB", (300, 200), (255, 0, 0)).save(patch_path)
check("patch.png written", os.path.exists(patch_path))

print("\n=== 5. place at the exact selection centre ===")
cx, cy = 100 + 300 // 2, 50 + 200 // 2
before = engine("document.inspect")
before_layers = len(before.get("layers", []))
result = engine("file.placeEmbedded", {
    "path": patch_path,
    "fit": False,
    "scale": 100,
    "center": [cx, cy],
})
print("    placeEmbedded ->", json.dumps(result)[:200])
after = engine("document.inspect")
after_layers = len(after.get("layers", []))
check("a new layer appeared", after_layers == before_layers + 1, f"{before_layers} -> {after_layers}")

placed = [l for l in after["layers"] if l.get("id") == result.get("layer")] if result.get("layer") else after["layers"][:1]
if placed:
    bounds = placed[0].get("bounds")
    print(f"    placed layer bounds: {bounds}")
    check("bounds match the selection [100,50,400,250]", bounds == [100, 50, 400, 250], str(bounds))

print("\n=== 6. layer.quickExportAsPng renders a layer ===")
layer_png = WORK + r"\layer.png"
try:
    engine("layer.quickExportAsPng", {"path": layer_png})
    check("layer png written", os.path.exists(layer_png), f"{os.path.getsize(layer_png) if os.path.exists(layer_png) else 0} bytes")
except RuntimeError as exc:
    check("layer.quickExportAsPng", False, str(exc)[:120])

print("\n" + ("ALL DESKTOP-PATH CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(0 if not failures else 1)
