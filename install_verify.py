"""Post-install verification, called by install.bat.

Checks the things a silent failure would otherwise hide: the core binary is in
place, the launcher replaced the original, the token is valid and shared with
the UI, the temp dir exists, and the companion's dependencies import.

Exit code 0 when everything passes, 1 when a check fails.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PHOTO_DIR = APP_DIR.parent / "PhotoCraft"

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"      {'OK  ' if ok else 'FAIL'}  {label}{(' :: ' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


def main() -> int:
    core = PHOTO_DIR / "photocraft_core.exe"
    launcher = PHOTO_DIR / "photocraft.exe"
    config = APP_DIR / "config.json"
    pythonw = APP_DIR / ".venv" / "Scripts" / "pythonw.exe"

    check("photocraft_core.exe present", core.is_file(), str(core))
    check("launcher installed as photocraft.exe", launcher.is_file(), str(launcher))
    # The launcher is a small PyInstaller stub; the core is the 170 MB original.
    if launcher.is_file() and core.is_file():
        check(
            "launcher replaced the original",
            launcher.stat().st_size < core.stat().st_size,
            f"{launcher.stat().st_size:,} vs {core.stat().st_size:,} bytes",
        )
    else:
        check("launcher replaced the original", False, "cannot compare until installed")
    check("pythonw.exe present", pythonw.is_file(), str(pythonw))
    check("ui.py present", (APP_DIR / "ui.py").is_file())
    check("patch temp dir present", (APP_DIR / ".tmp").is_dir())

    token = ""
    if config.is_file():
        try:
            token = str(json.loads(config.read_text(encoding="utf-8")).get("control_token") or "")
        except ValueError:
            token = ""
    valid = len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token)
    check("control token is 64-hex", valid, token[:12] + "..." if token else "missing")

    if pythonw.is_file():
        result = subprocess.run(
            [str(pythonw), "-c", "import customtkinter, PIL, dotenv, google.genai"],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        check("companion dependencies import", result.returncode == 0, result.stderr.strip()[:200])

    print()
    if problems:
        print(f"      {len(problems)} check(s) failed: {', '.join(problems)}")
        return 1
    print("      All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
