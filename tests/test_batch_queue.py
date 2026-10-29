"""Synthetic queue + real owned Windows worker processes; never run AI/VLC."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import batch_queue as batches
import studio
import task_store as store
import test_parallel as helpers

JP = "[00:00:00,000 --> 00:00:01,000] テスト。\n"
DRAFT = "[00:00:00,000 --> 00:00:01,000]\nJP: テスト。\nZH_DRAFT: 模拟翻译。\n\n"


class QueueStorageTest(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work" / "batch_checks"
        work.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.queue = batches.BatchQueue(self.folder / "queue.json")
        self.addCleanup(self.queue.close)
        self.tasks = []
        for name in ("A", "B"):
            folder = self.folder / name
            folder.mkdir()
            audio = folder / "same.wav"
            audio.write_bytes(b"same bytes")
            self.tasks.append(store.open_task(audio))

    def test_dedupe_identity_order_and_same_name_isolation(self):
        for task in self.tasks: self.assertTrue(self.queue.add(task))
        self.assertFalse(self.queue.add(self.tasks[0]))
        self.assertEqual(len(self.queue.entries), 2)
        self.assertNotEqual(*[entry["task_id"] for entry in self.queue.entries])
        self.assertEqual(self.queue.pending()["task_id"], self.tasks[0].identity["task_id"])
        self.queue.entries[0]["state"] = "completed"
        self.assertEqual(self.queue.pending()["task_id"], self.tasks[1].identity["task_id"])

    def test_persistence_completed_failed_pending_interrupted(self):
        self.queue.add(self.tasks[0]); self.queue.add(self.tasks[1])
        self.queue.entries[0].update(state="completed", stage="完成")
        self.queue.entries[1].update(state="translating", stage="Qwen")
        self.queue.save(); before = self.queue.path.read_bytes(); self.queue.close()
        reopened = batches.BatchQueue(self.queue.path)
        self.addCleanup(reopened.close)
        self.assertEqual([entry["state"] for entry in reopened.entries], ["completed", "interrupted"])
        self.assertIsNone(reopened.pending())
        self.assertEqual(reopened.path.read_bytes(), before)  # Opening never auto-processes/writes.
        reopened.entries[1].update(state="failed", detail="saved error"); reopened.save(); reopened.close()
        again = batches.BatchQueue(self.queue.path); self.addCleanup(again.close)
        self.assertEqual(again.entries[1]["detail"], "saved error")
        self.assertEqual(again.entries[1]["state"], "failed")

    def test_atomic_failure_keeps_old_metadata(self):
        self.queue.add(self.tasks[0]); self.queue.save()
        before = self.queue.path.read_bytes()
        self.queue.add(self.tasks[1])
        with patch.object(batches.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.queue.save()
        self.assertEqual(self.queue.path.read_bytes(), before)
        self.assertEqual(list(self.folder.glob("queue.json.tmp-*")), [])

    def test_second_writer_and_corrupt_data_do_not_reset(self):
        with self.assertRaises(batches.QueueError): batches.BatchQueue(self.queue.path)
        self.queue.close(); self.queue.path.write_bytes(b"{broken")
        with self.assertRaises(batches.QueueError): batches.BatchQueue(self.queue.path)
        self.assertEqual(self.queue.path.read_bytes(), b"{broken")

    def test_preflight_complete_partial_pending_missing_and_changed_audio(self):
        task = self.tasks[0]; self.queue.add(task); entry = self.queue.entries[0]
        self.assertFalse(batches.check_entry(entry)[1])
        store.write_new(task.result("japanese.txt"), JP.encode())
        self.assertEqual(batches.check_entry(entry)[2], "Qwen")
        store.write_new(task.result("draft_bilingual.txt"), DRAFT.encode())
        self.assertTrue(batches.check_entry(entry)[1])
        marker = task.directory / "draft_bilingual.txt.pending.json"
        marker.write_text("{}")
        with self.assertRaises(store.TaskError): batches.check_entry(entry)
        audio = Path(self.tasks[1].identity["path"]); self.queue.add(self.tasks[1]); other = self.queue.entries[1]
        audio.write_bytes(b"changed")
        with self.assertRaises(store.TaskError): batches.check_entry(other)
        with patch("run_pipeline.main") as pipeline:
            with self.assertRaises(store.TaskError): studio.worker("pipeline", audio, expected_task=other["task_id"])
            pipeline.assert_not_called()
        audio.unlink()  # This test's own disposable synthetic input only.
        with self.assertRaises(store.TaskError): batches.check_entry(other)

    def test_existing_formal_results_and_legacy_ownership_are_protected(self):
        task = self.tasks[0]; self.queue.add(task)
        task.result("output.srt").write_bytes(b"formal srt")
        original = task.result("output.srt").read_bytes()
        with self.assertRaises(store.TaskError): batches.check_entry(self.queue.entries[0])
        self.assertEqual(task.result("output.srt").read_bytes(), original)
        other = self.tasks[1]; self.queue.add(other)
        legacy = Path(other.identity["path"]).parent / "japanese.txt"
        legacy.write_text(JP, encoding="utf-8")
        with self.assertRaises(store.TaskError): batches.check_entry(self.queue.entries[1])
        self.assertFalse(other.result("japanese.txt").exists())

    def test_global_error_signals(self):
        for text in ("LM Studio CLI not found.", "CUDA out of memory", "WinError 112",
                     "HTTPConnectionPool: Connection refused", "Command ['lms.exe', 'load'] returned non-zero exit status 1"):
            self.assertTrue(batches.global_failure(text), text)
        self.assertFalse(batches.global_failure("Audio file not found"))

    def test_worker_rechecks_identity_after_task_preparation(self):
        with patch.object(store, "prepare_audio_task", return_value=self.tasks[1]), patch("run_pipeline.main") as pipeline:
            with self.assertRaises(store.TaskError):
                studio.worker("pipeline", self.tasks[0].identity["path"], expected_task=self.tasks[0].identity["task_id"])
            pipeline.assert_not_called()

    def clear_app(self):
        app = studio.Studio.__new__(studio.Studio)
        app.batch = self.queue
        app.busy = app.exiting = app.confirming = app.batch_running = app.batch_checking = False
        app.batch_error = ""
        app.pipeline_job = app.batch_current = app.batch_target = None
        app.player_job = object()  # Playback does not prevent clearing idle queue metadata.
        app.batch_generation = app.batch_failures = 0
        app.batch_notice = ""
        app.root = Mock()
        app.batch_window = None
        app.update_buttons = Mock()
        app.render_batch = Mock()
        app.append_log = Mock()
        return app

    def test_clear_confirmed_persists_and_preserves_all_work_files(self):
        for task in self.tasks:
            self.queue.add(task)
            for name in store.RESULT_NAMES:
                task.result(name).write_bytes(f"keep original {name}".encode())
        self.queue.entries[0]["state"] = "completed"
        self.queue.entries[1]["state"] = "cancelled"
        self.queue.save()
        protected = {path: path.read_bytes() for path in self.folder.rglob("*")
                     if path.is_file() and path not in (self.queue.path, self.queue.path.with_suffix(".lock"))}
        app = self.clear_app()
        player = app.player_job
        with patch("studio.messagebox.askyesno", return_value=True):
            app.clear_batch()
        self.assertEqual(self.queue.entries, [])
        self.assertIs(app.player_job, player)
        for path, content in protected.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.queue.close()
        reopened = batches.BatchQueue(self.queue.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.entries, [])

    def test_clear_declined_keeps_memory_and_disk(self):
        self.queue.add(self.tasks[0]); self.queue.save()
        original = self.queue.entries
        before = self.queue.path.read_bytes()
        app = self.clear_app()
        with patch("studio.messagebox.askyesno", return_value=False):
            app.clear_batch()
        self.assertIs(self.queue.entries, original)
        self.assertEqual(self.queue.path.read_bytes(), before)
        self.assertFalse(app.confirming)

    def test_clear_save_failure_rolls_back_and_keeps_original_metadata(self):
        self.queue.add(self.tasks[0]); self.queue.save()
        original = self.queue.entries
        before = self.queue.path.read_bytes()
        app = self.clear_app()
        with patch("studio.messagebox.askyesno", return_value=True), \
                patch("studio.messagebox.showerror"), \
                patch.object(batches.os, "replace", side_effect=OSError("disk full")):
            app.clear_batch()
        self.assertIs(self.queue.entries, original)
        self.assertEqual(self.queue.path.read_bytes(), before)
        self.assertIn("写入失败", app.batch_error)
        self.assertNotIn("已清空", app.batch_notice)

    def test_clear_blocked_during_processing_preflight_or_other_busy_state(self):
        self.queue.add(self.tasks[0])
        app = self.clear_app()
        with patch("studio.messagebox.askyesno") as confirm:
            for attribute, value in (("batch_running", True), ("batch_checking", True),
                                     ("pipeline_job", object()), ("busy", True),
                                     ("confirming", True), ("exiting", True), ("batch_error", "error")):
                original = getattr(app, attribute)
                setattr(app, attribute, value)
                app.clear_batch()
                setattr(app, attribute, original)
            self.queue.entries[0]["state"] = "translating"
            app.clear_batch()
            confirm.assert_not_called()
        self.assertEqual(len(self.queue.entries), 1)

    def test_clear_empty_queue_removes_display_rows_and_disables_button(self):
        app = self.clear_app()
        app.batch_window = Mock()
        app.batch_window.winfo_exists.return_value = True
        app.batch_list = Mock()
        app.batch_list.get_children.return_value = ("old-row",)
        app.batch_list.selection.return_value = ()
        app.batch_summary = Mock()
        app.batch_buttons = {"clear": Mock()}
        app._batch_draw_time = 0
        app._batch_rendered = ("old-row",)
        studio.Studio.render_batch(app, force=True)
        app.batch_list.delete.assert_called_once_with("old-row")
        app.batch_buttons["clear"].configure.assert_called_once_with(state="disabled")
        with patch("studio.messagebox.askyesno") as confirm:
            app.clear_batch()
            confirm.assert_not_called()


class QueueExecutionTest(unittest.TestCase):
    pump_until = helpers.ParallelTest.pump_until
    wait_player = helpers.ParallelTest.wait_player

    def setUp(self):
        helpers.ParallelTest.setUp(self)
        app = self.app
        self.queued = []
        for index in range(3):
            audio = self.directory / f"Q{index}.wav"
            audio.write_bytes(f"queue-{index}".encode())
            self.queued.append(store.open_task(audio))
        app.batch = batches.BatchQueue(self.directory / "queue.json")
        for task in self.queued: app.batch.add(task)
        app.batch.save()
        app.batch_window = None
        app.batch_error = ""
        app.batch_running = app.batch_checking = False
        app.batch_current = app.batch_target = None
        app.batch_generation = app.batch_failures = 0
        app.batch_notice = ""
        self.started = []
        self.jobs = []
        original_start = app.start_job
        def start(mode, task):
            if mode == "pipeline":
                self.assertIsNone(app.pipeline_job)
                self.started.append(task.identity["task_id"])
            original_start(mode, task)
            if mode == "pipeline" and app.pipeline_job: self.jobs.append(app.pipeline_job)
        app.start_job = start
        def command(mode, audio, gated=False):
            return [sys.executable, "-B", "-u", "-X", "utf8", str(ROOT / "tests/parallel_worker_fixture.py"),
                    mode, str(audio), "--database", str(self.history.database),
                    "--behavior", self.behavior.get(Path(audio).name, self.behavior.get(mode, "normal"))]
        patcher = patch("studio.worker_command", side_effect=command)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch("studio.messagebox.showwarning"); patcher.start(); self.addCleanup(patcher.stop)

    def cleanup_runs(self):
        self.app.batch_running = False
        helpers.ParallelTest.cleanup_runs(self)
        self.app.batch.close()

    def until_done(self):
        self.pump_until(lambda: not self.app.batch_running and self.app.pipeline_job is None and not self.app.batch_checking, timeout=18)

    def test_three_automatic_serial_jobs_outputs_and_completed_skip(self):
        app = self.app; app.start_batch(); self.until_done()
        self.assertEqual(self.started, [task.identity["task_id"] for task in self.queued])
        self.assertEqual([entry["state"] for entry in app.batch.entries], ["completed"] * 3)
        for task, job in zip(self.queued, self.jobs):
            self.assertEqual(store.check_draft_progress(task.result("japanese.txt"), task.result("draft_bilingual.txt")), (1, 1))
            self.assertTrue(job.tree.closed)
        app.start_batch(); self.assertEqual(len(self.started), 3)
        # Even if metadata says pending, actual complete files skip before worker/model launch.
        app.batch.entries[0]["state"] = "pending"; app.start_batch(); self.until_done()
        self.assertEqual(len(self.started), 3)

    def test_local_failure_continues_next_and_retry_reuses_japanese(self):
        self.behavior["Q0.wav"] = "fail-whisper"
        self.app.start_batch(); self.until_done()
        self.assertEqual([entry["state"] for entry in self.app.batch.entries], ["failed", "completed", "completed"])
        self.assertIn("Whisper failed", self.app.batch.entries[0]["detail"])
        self.assertTrue(list(self.queued[0].directory.glob("japanese.txt.partial-*")))
        self.behavior["Q0.wav"] = "normal"
        self.app.batch_list = Mock(); self.app.batch_list.selection.return_value = (self.app.batch.entries[0]["task_id"],)
        self.app.retry_batch_selected(); self.app.start_batch(); self.until_done()
        self.assertEqual(self.app.batch.entries[0]["state"], "completed")

    def test_global_failure_pauses_and_retry_reuses_completed_transcription(self):
        self.behavior["Q0.wav"] = "service-down"
        self.app.start_batch(); self.until_done()
        self.assertEqual([entry["state"] for entry in self.app.batch.entries], ["failed", "pending", "pending"])
        before = self.queued[0].result("japanese.txt").read_bytes()
        self.behavior["Q0.wav"] = "normal"
        self.app.batch_list = Mock(); self.app.batch_list.selection.return_value = (self.app.batch.entries[0]["task_id"],)
        self.app.retry_batch_selected(); self.app.start_batch(); self.until_done()
        self.assertEqual(self.queued[0].result("japanese.txt").read_bytes(), before)
        self.assertEqual([entry["state"] for entry in self.app.batch.entries], ["completed"] * 3)

    def test_pending_cancel_and_active_cancel_keep_other_jobs_and_player(self):
        app = self.app
        app.launch("player"); self.wait_player(); player = app.player_job
        app.batch_list = Mock(); app.batch_list.selection.return_value = (app.batch.entries[1]["task_id"],)
        app.cancel_batch_selected()
        self.behavior["Q0.wav"] = "wait"
        app.start_batch()
        self.pump_until(lambda: app.pipeline_job is not None and any("FIXTURE_READY" in str(call) for call in app.append_log.call_args_list))
        active = app.pipeline_job
        app.batch_list.selection.return_value = (app.batch.entries[0]["task_id"],)
        app.cancel_batch_selected(); self.until_done()
        self.assertEqual([entry["state"] for entry in app.batch.entries], ["cancelled", "cancelled", "completed"])
        self.assertTrue(active.tree.closed)
        with store.task_lock(self.queued[0].directory): pass
        self.assertIs(app.player_job, player); self.assertIsNone(player.process.poll())
        self.assertEqual(app.task.identity["task_id"], self.tasks[0].identity["task_id"])

    def test_stop_finishes_current_and_does_not_start_next(self):
        app = self.app; app.start_batch()
        self.pump_until(lambda: app.pipeline_job is not None)
        app.stop_batch(); self.until_done()
        self.assertEqual([entry["state"] for entry in app.batch.entries], ["completed", "pending", "pending"])
        self.assertEqual(len(self.started), 1)
        app.start_batch(); self.until_done(); self.assertEqual(len(self.started), 3)

    def test_stop_during_preflight_ignores_late_event(self):
        app = self.app; app.batch_running = True
        with patch("studio.threading.Thread"):
            app.advance_batch()
        token, task_id = app.batch_generation, app.batch_current
        app.stop_batch()
        app.consume_batch_event("batch_checked", (token, task_id, (self.queued[0], False, "Whisper", ""), None))
        self.assertEqual(app.batch.entries[0]["state"], "pending")
        self.assertEqual(self.started, [])

    def test_missing_audio_continues_and_two_failures_pause(self):
        Path(self.queued[0].identity["path"]).unlink()
        self.behavior["Q1.wav"] = "fail-whisper"
        self.app.start_batch(); self.until_done()
        self.assertEqual([entry["state"] for entry in self.app.batch.entries], ["failed", "failed", "pending"])
        self.assertEqual(self.started, [self.queued[1].identity["task_id"]])

    def test_same_playing_work_pauses_queue_without_stopping_player(self):
        app = self.app
        app.batch.entries[0].update(task_id=self.tasks[0].identity["task_id"], audio=self.tasks[0].identity["path"], directory=str(self.tasks[0].directory))
        app.launch("player"); self.wait_player(); player = app.player_job
        app.start_batch()
        self.assertFalse(app.batch_running)
        self.assertIs(app.player_job, player); self.assertIsNone(player.process.poll())
        self.assertEqual(self.started, [])
        self.assertEqual(app.batch.entries[0]["state"], "pending")

    def test_batch_add_picker_duplicate_invalid_does_not_change_current_selection(self):
        app = self.app; original = app.task
        extra = self.directory / "extra.mp3"; extra.write_bytes(b"synthetic extra")
        invalid = self.directory / "unsupported.txt"; invalid.write_bytes(b"text")
        selected = (str(extra), str(extra), str(invalid), str(self.directory / "missing.wav"))
        with patch.object(studio.filedialog, "askopenfilenames", return_value=selected): app.add_batch_audio()
        self.pump_until(lambda: not app.busy)
        self.assertEqual(len(app.batch.entries), 4)
        self.assertIs(app.task, original)
        self.assertFalse(app.batch_running)

    def test_manual_pipeline_and_player_of_preflight_target_are_blocked(self):
        app = self.app; app.batch_running = True
        with patch("studio.threading.Thread"): app.advance_batch()
        app.launch("pipeline", confirmed=True)
        self.assertEqual(self.started, [])
        self.assertTrue(app.task_in_use(self.queued[0], "pipeline"))
        app.start_player(self.queued[0]); self.assertIsNone(app.player_job)
        app.stop_batch()

    def test_real_progress_updates_state_without_guessing_or_marking_complete(self):
        app = self.app
        entry = app.batch.entries[0]
        app.batch_current = entry["task_id"]
        job = studio.ActiveJob("pipeline", self.queued[0], Mock())
        app.pipeline_job = job
        for line in ("Starting Whisper process...", "50%|xxx| 5/10 [seconds/s]", "Whisper process exited.",
                     "Starting Qwen translation...", "Translation progress: 1/1"):
            app.consume_job_event("job_line", job, line + "\n")
        self.assertEqual(entry["state"], "translating")
        self.assertIn("1 / 1", entry["progress"])
        self.assertEqual(entry["completed_stages"], ["Whisper"])
        app.pipeline_job = None; app.batch_current = None

    def test_metadata_disk_error_pauses_before_worker_launch(self):
        app = self.app
        with patch.object(app.batch, "save", side_effect=OSError("disk full")):
            app.start_batch()
        self.assertFalse(app.batch_running)
        self.assertFalse(app.batch_checking)
        self.assertEqual(self.started, [])
        self.assertIn("写入失败", app.batch_error)

    def test_close_during_batch_cleans_jobs_and_preserves_queue(self):
        app = self.app; self.behavior["Q0.wav"] = "wait"
        app.launch("player"); self.wait_player()
        app.start_batch(); self.pump_until(lambda: app.pipeline_job is not None)
        app.close(); self.until_done()
        self.pump_until(lambda: app.player_job is None and app.root.destroy.called)
        app.root.destroy.assert_called_once()
        reopened = batches.BatchQueue(app.batch.path); self.addCleanup(reopened.close)
        self.assertEqual([entry["state"] for entry in reopened.entries], ["cancelled", "pending", "pending"])


def check_native_gui():
    """Actual Tk widgets + harmless pipeline fixture in an isolated D: directory."""
    work = ROOT / "work" / "batch_checks"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        folder = Path(temporary)
        audio = []
        for index in range(3):
            path = folder / f"GUI{index}.wav"
            path.write_bytes(b"synthetic native GUI" + bytes([index]))
            audio.append(str(path))
        def command(mode, path, gated=False):
            return [sys.executable, "-B", "-u", "-X", "utf8", str(ROOT / "tests/parallel_worker_fixture.py"),
                    mode, str(path), "--database", str(folder / "history.sqlite3")]
        with patch("studio.ROOT", folder), patch("studio.worker_command", side_effect=command), \
                patch("studio.messagebox.askyesno", return_value=True), \
                patch("studio.filedialog.askopenfilenames", return_value=tuple(audio)):
            root = tk.Tk(); root.withdraw()
            app = studio.Studio(root)
            failures = []
            root.report_callback_exception = lambda kind, error, trace: failures.append(str(error))
            try:
                app.buttons["batch"].invoke(); app.select_page('pipeline')
                assert str(app.batch_buttons["start"]["state"]) == "disabled"
                app.batch_buttons["add"].invoke()
                def pump(condition, timeout=12):
                    deadline = time.monotonic() + timeout
                    while not condition():
                        root.update()
                        assert not failures, failures
                        if time.monotonic() > deadline: raise AssertionError("native GUI fixture timed out")
                        time.sleep(.02)
                    root.update()
                pump(lambda: not app.busy)
                app.render_batch(force=True)
                assert len(app.batch_list.get_children()) == 3
                assert str(app.batch_buttons["start"]["state"]) == "normal"
                first = app.batch.entries[0]["task_id"]
                app.batch_list.selection_set(first)
                root.update(); app.render_batch(force=True)
                assert str(app.batch_buttons["cancel"]["state"]) == "normal"
                with patch("studio.messagebox.askyesno", return_value=False):
                    app.batch_buttons["cancel"].invoke()
                assert app.batch.entries[0]["state"] == "pending"
                app.batch_buttons["start"].invoke()
                pump(lambda: app.pipeline_job is not None)
                app.render_batch(force=True)
                assert str(app.batch_buttons["stop"]["state"]) == "normal"
                assert str(app.buttons["select"]["state"]) == "normal"
                assert str(app.buttons["pipeline"]["state"]) == "disabled"
                app.batch_buttons["stop"].invoke()
                pump(lambda: app.pipeline_job is None and not app.batch_running)
                app.render_batch(force=True)
                assert [row["state"] for row in app.batch.entries] == ["completed", "pending", "pending"]
                preserved_page = app.batch_window
                app.select_page('player')
                app.buttons["batch"].invoke()
                assert app.batch_window is preserved_page
                app.select_page('pipeline')
                assert len(app.batch_list.get_children()) == 3
                app.batch_buttons["start"].invoke()
                pump(lambda: not app.batch_running and app.pipeline_job is None)
                app.render_batch(force=True)
                assert [row["state"] for row in app.batch.entries] == ["completed"] * 3
                for key in app.batch_list.get_children():
                    assert app.batch_list.item(key, "values")[1] == "已完成"
                app.close()
                reopened = batches.BatchQueue(folder / "outputs" / "batch_queue.json")
                try:
                    assert [row["state"] for row in reopened.entries] == ["completed"] * 3
                finally:
                    reopened.close()
                print("Native Tk batch GUI checks passed: add/list/cancel-decline/start/stop/resume/reopen/persistence; no AI/VLC.")
            finally:
                app.batch_running = False
                if app.pipeline_job:
                    app.cancel()
                    deadline = time.monotonic() + 5
                    while app.pipeline_job and time.monotonic() < deadline:
                        root.update(); time.sleep(.03)
                app.batch.close()
                try: root.destroy()
                except tk.TclError: pass


if __name__ == "__main__":
    if "--gui" in sys.argv:
        check_native_gui()
    else:
        unittest.main(verbosity=2)
