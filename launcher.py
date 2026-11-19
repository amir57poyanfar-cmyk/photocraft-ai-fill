"""PhotoCraft launcher: starts the core app plus the Generative Fill companion.

This file is compiled to ``E:\\PhotoCraft\\photocraft.exe`` by ``install.bat``,
so it is what every normal launch path hits: the Desktop shortcut, the Start
Menu entry, double-clicking the exe, or opening an image file.

What it does
------------
1. Ensures a 64-hex control token exists and persists it (shared with ``ui.py``
   through ``config.json``, so both sides always agree).
2. Ensures the patch temp directory exists.
3. Starts ``photocraft_core.exe`` with the control server enabled and forwards
   every argument it received, so "open with" still works.
4. Starts ``ui.py`` via ``pythonw.exe`` with no console, passing the core PID.
5. Waits for the core to exit, then closes the companion and exits.

It is a console-less binary: nothing here may write to stdout/stderr, so every
failure goes to a log file and, where it matters, to a message box.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

def _resolve_app_dir() -> Path:
    """Where the companion project lives (``E:\\ai-scatch``).

    Under PyInstaller ``--onefile`` the script is unpacked into a temp folder
    (``%TEMP%\\_MEIxxxx``), so ``__file__`` points there and would send every
    path lookup to the wrong place. ``sys.executable`` is the real binary, which
    install.bat places in ``E:\\PhotoCraft``, so the app dir is its sibling.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "ai-scatch"
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()
PHOTOCRAFT_DIR = APP_DIR.parent / "PhotoCraft"

CORE_EXE_NAME = "photocraft_core.exe"
LAUNCHER_EXE_NAME = "photocraft.exe"
DEFAULT_PORT = 8765

LOG_PATH = APP_DIR / "launcher.log"

#: How long to wait for the core to open its control port before starting the UI
#: anyway. The UI reconnects on its own, so this is a nicety, not a requirement.
PORT_WAIT_S = 20.0
POLL_S = 0.25


# --------------------------------------------------------------------- logging
def setup_logging() -> None:
    try:
        logging.basicConfig(
            filename=str(LOG_PATH),
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            filemode="a",
        )
    except Exception:
        logging.disable(logging.CRITICAL)


def log(message: str, *args: Any) -> None:
    try:
        logging.info(message, *args)
    except Exception:
        pass


# ------------------------------------------------------------------- messages
def show_error(title: str, message: str) -> None:
    """A console-less binary has no other way to report a fatal problem."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


# -------------------------------------------------------------------- helpers
def resolve_paths(app_dir: Path | None = None) -> tuple[Path, Path]:
    """Return ``(core_exe, launcher_exe)`` for the given app directory."""
    base = Path(app_dir) if app_dir else APP_DIR
    photo_dir = base.parent / "PhotoCraft"
    return photo_dir / CORE_EXE_NAME, photo_dir / LAUNCHER_EXE_NAME


def is_valid_token(token: str) -> bool:
    if not token or len(token) != 64:
        return False
    try:
        int(token, 16)
    except ValueError:
        return False
    return True


def new_token() -> str:
    import secrets

    return secrets.token_hex(32)


def load_or_create_token(config_path: Path) -> str:
    """Read the token from ``config.json``, creating and persisting one if absent.

    The UI reads the same file, so a token generated here is the token the UI
    authenticates with. That is the whole reason this is shared state and not
    an environment variable.
    """
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            log("config.json unreadable; regenerating token")

    token = str(data.get("control_token") or "").strip()
    if is_valid_token(token):
        return token

    token = new_token()
    data["control_token"] = token
    data.setdefault("photocraft_host", "127.0.0.1")
    data.setdefault("photocraft_port", DEFAULT_PORT)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log("Generated new control token and wrote %s", config_path)
    except OSError as exc:
        log("Could not persist token: %s", exc)
    return token


def ensure_patch_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_core_command(
    core_exe: Path,
    token: str,
    patch_dir: Path,
    port: int = DEFAULT_PORT,
    forwarded: Sequence[str] = (),
) -> list[str]:
    """The exact command line handed to ``photocraft_core.exe``.

    The forwarded arguments go last so an image path is treated as a file to
    open, exactly as it would be by the original binary.
    """
    command = [
        str(core_exe),
        "--control",
        str(port),
        "--control-token",
        token,
        "--automation-read-root",
        str(patch_dir),
        "--automation-write-root",
        str(patch_dir),
    ]
    command.extend(str(a) for a in forwarded)
    return command


def build_ui_command(pythonw: Path, ui_script: Path, core_pid: int, port: int = DEFAULT_PORT) -> list[str]:
    """The command line for the companion, run under ``pythonw.exe`` (no console)."""
    return [
        str(pythonw),
        str(ui_script),
        "--core-pid",
        str(core_pid),
        "--port",
        str(port),
    ]


def wait_for_port(host: str, port: int, timeout: float = PORT_WAIT_S) -> bool:
    """Wait until the control port accepts a connection."""
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(POLL_S)
    return False


def pid_alive(pid: int) -> bool:
    """True while ``pid`` is running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate(process: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """Close a child process politely, then firmly."""
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout)
        return
    except Exception:
        pass
    try:
        process.kill()
    except Exception:
        pass


def find_running_core() -> int | None:
    """PID of an already-running ``photocraft_core.exe``, if any.

    PhotoCraft is single-instance: a second launch hands its files to the
    running app as new tabs and exits. Without this check the launcher would
    start a second core that dies instantly, leaving a duplicate toolbar behind.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {CORE_EXE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        log("tasklist failed while looking for a running core")
        return None
    for line in result.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].strip('"').lower() == CORE_EXE_NAME.lower():
            try:
                return int(parts[1].strip('"'))
            except ValueError:
                continue
    return None


def _rpc(host: str, port: int, token: str, method: str, params: dict[str, Any] | None = None) -> Any:
    """One authenticated JSON-RPC call, without pulling in the full client."""
    import socket

    with socket.create_connection((host, port), timeout=10.0) as sock:
        stream = sock.makefile("rwb")

        def send(request_id: int, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
            payload = {"id": request_id, "method": name, "params": args or {}}
            stream.write((json.dumps(payload) + "\n").encode("utf-8"))
            stream.flush()
            line = stream.readline()
            if not line:
                raise OSError("server closed the connection")
            return json.loads(line.decode("utf-8"))

        send(1, "auth", {"token": token})
        reply = send(2, method, params)
    if not reply.get("ok"):
        raise OSError(str(reply.get("error") or "request rejected"))
    return reply.get("result")


def open_in_running_instance(files: Sequence[str], port: int = DEFAULT_PORT) -> int:
    """Ask the PhotoCraft already running to open ``files``, then exit.

    Spawning a second core does not work here: a running instance started with
    ``--control`` stops honouring PhotoCraft's own single-instance handoff, so a
    child would open its own window and hang. Going through the control server
    keeps one window and one toolbar.

    The server only reads inside its ``--automation-read-root``, so a file
    outside it is refused; that is reported rather than silently ignored.
    """
    token = load_or_create_token(APP_DIR / "config.json")
    opened: list[str] = []
    for path in files:
        # The desktop control server exposes app.open; the headless CLI server
        # exposes doc.open instead. Try both so either host works.
        last_error = "no method accepted"
        for method in ("app.open", "doc.open"):
            try:
                _rpc("127.0.0.1", port, token, method, {"path": str(path)})
                opened.append(str(path))
                log("Opened in the running instance via %s: %s", method, path)
                last_error = ""
                break
            except OSError as exc:
                last_error = str(exc)
                if "unknown method" not in last_error:
                    break
        if last_error:
            log("Could not open %s in the running instance: %s", path, last_error)
    return 0 if opened else 6


# ----------------------------------------------------------------------- main
def main(argv: Sequence[str] | None = None) -> int:
    setup_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    log("Launcher started; forwarded args: %s", args)

    core_exe, _launcher_exe = resolve_paths()
    if not core_exe.is_file():
        message = (
            f"PhotoCraft core not found:\n{core_exe}\n\n"
            "Run install.bat first, or restore photocraft_core.exe to photocraft.exe."
        )
        log(message)
        show_error("PhotoCraft", message)
        return 2

    config_path = APP_DIR / "config.json"
    token = load_or_create_token(config_path)
    patch_dir = ensure_patch_dir(APP_DIR / ".tmp")

    # PhotoCraft is single-instance: if it is already open, hand the files over
    # and exit rather than starting a second core and a duplicate toolbar.
    running = find_running_core()
    if running is not None:
        log("Core already running, pid=%s", running)
        if args:
            return open_in_running_instance(args)
        log("No files to forward; nothing to do")
        return 0

    pythonw = APP_DIR / ".venv" / "Scripts" / "pythonw.exe"
    ui_script = APP_DIR / "ui.py"
    if not pythonw.is_file() or not ui_script.is_file():
        message = (
            "The Generative Fill companion is not installed correctly.\n\n"
            f"Missing: {pythonw if not pythonw.is_file() else ui_script}\n\n"
            "Run install.bat in E:\\ai-scatch."
        )
        log(message)
        show_error("PhotoCraft", message)
        return 3

    command = build_core_command(core_exe, token, patch_dir, DEFAULT_PORT, args)
    log("Starting core: %s", " ".join(command[:6]) + " ...")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        core = subprocess.Popen(command, cwd=str(core_exe.parent), creationflags=creationflags)
    except OSError as exc:
        message = f"Could not start PhotoCraft:\n{exc}"
        log(message)
        show_error("PhotoCraft", message)
        return 4

    log("Core started, pid=%s", core.pid)

    ui: subprocess.Popen | None = None
    try:
        # Give the core a moment to open its control port so the toolbar's first
        # connect succeeds instead of showing a red dot for a second.
        ready = wait_for_port("127.0.0.1", DEFAULT_PORT)
        log("Control port ready: %s", ready)

        ui_command = build_ui_command(pythonw, ui_script, core.pid, DEFAULT_PORT)
        log("Starting companion: %s", " ".join(ui_command))
        try:
            ui = subprocess.Popen(
                ui_command,
                cwd=str(APP_DIR),
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            # The app is still usable without the companion; say so and carry on.
            log("Companion failed to start: %s", exc)
            show_error(
                "PhotoCraft",
                f"PhotoCraft started, but the Generative Fill toolbar could not launch:\n{exc}",
            )

        # Block until the user closes PhotoCraft.
        core.wait()
        log("Core exited with code %s", core.returncode)
    finally:
        terminate(ui)
        log("Launcher exiting")

    return 0


if __name__ == "__main__":
    sys.exit(main())
