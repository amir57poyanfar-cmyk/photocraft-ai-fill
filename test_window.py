"""Tests for window-tracking maths and the contextual state machine.

Everything here is pure: no ctypes, no Windows, no Tk. That is deliberate, so
the positioning logic can be verified on any machine.

Run:  python -m unittest -v test_window.py
"""

from __future__ import annotations

import unittest

import winwindow
from winwindow import BAR, HIDDEN, PILL, Rect


class TestRect(unittest.TestCase):
    def test_dimensions(self):
        r = Rect(100, 50, 400, 250)
        self.assertEqual(r.width, 300)
        self.assertEqual(r.height, 200)
        self.assertEqual(r.area, 60000)
        self.assertEqual(r.center_x, 250)
        self.assertEqual(r.as_tuple(), (100, 50, 400, 250))

    def test_from_tuple_valid(self):
        self.assertEqual(Rect.from_tuple([10, 20, 110, 220]), Rect(10, 20, 110, 220))

    def test_from_tuple_rejects_degenerate(self):
        for bad in ([10, 20, 10, 220], [10, 20, 110, 20], [10, 20, 5, 5]):
            self.assertIsNone(Rect.from_tuple(bad), bad)

    def test_from_tuple_rejects_garbage(self):
        self.assertIsNone(Rect.from_tuple(["a", "b", "c", "d"]))
        self.assertIsNone(Rect.from_tuple([1, 2]))

    def test_negative_origin_is_allowed(self):
        """A window on a second monitor to the left has negative coordinates."""
        r = Rect(-1920, 100, 0, 1100)
        self.assertEqual(r.width, 1920)


class TestClampInside(unittest.TestCase):
    def setUp(self):
        self.window = Rect(100, 100, 1700, 1100)  # 1600x1000

    def test_bottom_centre_is_centred(self):
        x, y = winwindow.bottom_center(self.window, 640, 80)
        self.assertEqual(x, 100 + (1600 - 640) // 2)
        self.assertEqual(y, 1100 - 80 - 18)

    def test_bar_stays_inside_window(self):
        x, y = winwindow.bottom_center(self.window, 640, 80)
        self.assertGreaterEqual(x, self.window.left)
        self.assertLessEqual(x + 640, self.window.right)
        self.assertGreaterEqual(y, self.window.top)
        self.assertLessEqual(y + 80, self.window.bottom)

    def test_wide_bar_is_clamped_not_centred_off_screen(self):
        """A bar wider than the window must be pinned, not overflow both sides."""
        x, _ = winwindow.clamp_inside(Rect(0, 0, 500, 400), 640, 80, margin=12)
        self.assertEqual(x, 12)

    def test_tall_widget_is_clamped_to_top(self):
        _, y = winwindow.clamp_inside(Rect(0, 0, 800, 60), 640, 80, margin=12)
        self.assertEqual(y, 12)

    def test_margin_respected(self):
        x, y = winwindow.clamp_inside(Rect(0, 0, 700, 500), 690, 480, margin=20)
        self.assertGreaterEqual(x, 20)
        self.assertGreaterEqual(y, 20)

    def test_negative_coordinate_window(self):
        """Multi-monitor setups put windows at negative x; maths must still work."""
        window = Rect(-1920, 0, 0, 1080)
        x, y = winwindow.bottom_center(window, 640, 80)
        self.assertGreaterEqual(x, -1920)
        self.assertLessEqual(x + 640, 0)
        self.assertLessEqual(y + 80, 1080)

    def test_exact_fit(self):
        window = Rect(0, 0, 640, 80)
        x, y = winwindow.clamp_inside(window, 640, 80, margin=0)
        self.assertEqual((x, y), (0, 0))


class TestCornerPill(unittest.TestCase):
    def test_sits_bottom_right(self):
        window = Rect(100, 100, 1700, 1100)
        x, y = winwindow.corner_pill(window, 104, 40, margin=14)
        self.assertEqual(x, 1700 - 104 - 14)
        self.assertEqual(y, 1100 - 40 - 14)

    def test_never_leaves_window(self):
        window = Rect(0, 0, 300, 200)
        x, y = winwindow.corner_pill(window, 104, 40)
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x + 104, 300)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + 40, 200)

    def test_pill_and_bar_differ(self):
        window = Rect(0, 0, 1600, 1000)
        pill = winwindow.corner_pill(window, 104, 40)
        bar = winwindow.bottom_center(window, 104, 40)
        self.assertNotEqual(pill, bar, "the pill must be out of the bar's way")


class TestAnchorFor(unittest.TestCase):
    def test_pill_mode_uses_corner(self):
        window = Rect(0, 0, 1600, 1000)
        self.assertEqual(
            winwindow.anchor_for(PILL, window, 104, 40),
            winwindow.corner_pill(window, 104, 40),
        )

    def test_bar_mode_uses_bottom_centre(self):
        window = Rect(0, 0, 1600, 1000)
        self.assertEqual(
            winwindow.anchor_for(BAR, window, 640, 80),
            winwindow.bottom_center(window, 640, 80),
        )


class TestDecideMode(unittest.TestCase):
    """The contextual visibility rules, one case per rule."""

    def decide(self, **overrides):
        base = dict(
            photo_running=True,
            photo_minimized=False,
            photo_foreground=True,
            our_foreground=False,
            settings_open=False,
            typing=False,
            auto_popup=True,
            has_selection=False,
        )
        base.update(overrides)
        return winwindow.decide_mode(**base)

    def test_selection_pops_the_bar(self):
        self.assertEqual(self.decide(has_selection=True), BAR)

    def test_no_selection_collapses_to_pill(self):
        self.assertEqual(self.decide(has_selection=False), PILL)

    def test_auto_popup_off_keeps_pill_even_with_selection(self):
        self.assertEqual(self.decide(has_selection=True, auto_popup=False), PILL)

    def test_minimized_hides(self):
        self.assertEqual(self.decide(photo_minimized=True, has_selection=True), HIDDEN)

    def test_other_app_in_front_hides(self):
        self.assertEqual(
            self.decide(photo_foreground=False, our_foreground=False, has_selection=True),
            HIDDEN,
        )

    def test_our_own_window_focused_stays_visible(self):
        """Clicking the prompt must not make the bar vanish under the cursor."""
        self.assertEqual(
            self.decide(photo_foreground=False, our_foreground=True, has_selection=True),
            BAR,
        )

    def test_typing_keeps_bar_even_when_focus_moves(self):
        self.assertEqual(
            self.decide(photo_foreground=False, our_foreground=False, typing=True),
            BAR,
        )

    def test_typing_beats_minimized(self):
        self.assertEqual(self.decide(photo_minimized=True, typing=True), BAR)

    def test_settings_open_keeps_bar(self):
        self.assertEqual(
            self.decide(photo_foreground=False, our_foreground=False, settings_open=True),
            BAR,
        )

    def test_settings_open_beats_minimized(self):
        self.assertEqual(self.decide(photo_minimized=True, settings_open=True), BAR)

    # -- sticky manual expand: the bug that made the gear unclickable --------
    def test_manual_expand_survives_losing_the_selection(self):
        """Clicking the pill then having no selection must not fold the bar."""
        self.assertEqual(self.decide(user_expanded=True, has_selection=False), BAR)

    def test_manual_expand_survives_auto_popup_being_off(self):
        self.assertEqual(
            self.decide(user_expanded=True, has_selection=False, auto_popup=False), BAR
        )

    def test_manual_expand_survives_focus_moving_to_our_own_dialog(self):
        """The Settings dialog is a second window; focus there is still ours."""
        self.assertEqual(
            self.decide(user_expanded=True, photo_foreground=False, our_foreground=True),
            BAR,
        )

    def test_manual_expand_still_hides_when_photocraft_is_minimized(self):
        self.assertEqual(self.decide(user_expanded=True, photo_minimized=True), HIDDEN)

    def test_manual_expand_still_hides_for_an_unrelated_app(self):
        self.assertEqual(
            self.decide(user_expanded=True, photo_foreground=False, our_foreground=False),
            HIDDEN,
        )

    def test_manual_expand_comes_back_as_a_bar_not_a_pill(self):
        """Hide then return: the user's choice is remembered."""
        hidden = self.decide(user_expanded=True, photo_foreground=False, our_foreground=False)
        self.assertEqual(hidden, HIDDEN)
        self.assertEqual(self.decide(user_expanded=True), BAR)

    def test_collapsing_returns_to_pill_behaviour(self):
        """user_expanded=False restores the automatic rules exactly."""
        self.assertEqual(self.decide(user_expanded=False, has_selection=False), PILL)
        self.assertEqual(self.decide(user_expanded=False, has_selection=True), BAR)

    def test_manual_expand_does_not_resurrect_a_dead_photocraft(self):
        self.assertEqual(self.decide(user_expanded=True, photo_running=False), HIDDEN)

    def test_photo_not_running_hides(self):
        self.assertEqual(self.decide(photo_running=False, has_selection=True), HIDDEN)

    def test_photo_not_running_beats_everything(self):
        self.assertEqual(
            self.decide(photo_running=False, typing=True, settings_open=True), HIDDEN
        )


class TestForegroundOwner(unittest.TestCase):
    """``foreground_is_one_of`` is what keeps the bar alive while Settings is up."""

    def test_empty_pid_list_is_false(self):
        self.assertFalse(winwindow.foreground_is_one_of([]))

    def test_zero_pids_are_ignored(self):
        self.assertFalse(winwindow.foreground_is_one_of([0, 0]))

    def test_matches_the_process_that_owns_the_foreground_window(self):
        """Validates the GetWindowThreadProcessId wiring end to end."""
        if not winwindow.IS_WINDOWS:
            self.skipTest("Windows only")
        hwnd = winwindow.get_foreground_window()
        if not hwnd:
            self.skipTest("no foreground window in this session")
        owner = winwindow.get_window_pid(hwnd)
        self.assertTrue(owner)
        self.assertTrue(winwindow.foreground_is_one_of([owner]))

    def test_console_process_does_not_own_the_foreground_window(self):
        """A background process must not be mistaken for the focused app."""
        if not winwindow.IS_WINDOWS:
            self.skipTest("Windows only")
        import os

        hwnd = winwindow.get_foreground_window()
        if not hwnd:
            self.skipTest("no foreground window in this session")
        if winwindow.get_window_pid(hwnd) == os.getpid():
            self.skipTest("this process happens to own the foreground")
        self.assertFalse(winwindow.foreground_is_one_of([os.getpid()]))

    def test_unrelated_pid_does_not_match(self):
        if not winwindow.IS_WINDOWS:
            self.skipTest("Windows only")
        self.assertFalse(winwindow.foreground_is_one_of([999999]))


class TestNonWindowsSafety(unittest.TestCase):
    """The ctypes layer must degrade quietly instead of raising."""

    def test_helpers_return_none_off_windows(self):
        if winwindow.IS_WINDOWS:
            self.skipTest("Windows host: bindings are live")
        self.assertIsNone(winwindow.find_main_window(1234))
        self.assertIsNone(winwindow.get_window_rect(1))
        self.assertEqual(winwindow.get_foreground_window(), 0)
        self.assertFalse(winwindow.is_window_visible(1))
        self.assertFalse(winwindow.is_minimized(1))

    def test_set_dpi_aware_never_raises(self):
        winwindow.set_dpi_aware()


class TestLiveBindings(unittest.TestCase):
    """On Windows, the real bindings must answer for a real window."""

    def test_own_window_is_findable(self):
        if not winwindow.IS_WINDOWS:
            self.skipTest("Windows only")
        import os

        pid = os.getpid()
        self.assertTrue(winwindow.pid_is_alive(pid))
        self.assertNotEqual(winwindow.get_foreground_window(), 0)

    def test_find_pid_by_name_returns_none_for_missing(self):
        if not winwindow.IS_WINDOWS:
            self.skipTest("Windows only")
        self.assertIsNone(winwindow.find_pid_by_name(["definitely_not_running_xyz.exe"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
