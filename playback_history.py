"""Playback metadata only; no media deletion, decoding or model dependencies."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import os
from app_runtime import data_root

DEFAULT_DATABASE = data_root() / "playback_history.sqlite3"
SCHEMA_VERSION = 2


class HistoryError(ValueError):
    pass


def display_time(value):
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError("missing timezone")
        return instant.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as error:
        raise HistoryError("播放历史中的时间无效，原数据已保留。") from error


class HistoryStore:
    def __init__(self, database=DEFAULT_DATABASE):
        self.database = Path(database)

    @contextmanager
    def connection(self):
        connection = None
        try:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database, timeout=0.25)
            connection.row_factory = sqlite3.Row
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise HistoryError("播放历史版本不受支持，原数据已保留。")
            if version < SCHEMA_VERSION:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    # Recheck after acquiring the write lock: another player may
                    # have migrated the same database while we were waiting.
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (0, 1, SCHEMA_VERSION):
                        raise HistoryError("播放历史版本不受支持，原数据已保留。")
                    connection.execute("""CREATE TABLE IF NOT EXISTS playback_history (
                        work_id TEXT PRIMARY KEY NOT NULL,
                        audio_name TEXT NOT NULL,
                        audio_path TEXT NOT NULL,
                        task_directory TEXT NOT NULL,
                        last_played_utc TEXT NOT NULL
                    )""")
                    if version < SCHEMA_VERSION:
                        connection.execute("ALTER TABLE playback_history ADD COLUMN position_ms INTEGER NOT NULL DEFAULT 0")
                        connection.execute("ALTER TABLE playback_history ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0")
                        connection.execute("ALTER TABLE playback_history ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
                        connection.execute("ALTER TABLE playback_history ADD COLUMN playback_session TEXT")
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            with connection:
                yield connection
        except (sqlite3.Error, OSError) as error:
            raise HistoryError("播放历史暂不可用（数据损坏、文件被占用或无法写入）。"
                               "音频播放可继续，原数据不会自动删除。\n" + str(error)) from error
        finally:
            if connection is not None:
                connection.close()

    def record(self, task, played_at=None):
        with self.connection() as connection:
            self._record(connection, task, played_at)

    def _record(self, connection, task, played_at=None):
        identity = task.identity
        work_id = identity["task_id"]
        if not re.fullmatch(r"[0-9a-f]{64}", work_id):
            raise HistoryError("无效的作品标识，未记录历史。")
        instant = played_at if played_at is not None else datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise HistoryError("播放时间必须包含时区。")
        stamp = instant.astimezone(timezone.utc).isoformat(timespec="microseconds")
        connection.execute("""INSERT INTO playback_history
                (work_id, audio_name, audio_path, task_directory, last_played_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(work_id) DO UPDATE SET
                    audio_name=excluded.audio_name, audio_path=excluded.audio_path,
                    task_directory=excluded.task_directory,
                    last_played_utc=MAX(playback_history.last_played_utc, excluded.last_played_utc)
                """, (work_id, Path(identity["path"]).name, identity["path"],
                      str(task.directory), stamp))

    def resume_info(self, work_id):
        with self.connection() as connection:
            row = connection.execute("SELECT position_ms, duration_ms, completed "
                                     "FROM playback_history WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            return None
        position, duration, completed = row
        if (type(position) is not int or type(duration) is not int
                or type(completed) is not int or completed != 0
                or position <= 0 or duration <= position):
            return None
        return {"position_ms": position, "duration_ms": duration}

    def begin_playback(self, task, session, position, duration):
        self._validate_progress(position, duration)
        with self.connection() as connection:
            self._record(connection, task)
            connection.execute("UPDATE playback_history SET position_ms=?, duration_ms=?, "
                               "completed=0, playback_session=? WHERE work_id=?",
                               (position, duration, session, task.identity["task_id"]))

    @staticmethod
    def _validate_progress(position, duration):
        if (type(position) is not int or type(duration) is not int
                or duration <= 0 or position < 0 or position > duration):
            raise HistoryError("播放进度无效，未保存。")

    def save_progress(self, work_id, session, position, duration, completed=False):
        self._validate_progress(position, duration)
        with self.connection() as connection:
            result = connection.execute("UPDATE playback_history SET position_ms=?, duration_ms=?, "
                                        "completed=? WHERE work_id=? AND playback_session=?",
                                        (position, duration, int(completed), work_id, session))
            # A cleared record or a newer player session must not be overwritten.
            return result.rowcount == 1

    def list_entries(self):
        with self.connection() as connection:
            entries = [dict(row) for row in connection.execute(
                "SELECT work_id, audio_name, audio_path, task_directory, last_played_utc "
                "FROM playback_history ORDER BY last_played_utc DESC, work_id")]
        for entry in entries:
            if (not isinstance(entry["work_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["work_id"])
                    or not all(isinstance(entry[key], str) and entry[key]
                               for key in ("audio_name", "audio_path", "task_directory"))):
                raise HistoryError("播放历史记录无效，原数据已保留。")
            display_time(entry["last_played_utc"])
        return entries

    def clear(self):
        # Only metadata rows in this database; never follow stored paths.
        with self.connection() as connection:
            connection.execute("DELETE FROM playback_history")


def clear_confirmed(history, ask):
    if not ask("清空播放历史", "清空全部播放历史和保存的播放进度？\n只清除历史元数据，不删除音频、字幕或汉化任务。"):
        return False
    history.clear()
    return True


def reopen_task(entry):
    """Verify the original identity before loading a historical work."""
    from task_store import load_task, open_task
    audio = Path(entry["audio_path"])
    directory = Path(entry["task_directory"])
    if not audio.is_file() and not (directory / "task.json").is_file():
        raise HistoryError("原音频文件不存在，可能已移动或删除。\n"
                           + str(audio) + "\n历史记录已保留；可用“选择音频”打开新位置。")
    if (directory / "task.json").is_file():
        task = load_task(directory, verify_audio=False)
        if not Path(task.identity['path']).is_file():
            raise HistoryError("历史音频已移动或不存在，记录保留；请核对文件位置。")
        task.verify_audio()
    else:
        task = open_task(audio)
    if (task.identity["task_id"] != entry["work_id"]
            or (os.path.normcase(os.path.abspath(task.identity["path"])) != os.path.normcase(os.path.abspath(audio))
                and task.identity.get("origin_normalized_path") != os.path.normcase(os.path.abspath(audio)))):
        raise HistoryError("此路径的音频身份与历史记录不同，未启动播放。请重新选择音频。")
    return task


class PlaybackRecorder:
    """One write per player session, only after VLC really reaches Playing."""
    def __init__(self, task, history=None):
        self.task = task
        self.history = history if history is not None else HistoryStore()
        self.attempted = False

    def observe(self, playing):
        if not playing or self.attempted:
            return
        self.attempted = True
        # The caller reports errors once; subsequent 50 ms ticks never retry.
        self.history.record(self.task)
