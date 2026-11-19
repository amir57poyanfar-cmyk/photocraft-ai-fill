"""Tests for the launcher: argument forwarding, token handling, backup logic.

The backup/restore logic is tested against a temporary folder that mimics the
PhotoCraft layout, so the real 170 MB binary is never touched.

Run:  python -m unittest -v test_launcher.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher


class TestTokenHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="launcher_test_"))
        self.config = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generates_and_persists_token(self):
        token = launcher.load_or_create_token(self.config)
        self.assertEqual(len(token), 64)
        self.assertTrue(launcher.is_valid_token(token))
        self.assertTrue(self.config.exists())
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["control_token"], token)

    def test_token_is_stable_across_calls(self):
        first = launcher.load_or_create_token(self.config)
        second = launcher.load_or_create_token(self.config)
        self.assertEqual(first, second, "the UI must see the same token")

    def test_reuses_existing_valid_token(self):
        good = "a" * 64
        self.config.write_text(json.dumps({"control_token": good}), encoding="utf-8")
        self.assertEqual(launcher.load_or_create_token(self.config), good)

    def test_replaces_invalid_token(self):
        self.config.write_text(json.dumps({"control_token": "nope"}), encoding="utf-8")
        token = launcher.load_or_create_token(self.config)
        self.assertNotEqual(token, "nope")
        self.assertTrue(launcher.is_valid_token(token))

    def test_survives_corrupt_config(self):
        self.config.write_text("{not json", encoding="utf-8")
        token = launcher.load_or_create_token(self.config)
        self.assertTrue(launcher.is_valid_token(token))

    def test_preserves_other_settings(self):
        self.config.write_text(
            json.dumps({"gemini_api_key": "keep-me", "control_token": "bad"}), encoding="utf-8"
        )
        launcher.load_or_create_token(self.config)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["gemini_api_key"], "keep-me")

    def test_token_validation(self):
        self.assertTrue(launcher.is_valid_token("0123456789abcdef" * 4))
        for bad in ("", "abc", "z" * 64, "a" * 63, "a" * 65, None):
            self.assertFalse(launcher.is_valid_token(bad), bad)


class TestCommandBuilding(unittest.TestCase):
    def test_core_command_has_control_flags(self):
        cmd = launcher.build_core_command(
            Path(r"E:\PhotoCraft\photocraft_core.exe"), "a" * 64, Path(r"E:\ai-scatch\.tmp")
        )
        self.assertEqual(cmd[0], r"E:\PhotoCraft\photocraft_core.exe")
        self.assertIn("--control", cmd)
        self.assertEqual(cmd[cmd.index("--control") + 1], "8765")
        self.assertEqual(cmd[cmd.index("--control-token") + 1], "a" * 64)
        self.assertEqual(cmd[cmd.index("--automation-read-root") + 1], r"E:\ai-scatch\.tmp")
        self.assertEqual(cmd[cmd.index("--automation-write-root") + 1], r"E:\ai-scatch\.tmp")

    def test_forwards_cli_arguments(self):
        """Double-clicking an image must still open it."""
        forwarded = [r"C:\Users\me\Pictures\photo.png"]
        cmd = launcher.build_core_command(
            Path("core.exe"), "a" * 64, Path("tmp"), forwarded=forwarded
        )
        self.assertEqual(cmd[-1], forwarded[0])
        self.assertIn(r"C:\Users\me\Pictures\photo.png", cmd)

    def test_forwards_multiple_arguments_in_order(self):
        forwarded = ["--new-window", r"C:\a.png", r"C:\b.png"]
        cmd = launcher.build_core_command(Path("core.exe"), "a" * 64, Path("tmp"), forwarded=forwarded)
        self.assertEqual(cmd[-3:], forwarded)

    def test_no_forwarded_arguments_is_fine(self):
        cmd = launcher.build_core_command(Path("core.exe"), "a" * 64, Path("tmp"))
        self.assertEqual(cmd[-1], r"tmp")

    def test_forwarded_args_come_after_control_flags(self):
        """Order matters: our flags must not be swallowed by a forwarded arg."""
        cmd = launcher.build_core_command(
            Path("core.exe"), "a" * 64, Path("tmp"), forwarded=["--control", "9999"]
        )
        self.assertLess(cmd.index("--control"), len(cmd) - 2)

    def test_ui_command_uses_pythonw_and_pid(self):
        cmd = launcher.build_ui_command(
            Path(r"E:\ai-scatch\.venv\Scripts\pythonw.exe"), Path(r"E:\ai-scatch\ui.py"), 4321
        )
        self.assertIn("pythonw.exe", cmd[0])
        self.assertEqual(cmd[cmd.index("--core-pid") + 1], "4321")
        self.assertEqual(cmd[cmd.index("--port") + 1], "8765")

    def test_custom_port_propagates(self):
        core = launcher.build_core_command(Path("core.exe"), "a" * 64, Path("tmp"), port=9001)
        ui = launcher.build_ui_command(Path("pythonw.exe"), Path("ui.py"), 1, port=9001)
        self.assertEqual(core[core.index("--control") + 1], "9001")
        self.assertEqual(ui[ui.index("--port") + 1], "9001")


class TestPatchDir(unittest.TestCase):
    def test_creates_missing_dir(self):
        tmp = Path(tempfile.mkdtemp(prefix="patchdir_"))
        try:
            target = tmp / "nested" / ".tmp"
            launcher.ensure_patch_dir(target)
            self.assertTrue(target.is_dir())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_idempotent(self):
        tmp = Path(tempfile.mkdtemp(prefix="patchdir_"))
        try:
            launcher.ensure_patch_dir(tmp)
            launcher.ensure_patch_dir(tmp)
            self.assertTrue(tmp.is_dir())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBackupLogic(unittest.TestCase):
    """The rename/backup rules, exercised on a fake PhotoCraft folder.

    install.bat performs the rename, but the *rules* it must obey are encoded
    here so a regression in the sequence is caught without touching the real
    binary.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup_test_"))
        self.photo = self.tmp / "PhotoCraft"
        self.photo.mkdir()
        self.exe = self.photo / "photocraft.exe"
        self.core = self.photo / "photocraft_core.exe"
        self.original = self.photo / "photocraft_original.exe"
        self.exe.write_bytes(b"ORIGINAL-BINARY" * 100)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self) -> None:
        """Mirror of install.bat step 1."""
        if self.core.exists():
            return  # idempotent: never overwrite
        if not self.exe.exists():
            raise FileNotFoundError("no binary to back up")
        if not self.original.exists():
            shutil.copy2(self.exe, self.original)
        self.exe.rename(self.core)

    def test_first_install_renames_and_backs_up(self):
        self.install()
        self.assertTrue(self.core.exists())
        self.assertFalse(self.exe.exists())
        self.assertTrue(self.original.exists())
        self.assertEqual(self.core.read_bytes(), b"ORIGINAL-BINARY" * 100)

    def test_second_install_is_a_no_op(self):
        self.install()
        before = self.core.read_bytes()
        self.install()  # must not touch anything
        self.assertEqual(self.core.read_bytes(), before)
        self.assertFalse(self.exe.exists(), "must not recreate photocraft.exe")

    def test_repeated_install_never_overwrites_original(self):
        self.install()
        # Simulate a launcher having been installed in the meantime.
        self.exe.write_bytes(b"LAUNCHER")
        self.install()
        self.assertEqual(self.core.read_bytes(), b"ORIGINAL-BINARY" * 100)
        self.assertEqual(self.original.read_bytes(), b"ORIGINAL-BINARY" * 100)

    def test_uninstall_restores_original(self):
        self.install()
        self.exe.write_bytes(b"LAUNCHER")  # the compiled launcher
        # Mirror of uninstall.bat step 2.
        if self.core.exists():
            if self.exe.exists():
                self.exe.unlink()
            self.core.rename(self.exe)
        self.assertTrue(self.exe.exists())
        self.assertFalse(self.core.exists())
        self.assertEqual(self.exe.read_bytes(), b"ORIGINAL-BINARY" * 100)

    def test_install_without_any_binary_fails_loudly(self):
        self.exe.unlink()
        with self.assertRaises(FileNotFoundError):
            self.install()

    def test_install_preserves_existing_backup(self):
        self.original.write_bytes(b"OLDER-BACKUP")
        self.install()
        self.assertEqual(self.original.read_bytes(), b"OLDER-BACKUP", "backup must not be clobbered")


class TestProcessHelpers(unittest.TestCase):
    def test_pid_alive_for_self(self):
        import os

        self.assertTrue(launcher.pid_alive(os.getpid()))

    def test_pid_alive_for_bogus_pid(self):
        self.assertFalse(launcher.pid_alive(0))
        self.assertFalse(launcher.pid_alive(-1))

    def test_terminate_handles_none(self):
        launcher.terminate(None)  # must not raise

    def test_terminate_finished_process(self):
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        launcher.terminate(proc)  # must not raise


class TestFrozenPaths(unittest.TestCase):
    """Under --onefile, __file__ lives in a temp dir; paths must not follow it."""

    def test_source_run_uses_file_location(self):
        import launcher as mod

        self.assertEqual(mod._resolve_app_dir(), Path(mod.__file__).resolve().parent)

    def test_frozen_run_uses_executable_location(self):
        import launcher as mod

        fake_exe = Path(r"E:\PhotoCraft\photocraft.exe")
        with mock.patch.object(mod.sys, "frozen", True, create=True), \
             mock.patch.object(mod.sys, "executable", str(fake_exe)):
            self.assertEqual(mod._resolve_app_dir(), Path(r"E:\ai-scatch"))

    def test_frozen_app_dir_is_not_the_meipass_temp(self):
        import launcher as mod

        fake_exe = Path(r"E:\PhotoCraft\photocraft.exe")
        with mock.patch.object(mod.sys, "frozen", True, create=True), \
             mock.patch.object(mod.sys, "executable", str(fake_exe)):
            resolved = mod._resolve_app_dir()
        self.assertNotIn("_MEI", str(resolved))
        self.assertEqual(resolved.name, "ai-scatch")


class TestSingleInstance(unittest.TestCase):
    """PhotoCraft is single-instance; a second launch must not spawn a 2nd core."""

    def test_find_running_core_parses_tasklist(self):
        fake = 'INFO: No tasks are running\n"photocraft_core.exe","4321","Console","1","120,000 K"\n'
        with mock.patch.object(launcher.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=fake, returncode=0)
            self.assertEqual(launcher.find_running_core(), 4321)

    def test_find_running_core_returns_none_when_absent(self):
        with mock.patch.object(launcher.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="INFO: No tasks are running\n", returncode=0)
            self.assertIsNone(launcher.find_running_core())

    def test_find_running_core_survives_tasklist_failure(self):
        with mock.patch.object(launcher.subprocess, "run", side_effect=OSError("boom")):
            self.assertIsNone(launcher.find_running_core())

    def test_open_in_running_instance_uses_control_server(self):
        """A second core would open its own window, so go through the socket."""
        with mock.patch.object(launcher, "_rpc", return_value={"ok": True}) as rpc, \
             mock.patch.object(launcher, "load_or_create_token", return_value="a" * 64):
            code = launcher.open_in_running_instance([r"C:\a.png"])
        self.assertEqual(code, 0)
        rpc.assert_called_once()
        self.assertEqual(rpc.call_args[0][3], "app.open")
        self.assertEqual(rpc.call_args[0][4], {"path": r"C:\a.png"})

    def test_falls_back_to_doc_open_on_cli_server(self):
        """The headless CLI server names it doc.open instead."""
        calls = []

        def fake_rpc(host, port, token, method, params=None):
            calls.append(method)
            if method == "app.open":
                raise OSError("unknown method `app.open`")
            return {}

        with mock.patch.object(launcher, "_rpc", side_effect=fake_rpc), \
             mock.patch.object(launcher, "load_or_create_token", return_value="a" * 64):
            code = launcher.open_in_running_instance([r"C:\a.png"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["app.open", "doc.open"])

    def test_does_not_fall_back_on_a_real_error(self):
        """A path outside the read root is not a name mismatch; do not retry."""
        calls = []

        def fake_rpc(host, port, token, method, params=None):
            calls.append(method)
            raise OSError("path is outside the automation read root")

        with mock.patch.object(launcher, "_rpc", side_effect=fake_rpc), \
             mock.patch.object(launcher, "load_or_create_token", return_value="a" * 64):
            code = launcher.open_in_running_instance([r"C:\outside.png"])
        self.assertEqual(code, 6)
        self.assertEqual(calls, ["app.open"])

    def test_open_in_running_instance_opens_every_file(self):
        with mock.patch.object(launcher, "_rpc", return_value={}) as rpc, \
             mock.patch.object(launcher, "load_or_create_token", return_value="a" * 64):
            launcher.open_in_running_instance([r"C:\a.png", r"C:\b.png"])
        self.assertEqual(rpc.call_count, 2)

    def test_existing_instance_short_circuits_before_starting_core(self):
        tmp = Path(tempfile.mkdtemp(prefix="single_"))
        try:
            core = tmp / "photocraft_core.exe"
            core.write_bytes(b"x")
            with mock.patch.object(launcher, "APP_DIR", tmp), \
                 mock.patch.object(launcher, "resolve_paths", return_value=(core, tmp / "photocraft.exe")), \
                 mock.patch.object(launcher, "find_running_core", return_value=999), \
                 mock.patch.object(launcher, "open_in_running_instance", return_value=0) as fwd, \
                 mock.patch.object(launcher.subprocess, "Popen") as popen:
                code = launcher.main([r"C:\a.png"])
            self.assertEqual(code, 0)
            fwd.assert_called_once()
            popen.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_existing_instance_without_files_exits_quietly(self):
        tmp = Path(tempfile.mkdtemp(prefix="single_"))
        try:
            core = tmp / "photocraft_core.exe"
            core.write_bytes(b"x")
            with mock.patch.object(launcher, "APP_DIR", tmp), \
                 mock.patch.object(launcher, "resolve_paths", return_value=(core, tmp / "photocraft.exe")), \
                 mock.patch.object(launcher, "find_running_core", return_value=999), \
                 mock.patch.object(launcher.subprocess, "Popen") as popen:
                code = launcher.main([])
            self.assertEqual(code, 0)
            popen.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMainGuards(unittest.TestCase):
    def test_missing_core_reports_error(self):
        with mock.patch.object(launcher, "resolve_paths", return_value=(Path("nope.exe"), Path("x"))), \
             mock.patch.object(launcher, "show_error") as box:
            code = launcher.main([])
        self.assertEqual(code, 2)
        box.assert_called_once()

    def test_missing_ui_reports_error(self):
        tmp = Path(tempfile.mkdtemp(prefix="main_test_"))
        try:
            core = tmp / "photocraft_core.exe"
            core.write_bytes(b"x")
            with mock.patch.object(launcher, "APP_DIR", tmp), \
                 mock.patch.object(launcher, "resolve_paths", return_value=(core, tmp / "photocraft.exe")), \
                 mock.patch.object(launcher, "find_running_core", return_value=None), \
                 mock.patch.object(launcher, "show_error") as box:
                code = launcher.main([])
            self.assertEqual(code, 3)
            box.assert_called_once()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
