"""Windows window tracking for the PhotoCraft companion.

Two layers, deliberately separated:

* **Pure geometry** (``Rect``, ``clamp_inside``, ``bottom_center``, ...) with no
  ctypes and no Windows calls, so the positioning maths is unit-testable on any
  machine.
* **Thin ctypes bindings** (``find_main_window``, ``get_window_rect``, ...) that
  only gather facts and hand them to the pure layer.

Nothing here raises on a non-Windows host: the bindings degrade to ``None`` and
the caller falls back to "no tracking".
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from typing import Iterable

try:
    import ctypes.wintypes as wintypes
except Exception:  # pragma: no cover - non-Windows
    wintypes = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------- modes
HIDDEN = "hidden"
PILL = "pill"
BAR = "bar"


# ------------------------------------------------------------------ geometry
@dataclass(frozen=True)
class Rect:
    """A rectangle in physical screen pixels."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center_x(self) -> int:
        return self.left + self.width // 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    @classmethod
    def from_tuple(cls, values: Iterable[int]) -> "Rect | None":
        try:
            left, top, right, bottom = (int(v) for v in values)
        except (TypeError, ValueError):
            return None
        if right <= left or bottom <= top:
            return None
        return cls(left, top, right, bottom)


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp, tolerating an inverted range (window smaller than the widget)."""
    if high < low:
        return low
    return max(low, min(value, high))


def clamp_inside(window: Rect, width: int, height: int, margin: int = 12) -> tuple[int, int]:
    """Centre ``width``x``height`` horizontally in ``window``, pinned to its bottom.

    The result is always clamped inside the window with ``margin`` to spare; if
    the window is smaller than the widget the widget is pinned to the top-left
    inset instead of being pushed off-screen.
    """
    x = window.left + (window.width - width) // 2
    y = window.bottom - height - margin

    x = _clamp(x, window.left + margin, window.right - width - margin)
    y = _clamp(y, window.top + margin, window.bottom - height - margin)
    return x, y


def bottom_center(window: Rect, width: int, height: int, margin: int = 18) -> tuple[int, int]:
    """Where the contextual task bar sits: bottom-centre of the PhotoCraft window."""
    return clamp_inside(window, width, height, margin)


def corner_pill(window: Rect, width: int, height: int, margin: int = 14) -> tuple[int, int]:
    """Where the collapsed pill sits: bottom-right, out of the way of the canvas."""
    x = window.right - width - margin
    y = window.bottom - height - margin
    x = _clamp(x, window.left + margin, window.right - width - margin)
    y = _clamp(y, window.top + margin, window.bottom - height - margin)
    return x, y


def anchor_for(mode: str, window: Rect, width: int, height: int) -> tuple[int, int]:
    """Pick the anchor point for a given mode."""
    if mode == PILL:
        return corner_pill(window, width, height)
    return bottom_center(window, width, height)


def decide_mode(
    *,
    photo_running: bool,
    photo_minimized: bool,
    photo_foreground: bool,
    our_foreground: bool,
    settings_open: bool,
    typing: bool,
    auto_popup: bool,
    has_selection: bool,
    user_expanded: bool = False,
) -> str:
    """The contextual-visibility state machine.

    Firefly behaviour: the bar appears when there is something to act on, and
    gets out of the way otherwise. The escape hatches are the ones a user would
    be furious to lose: typing a prompt, an open settings dialog, and a bar the
    user opened by hand.

    ``user_expanded`` is sticky. Once someone clicks the pill or the gear, the
    bar stays a bar until they collapse it themselves. Auto-collapsing a bar
    someone just opened is how the previous version made the gear unclickable:
    the poll ran, saw no selection, and folded the bar out from under the
    cursor. Only minimizing PhotoCraft or switching to an unrelated window
    takes it off screen, and it comes back as a bar, not a pill.
    """
    if not photo_running:
        return HIDDEN

    if user_expanded:
        if photo_minimized:
            return HIDDEN
        if not (photo_foreground or our_foreground):
            return HIDDEN
        return BAR

    # Never yank the window away from someone who is using it.
    if settings_open or typing:
        return BAR
    if photo_minimized:
        return HIDDEN
    if not (photo_foreground or our_foreground):
        return HIDDEN
    if auto_popup and has_selection:
        return BAR
    return PILL


# ------------------------------------------------------------------- ctypes
if IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    GW_OWNER = 4
    SW_SHOWNOACTIVATE = 4
    SW_HIDE = 0
else:  # pragma: no cover
    _user32 = None
    _kernel32 = None
    _WNDENUMPROC = None
    GW_OWNER = 4
    SW_SHOWNOACTIVATE = 4
    SW_HIDE = 0


def set_dpi_aware() -> None:
    """Make this process DPI aware so GetWindowRect and Tk agree on pixels.

    Without this, Windows virtualises coordinates for the process and every
    computed position is off by the display scale factor.
    """
    if not IS_WINDOWS:
        return
    try:  # Windows 10 1703+
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:  # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:  # Vista+
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        log.debug("Could not set DPI awareness", exc_info=True)


def get_window_pid(hwnd: int) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    pid = ctypes.c_ulong(0)
    try:
        _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value)


def foreground_is_one_of(pids: Iterable[int]) -> bool:
    """True when the foreground window belongs to any of ``pids``.

    Comparing handles only works while the toolbar is the exact window that
    has focus. The moment the Settings dialog opens it becomes a *different*
    top-level window in the same process, the handle comparison fails, and the
    old code read that as "the user left PhotoCraft" and hid the bar. Matching
    on process id instead means every window we own counts as ours.
    """
    wanted = {int(p) for p in pids if p}
    if not wanted:
        return False
    hwnd = get_foreground_window()
    if not hwnd:
        return False
    return get_window_pid(hwnd) in wanted


def get_window_rect(hwnd: int) -> Rect | None:
    if not IS_WINDOWS or not hwnd or wintypes is None:
        return None
    rect = wintypes.RECT()
    try:
        if not _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return None
    except Exception:
        return None
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def is_window_visible(hwnd: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(_user32.IsWindowVisible(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


def is_minimized(hwnd: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(_user32.IsIconic(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


def get_foreground_window() -> int:
    if not IS_WINDOWS:
        return 0
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def find_main_window(pid: int) -> int | None:
    """The largest visible, unowned top-level window belonging to ``pid``.

    Splash screens and tool palettes are owned or small, so the largest
    candidate is reliably the main PhotoCraft window.
    """
    if not IS_WINDOWS or not pid:
        return None

    best: int | None = None
    best_area = 0

    def callback(hwnd, _lparam):  # pragma: no cover - called by Windows
        nonlocal best, best_area
        try:
            if not is_window_visible(hwnd):
                return True
            if _user32.GetWindow(ctypes.c_void_p(hwnd), GW_OWNER):
                return True
            if get_window_pid(hwnd) != pid:
                return True
            rect = get_window_rect(hwnd)
            if rect is None or rect.width < 200 or rect.height < 150:
                return True
            if rect.area > best_area:
                best, best_area = int(hwnd), rect.area
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    except Exception:
        log.debug("EnumWindows failed", exc_info=True)
        return None
    return best


def show_without_activating(hwnd: int) -> None:
    """Show a window without stealing focus from PhotoCraft."""
    if not IS_WINDOWS or not hwnd:
        return
    try:
        _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)
    except Exception:
        log.debug("ShowWindow failed", exc_info=True)


def hide_window(hwnd: int) -> None:
    if not IS_WINDOWS or not hwnd:
        return
    try:
        _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)
    except Exception:
        log.debug("ShowWindow hide failed", exc_info=True)


def pid_is_alive(pid: int) -> bool:
    """True when ``pid`` is a live process."""
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def find_pid_by_name(names: Iterable[str]) -> int | None:
    """Locate a running process by executable name (case-insensitive)."""
    if not IS_WINDOWS:
        return None
    wanted = {n.lower() for n in names}
    try:
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        log.debug("tasklist failed", exc_info=True)
        return None
    for line in out.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        image = parts[0].strip('"').lower()
        if image in wanted:
            try:
                return int(parts[1].strip('"'))
            except ValueError:
                continue
    return None


def toplevel_hwnd(widget_id: int) -> int:
    """Tk's ``winfo_id`` is the child HWND; walk up to the real top-level."""
    if not IS_WINDOWS or not widget_id:
        return 0
    try:
        parent = _user32.GetParent(ctypes.c_void_p(widget_id))
        return int(parent or widget_id)
    except Exception:
        return int(widget_id)
