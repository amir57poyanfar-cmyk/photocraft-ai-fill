"""Live integration test against the REAL PhotoCraft control server.

Requires a running server, e.g.:

    photocraft-cli serve --port 8799 --control-token <64 hex> \
        --automation-read-root E:\\ai-scatch --automation-write-root E:\\ai-scatch

Run:  python test_live.py --port 8799 --token <64 hex>
"""

from __future__ import annotations

import argparse
import sys
import time

from PIL import Image

from config import Config
from photocraft_client import PhotocraftClient, Selection

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{(' :: ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    cfg = Config(
        api_key="",
        photocraft_host=args.host,
        photocraft_port=args.port,
        control_token=args.token,
        connect_timeout=5.0,
        response_timeout=120.0,
        context_padding=0,
        temp_dir=r"E:\ai-scatch\.tmp",
    )
    client = PhotocraftClient(cfg)

    try:
        client.connect()
        check("auth handshake", client.connected)

        # Fresh document with a known size.
        client._request("doc.new", {"width": 800, "height": 600})
        state = client.get_document_state()
        check("doc.inspect canvas", (state.width, state.height) == (800, 600), f"{state.width}x{state.height}")
        check("no selection initially", state.has_selection is False)

        # Render.
        canvas = client.render_canvas()
        check("doc.render decoded", canvas.size == (800, 600), f"{canvas.size} {canvas.mode}")

        # Create a real rectangular selection through the engine.
        client.run_command("select.rect", {"x": 100, "y": 50, "width": 300, "height": 200})
        state = client.get_document_state()
        check("selection detected", state.has_selection is True, str(state.selection))
        check(
            "selection bounds correct",
            state.selection is not None and state.selection.box == (100, 50, 400, 250),
            str(state.selection.box if state.selection else None),
        )

        # Crop the selection.
        crop = client.crop_selection(canvas, state.selection)
        check("crop_selection size", crop.size == (300, 200), str(crop.size))

        # Place a patch and confirm it lands exactly on the selection.
        patch = Image.new("RGBA", (300, 200), (255, 40, 40, 255))
        placed = client.place_in_canvas(patch, state.selection, name="AI Fill - live test")
        check("doc.place returned layer", bool(placed.get("layer")), str(placed.get("layer")))

        after = client.get_document_state()
        layer = next((L for L in (after.layers or []) if L.get("id") == placed.get("layer")), None)
        check("placed layer exists", layer is not None)
        if layer:
            b = layer.get("bounds")
            check(
                "layer aligned to selection (100,50)",
                b is not None and int(b[0]) == 100 and int(b[1]) == 50,
                f"bounds={b}",
            )
            check("layer renamed", layer.get("name") == "AI Fill - live test", repr(layer.get("name")))

        # Patch file must be gone.
        import glob

        leftovers = glob.glob(r"E:\ai-scatch\.tmp\aifill_*.png")
        check("patch file cleaned up", not leftovers, str(leftovers))

        # Deselect works.
        client.deselect()
        state = client.get_document_state()
        check("deselect clears selection", state.has_selection is False)

        # Full-canvas path (no selection) places centred without translation.
        full = Image.new("RGBA", (800, 600), (20, 200, 120, 255))
        placed2 = client.place_in_canvas(full, Selection(0, 0, 800, 600), name="AI Fill - full")
        check("full-canvas place", bool(placed2.get("layer")), str(placed2.get("layer")))

    finally:
        client.disconnect()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        return 1
    print("ALL LIVE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
