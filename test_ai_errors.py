"""Error-path checks for the AI service (no valid key needed).

Every check here is offline: none of them reaches a provider. What they pin is
the contract the engine promises: a missing key says so in one clear sentence,
a bad prompt is rejected before any request, and a bogus key produces a clean
error rather than a raw traceback.
"""

from PIL import Image

from ai_service import (
    AIError,
    AIService,
    MissingAPIKeyError,
    NO_KEY_MESSAGE,
    build_fill_prompt,
)
from config import Config

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {label}{(' :: ' + detail) if detail else ''}")


# 1. Nothing configured: the exact message the user asked for.
svc = AIService(Config())
try:
    svc.generative_fill(Image.new("RGB", (8, 8)), "add a hat")
    check("missing key raises", False, "no exception")
except MissingAPIKeyError as exc:
    check("missing key raises MissingAPIKeyError", True, str(exc)[:60])
    check("message is the specified one", str(exc) == NO_KEY_MESSAGE, str(exc))
except AIError as exc:
    check("missing key raises MissingAPIKeyError", False, f"got AIError: {exc}")

# 2. Empty prompt is rejected before any network call.
svc2 = AIService(Config(api_key="fake-key-for-testing", base_url="https://relay.test/v1"))
try:
    svc2.generative_fill(Image.new("RGB", (8, 8)), "   ")
    check("empty prompt rejected", False, "no exception")
except AIError as exc:
    check("empty prompt rejected", "empty" in str(exc).lower(), str(exc)[:50])

# 3. None image is rejected.
try:
    svc2.generative_fill(None, "add a hat")  # type: ignore[arg-type]
    check("none image rejected", False, "no exception")
except AIError as exc:
    check("none image rejected", True, str(exc)[:50])

# 4. available flag reflects configuration, and there is no keyless mode.
check("available False without key", AIService(Config()).available is False)
check("available True with key", svc2.available is True)
check(
    "available True with endpoint only",
    AIService(Config(base_url="http://localhost:1234/v1")).available is True,
)
check("no keyless free mode", Config().is_free_mode is False)

# 5. Prompt construction differs for selection vs full canvas.
sel_prompt = build_fill_prompt("add sunglasses", is_selection=True)
full_prompt = build_fill_prompt("add sunglasses", is_selection=False)
check("selection prompt mentions crop", "crop" in sel_prompt.lower())
check("selection prompt asks for same dimensions", "same pixel dimensions" in sel_prompt.lower())
check("full prompt asks for full image", "full edited image" in full_prompt.lower())
check("prompts differ", sel_prompt != full_prompt)
check("prompt carries the instruction", "add sunglasses" in sel_prompt and "add sunglasses" in full_prompt)

# 6. Context note appears only when a context image is supplied.
noted = build_fill_prompt("x", is_selection=True, context_note="A second image is context.")
check("context note included", "A second image is context." in noted)
check("context note absent by default", "A second image is context." not in sel_prompt)

# 7. A Persian prompt is passed through logically, with a language rule.
persian = "متن های داخلش رو پاک کن"
persian_prompt = build_fill_prompt(persian, is_selection=True)
check("persian prompt kept logical", persian in persian_prompt)
check("persian rule added", "Persian" in persian_prompt)
check("english prompt has no persian rule", "Persian" not in sel_prompt)

failed = [label for label, ok, _ in results if not ok]
print()
print("ALL AI CHECKS PASSED" if not failed else f"FAILED: {failed}")
raise SystemExit(1 if failed else 0)
