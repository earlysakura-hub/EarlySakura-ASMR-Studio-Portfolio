"""SQLite library metadata. Titles/tags never rename or delete media files."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import uuid

from app_runtime import data_root, configuration

DEFAULT_ROOT = Path.home() / "EarlySakuraLibrary"
SCHEMA = 1
SYSTEM_NAMES = {"喜爱", "待处理", "待精修", "已精修", "全部", "已处理"}


class LibraryError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class LibraryStore:
    def __init__(self, database=None, history=None):
        self.database = Path(database or data_root() / "media_library.sqlite3")
        self.history = Path(history or self.database.parent / "playback_history.sqlite3")

    @contextmanager
    def connection(self):
        connection = None
        try:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database, timeout=.5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA):
                raise LibraryError("媒体库数据库版本不支持，原文件保留。")
            if version == 0:
                with connection:
                    connection.executescript("""
                        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS works(
                            work_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
                            original_name TEXT NOT NULL, audio_path TEXT UNIQUE NOT NULL,
                            task_directory TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', rj TEXT,
                            favorite INTEGER NOT NULL DEFAULT 0, auto_state TEXT NOT NULL DEFAULT 'pending',
                            archive_state TEXT NOT NULL DEFAULT 'pending', refinement TEXT NOT NULL DEFAULT 'pending',
                            detail TEXT NOT NULL DEFAULT '', created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                            normalized TEXT UNIQUE NOT NULL, system INTEGER NOT NULL DEFAULT 0);
                        CREATE TABLE IF NOT EXISTS work_tags(work_id TEXT REFERENCES works(work_id),
                            tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY(work_id,tag_id));
                        CREATE TABLE IF NOT EXISTS operations(op_id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
                            kind TEXT NOT NULL, state TEXT NOT NULL, journal TEXT NOT NULL, created_utc TEXT NOT NULL);
                        INSERT OR IGNORE INTO tags(id,name,normalized,system) VALUES(1,'喜爱','喜爱',1);
                        PRAGMA user_version=1;
                    """)
            with connection:
                yield connection
        except (sqlite3.Error, OSError) as error:
            raise LibraryError(f"媒体库暂不可用，文件未自动删除：{error}") from error
        finally:
            if connection is not None: connection.close()

    def root(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
        return Path(row[0]) if row else Path(configuration().get("library_root") or DEFAULT_ROOT)

    def configure(self, root, create=True):
        root = Path(root)
        if not root.is_absolute() or root.is_symlink() or root.is_junction():
            raise LibraryError("媒体库必须使用普通的绝对路径目录。")
        with self.connection() as db:
            old = db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
            occupied = db.execute("SELECT 1 FROM works LIMIT 1").fetchone()
            if old and os.path.normcase(old[0]) != os.path.normcase(str(root)) and occupied:
                raise LibraryError("已有作品记录，不能直接更换根目录；需要单独迁移。")
            if create:
                for name in ("待处理音频", "已处理作品"):
                    (root / name).mkdir(parents=True, exist_ok=True)
            db.execute("INSERT INTO settings VALUES('root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(root),))

    def in_inbox(self, audio):
        return os.path.normcase(str(Path(audio).absolute().parent)) == os.path.normcase(str(self.root() / "待处理音频"))

    def register(self, task, work_id=None):
        task.verify_audio()
        identity = task.identity
        stem = Path(identity["path"]).stem
        rj = stem.upper() if re.fullmatch(r"RJ\d+", stem, re.I) else None
        with self.connection() as db:
            row = db.execute("SELECT work_id FROM works WHERE task_id=?", (identity["task_id"],)).fetchone()
            if row:
                return row[0]
            work_id = work_id or uuid.uuid4().hex
            db.execute("INSERT INTO works(work_id,task_id,original_name,audio_path,task_directory,rj,created_utc,updated_utc) VALUES(?,?,?,?,?,?,?,?)",
                       (work_id, identity["task_id"], Path(identity["path"]).name, identity["path"], str(task.directory), rj, now(), now()))
        return work_id

    def get(self, work_id=None, task_id=None):
        with self.connection() as db:
            row = db.execute("SELECT * FROM works WHERE " + ("work_id=?" if work_id is not None else "task_id=?"),
                             (work_id if work_id is not None else task_id,)).fetchone()
            if row is None: return None
            result = dict(row)
            result["tags"] = [item[0] for item in db.execute("SELECT tags.name FROM tags JOIN work_tags ON tags.id=work_tags.tag_id WHERE work_tags.work_id=? ORDER BY tags.normalized", (result["work_id"],))]
        result["display_title"] = result["title"] or Path(result["original_name"]).stem
        result["state"] = ("待处理" if result["archive_state"] != "archived" else
                           "已精修" if result["refinement"] == "refined" else "待精修")
        result["files"] = {name: str(Path(result["task_directory"]) / name) for name in
                           ("japanese.txt", "draft_bilingual.txt", "final_bilingual.txt", "output.srt", "full_plot_summary.txt")}
        return result

    def list(self, search="", status="全部", tag_id=None):
        with self.connection() as db:
            ids = [row[0] for row in db.execute("SELECT work_id FROM works ORDER BY created_utc DESC,work_id")]
            matching = set(row[0] for row in db.execute("SELECT work_id FROM work_tags WHERE tag_id=?", (tag_id,))) if tag_id and tag_id != 1 else None
        results = []
        for work_id in ids:
            work = self.get(work_id)
            if search.casefold() not in (work["display_title"] + " " + work["original_name"] + " " + (work["rj"] or "")).casefold(): continue
            if status == "喜爱" and not work["favorite"]: continue
            if status == "已处理" and work["archive_state"] != "archived": continue
            if status in ("待处理", "待精修", "已精修") and status != work["state"]: continue
            if tag_id == 1 and not work["favorite"]: continue
            if matching is not None and work_id not in matching: continue
            results.append(work)
        return results

    def update(self, work_id, **fields):
        allowed = {"title", "favorite", "auto_state", "archive_state", "refinement", "detail"}
        if not fields or not set(fields) <= allowed: raise LibraryError("无效作品更新。")
        if "title" in fields and (not isinstance(fields["title"], str) or "\0" in fields["title"]): raise LibraryError("标题无效。")
        if "favorite" in fields and fields["favorite"] not in (0, 1): raise LibraryError("喜爱状态无效。")
        for key, values in {'auto_state': {'pending','transcribing','translating','completed','failed','cancelled'},
                            'archive_state': {'pending','archiving','archived','failed'}, 'refinement': {'pending','refined'}}.items():
            if key in fields and fields[key] not in values: raise LibraryError('无效处理状态。')
        with self.connection() as db:
            result = db.execute("UPDATE works SET " + ",".join(key + "=?" for key in fields) + ",updated_utc=? WHERE work_id=?",
                                (*fields.values(), now(), work_id))
            if result.rowcount != 1: raise LibraryError("作品不存在。")

    def tags(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tags ORDER BY system DESC,normalized")]

    def save_tag(self, name, tag_id=None):
        name = name.strip()
        if not name or name in SYSTEM_NAMES or "\0" in name: raise LibraryError("标签为空或使用了系统保留名称。")
        with self.connection() as db:
            if tag_id is not None:
                if not db.execute("SELECT 1 FROM tags WHERE id=? AND system=0", (tag_id,)).fetchone(): raise LibraryError("系统标签不能修改。")
                db.execute("UPDATE tags SET name=?,normalized=? WHERE id=?", (name, name.casefold(), tag_id))
                return tag_id
            row = db.execute("SELECT id FROM tags WHERE normalized=?", (name.casefold(),)).fetchone()
            if row: return row[0]
            return db.execute("INSERT INTO tags(name,normalized) VALUES(?,?)", (name, name.casefold())).lastrowid

    def delete_tag(self, tag_id):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM tags WHERE id=? AND system=0", (tag_id,)).fetchone(): raise LibraryError("系统喜爱标签不能删除。")
            db.execute("DELETE FROM tags WHERE id=?", (tag_id,))

    def set_tags(self, work_id, tag_ids):
        tag_ids = set(tag_ids)
        with self.connection() as db:
            for tag_id in tag_ids:
                if not db.execute("SELECT 1 FROM tags WHERE id=? AND system=0", (tag_id,)).fetchone(): raise LibraryError("请用爱心管理喜爱；普通标签不存在。")
            db.execute("DELETE FROM work_tags WHERE work_id=?", (work_id,))
            db.executemany("INSERT INTO work_tags VALUES(?,?)", ((work_id, tag_id) for tag_id in tag_ids))

    def operation(self, op_id, work_id, kind, state, journal):
        with self.connection() as db:
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?) ON CONFLICT(op_id) DO UPDATE SET state=excluded.state,journal=excluded.journal",
                       (op_id, work_id, kind, state, str(journal), now()))

    def pending_operations(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM operations WHERE state NOT IN ('done','rolled_back') ORDER BY created_utc")]

    def relocate(self, work_id, task, refined=False):
        # Metadata and history paths commit together; position/session are untouched.
        with self.connection() as db:
            attached = self.history.is_file()
            if attached:
                db.execute("ATTACH DATABASE ? AS history", (str(self.history),))
                if not db.execute("SELECT 1 FROM history.sqlite_master WHERE type='table' AND name='playback_history'").fetchone():
                    raise LibraryError("历史数据库结构异常，未更新路径。")
            db.execute("UPDATE works SET audio_path=?,task_directory=?,auto_state='completed',archive_state='archived',refinement=?,detail='',updated_utc=? WHERE work_id=?",
                       (task.identity["path"], str(task.directory), "refined" if refined else "pending", now(), work_id))
            if attached:
                db.execute("UPDATE history.playback_history SET audio_path=?,task_directory=? WHERE work_id=?",
                           (task.identity["path"], str(task.directory), task.identity["task_id"]))

    def resolve(self, task_id):
        import task_store
        work = self.get(task_id=task_id)
        return task_store.load_task(work["task_directory"]) if work and work["archive_state"] == "archived" else None
