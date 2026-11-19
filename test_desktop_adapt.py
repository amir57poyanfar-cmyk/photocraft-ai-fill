"""Verify the adaptive client against the live desktop GUI server.

The desktop build answers no ``doc.*`` method. This checks the fallbacks work
end to end against the real ``photocraft_core.exe``:

  * ``get_document_state()`` falls back to ``document.inspect`` and returns
    real selection bounds;
  * ``render_canvas()`` falls back to ``edit.copyMerged`` + clipboard and
    returns the selection region;
  * ``place_in_canvas()`` falls back to ``edit.paste`` and the layer lands at
    the exact selection origin.

Launch the app first (``photocraft.exe``), open a document, then run:

    python test_desktop_adapt.py <token> [port]
"""

import sys

sys.path.insert(0, r"E:\ai-scatch")

from PIL import Image

import clipboard_image
from config import Config
from photocraft_client import NoDocumentError, PhotocraftClient, RemoteError

TOKEN = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8765

cfg = Config(
    photocraft_host="127.0.0.1",
    photocraft_port=PORT,
    control_token=TOKEN,
    connect_timeout=5.0,
    response_timeout=30.0,
    temp_dir=r"E:\ai-scatch\.tmp",
)
client = PhotocraftClient(cfg)
client.connect()
print("connected:", client.connected)

failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


print("\n=== state falls back to the engine command ===")
try:
    state = client.get_document_state()
    print(f"    canvas={state.width}x{state.height} hasSelection={state.has_selection}")
    print(f"    selection={state.selection}")
    check("canvas size read", state.width > 0 and state.height > 0, f"{state.width}x{state.height}")
except NoDocumentError:
    print("    no document open - open one in PhotoCraft and re-run")
    sys.exit(2)
except RemoteError as exc:
    check("get_document_state", False, f"{exc.method}: {exc.message}")
    sys.exit(1)

print("\n=== capture falls back to edit.copyMerged ===")
try:
    client.run_command("select.rect", {"x": 100, "y": 50, "width": 300, "height": 200})
    state = client.get_document_state()
    check("selection drawn", state.has_selection and state.selection is not None, str(state.selection))
    if state.selection:
        check("bounds match the marquee",
              (state.selection.x, state.selection.y) == (100, 50), str(state.selection))

    image = client.render_canvas()
    print(f"    render_canvas -> {image.size} {image.mode}")
    check("capture returned an image", image is not None)
    if state.selection:
        check("capture is the selection region",
              image.size == (state.selection.width, state.selection.height),
              f"{image.size} vs {(state.selection.width, state.selection.height)}")
except RemoteError as exc:
    check("render_canvas", False, f"{exc.method}: {exc.message}")

print("\n=== place falls back to edit.paste ===")
try:
    patch = Image.new("RGB", (300, 200), (255, 0, 0))
    placed = client.place_in_canvas(patch, state.selection, name="AI Fill - adapt test")
    print(f"    placed: {placed}")
    check("place created a layer", placed.get("layer") is not None, str(placed.get("layer")))
    check("place used the clipboard path", placed.get("via") == "clipboard", str(placed.get("via")))

    after = client.get_document_state()
    target = next((l for l in (after.layers or []) if l.get("id") == placed.get("layer")), None)
    print(f"    layer now: {target}")
    if target and state.selection:
        bounds = target.get("bounds") or []
        check("layer landed at the selection origin",
              len(bounds) == 4 and bounds[0] == state.selection.x and bounds[1] == state.selection.y,
              str(bounds))
    check("clipboard cleared afterwards", clipboard_image.read_image() is None)
except RemoteError as exc:
    check("place_in_canvas", False, f"{exc.method}: {exc.message}")

client.disconnect()
print("\n" + ("ALL DESKTOP ADAPTATION CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
sys.exit(0 if not failures else 1)
