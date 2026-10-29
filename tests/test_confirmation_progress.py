"""Small read-only preview and simulated-log checks. No AI/VLC calls."""
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import studio

STAMP = "[00:00:00,100 --> 00:00:01,500]"
JP = STAMP + " hello\n"
DRAFT = STAMP + "\nJP: hello\nZH_DRAFT: translated\n\n"


class ConfirmationProgressTest(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work/gui_confirmation_checks"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.task = types.SimpleNamespace(identity={"path": str(self.directory / "audio.mp3")},
            result=lambda name: self.directory / name)

    def write(self, name, text):
        (self.directory / name).write_text(text, encoding="utf-8")

    def preview(self):
        with patch.object(studio, "quick_duration", return_value="未知"):
            return studio.processing_preview(self.task)

    def test_preview_new_resumed_and_completed(self):
        info = self.preview()
        self.assertIn("开始 Whisper", info["plan"])
        self.assertEqual(info["states"]["japanese.txt"], "未生成")
        self.write("japanese.txt", JP)
        info = self.preview()
        self.assertIn("复用已有日文", info["plan"])
        self.assertIn("0 / 1", info["states"]["draft_bilingual.txt"])
        self.write("draft_bilingual.txt", DRAFT)
        self.write("final_bilingual.txt", "unchanged final")
        self.write("output.srt", "unchanged srt")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        info = self.preview()
        self.assertTrue(info["complete"])
        self.assertIn("无需重新处理", info["plan"])
        self.assertEqual(info["states"]["final_bilingual.txt"], "存在")
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()})

    def test_preview_pending_and_protected_outputs_not_complete(self):
        self.write("japanese.txt", JP)
        self.write("draft_bilingual.txt", DRAFT)
        self.write("draft_bilingual.txt.pending.json", "{}")
        info = self.preview()
        self.assertFalse(info["complete"])
        self.assertTrue(info["blocked"])
        self.assertIn("未完成", info["states"]["draft_bilingual.txt"])

    def test_duration_unknown_without_tool_and_timeout(self):
        with patch.object(studio.shutil, "which", return_value=None), patch.object(studio.subprocess, "run") as run:
            self.assertEqual(studio.quick_duration("audio.mp3"), "未知")
            run.assert_not_called()
        with patch.object(studio.shutil, "which", return_value="ffprobe"), \
                patch.object(studio.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffprobe", 2)) as run:
            self.assertEqual(studio.quick_duration("audio.mp3"), "未知")
            self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_duration_uses_metadata_not_estimated_time(self):
        with patch.object(studio.shutil, "which", return_value="ffprobe"), \
                patch.object(studio.subprocess, "run", return_value=types.SimpleNamespace(stdout="3661.2", returncode=0)):
            self.assertEqual(studio.quick_duration("audio.mp3"), "01:01:01")

    def test_launch_requires_confirmation_before_any_job(self):
        app = studio.Studio.__new__(studio.Studio)
        app.busy, app.confirming = False, False
        app.confirm_start = Mock()
        with patch.object(studio, "ProcessTree") as jobs:
            app.launch("pipeline")
        jobs.assert_not_called()
        app.confirm_start.assert_called_once()

    def test_confirmation_cancel_and_accept_callbacks(self):
        for accept in (False, True):
            app = studio.Studio.__new__(studio.Studio)
            app.root, app.task, app.update_buttons, app.launch = Mock(), self.task, Mock(), Mock()
            buttons = {}
            def button(*args, **kwargs):
                widget = Mock()
                buttons[kwargs["text"]] = (widget, kwargs["command"])
                return widget
            # Synchronous stand-in for read-only preview; no real thread/window.
            def thread(*args, **kwargs):
                return types.SimpleNamespace(start=kwargs["target"])
            info = self.preview()
            with patch.object(studio.tk, "Toplevel", return_value=Mock()), \
                    patch.object(studio.tk, "StringVar", return_value=Mock()), \
                    patch.object(studio.ttk, "Frame", return_value=Mock()), \
                    patch.object(studio.ttk, "Label", return_value=Mock()), \
                    patch.object(studio.ttk, "Button", side_effect=button), \
                    patch.object(studio.threading, "Thread", side_effect=thread), \
                    patch.object(studio, "processing_preview", return_value=info), \
                    patch.object(studio, "ProcessTree") as jobs:
                app.confirm_start()
                app.root.after.call_args.args[1]()
                buttons["确认开始" if accept else "取消"][1]()
                jobs.assert_not_called()
            self.assertFalse(app.confirming)
            if accept:
                app.launch.assert_called_once_with("pipeline", confirmed=True)
            else:
                app.launch.assert_not_called()
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_whisper_real_seconds_and_unknown_fallback(self):
        p = studio.ProcessingProgress()
        p.feed("Starting Whisper process...")
        self.assertIsNone(p.percent)
        p.feed(" 21%|██        | 25.28/119.97 [00:06<00:26, 3.63seconds/s]")
        self.assertAlmostEqual(p.percent, 25.28 / 119.97 * 100)
        self.assertIn("Whisper", p.label)
        p = studio.ProcessingProgress()
        p.feed("Starting Whisper process...")
        p.feed(" 99% arbitrary unrelated message")
        p.feed(" 99%|██| 1/120 [00:01<00:01, 1seconds/s]")
        self.assertIsNone(p.percent)

    def test_stage_transition_does_not_confuse_model_loading(self):
        p = studio.ProcessingProgress()
        p.feed("Starting Whisper process...")
        p.feed("100%|████| 119.97000000000001/119.97 [00:19<00:00, 6.02seconds/s]")
        self.assertEqual(p.percent, 100)
        p.feed("Whisper process exited.")
        self.assertIsNone(p.percent)
        p.feed("Loading Qwen 4B...")
        p.feed("Loading qwen/qwen3-4b-2507 87%")
        self.assertIsNone(p.percent)

    def test_qwen_saved_count_not_batch_start_or_elapsed_time(self):
        p = studio.ProcessingProgress()
        for text in ("Starting Qwen translation...", "共有 1221 句日文", "已经翻译 350 句", "正在翻译 351 - 360"):
            p.feed(text)
        self.assertEqual((p.done, p.total), (350, 1221))
        p.feed("这一批已保存。")
        self.assertEqual(p.done, 360)
        p.feed("这一批已保存。")
        self.assertEqual(p.done, 360)
        p.feed("正在翻译 999 - 1000")
        p.feed("这一批已保存。")
        self.assertEqual(p.done, 360)

    def test_last_saved_batch_still_waits_for_validated_finish(self):
        p = studio.ProcessingProgress()
        for text in ("Translation progress: 10/19", "正在翻译 11 - 19", "这一批已保存。", "全文粗翻完成。"):
            p.feed(text)
        self.assertEqual((p.done, p.total), (19, 19))
        self.assertNotIn("处理完成", p.label)
        self.assertEqual(studio.pipeline_outcome("error", "failed"), "处理失败（已有结果保留）")
        self.assertEqual(studio.pipeline_outcome("done", "取消完成：tree exited"), "已取消（已完成阶段保留）")
        self.assertEqual(studio.pipeline_outcome("done", "validated"), "处理完成")

    def test_render_modes_and_terminal_cancel(self):
        app = studio.Studio.__new__(studio.Studio)
        app.active_mode, app.cancelling, app.recovery_required = "pipeline", False, False
        app.pipeline_job = types.SimpleNamespace(task=self.task)
        app.pipeline_status = Mock()
        app.processing_progress = studio.ProcessingProgress()
        app._rendered_progress = None
        app.progress, app.progress_text = Mock(), Mock()
        app.render_progress()
        app.progress.configure.assert_called_with(mode="indeterminate", value=0)
        app.processing_progress.feed("Translation progress: 350/1221")
        app.render_progress()
        app.progress.configure.assert_called_with(mode="determinate", maximum=100, value=350/1221*100)
        app.finish_progress("done", "取消完成：tree exited", True)
        app.progress.configure.assert_called_with(mode="determinate", value=0)
        app.progress_text.set.assert_called_with("已取消（已完成阶段保留）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
