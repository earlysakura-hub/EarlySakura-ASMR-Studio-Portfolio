"""Small persistent serial queue; business files remain owned by task_store."""
import json
import os
from pathlib import Path
import re
import uuid

import task_store as store

LABELS = {"pending": "等待中", "checking": "核对中", "transcribing": "转写中",
          "translating": "粗翻中", "completed": "已完成", "failed": "失败",
          "cancelled": "已取消", "interrupted": "上次中断"}
ACTIVE = {"checking", "transcribing", "translating"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".ogg"}


class QueueError(ValueError):
    pass


def global_failure(text):
    """Conservative pause signals from real exception/log output, not timing."""
    return bool(re.search(
        r"CLI not found|running on a different port|ConnectionError|Connection refused|"
        r"Failed to establish a new connection|HTTPConnectionPool|ReadTimeout|"
        r"out of memory|CUDA.*error|cuDNN.*error|CUBLAS.*error|no space left|"
        r"(?:failed|unable|cannot|could not) to load.*model|model.*not found|"
        r"disk.*full|WinError (?:112|39)|磁盘空间|显存不足|模型服务不可用|"
        r"Command .*lms(?:\.exe)?.*(?:load|server|ps|unload).*returned non-zero",
        text, re.I))


def check_entry(entry):
    task = store.load_task(entry["directory"])
    if task.identity["task_id"] != entry["task_id"] or (os.path.normcase(task.identity["path"]) != os.path.normcase(entry["audio"])
            and task.identity.get("origin_normalized_path") != os.path.normcase(entry["audio"])):
        raise store.TaskError("队列作品身份与任务清单不匹配；未启动模型。")
    with store.task_lock(task.directory):
        jp, draft = task.result("japanese.txt"), task.result("draft_bilingual.txt")
        if (task.directory / "draft_bilingual.txt.pending.json").exists():
            raise store.TaskError("粗翻存在中断 pending 标记；原文件保留，需人工检查，未启动模型。")
        done, total = store.check_draft_progress(jp, draft) if jp.exists() else (0, 0)
        complete = total > 0 and done == total
        if not complete:
            reason = store.pipeline_block_reason(task)
            if reason:
                raise store.TaskError(reason)
            if draft.exists() and not jp.exists():
                raise store.TaskError("已有粗翻但缺少日文原文，需人工检查。")
    return task, complete, "Qwen" if jp.exists() else "Whisper", f"{done}/{total} 条" if total else "进度未知"


class BatchQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = []
        self._lock = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A second GUI may play audio but cannot overwrite this GUI's queue.
        import msvcrt
        lock = self.path.with_suffix(".lock").open("a+b")
        try:
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"0"); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            lock.close()
            raise QueueError("另一窗口正在管理队列；此窗口的批量功能不可用。") from error
        self._lock = lock
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data["schema_version"] != 1 or not isinstance(data["entries"], list):
                    raise ValueError("不支持的队列版本")
                seen = set()
                for entry in data["entries"]:
                    if (not isinstance(entry, dict) or not re.fullmatch(r"[0-9a-f]{64}", entry["task_id"])
                            or entry["task_id"] in seen or entry["state"] not in LABELS
                            or any(not isinstance(entry.get(key), str) for key in ("audio", "directory", "stage", "progress", "detail"))
                            or not Path(entry["audio"]).is_absolute() or not Path(entry["directory"]).is_absolute()):
                        raise ValueError("无效或重复的队列记录")
                    seen.add(entry["task_id"])
                    if entry["state"] in ACTIVE:
                        entry.update(state="interrupted", detail="上次未确认完成；请核对后选择重试，再开始队列。")
                    self.entries.append(entry)
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.close()
            raise QueueError(f"队列无法读取，原文件保留，未自动重置：{error}") from error

    def get(self, task_id):
        return next((entry for entry in self.entries if entry["task_id"] == task_id), None)

    def add(self, task):
        if self.get(task.identity["task_id"]) is not None:
            return False
        self.entries.append({"task_id": task.identity["task_id"], "audio": task.identity["path"],
                             "directory": str(task.directory), "state": "pending", "stage": "待核对",
                             "progress": "", "detail": "", "completed_stages": [],
                             "results": {name: str(task.result(name)) for name in store.RESULT_NAMES}})
        return True

    def pending(self):
        return next((entry for entry in self.entries if entry["state"] == "pending"), None)

    def save(self):
        if self._lock is None:
            raise QueueError("队列已关闭，未写入。")
        temporary = self.path.with_name(self.path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"schema_version": 1, "entries": self.entries}, output, ensure_ascii=False, indent=2)
                output.write("\n"); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()  # Only this write's own queue metadata temp file.

    def close(self):
        if self._lock is not None:
            import msvcrt
            self._lock.seek(0)
            msvcrt.locking(self._lock.fileno(), msvcrt.LK_UNLCK, 1)
            self._lock.close()
            self._lock = None
