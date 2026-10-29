"""GUI integration checks with synthetic files; no models, VLC or GUI windows."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock, call

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import studio
import task_store as store

STAMP = "[00:00:00,123 --> 00:00:01,456]"
JP = STAMP + " こんにちは。\n"
DRAFT = STAMP + "\nJP: こんにちは。\nZH_DRAFT: 你好。\n\n"
FINAL = STAMP + "\nJP: こんにちは。\nZH_FINAL: 你好。\n\n"


class StudioTest(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work" / "gui_checks"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.audio = Path(self.temp.name) / "日文 & test.mp3"
        self.audio.write_bytes(b"synthetic-only-no-decoding")
        self.task = store.open_task(self.audio)

    def put(self, name, text):
        path = self.task.result(name)
        path.write_text(text, encoding="utf-8")
        return path

    def snapshot(self):
        return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in self.task.directory.iterdir() if p.is_file()}

    def test_import_has_no_business_startup(self):
        result = subprocess.run([sys.executable, "-B", "-c",
            "import studio, sys, tkinter; assert tkinter._default_root is None; "
            "assert not any(n in sys.modules for n in ('vlc','faster_whisper','translate','make_srt','floating_subtitle_player'))"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_valid_final_and_invalid_shapes(self):
        path = self.put("final_bilingual.txt", FINAL)
        self.assertEqual(studio.validate_final(path), 1)
        for text in ("", FINAL.replace("你好。", ""), FINAL.replace("JP:", "BAD:"),
                     FINAL.replace("00:00:01,456", "00:00:00,001"), FINAL.replace("00:00:00,123", "00:70:00,123")):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(store.TaskError):
                studio.validate_final(path)

    def test_test_final_rejected_before_converter(self):
        self.put("final_bilingual.txt", FINAL.replace("你好。", "【端到端测试字幕，非正式精修】你好。"))
        with patch.object(studio.runpy, "run_path") as runner:
            with self.assertRaises(store.TaskError):
                studio.worker("srt", self.audio)
            runner.assert_not_called()
        states, _, final_ok, _ = studio.inspect_task(self.task)
        self.assertFalse(final_ok)
        self.assertIn("测试", states["final_bilingual.txt"])

    def test_complete_task_does_not_invoke_pipeline(self):
        self.put("japanese.txt", JP)
        self.put("draft_bilingual.txt", DRAFT)
        before = self.snapshot()
        with patch("run_pipeline.main") as pipeline:
            studio.worker("pipeline", self.audio)
            pipeline.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_legacy_early_return_is_not_success(self):
        with patch("run_pipeline.main") as pipeline:
            with self.assertRaises(store.TaskError):
                studio.worker("pipeline", self.audio)
            pipeline.assert_called_once_with(self.audio, offer_legacy=False)

    def test_real_srt_cli_requires_fresh_overwrite_confirmation(self):
        from make_srt import prepare_srt
        self.put("final_bilingual.txt", FINAL)
        command = studio.worker_command("srt", self.audio) + ["--srt-plan", json.dumps(prepare_srt(self.task))]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task.result("output.srt").read_text(encoding="utf-8"), "1\n00:00:00,123 --> 00:00:01,456\n你好。\n\n")
        before = self.snapshot()
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.snapshot())

    def test_player_dispatch_uses_selected_audio(self):
        self.put("output.srt", "1\n00:00:00,123 --> 00:00:01,456\n你好。\n")
        captured = []
        with patch.object(sys, "argv", ["studio.py"]), patch.object(studio.runpy, "run_path", side_effect=lambda *a, **kw: captured.append((a, list(sys.argv)))):
            studio.worker("player", self.audio)
        self.assertEqual(captured[0][0][0], str(ROOT / "floating_subtitle_player.py"))
        self.assertEqual(captured[0][1][1], str(self.audio))

    def test_test_srt_is_labelled_not_formal(self):
        self.put("output.srt", "【端到端测试字幕，非正式精修】")
        states, _, _, playable = studio.inspect_task(self.task)
        self.assertTrue(playable)
        self.assertIn("非正式", states["output.srt"])

    def test_command_keeps_unicode_path_one_argument(self):
        command = studio.worker_command("pipeline", self.audio)
        self.assertEqual(command[-1], str(self.audio))
        self.assertEqual(command[-3:-1], ["--worker", "pipeline"])
        with patch.object(sys, "frozen", True, create=True):
            helper = Path(sys.executable).with_name("EarlySakuraWorker.exe")
            with patch.object(studio.runtime, "worker_executable", return_value=helper):
                self.assertEqual(studio.worker_command("pipeline", self.audio),
                                 [str(helper), "--worker", "pipeline", str(self.audio)])

    def test_changed_entries_parse(self):
        for name in ("studio.py", "run_pipeline.py", "make_srt.py", "floating_subtitle_player.py"):
            ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)


class SmallUIChangesTest(unittest.TestCase):
    """No Tk window, processes, audio reads, or task-file writes."""

    def test_ui_audio_picker_always_starts_at_asmr(self):
        app = studio.Studio.__new__(studio.Studio)
        app.busy = app.exiting = False
        app.root = object()
        app.task = Mock()
        app.background = Mock()
        with patch.object(studio.filedialog, "askopenfilename", return_value="") as picker:
            app.select_audio()
            app.task = Mock()  # A different previous task must not affect the dialog.
            app.select_audio()
        self.assertEqual(picker.call_count, 2)
        for invocation in picker.call_args_list:
            self.assertEqual(invocation.kwargs["initialdir"], studio.runtime.audio_directory())
        app.background.assert_not_called()

    def test_ui_clear_log_changes_only_display(self):
        app = studio.Studio.__new__(studio.Studio)
        app.busy = False
        app.task, app.flags, app.status = object(), (True, True, True), object()
        app.log = Mock()
        before = vars(app).copy()
        with patch("builtins.open", side_effect=AssertionError("No disk access")), \
                patch.object(Path, "unlink", side_effect=AssertionError("No file deletion")):
            app.clear_log()
        self.assertEqual(vars(app), before)
        self.assertEqual(app.log.method_calls, [call.configure(state="normal"),
                         call.delete("1.0", "end"), call.configure(state="disabled")])

    def test_ui_clear_log_refuses_while_busy(self):
        app = studio.Studio.__new__(studio.Studio)
        app.busy = True
        app.log = Mock()
        app.clear_log()
        self.assertEqual(app.log.method_calls, [])

    def test_ui_clear_button_tracks_idle_even_without_task(self):
        app = studio.Studio.__new__(studio.Studio)
        app.task = None
        button = Mock()
        app.buttons = {"clear_log": button}
        for busy, expected in ((False, "normal"), (True, "disabled"), (False, "normal")):
            app.busy = busy
            app.update_buttons()
            button.configure.assert_called_with(state=expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
