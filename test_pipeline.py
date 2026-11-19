"""End-to-end generative-fill pipeline with a stubbed AI service.

Exercises exactly what the toolbar does, without a Gemini key:

    connect -> inspect -> render -> crop selection (+padding)
            -> (stub) generative_fill -> fit patch -> place -> translate
            -> verify exact bounds -> verify temp cleanup

Run:  python test_pipeline.py
"""

from __future__ import annotations

import glob
import socket
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

from ai_service import FillResult
from config import Config
from mock_photocraft import DEFAULT_TOKEN, MockPhotocraft
from photocraft_client import PhotocraftClient, Selection

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{(' :: ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StubAI:
    """Returns a solid patch at the same size it was handed."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generative_fill(self, image, prompt, is_selection=True, *, context_image=None, max_retries=3):
        self.calls.append(
            {
                "size": image.size,
                "prompt": prompt,
                "is_selection": is_selection,
                "has_context": context_image is not None,
            }
        )
        return FillResult(
            image=Image.new("RGBA", image.size, (255, 120, 40, 255)),
            model="stub-model",
            prompt=prompt,
            used_reference=True,
            elapsed_s=0.5,
        )


def run_fill(client: PhotocraftClient, ai: StubAI, prompt: str, padding: int) -> dict:
    """Mirror of FillToolbar._generate_worker, minus the Tk parts."""
    state = client.get_document_state()
    selection = state.selection if state.has_selection else None
    canvas = client.render_canvas()

    if selection is not None:
        padded = selection.padded(padding, state.canvas)
        source = client.crop_selection(canvas, padded)
        target = selection
    else:
        source = canvas
        target = Selection(0, 0, state.width, state.height)

    result = ai.generative_fill(
        source,
        prompt,
        is_selection=selection is not None,
        context_image=canvas if selection is not None else None,
    )

    patch = result.image
    if selection is not None:
        padded = selection.padded(padding, state.canvas)
        if patch.size != source.size:
            patch = patch.resize(source.size, Image.LANCZOS)
        if source.size != (selection.width, selection.height):
            ox, oy = selection.x - padded.x, selection.y - padded.y
            patch = patch.crop((ox, oy, ox + selection.width, oy + selection.height))

    placed = client.place_in_canvas(
        patch, target, name=f"AI Fill - {prompt[:40]}", align_to_rect=selection is not None
    )
    return {"placed": placed, "source_size": source.size, "patch_size": patch.size, "selection": selection}


def main() -> int:
    port = free_port()
    server = MockPhotocraft(port=port, width=1200, height=800).start()
    tmp = tempfile.mkdtemp(prefix="aifill_pipe_")
    cfg = Config(
        api_key="stub",
        photocraft_host="127.0.0.1",
        photocraft_port=port,
        control_token=DEFAULT_TOKEN,
        connect_timeout=2.0,
        response_timeout=20.0,
        context_padding=32,
        send_full_context=True,
        temp_dir=tmp,
    )
    client = PhotocraftClient(cfg)
    ai = StubAI()

    try:
        client.connect()
        check("connected", client.connected)

        # ---------- case 1: with a selection, padded crop ----------
        server.selection = [200, 150, 400, 250]
        out = run_fill(client, ai, "add a retro neon sign", 32)

        check("selection path used", ai.calls[-1]["is_selection"] is True)
        check("full canvas sent as context", ai.calls[-1]["has_context"] is True)
        check(
            "crop padded by 32px",
            out["source_size"] == (400 + 64, 250 + 64),
            f"{out['source_size']}",
        )
        check(
            "patch trimmed back to selection",
            out["patch_size"] == (400, 250),
            f"{out['patch_size']}",
        )
        check("layer placed", bool(out["placed"].get("layer")), str(out["placed"].get("layer")))

        rec = server.placed[-1]
        check(
            "placed at exact selection origin",
            int(rec["bounds"][0]) == 200 and int(rec["bounds"][1]) == 150,
            f"bounds={rec['bounds']}",
        )
        layer = next(L for L in server.layers if L["id"] == rec["layer"])
        check("layer named from prompt", layer["name"].startswith("AI Fill - add a retro"), layer["name"])

        # ---------- case 2: no selection -> full canvas ----------
        server.selection = None
        out2 = run_fill(client, ai, "make background a sunset lake", 32)
        check("full-canvas path used", ai.calls[-1]["is_selection"] is False)
        check("no context image when full canvas", ai.calls[-1]["has_context"] is False)
        check("full canvas size", out2["source_size"] == (1200, 800), str(out2["source_size"]))
        check("full-canvas layer placed", bool(out2["placed"].get("layer")))

        # ---------- case 3: model returns wrong size ----------
        class WrongSizeAI(StubAI):
            def generative_fill(self, image, prompt, is_selection=True, *, context_image=None, max_retries=3):
                self.calls.append({"size": image.size, "prompt": prompt, "is_selection": is_selection,
                                   "has_context": context_image is not None})
                return FillResult(
                    image=Image.new("RGBA", (512, 512), (10, 200, 90, 255)),
                    model="stub-model", prompt=prompt, used_reference=True, elapsed_s=0.3,
                )

        server.selection = [50, 60, 300, 200]
        out3 = run_fill(client, WrongSizeAI(), "remove person", 0)
        check("wrong-size patch resized to selection", out3["patch_size"] == (300, 200), str(out3["patch_size"]))
        rec3 = server.placed[-1]
        check(
            "wrong-size patch still aligned",
            int(rec3["bounds"][0]) == 50 and int(rec3["bounds"][1]) == 60,
            f"bounds={rec3['bounds']}",
        )

        # ---------- cleanup ----------
        leftovers = glob.glob(str(Path(tmp) / "aifill_*.png"))
        check("no patch files left behind", not leftovers, str(leftovers))

    finally:
        client.disconnect()
        server.stop()

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        return 1
    print("ALL PIPELINE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
