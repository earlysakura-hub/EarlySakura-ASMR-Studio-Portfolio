"""Real independent Windows Job Objects and harmless fixtures; no AI or VLC."""
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import studio
import task_store as store
from playback_history import HistoryStore


class ParallelTest(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work" / "parallel_checks"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.tasks = []
        for name in ("A", "B", "C"):
            audio = self.directory / (name + ".wav")
            audio.write_bytes(b"synthetic-" + name.encode())
            task = store.open_task(audio)
            if name != "B":
                store.write_new(task.result("output.srt"), b"1\n00:00:00,000 --> 00:00:01,000\nfixture\n")
            self.tasks.append(task)
        self.history = HistoryStore(self.directory / "history.sqlite3")
        self.behavior = {}
        def command(mode, audio, gated=False):
            return [sys.executable, "-B", "-u", "-X", "utf8", str(ROOT / "tests/parallel_worker_fixture.py"),
                    mode, str(audio), "--database", str(self.history.database),
                    "--behavior", self.behavior.get(mode, "normal")]
        for target, options in (("worker_command", {"side_effect": command}), ("ROOT", {"new": self.directory}),
                                ("messagebox.showerror", {}), ("messagebox.showinfo", {}),
                                ("messagebox.askyesno", {"return_value": True})):
            patcher = patch("studio." + target, **options)
            patcher.start(); self.addCleanup(patcher.stop)
        self.app = app = studio.Studio.__new__(studio.Studio)
        app.root = Mock()
        app.task = self.tasks[0]
        app.flags = (False, False, True)
        app.busy = app.confirming = app.exiting = app.cancelling = app.recovery_required = app.close_after_cancel = False
        app.pipeline_job = app.player_job = app.pending_player_task = app.tree = app.active_mode = None
        app.events = queue.Queue()
        app.processing_progress = studio.ProcessingProgress()
        app._rendered_progress = None
        main_thread = threading.get_ident()
        def main_only(*args, **kwargs):
            self.assertEqual(threading.get_ident(), main_thread, "GUI mutation from worker")
        for key in ("status", "player_status", "pipeline_status", "progress_text", "audio_name", "audio_path"):
            value = Mock(); value.set.side_effect = main_only; setattr(app, key, value)
        app.progress = Mock()
        app.progress.configure.side_effect = main_only
        app.file_states = {name: Mock() for name in store.RESULT_NAMES}
        app.buttons = {key: Mock() for key in ("select", "pipeline", "cancel", "refresh", "folder", "import", "srt", "player", "stop_player", "history", "clear_log")}
        app.append_log = Mock(side_effect=main_only)
        self.addCleanup(self.cleanup_runs)

    def pump_until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.drain()
            if predicate(): return
            time.sleep(0.03)
        self.fail("parallel fixture timed out: " + str(self.app.append_log.call_args_list[-5:]))

    def cleanup_runs(self):
        self.app.exiting = False
        self.app.pending_player_task = None
        if self.app.pipeline_job: self.app.cancel()
        if self.app.player_job: self.app.stop_player()
        self.pump_until(lambda: self.app.pipeline_job is None and self.app.player_job is None)

    def select(self, task):
        with patch.object(studio.filedialog, "askopenfilename", return_value=task.identity["path"]):
            self.app.select_audio()
        self.pump_until(lambda: not self.app.busy)

    def wait_player(self):
        self.pump_until(lambda: self.history.resume_info(self.app.player_job.task.identity["task_id"]) is not None)

    def wait_pipeline(self):
        self.pump_until(lambda: self.app.pipeline_job is not None and
                        any("FIXTURE_READY" in str(call) for call in self.app.append_log.call_args_list))

    def test_play_then_translate_selection_does_not_retarget_job(self):
        app = self.app
        app.launch("player"); self.wait_player()
        player = app.player_job
        original = self.tasks[0].result("output.srt").read_bytes()
        self.select(self.tasks[1])
        app.launch("pipeline", confirmed=True)
        self.assertFalse(app.busy)
        self.select(self.tasks[0])
        self.pump_until(lambda: app.pipeline_job is None)
        self.assertIs(app.player_job, player)
        self.assertIsNone(player.process.poll())
        self.assertEqual(app.task.identity["task_id"], self.tasks[0].identity["task_id"])
        self.assertEqual(store.check_draft_progress(self.tasks[1].result("japanese.txt"), self.tasks[1].result("draft_bilingual.txt")), (1, 1))
        self.assertEqual(self.tasks[0].result("output.srt").read_bytes(), original)
        self.assertEqual([row["work_id"] for row in self.history.list_entries()], [self.tasks[0].identity["task_id"]])

    def test_translate_then_play_cancel_isolated_and_second_pipeline_blocked(self):
        app = self.app
        self.behavior["pipeline"] = "wait"
        self.select(self.tasks[1]); app.launch("pipeline", confirmed=True); self.wait_pipeline()
        pipeline = app.pipeline_job
        self.select(self.tasks[0]); app.launch("player"); self.wait_player()
        player = app.player_job
        app.launch("pipeline", confirmed=True)
        self.assertIs(app.pipeline_job, pipeline)
        app.cancel()
        self.pump_until(lambda: app.pipeline_job is None)
        self.assertTrue(pipeline.tree.closed)
        self.assertIs(app.player_job, player)
        self.assertIsNone(player.process.poll())
        with store.task_lock(self.tasks[1].directory): pass
        self.assertTrue(list(self.tasks[1].directory.glob("japanese.txt.partial-*")))

    def test_switch_player_saves_progress_and_keeps_pipeline_target(self):
        app = self.app
        self.behavior["pipeline"] = "wait"
        self.select(self.tasks[1]); app.launch("pipeline", confirmed=True); self.wait_pipeline()
        pipeline = app.pipeline_job
        self.select(self.tasks[0]); app.launch("player"); self.wait_player()
        old = app.player_job
        self.select(self.tasks[2]); app.launch("player")
        self.pump_until(lambda: app.player_job is not None and app.player_job is not old)
        self.wait_player()
        self.assertTrue(old.tree.closed)
        self.assertIs(app.pipeline_job, pipeline)
        self.assertIsNone(pipeline.process.poll())
        for index in (0, 2):
            self.assertGreater(self.history.resume_info(self.tasks[index].identity["task_id"])["position_ms"], 0)

    def test_pipeline_errors_never_close_player_and_preserve_safe_outputs(self):
        app = self.app
        app.launch("player"); self.wait_player()
        player = app.player_job
        self.behavior["pipeline"] = "fail-whisper"
        self.select(self.tasks[1]); app.launch("pipeline", confirmed=True)
        self.pump_until(lambda: app.pipeline_job is None)
        self.assertIsNone(player.process.poll())
        self.assertFalse(self.tasks[1].result("japanese.txt").exists())
        Path(self.tasks[1].identity["path"]).unlink()  # This test's synthetic input only.
        app.launch("pipeline", confirmed=True)
        self.pump_until(lambda: app.pipeline_job is None)
        self.assertIsNone(player.process.poll())
        # A new isolated input, no formal subtitle, for the failed Qwen stage.
        audio = self.directory / "failed-Qwen.wav"; audio.write_bytes(b"failed-Qwen")
        task = store.open_task(audio)
        self.behavior["pipeline"] = "fail-qwen"
        self.select(task); app.launch("pipeline", confirmed=True)
        self.pump_until(lambda: app.pipeline_job is None)
        self.assertTrue(task.result("japanese.txt").exists())
        self.assertTrue((task.directory / "draft_bilingual.txt.pending.json").exists())
        self.assertIs(app.player_job, player)
        self.assertIsNone(player.process.poll())

    def test_player_error_does_not_cancel_pipeline(self):
        app = self.app
        self.behavior["pipeline"] = "wait"
        self.select(self.tasks[1]); app.launch("pipeline", confirmed=True); self.wait_pipeline()
        pipeline = app.pipeline_job
        self.behavior["player"] = "fail-player"
        self.select(self.tasks[0]); app.launch("player")
        self.pump_until(lambda: app.player_job is None)
        self.assertIs(app.pipeline_job, pipeline)
        self.assertIsNone(pipeline.process.poll())
        self.assertFalse(pipeline.tree.cancel_requested.is_set())

    def test_shutdown_waits_for_both_owned_jobs_and_preserves_progress(self):
        app = self.app
        self.behavior["pipeline"] = "wait"
        self.select(self.tasks[1]); app.launch("pipeline", confirmed=True); self.wait_pipeline()
        pipeline = app.pipeline_job
        self.select(self.tasks[0]); app.launch("player"); self.wait_player()
        player = app.player_job
        with patch.object(studio.messagebox, "askyesno", return_value=False): app.close()
        self.assertFalse(app.exiting)
        app.close()
        app.root.destroy.assert_not_called()
        self.pump_until(lambda: app.pipeline_job is None and app.player_job is None)
        self.assertTrue(pipeline.tree.closed and player.tree.closed)
        app.root.destroy.assert_called_once()
        self.assertIsNotNone(self.history.resume_info(self.tasks[0].identity["task_id"]))

    def test_button_states_same_work_guard_and_late_events(self):
        app = self.app
        pipeline = studio.ActiveJob("pipeline", self.tasks[1], Mock())
        player = studio.ActiveJob("player", self.tasks[0], Mock())
        app.pipeline_job, app.player_job = pipeline, player
        app.update_buttons()
        for key in ("select", "history", "refresh", "stop_player"):
            app.buttons[key].configure.assert_called_with(state="normal")
        for key in ("pipeline", "import", "srt", "clear_log"):
            app.buttons[key].configure.assert_called_with(state="disabled")
        self.assertTrue(app.task_in_use(self.tasks[0]))
        stale = studio.ActiveJob("pipeline", self.tasks[2], Mock())
        app.consume_job_event("job_error", stale, "late failure")
        self.assertIs(app.pipeline_job, pipeline)
        self.assertIs(app.player_job, player)
        stage = app.processing_progress.stage
        app.consume_job_event("job_line", player, "Starting Qwen translation...\n")
        self.assertEqual(app.processing_progress.stage, stage)
        app.pipeline_job = app.player_job = None  # No processes were started here.

    def test_unconfirmed_cancel_keeps_own_slot_and_other_player_running(self):
        app = self.app
        pipeline = studio.ActiveJob("pipeline", self.tasks[1], Mock())
        player = studio.ActiveJob("player", self.tasks[0], Mock())
        app.pipeline_job, app.player_job = pipeline, player
        app.exiting = True
        app.consume_job_event("job_unsafe", pipeline, "simulated cleanup failure")
        self.assertFalse(app.exiting)
        self.assertTrue(pipeline.recovery_required)
        self.assertIs(app.pipeline_job, pipeline)
        self.assertIs(app.player_job, player)
        player.tree.cancel.assert_not_called()
        app.root.destroy.assert_not_called()
        app.buttons["cancel"].configure.assert_called_with(state="normal")
        app.pipeline_job = app.player_job = None

    def test_gui_player_worker_enables_control_without_changing_audio(self):
        captured = []
        with patch.object(sys, "argv", ["fixture"]), patch.object(studio.runpy, "run_path", side_effect=lambda *args, **kwargs: captured.append(list(sys.argv))):
            studio.worker("player", self.tasks[0].identity["path"], player_control=True)
        self.assertEqual(captured[0][1:], [self.tasks[0].identity["path"], "--studio-control"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
