"""Synthetic media and VLC states only; no real GUI, playback or inference."""
import ast
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import playback_history as history
import studio
import task_store


class PlaybackHistoryTest(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work" / "history_checks"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.db = self.directory / "metadata" / "history.sqlite3"
        self.history = history.HistoryStore(self.db)
        self.first = self.make_task("A", b"synthetic A")
        self.second = self.make_task("B", b"synthetic B")
        self.time = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)

    def make_task(self, directory, content):
        audio = self.directory / directory / "同名 日文 & ASMR.mp3"
        audio.parent.mkdir()
        audio.write_bytes(content)
        task = task_store.open_task(audio)
        task.result("output.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n模拟字幕\n", encoding="utf-8")
        return task

    def snapshot_works(self):
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                for folder in ("A", "B") for path in (self.directory / folder).rglob("*") if path.is_file()}

    def test_first_real_play_state_records_once_and_pause_does_not_duplicate(self):
        recorder = history.PlaybackRecorder(self.first, self.history)
        recorder.observe(False)
        self.assertFalse(self.db.exists())
        recorder.observe(True)
        first = self.history.list_entries()
        for playing in (True, False, True, True):
            recorder.observe(playing)
        self.assertEqual(self.history.list_entries(), first)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["work_id"], self.first.identity["task_id"])
        self.assertEqual(first[0]["audio_path"], self.first.identity["path"])

    def test_replaying_updates_timestamp_without_duplicate(self):
        self.history.record(self.first, self.time)
        self.history.record(self.first, self.time + timedelta(seconds=1))
        rows = self.history.list_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last_played_utc"], (self.time + timedelta(seconds=1)).isoformat(timespec="microseconds"))

    def test_same_names_are_distinct_and_latest_work_sorts_first(self):
        self.history.record(self.first, self.time)
        self.history.record(self.second, self.time + timedelta(seconds=1))
        self.assertEqual([row["work_id"] for row in self.history.list_entries()],
                         [self.second.identity["task_id"], self.first.identity["task_id"]])
        self.history.record(self.first, self.time + timedelta(seconds=2))
        self.assertEqual(self.history.list_entries()[0]["work_id"], self.first.identity["task_id"])

    def test_reopened_store_and_new_process_keep_history(self):
        self.history.record(self.first, self.time)
        restarted = history.HistoryStore(self.db)
        self.assertEqual(restarted.list_entries(), self.history.list_entries())
        restarted.record(self.second, self.time + timedelta(seconds=1))
        self.assertEqual(len(self.history.list_entries()), 2)
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c",
            "from playback_history import HistoryStore; import sys; "
            "rows=HistoryStore(sys.argv[1]).list_entries(); "
            "assert len(rows)==2 and rows[0]['work_id']==sys.argv[2]",
            str(self.db), self.second.identity["task_id"]], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_clear_cancel_preserves_history_then_confirm_deletes_metadata_only(self):
        self.history.record(self.first, self.time)
        self.history.record(self.second, self.time)
        rows, works = self.history.list_entries(), self.snapshot_works()
        app = studio.Studio.__new__(studio.Studio)
        app.history = self.history
        with patch.object(studio.messagebox, "askyesno", return_value=False):
            self.assertFalse(app.clear_history(object()))
        self.assertEqual(self.history.list_entries(), rows)
        with patch.object(studio.messagebox, "askyesno", return_value=True):
            self.assertTrue(app.clear_history(object()))
        self.assertEqual(history.HistoryStore(self.db).list_entries(), [])
        self.assertEqual(self.snapshot_works(), works)

    def test_reopen_uses_original_task_and_subtitle_without_modification(self):
        self.history.record(self.first, self.time)
        before = self.snapshot_works()
        task = history.reopen_task(self.history.list_entries()[0])
        self.assertEqual(task.identity, self.first.identity)
        self.assertEqual(task.result("output.srt"), self.first.result("output.srt"))
        self.assertEqual(self.snapshot_works(), before)

    def test_missing_audio_warns_does_not_delete_history_or_start_worker(self):
        self.history.record(self.first, self.time)
        entry = self.history.list_entries()[0]
        Path(entry["audio_path"]).unlink()  # Only this test's synthetic file.
        with self.assertRaises(history.HistoryError):
            history.reopen_task(entry)
        app = studio.Studio.__new__(studio.Studio)
        app.busy, app.confirming, app.background = False, False, Mock()
        with patch.object(studio.messagebox, "showwarning") as warning:
            app.open_history_entry(entry, Mock())
            warning.assert_called_once()
        app.background.assert_not_called()
        self.assertEqual(self.history.list_entries(), [entry])

    def test_changed_audio_content_never_plays_as_old_work(self):
        self.history.record(self.first, self.time)
        entry = self.history.list_entries()[0]
        Path(entry["audio_path"]).write_bytes(b"another synthetic work")
        with self.assertRaises(task_store.TaskError):
            history.reopen_task(entry)
        self.assertEqual(self.history.list_entries(), [entry])

    def test_corrupt_database_preserved_and_recorder_reports_only_once(self):
        self.db.parent.mkdir()
        self.db.write_bytes(b"deliberately corrupt simulated database")
        old = self.db.read_bytes()
        recorder = history.PlaybackRecorder(self.first, self.history)
        with self.assertRaises(history.HistoryError):
            recorder.observe(True)
        recorder.observe(True)
        with self.assertRaises(history.HistoryError):
            self.history.list_entries()
        self.assertEqual(self.db.read_bytes(), old)
        self.assertTrue(self.first.result("output.srt").exists())

    def test_locked_database_failure_keeps_previous_record(self):
        self.history.record(self.first, self.time)
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(history.HistoryError):
                self.history.record(self.second, self.time)
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(len(self.history.list_entries()), 1)

    def test_invalid_record_and_unknown_schema_are_reported_without_reset(self):
        self.history.record(self.first, self.time)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE playback_history SET last_played_utc='invalid'")
        with self.assertRaises(history.HistoryError):
            self.history.list_entries()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("PRAGMA user_version=99")
        before = self.db.read_bytes()
        with self.assertRaises(history.HistoryError):
            self.history.clear()
        self.assertEqual(self.db.read_bytes(), before)

    def test_history_reopen_requests_background_identity_check(self):
        self.history.record(self.first, self.time)
        app = studio.Studio.__new__(studio.Studio)
        app.busy, app.confirming, app.background = False, False, Mock()
        dialog = Mock()
        app.open_history_entry(self.history.list_entries()[0], dialog)
        dialog.destroy.assert_not_called()  # Fixed history page survives opening a work.
        self.assertEqual(app.background.call_args.kwargs["mode"], "history")
        task, result, _ = app.background.call_args.args[1]()
        self.assertEqual(task.identity, self.first.identity)
        self.assertTrue(result[3])

    def test_history_loading_launches_player_but_never_pipeline(self):
        app = studio.Studio.__new__(studio.Studio)
        for name in ("root", "progress", "audio_name", "audio_path", "status",
                     "finish_progress", "update_buttons", "render_progress", "launch"):
            setattr(app, name, Mock())
        app.file_states = {}
        app.close_after_cancel = False
        for playable in (True, False):
            app.events = queue.Queue()
            app.active_mode = "history"
            app.events.put(("done", (self.first, ({}, False, False, playable), "loaded")))
            app.launch.reset_mock()
            with patch.object(studio.messagebox, "showinfo") as info:
                app.drain()
                # Library works awaiting refinement may intentionally have no SRT.
                app.launch.assert_called_once_with("player")
                info.assert_not_called()

    def test_player_tick_keeps_subtitle_time_and_controls_when_history_fails(self):
        tree = ast.parse((ROOT / "floating_subtitle_player.py").read_text(encoding="utf-8-sig"))
        names = {"update_subtitle", "format_time", "toggle_play", "seek", "close_player",
                 "report_save_errors", "wait_for_progress_save"}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        player, root, label, time_label, hotkeys = Mock(), Mock(), Mock(), Mock(), Mock()
        player.get_time.return_value = 500
        player.get_length.return_value = 1000
        player.get_state.return_value = "Playing"
        resume = Mock()
        resume.tick.return_value = True
        resume.pending_seek = None
        resume.finished = False
        resume.last_snapshot = None
        resume.jump_to.side_effect = player.set_time
        resume.writer.errors = queue.Queue()
        resume.writer.errors.put("mock unavailable")
        resume.writer.done.is_set.return_value = True
        namespace = dict(player=player, root=root, subtitle_label=label, time_label=time_label,
                         progress_bar={}, hotkeys=hotkeys, current_subtitle="", subtitle_offset_ms=0,
                         subtitles=[(0, 1000, "模拟字幕")], vlc=SimpleNamespace(State=SimpleNamespace(Playing="Playing")),
                         resume=resume, queue=queue, closing_player=False, dragging_progress=False,
                         report_player_status=Mock(),
                         save_warning_shown=False, HistoryError=history.HistoryError, messagebox=Mock())
        exec(compile(ast.Module(body=functions, type_ignores=[]), "player_controls", "exec"), namespace)
        namespace["update_subtitle"]()
        label.config.assert_called_with(text="模拟字幕")
        time_label.config.assert_called_once()
        self.assertEqual(namespace["progress_bar"]["value"], 50)
        root.after.assert_called_once_with(50, namespace["update_subtitle"])
        namespace["messagebox"].showwarning.assert_called_once()
        player.is_playing.return_value = True
        namespace["toggle_play"]()
        player.pause.assert_called_once()
        player.is_playing.return_value = False
        namespace["toggle_play"]()
        player.play.assert_called_once()
        namespace["seek"](5)
        player.set_time.assert_called_once_with(5500)
        namespace["close_player"]()
        hotkeys.stop.assert_called_once()
        player.stop.assert_called_once()
        root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
