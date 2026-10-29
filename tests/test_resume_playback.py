"""Isolated resume/migration/player-flow checks; never touch the real history DB."""
import ast
from contextlib import closing
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
from playback_history import HistoryStore, HistoryError, clear_confirmed, reopen_task
from playback_resume import ResumePlayback, ProgressWriter, choose_resume
import task_store


class FakePlayer:
    def __init__(self):
        self.position, self.duration, self.status = 0, 60000, "Playing"
        self.targets = []
    def get_time(self): return self.position
    def get_length(self): return self.duration
    def get_state(self): return self.status
    def is_seekable(self): return True
    def set_time(self, target): self.targets.append(target)
    def pause(self): self.status = "Paused"


class ResumeTest(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work" / "resume_checks"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = HistoryStore(self.directory / "history.sqlite3")
        self.tasks = []
        self.sessions = []
        for name in ("A", "B"):
            audio = self.directory / (name + ".wav")
            audio.write_bytes(name.encode())
            self.tasks.append(task_store.open_task(audio))
        self.addCleanup(self.close_sessions)
        self.clock = [0.0]

    def close_sessions(self):
        for session in self.sessions:
            session.close()
            self.assertTrue(session.writer.done.wait(3))

    def session(self, index=0, position=0):
        player = FakePlayer()
        session = ResumePlayback(player, self.tasks[index], position, self.store, clock=lambda: self.clock[0])
        self.sessions.append(session)
        return session, player

    def wait_saved(self, index, position):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.store.connection() as connection:
                row = connection.execute("SELECT position_ms FROM playback_history WHERE work_id=?",
                                         (self.tasks[index].identity["task_id"],)).fetchone()
            if row and row[0] == position:
                return
            time.sleep(0.01)
        self.fail(f"position {position} not persisted")

    def test_first_play_periodic_save_and_close_capture(self):
        self.assertIsNone(self.store.resume_info(self.tasks[0].identity["task_id"]))
        session, player = self.session()
        self.assertTrue(session.tick())
        player.position = 10000
        self.clock[0] = 4.9
        session.tick()
        self.wait_saved(0, 0)
        self.clock[0] = 5
        session.tick()
        self.wait_saved(0, 10000)
        player.position = 12500
        session.close()
        self.assertTrue(session.writer.done.wait(3))
        self.assertEqual(self.store.resume_info(self.tasks[0].identity["task_id"])["position_ms"], 12500)

    def test_restore_waits_for_vlc_then_subtitles_follow_actual_clock(self):
        self.store.begin_playback(self.tasks[0], "old", 25000, 60000)
        session, player = self.session(position=25000)
        self.assertFalse(session.tick())
        self.assertEqual(player.targets, [25000])
        self.assertFalse(session.tick())
        self.assertEqual(self.store.resume_info(self.tasks[0].identity["task_id"])["position_ms"], 25000)
        player.position = 25200
        tree = ast.parse((ROOT / "floating_subtitle_player.py").read_text(encoding="utf-8-sig"))
        names = {"update_subtitle", "format_time", "report_save_errors"}
        namespace = dict(resume=session, player=player, current_subtitle="", subtitle_offset_ms=0,
                         subtitles=[(0, 1000, "开头"), (25000, 30000, "恢复后的字幕")],
                         closing_player=False, dragging_progress=False, save_warning_shown=False,
                         root=Mock(), subtitle_label=Mock(), time_label=Mock(), progress_bar={},
                         queue=queue, messagebox=Mock(), HistoryError=HistoryError)
        namespace["report_player_status"] = Mock()
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), "player_flow", "exec"), namespace)
        namespace["update_subtitle"]()
        namespace["subtitle_label"].config.assert_called_with(text="恢复后的字幕")
        namespace["time_label"].config.assert_called_with(text="00:00:25 / 00:01:00")
        self.assertEqual(namespace["progress_bar"]["value"], 42)
        player.status, player.position = "Ended", 0
        namespace["update_subtitle"]()
        self.assertEqual(namespace["progress_bar"]["value"], 100)
        namespace["time_label"].config.assert_called_with(text="00:01:00 / 00:01:00")

    def test_start_over_replaces_old_position_only_after_actual_play(self):
        self.store.begin_playback(self.tasks[0], "old", 25000, 60000)
        session, player = self.session()
        player.status = "Opening"
        self.assertFalse(session.tick())
        self.assertEqual(self.store.resume_info(self.tasks[0].identity["task_id"])["position_ms"], 25000)
        player.status = "Playing"
        session.tick()
        self.wait_saved(0, 0)
        self.assertIsNone(self.store.resume_info(self.tasks[0].identity["task_id"]))

    def test_pause_stop_and_two_works_keep_separate_positions(self):
        first, a = self.session()
        second, b = self.session(1)
        first.tick(); second.tick()
        a.position, b.position = 11000, 33000
        first.save_now(); a.status = "Paused"; first.tick()
        second.save_now(); b.status = "Paused"; second.tick()
        self.wait_saved(0, 11000); self.wait_saved(1, 33000)
        a.status, a.position = "Stopped", 0
        first.tick()
        first.close(); self.assertTrue(first.writer.done.wait(3))
        self.assertEqual(self.store.resume_info(self.tasks[0].identity["task_id"])["position_ms"], 11000)
        entry = next(row for row in self.store.list_entries() if row["work_id"] == self.tasks[1].identity["task_id"])
        task = reopen_task(entry)
        self.assertEqual(self.store.resume_info(task.identity["task_id"])["position_ms"], 33000)

    def test_rapid_seeks_ignore_old_clock_and_near_end_is_not_complete(self):
        session, player = self.session()
        session.tick()
        session.jump_to(12000); session.jump_to(40000)
        player.position = 12000
        self.assertFalse(session.tick())
        player.position = 40000
        self.assertTrue(session.tick())
        self.wait_saved(0, 40000)
        session.jump_to(60000)
        self.assertEqual(player.targets[-1], 59000)
        player.position = 59000
        session.tick()
        self.wait_saved(0, 59000)
        self.assertIsNotNone(self.store.resume_info(self.tasks[0].identity["task_id"]))
        player.status, player.position = "Ended", 0
        session.tick()
        self.wait_saved(0, 60000)
        self.assertIsNone(self.store.resume_info(self.tasks[0].identity["task_id"]))
        session.close(); self.assertTrue(session.writer.done.wait(3))
        self.assertIsNone(self.store.resume_info(self.tasks[0].identity["task_id"]))

    def test_failed_seek_does_not_overwrite_original_resume(self):
        self.store.begin_playback(self.tasks[0], "old", 25000, 60000)
        session, player = self.session(position=25000)
        session.tick()
        self.clock[0] = 6
        with self.assertRaises(HistoryError): session.tick()
        self.assertEqual(player.status, "Paused")
        session.close(); self.assertTrue(session.writer.done.wait(3))
        self.assertEqual(self.store.resume_info(self.tasks[0].identity["task_id"])["position_ms"], 25000)

    def test_legacy_schema_migration_preserves_original_columns_and_rows(self):
        old = HistoryStore(self.directory / "version1.sqlite3")
        with closing(sqlite3.connect(old.database)) as connection, connection:
            connection.execute("CREATE TABLE playback_history (work_id TEXT PRIMARY KEY, audio_name TEXT, audio_path TEXT, task_directory TEXT, last_played_utc TEXT)")
            connection.execute("INSERT INTO playback_history VALUES (?, ?, ?, ?, ?)",
                               (self.tasks[0].identity["task_id"], "旧作品", self.tasks[0].identity["path"], str(self.tasks[0].directory), "2026-10-12T01:00:00+00:00"))
            connection.execute("PRAGMA user_version=1")
        rows = old.list_entries()
        self.assertEqual(rows[0]["audio_name"], "旧作品")
        self.assertEqual(rows[0]["last_played_utc"], "2026-10-12T01:00:00+00:00")
        self.assertIsNone(old.resume_info(self.tasks[0].identity["task_id"]))
        with old.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_invalid_positions_and_completed_records_do_not_prompt(self):
        self.store.begin_playback(self.tasks[0], "old", 10000, 60000)
        for position, duration, completed in ((0, 60000, 0), (-1, 60000, 0), (70000, 60000, 0),
                                              (10000, 0, 0), (10000, 60000, 1), ("bad", 60000, 0)):
            with self.store.connection() as connection:
                connection.execute("UPDATE playback_history SET position_ms=?, duration_ms=?, completed=?", (position, duration, completed))
            self.assertIsNone(self.store.resume_info(self.tasks[0].identity["task_id"]))

    def test_cancel_resume_dialog_does_not_write_and_choices_are_explicit(self):
        saved = {"position_ms": 25000, "duration_ms": 60000}
        self.store.begin_playback(self.tasks[0], "old", 25000, 60000)
        commands = {}
        def button(parent, **kwargs):
            commands[kwargs["text"]] = kwargs["command"]
            return Mock()
        for option, expected in (("继续上次播放", 25000), ("从头播放", 0), ("取消", None)):
            root = Mock()
            root.wait_window.side_effect = lambda dialog: commands[option]()
            with patch("tkinter.Toplevel"), patch("tkinter.ttk.Frame"), patch("tkinter.ttk.Label"), patch("tkinter.ttk.Button", side_effect=button):
                self.assertEqual(choose_resume(root, saved, lambda ms: str(ms)), expected)
            self.assertEqual(self.store.resume_info(self.tasks[0].identity["task_id"]), saved)
        self.assertEqual(choose_resume(Mock(), None, str), 0)

    def test_clear_and_newer_session_prevent_stale_writer_restoring_progress(self):
        task = self.tasks[0]
        self.store.begin_playback(task, "old", 10000, 60000)
        self.store.begin_playback(task, "new", 20000, 60000)
        self.assertFalse(self.store.save_progress(task.identity["task_id"], "old", 30000, 60000))
        self.assertEqual(self.store.resume_info(task.identity["task_id"])["position_ms"], 20000)
        clear_confirmed(self.store, lambda *args: True)
        self.assertFalse(self.store.save_progress(task.identity["task_id"], "new", 30000, 60000))
        self.assertEqual(self.store.list_entries(), [])
        self.assertTrue(Path(task.identity["path"]).is_file())

    def test_slow_disk_write_does_not_block_playback_tick(self):
        from threading import Event
        entered, release = Event(), Event()
        store = Mock()
        def slow(*args): entered.set(); release.wait(2)
        store.begin_playback.side_effect = slow
        store.save_progress.return_value = True
        session = ResumePlayback(FakePlayer(), self.tasks[0], history=store)
        self.sessions.append(session)
        started = time.monotonic()
        session.tick()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(entered.wait(1))
        release.set()

    def test_abrupt_process_exit_keeps_last_periodic_checkpoint(self):
        # Real process termination with a simulated clock; no VLC initialization.
        code = '''
import sys, time
from task_store import load_task
from playback_history import HistoryStore
from playback_resume import ResumePlayback
started=time.monotonic()
class Player:
    def get_state(self): return "Playing"
    def get_time(self): return 10000 + int((time.monotonic()-started)*1000)
    def get_length(self): return 60000
task=load_task(sys.argv[1])
session=ResumePlayback(Player(), task, history=HistoryStore(sys.argv[2]))
while True:
    session.tick()
    time.sleep(0.05)
'''
        process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-c", code,
                                    str(self.tasks[0].directory), str(self.store.database)],
                                   cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            saved = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(process.stderr.read().decode("utf-8", errors="replace"))
                saved = self.store.resume_info(self.tasks[0].identity["task_id"])
                if saved and saved["position_ms"] >= 14000:
                    break
                time.sleep(0.05)
            self.assertTrue(saved and saved["position_ms"] >= 14000, saved)
            process.kill(); process.wait(timeout=3)
            self.assertEqual(HistoryStore(self.store.database).resume_info(self.tasks[0].identity["task_id"]), saved)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=3)
            process.stderr.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
