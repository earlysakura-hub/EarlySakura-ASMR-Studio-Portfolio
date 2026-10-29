"""Audio-bound tasks shared by the five original scripts (standard library only)."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from app_runtime import configuration

RESULT_NAMES = ("japanese.txt", "draft_bilingual.txt", "final_bilingual.txt", "output.srt")
SCHEMA_VERSION = 1


class TaskError(ValueError):
    pass


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def absolute_path(path):
    # Normalize dots and relative paths without relying on Windows realpath
    # privileges. Different path aliases conservatively get separate tasks.
    return Path(os.path.abspath(os.fspath(path)))


def audio_identity(audio):
    """Hash all bytes; size/mtime alone must never decide task reuse."""
    path = absolute_path(audio)
    if not path.is_file():
        raise TaskError("音频路径不是文件。")
    before = path.stat()
    digest = file_hash(path)
    after = path.stat()
    fields = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev")
    if any(getattr(before, key) != getattr(after, key) for key in fields):
        raise TaskError("计算身份时音频发生变化，请等待文件保存完成后重试。")
    normalized = os.path.normcase(str(path))
    task_id = hashlib.sha256((normalized + "\0" + digest).encode("utf-8")).hexdigest()
    return {"path": str(path), "normalized_path": normalized, "sha256": digest,
            "size": after.st_size, "mtime_ns": after.st_mtime_ns, "task_id": task_id}


def _plain_path(path):
    path = Path(path)
    if path.is_symlink() or path.is_junction():
        raise TaskError(f"为保护文件，任务内部不接受链接或目录联接：{path}")
    return path


@contextmanager
def task_lock(directory):
    """OS releases the lock even if a worker is killed; no stale lock timeout."""
    import msvcrt
    directory = _plain_path(directory)
    lock_path = _plain_path(directory / ".task.lock")
    with lock_path.open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise TaskError("此任务正被另一个进程使用，请稍后再试。") from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def partial_path(destination):
    return Path(destination).with_name(Path(destination).name + ".partial-" + uuid.uuid4().hex)


def publish_new(partial, destination):
    """Atomic no-clobber publication on Windows; keep failed partials for inspection."""
    partial, destination = _plain_path(partial), _plain_path(destination)
    if destination.exists():
        raise FileExistsError(f"已存在，未覆盖：{destination}")
    # Windows rename is atomic on the same volume and refuses an existing target.
    # Unlike replace(), it will not silently overwrite a file created in a race.
    if os.name != "nt":
        raise TaskError("本版本的安全发布仅支持 Windows。")
    partial.rename(destination)


def write_new(destination, content):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"已存在，未覆盖：{destination}")
    partial = partial_path(destination)
    with partial.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    publish_new(partial, destination)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@dataclass(frozen=True)
class Task:
    directory: Path
    identity: dict

    def result(self, name):
        if name not in RESULT_NAMES:
            raise TaskError(f"未知输出类型：{name}")
        return _plain_path(self.directory / name)

    def verify_audio(self):
        current = audio_identity(self.identity["path"])
        if (current["sha256"] != self.identity["sha256"] or
                current["normalized_path"] != self.identity["normalized_path"]):
            raise TaskError("音频内容或路径已改变；请重新选择音频建立对应任务。旧任务已保留。")


def load_task(directory, verify_audio=True):
    directory = _plain_path(Path(directory).absolute())
    try:
        data = json.loads(_plain_path(directory / "task.json").read_text(encoding="utf-8"))
        identity = data["audio"]
        origin = identity.get("origin_normalized_path", identity["normalized_path"])
        if not isinstance(origin, str) or os.path.normcase(str(absolute_path(origin))) != origin:
            raise ValueError("Invalid origin")
        expected = hashlib.sha256((origin + "\0" + identity["sha256"]).encode("utf-8")).hexdigest()
        if (data["schema_version"] != SCHEMA_VERSION or identity["task_id"] != expected
                or os.path.normcase(str(absolute_path(identity["path"]))) != identity["normalized_path"]
                or not re.fullmatch(r"[0-9a-f]{64}", identity["sha256"])):
            raise ValueError("Invalid identity")
        if "redirect_to" in data:
            target = _plain_path(absolute_path(data["redirect_to"]))
            if target == directory or "redirect_to" in json.loads((target / "task.json").read_text(encoding="utf-8")):
                raise ValueError("Invalid redirect chain")
            moved = load_task(target, verify_audio=verify_audio)
            if moved.identity["task_id"] != identity["task_id"]:
                raise ValueError("Redirect identity mismatch")
            return moved
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise TaskError(f"任务清单缺失或无效，未自动修复：{directory}") from error
    task = Task(directory, identity)
    for name in RESULT_NAMES:
        task.result(name)
    if verify_audio:
        task.verify_audio()
    return task


def open_task(audio, task_root=None):
    identity = audio_identity(audio)
    adjacent = Path(identity["path"]).parent / "task.json"
    if task_root is None and adjacent.is_file():
        data = json.loads(_plain_path(adjacent).read_text(encoding="utf-8"))
        if data.get("library_work_id"):
            task = load_task(adjacent.parent, verify_audio=False)
            if (task.identity["sha256"] != identity["sha256"] or
                    task.identity["normalized_path"] != identity["normalized_path"]):
                raise TaskError("媒体库音频身份不匹配，未接管或覆盖。")
            return task
    root = _plain_path(Path(task_root) if task_root else Path(identity["path"]).parent / "EarlySakuraTasks")
    root.mkdir(parents=True, exist_ok=True)
    directory = _plain_path(root / identity["task_id"][:24])
    directory.mkdir(exist_ok=True)
    with task_lock(directory):
        manifest = directory / "task.json"
        if manifest.exists():
            task = load_task(directory, verify_audio=False)
            if task.identity["task_id"] != identity["task_id"]:
                raise TaskError("任务标识冲突；为保护数据已停止。")
            return task
        # After a crash during initial creation, only the lock and unpublished
        # manifest may exist. Never claim an orphaned directory containing results.
        unexpected = [p for p in directory.iterdir()
                      if p.name != ".task.lock" and not p.name.startswith("task.json.partial-")]
        if unexpected:
            raise TaskError("目录内已有文件但缺少任务清单；未自动接管或覆盖。")
        write_new(manifest, _json_bytes({"schema_version": SCHEMA_VERSION,
                  "created_utc": datetime.now(timezone.utc).isoformat(), "audio": identity}))
    return Task(directory, identity)


def task_identity(audio):
    """Read-only identity for a managed location, without creating a task."""
    audio = absolute_path(audio)
    adjacent = audio.parent / "task.json"
    if adjacent.is_file() and json.loads(_plain_path(adjacent).read_text(encoding="utf-8")).get("library_work_id"):
        task = load_task(audio.parent)
        if os.path.normcase(str(audio)) != task.identity["normalized_path"]:
            raise TaskError("媒体库路径与作品不匹配。")
        return task.identity
    return audio_identity(audio)


def import_legacy(task, sources):
    """Explicit association only. Copy bytes, reject conflicts; repeat is idempotent."""
    if not sources:
        return
    with task_lock(task.directory):
        task.verify_audio()
        pending, receipt = {}, []
        for name, source in sources.items():
            target = task.result(name)
            source = absolute_path(source)
            if (source.parent / "task.json").exists():
                owner = load_task(source.parent, verify_audio=False)
                if owner.identity["task_id"] != task.identity["task_id"]:
                    raise TaskError("所选文件已经属于另一个音频任务，未导入。")
            content = source.read_bytes()
            if not content:
                raise TaskError(f"不导入空结果文件：{source}")
            if target.exists() and target.read_bytes() != content:
                raise FileExistsError(f"任务已有不同内容，未覆盖：{target}")
            pending[name] = content
            receipt.append({"source": str(source), "destination": name,
                            "sha256": hashlib.sha256(content).hexdigest()})
        # Preflight the whole selection before writing any result. An interrupted
        # import may leave some complete files; importing again verifies/reuses them.
        for name, content in pending.items():
            target = task.result(name)
            if not target.exists():
                write_new(target, content)
        write_new(task.directory / ("import-" + uuid.uuid4().hex + ".json"),
                  _json_bytes({"created_utc": datetime.now(timezone.utc).isoformat(),
                               "audio_task_id": task.identity["task_id"], "files": receipt}))


def legacy_srt_name(audio):
    aliases = configuration().get('legacy_srt_aliases', {})
    if not isinstance(aliases, dict): raise TaskError('历史字幕别名配置必须是对象。')
    name = next((value for key, value in aliases.items() if key.casefold() == Path(audio).name.casefold()), None)
    if name is not None and (not isinstance(name, str) or Path(name).name != name or Path(name).suffix.lower() != '.srt'):
        raise TaskError('历史字幕别名必须是 SRT 文件名，不能包含目录。')
    return name


def legacy_sources(task):
    """Candidates only, never evidence of ownership; aliases are user configured."""
    audio = Path(task.identity["path"])
    sources = {name: audio.parent / name for name in RESULT_NAMES
               if (audio.parent / name).is_file()}
    alias = legacy_srt_name(audio)
    historical = audio.parent / alias if alias else None
    if historical is not None and historical.is_file() and "output.srt" not in sources:
        sources["output.srt"] = historical
    return sources


def map_legacy_selection(task, selected):
    sources = {}
    for item in selected:
        path = Path(item)
        name = path.name
        alias = legacy_srt_name(task.identity['path'])
        if alias and name.casefold() == alias.casefold():
            name = "output.srt"
        if name not in RESULT_NAMES:
            raise TaskError("请选择 japanese.txt、draft_bilingual.txt、final_bilingual.txt、output.srt 或为当前音频配置的历史字幕别名。")
        if name in sources:
            raise TaskError(f"同一结果类型只能选择一个文件：{name}")
        sources[name] = path
    return sources


def pipeline_block_reason(task):
    """Never start expensive inference while legacy ownership/conflicts are unresolved."""
    for name, source in legacy_sources(task).items():
        target = task.result(name)
        if not target.is_file() or file_hash(source) != file_hash(target):
            return f"音频旁有尚未导入或与任务冲突的旧结果：{source.name}。请先确认归属并导入；未启动转写或粗翻。"
    if any(task.result(name).exists() for name in ("final_bilingual.txt", "output.srt")):
        return "任务已有精修或字幕，但转写/粗翻未完整匹配。为保护旧作品，未自动补跑；请先检查或导入已有结果。"
    return None


def choose_import(task, sources=None, parent=None):
    """Dialogs explicitly ask the user to assert ownership; proximity is not proof."""
    from tkinter import Tk, filedialog, messagebox
    owned_root = None
    if parent is None:
        owned_root = parent = Tk()
        parent.withdraw()
    try:
        if sources is None:
            selected = filedialog.askopenfilenames(parent=parent, title="选择属于该音频的旧结果（取消则不导入）",
                        initialdir=str(Path(task.identity["path"]).parent), filetypes=[("文本/字幕", "*.txt *.srt")])
            if not selected:
                return False
            sources = map_legacy_selection(task, selected)
        detail = "\n".join(f"{path} → {name}" for name, path in sources.items())
        if not messagebox.askyesno("确认旧结果归属", f"音频：{task.identity['path']}\n\n以下文件确实属于这段音频吗？\n{detail}\n\n只复制到任务目录，原文件保留；冲突时停止。", parent=parent, default="no"):
            return False
        import_legacy(task, sources)
        return True
    finally:
        if owned_root:
            owned_root.destroy()


def prepare_audio_task(audio, offer_legacy=True, parent=None):
    task = open_task(audio)
    print(f"Task directory: {task.directory}")
    if (offer_legacy and not any(task.result(name).exists() for name in RESULT_NAMES)
            and legacy_sources(task)):
        choose_import(task, parent=parent)
    return task


def task_for_text(input_file, canonical_name):
    input_file = absolute_path(input_file)
    if (input_file.parent / "task.json").exists():
        task = load_task(input_file.parent)
        if os.path.normcase(str(input_file)) != os.path.normcase(str(absolute_path(task.result(canonical_name)))):
            raise TaskError(f"任务内应选择 {canonical_name}。")
        return task
    from tkinter import Tk, filedialog
    root = Tk()
    root.withdraw()
    try:
        audio = filedialog.askopenfilename(parent=root, title="为旧文本指定所属音频（原文件保留）",
                    filetypes=[("Audio files", "*.m4a *.mp3 *.wav"), ("All files", "*.*")])
        if not audio:
            raise TaskError("已取消关联音频，未处理旧文件。")
        task = open_task(audio)
        sources = {canonical_name: input_file}
        if canonical_name == "japanese.txt":
            old_draft = input_file.parent / "draft_bilingual.txt"
            if old_draft.is_file():
                sources["draft_bilingual.txt"] = old_draft
        if not choose_import(task, sources, parent=root):
            raise TaskError("已取消旧文件导入。")
        return task
    finally:
        root.destroy()



def check_draft_progress(japanese_path, draft_path):

    if Path(draft_path).with_name("draft_bilingual.txt.pending.json").exists():
        raise TaskError("粗翻写入曾中断，存在 pending 标记；不能判为完成，原文件保留，需人工检查。")

    with open(japanese_path, "r", encoding="utf-8-sig") as file:
        japanese_lines = [
            line.strip()
            for line in file
            if line.strip()
        ]

    total = len(japanese_lines)

    if total == 0:
        raise ValueError("japanese.txt is empty.")

    # 没有草稿：从第一句开始
    if not draft_path.exists():
        return 0, total

    with open(draft_path, "r", encoding="utf-8-sig") as file:
        content = file.read().strip()

    if not content:
        return 0, total

    # 按空行分隔每组翻译
    blocks = re.split(r"\n\s*\n", content)

    translated_count = len(blocks)

    if translated_count > total:
        raise ValueError(
            "Draft contains more sentences than japanese.txt."
        )

    # 逐句检查
    for index, block in enumerate(blocks, start=1):

        lines = block.splitlines()

        if len(lines) != 3:
            raise ValueError(
                f"Invalid draft format at sentence {index}."
            )

        original = japanese_lines[index - 1]

        if "] " not in original:
            raise ValueError(
                f"Invalid Japanese timestamp at sentence {index}."
            )

        timestamp, japanese = original.split("] ", 1)

        expected_timestamp = timestamp + "]"
        expected_japanese = f"JP: {japanese}"

        if lines[0] != expected_timestamp:
            raise ValueError(
                f"Timestamp mismatch at sentence {index}."
            )

        if lines[1] != expected_japanese:
            raise ValueError(
                f"Japanese text mismatch at sentence {index}."
            )

        if not lines[2].startswith("ZH_DRAFT: "):
            raise ValueError(
                f"Missing Chinese translation at sentence {index}."
            )

        if not lines[2][len("ZH_DRAFT: "):].strip():
            raise ValueError(
                f"Empty Chinese translation at sentence {index}."
            )

    return translated_count, total


def main():
    """Optional import tool; the original five entry points remain available."""
    from tkinter import Tk, filedialog, messagebox
    root = Tk()
    root.withdraw()
    try:
        audio = filedialog.askopenfilename(parent=root, title="选择音频以打开任务或导入旧结果")
        if audio:
            task = open_task(audio)
            choose_import(task, parent=root)
            messagebox.showinfo("任务目录", str(task.directory), parent=root)
    except (OSError, TaskError) as error:
        messagebox.showerror("操作已停止", str(error), parent=root)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
