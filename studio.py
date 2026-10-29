"""Small Tk launcher. Business scripts run only in explicit worker processes."""
import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime
import multiprocessing
import math
import os
from pathlib import Path
import queue
import re
import runpy
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import uuid

import task_store as store
from windows_job import ProcessTree, Cancelled
from playback_history import HistoryStore, HistoryError, display_time, clear_confirmed, reopen_task
import batch_queue as batches
import app_runtime as runtime
from media_library import LibraryStore
from player_ipc import encode_command

ROOT = Path(__file__).absolute().parent
TEST_MARKERS = ("端到端测试字幕", "非正式精修")
STAMP = re.compile(r"\[(\d{2}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2}):([0-5]\d):([0-5]\d),(\d{3})\]")
LEGACY_STAMP = re.compile(r"\[(\d{2}:[0-5]\d:[0-5]\d) --> (\d{2}:[0-5]\d:[0-5]\d)\]")


def validate_final(path, allow_legacy_seconds=False):
    """Validate format, never certify translation quality or authorship."""
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    if any(marker in text for marker in TEST_MARKERS):
        raise store.TaskError("检测到非正式测试精修标记，不能作为正式精修生成字幕。")
    if not text:
        raise store.TaskError("精修文件为空。")
    blocks = re.split(r"\n\s*\n", text)
    previous = -1
    for index, block in enumerate(blocks, 1):
        lines = [line.strip() for line in block.splitlines()]
        if lines and allow_legacy_seconds:
            # Validation in memory only: never rewrite a legacy final file.
            old_stamp = LEGACY_STAMP.fullmatch(lines[0])
            if old_stamp:
                lines[0] = f"[{old_stamp[1]},000 --> {old_stamp[2]},000]"
        match = STAMP.fullmatch(lines[0]) if lines else None
        if (len(lines) != 3 or not match or not lines[1].startswith("JP:")
                or not lines[1][3:].strip() or not lines[2].startswith("ZH_FINAL:")
                or not lines[2][9:].strip()):
            raise store.TaskError(f"第 {index} 块应为时间戳、非空 JP:、非空 ZH_FINAL: 三行。")
        h, m, s, ms, eh, em, es, ems = map(int, match.groups())
        start = ((h * 60 + m) * 60 + s) * 1000 + ms
        end = ((eh * 60 + em) * 60 + es) * 1000 + ems
        # Legacy whole-second timestamps may collapse a subsecond cue to 0s.
        # This mode only labels an existing legacy file, never enables generation.
        if end < start or (end == start and not allow_legacy_seconds) or start < previous:
            raise store.TaskError(f"第 {index} 块时间范围无效或顺序倒退。")
        previous = start
    return len(blocks)


def inspect_task(task):
    states = {}
    candidates = store.legacy_sources(task)
    for name in store.RESULT_NAMES:
        path = task.result(name)
        states[name] = f"已存在（{path.stat().st_size:,} 字节）" if path.is_file() else "未生成"
        if not path.exists() and name in candidates:
            states[name] = f"未导入：发现 {candidates[name].name}（归属待确认）"
    complete, final_ok = False, False
    jp, draft = task.result("japanese.txt"), task.result("draft_bilingual.txt")
    if jp.is_file():
        try:
            done, total = store.check_draft_progress(jp, draft)
            states["draft_bilingual.txt"] = f"粗翻进度 {done}/{total}" + ("（未完成）" if done < total else "")
            complete = done == total and total > 0
        except (ValueError, OSError, UnicodeError) as error:
            states["draft_bilingual.txt"] = f"需检查：{error}"
    elif draft.exists():
        states["draft_bilingual.txt"] = "缺少日文原文，不能继续处理"
    partials = list(task.directory.glob("japanese.txt.partial-*"))
    if partials and not jp.exists():
        states["japanese.txt"] = f"转写未完成（保留 {len(partials)} 个中断文件）"
    final = task.result("final_bilingual.txt")
    if final.is_file():
        try:
            count = validate_final(final)
            states["final_bilingual.txt"] = f"格式有效（{count} 条），精修质量待人工确认"
            final_ok = True
        except (ValueError, OSError, UnicodeError) as error:
            states["final_bilingual.txt"] = str(error)
            try:
                count = validate_final(final, allow_legacy_seconds=True)
                states["final_bilingual.txt"] = f"旧版秒级精修（{count} 条），原样保留；请导入配套 SRT"
            except (ValueError, OSError, UnicodeError):
                pass
    srt = task.result("output.srt")
    playable = srt.is_file() and srt.stat().st_size > 0
    if playable:
        try:
            if any(marker in srt.read_text(encoding="utf-8-sig") for marker in TEST_MARKERS):
                states["output.srt"] = "测试字幕（非正式成果，可预览）"
        except (OSError, UnicodeError) as error:
            states["output.srt"] = f"字幕无法读取：{error}"
            playable = False
    for name, source in candidates.items():
        target = task.result(name)
        if target.is_file() and store.file_hash(source) != store.file_hash(target):
            states[name] += "；与音频旁旧文件冲突，未覆盖"
    return states, complete, final_ok, playable


def worker(mode, audio, srt_plan=None, player_control=False, expected_task=None):
    # No Tk or model work is triggered merely by importing this entry module.
    if mode in ("transcribe", "translate"):
        runtime.run_script(mode + ".py", audio)
        return
    if expected_task and store.task_identity(audio)["task_id"] != expected_task:
        raise store.TaskError("音频身份已改变，与队列不匹配；未启动模型。")
    task = store.prepare_audio_task(audio, offer_legacy=False)
    if expected_task and task.identity["task_id"] != expected_task:
        raise store.TaskError("任务准备期间音频身份改变；未启动模型，原队列与作品结果保留。")
    if mode == "pipeline":
        if inspect_task(task)[1]:
            print("已有完整转写和粗翻，已跳过处理。", flush=True)
            return
        reason = store.pipeline_block_reason(task)
        if reason:
            raise store.TaskError(reason)
        import run_pipeline
        run_pipeline.main(audio, offer_legacy=False)
        # The legacy pipeline sometimes returns normally on failure. Verify files.
        if not inspect_task(task)[1]:
            raise store.TaskError("流程已停止，但转写/粗翻尚未完整完成；请检查上方日志。")
        return
    if mode == "srt":
        from make_srt import generate_srt
        if srt_plan is None:
            raise store.TaskError("缺少字幕来源与替换确认，未生成 SRT。")
        generate_srt(task, srt_plan)
        return
    else:
        script, argument = "floating_subtitle_player.py", task.identity["path"]
    sys.argv = [str(ROOT / script), str(argument)]
    if mode == "player" and player_control:
        sys.argv.append("--studio-control")
    runpy.run_path(str(ROOT / script), run_name="__main__")


def worker_command(mode, audio, gated=False):
    return runtime.worker_command(mode, audio, gated, source_root=ROOT)


def quick_duration(audio):
    """Optional metadata probe with a hard timeout; never decode audio or load AI."""
    probe = runtime.external_program("ffprobe")
    if not probe:
        return "未知"
    try:
        result = subprocess.run([probe, "-v", "error", "-probesize", "1048576",
            "-analyzeduration", "1000000", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        seconds = float(result.stdout.strip())
        if result.returncode or not math.isfinite(seconds) or seconds <= 0:
            return "未知"
        seconds = round(seconds)
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "未知"


def processing_preview(task):
    """Read-only description; the worker still performs all authoritative checks."""
    jp, draft = task.result("japanese.txt"), task.result("draft_bilingual.txt")
    jp_ok = jp.is_file() and jp.stat().st_size > 0
    complete, blocked, counts = False, None, ""
    if jp_ok:
        try:
            done, total = store.check_draft_progress(jp, draft)
            complete = total > 0 and done == total
            counts = f"（{done} / {total} 条）"
        except (ValueError, OSError, UnicodeError) as error:
            blocked = str(error)
    elif jp.exists():
        blocked = "已有日文文件为空或无效，请先检查。"
    elif draft.exists():
        blocked = "已有粗翻但缺少日文原文，请先检查或导入。"
    if not complete and not blocked:
        blocked = store.pipeline_block_reason(task)
    plan = ("所有处理阶段已完成，无需重新处理" if complete else
            "复用已有日文，继续 Qwen 粗翻" if jp_ok else
            "开始 Whisper 日文转写，完成后自动进行 Qwen 粗翻")
    if blocked:
        plan = "暂不能开始：" + blocked
    states = {
        "japanese.txt": "已完成" if jp_ok else "未生成" if not jp.exists() else "未完成（需检查）",
        "draft_bilingual.txt": ("已完成" if complete else "未完成") + counts,
        "final_bilingual.txt": "存在" if task.result("final_bilingual.txt").is_file() else "不存在",
        "output.srt": "存在" if task.result("output.srt").is_file() else "不存在",
    }
    return {"filename": Path(task.identity["path"]).name, "duration": quick_duration(task.identity["path"]),
            "states": states, "plan": plan, "blocked": blocked, "complete": complete}


class ProcessingProgress:
    """Stage-local values derived only from the existing worker output."""
    def __init__(self):
        self.stage = "准备"
        self.done = self.total = self.batch_end = None
        self.percent = None
        self.label = "准备处理，等待实际进度……"

    def feed(self, text):
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        if "Starting Whisper process" in text:
            self.stage, self.percent, self.label = "Whisper", None, "Whisper 日文转写中（进度未知）"
        elif "Whisper process exited" in text or "Whisper transcription completed" in text or "Skipping Whisper" in text:
            self.stage, self.percent, self.label = "准备粗翻", None, "转写已完成或已复用，正在准备 Qwen 粗翻……"
        elif "Loading Qwen" in text:
            self.stage, self.percent, self.label = "准备粗翻", None, "正在加载 Qwen（非翻译进度）……"
        elif "Starting Qwen translation" in text:
            self.stage, self.percent, self.label = "Qwen", None, "Qwen 粗翻中，等待实际条数……"
        if self.stage == "Whisper":
            # tqdm seconds counter is specific to faster-whisper, unlike CLI load %.
            match = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)%\|[^\r\n]*?\|\s*(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)\s*\[[^\r\n]*seconds/s", text)
            if match:
                percent, position, total = map(float, match.groups())
                if 0 <= percent <= 100 and total > 0 and 0 <= position <= total + 0.001:
                    actual = min(100.0, position / total * 100)
                    if abs(actual - percent) <= 1.1:
                        self.percent = actual
                        self.label = f"Whisper 日文转写：{actual:.1f}%（音频处理进度）"
        progress = re.fullmatch(r"Translation progress: (\d+)/(\d+)", text.strip())
        total = re.fullmatch(r"共有 (\d+) 句日文", text.strip())
        done = re.fullmatch(r"已经翻译 (\d+) 句", text.strip())
        batch = re.fullmatch(r"正在翻译\s+(\d+)\s*-\s*(\d+)", text.strip())
        if progress:
            self.done, self.total = map(int, progress.groups())
        if total:
            self.total = int(total[1])
        if done:
            self.done = int(done[1])
        if batch:
            start, end = map(int, batch.groups())
            self.batch_end = end if (self.done is not None and self.total is not None
                and start == self.done + 1 and start <= end <= self.total) else None
        if text.strip() == "这一批已保存。" and self.batch_end is not None:
            self.done, self.batch_end = self.batch_end, None
        if progress or total or done or batch or text.strip() == "这一批已保存。":
            if self.total and self.done is not None and 0 <= self.done <= self.total:
                self.stage = "Qwen"
                self.percent = self.done / self.total * 100
                self.label = f"Qwen 粗翻：{self.done} / {self.total} 条（已保存）"
                if self.batch_end is not None:
                    self.label += f"；正在处理 {self.done + 1}–{self.batch_end} 条"
        # Model/log completion is provisional; only the parent exit + output
        # validation event may display task completion.


def pipeline_outcome(kind, status, cancelled=False):
    if kind != "done":
        return "处理失败（已有结果保留）"
    if cancelled or status.startswith("取消完成"):
        return "已取消（已完成阶段保留）"
    return "处理完成"


@dataclass(eq=False)
class ActiveJob:
    mode: str
    task: object
    tree: object
    process: object = None
    stop_requested: bool = False
    recovery_required: bool = False
    control_lock: object = field(default_factory=threading.Lock)
    log_tail: list = field(default_factory=list)
    archive_started: object = field(default_factory=threading.Event)


class Studio:
    def __init__(self, root):
        self.root, self.task = root, None
        self.events = queue.Queue()
        self.busy = False
        self.confirming = False
        self.processing_progress = ProcessingProgress()
        self._rendered_progress = None
        self.tree = None
        self.active_mode = None
        self.cancelling = False
        self.close_after_cancel = False
        self.recovery_required = False
        self.flags = (False, False, False)
        self.pipeline_job = None
        self.player_job = None
        self.pending_player_task = None
        self.exiting = False
        self.history = HistoryStore(runtime.data_root(ROOT) / "playback_history.sqlite3")
        self.history_window = None
        self.library = LibraryStore(runtime.data_root(ROOT) / "media_library.sqlite3")
        self.library_window = None
        self.library_busy_tasks = set()
        self.batch = None
        self.batch_error = ""
        self.batch_running = False
        self.batch_current = None
        self.batch_checking = False
        self.batch_generation = 0
        self.batch_target = None
        self.batch_failures = 0
        self.batch_window = None
        self.batch_notice = "队列未开始。"
        self._batch_rendered = None
        self._batch_draw_time = 0
        try:
            self.batch = batches.BatchQueue(runtime.data_root(ROOT) / "batch_queue.json")
        except (OSError, batches.QueueError) as error:
            self.batch_error = str(error)
        root.title("EarlySakura ASMR Studio")
        root.geometry("1200x850")
        root.minsize(1000, 680)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.pages = {}
        for key, label in (("library", "媒体库"), ("player", "播放器"), ("pipeline", "汉化任务"), ("batch", "任务队列"), ("history", "播放历史")):
            page = self.pages[key] = ttk.Frame(self.notebook)
            self.notebook.add(page, text=label)
        self.player_status = tk.StringVar(value="当前播放：未打开")
        self.pipeline_status = tk.StringVar(value="后台汉化：空闲")
        self.player_buttons = {}
        from player_page import PlayerPage
        self.player_page = PlayerPage(self, self.pages["player"])
        self.task_canvas = tk.Canvas(self.pages["pipeline"], highlightthickness=0)
        task_scroll = ttk.Scrollbar(self.pages["pipeline"], orient="vertical", command=self.task_canvas.yview)
        self.task_canvas.configure(yscrollcommand=task_scroll.set)
        task_scroll.pack(side="right", fill="y")
        self.task_canvas.pack(side="left", fill="both", expand=True)
        frame = ttk.Frame(self.task_canvas, padding=16)
        task_window = self.task_canvas.create_window(0, 0, window=frame, anchor="nw")
        def size_task(event=None):
            width = max(1, self.task_canvas.winfo_width())
            height = max(frame.winfo_reqheight(), self.task_canvas.winfo_height())
            self.task_canvas.itemconfigure(task_window, width=width, height=height)
            self.task_canvas.configure(scrollregion=(0, 0, width, height))
        self.task_canvas.bind("<Configure>", size_task)
        frame.bind("<Configure>", size_task)
        self.audio_name = tk.StringVar(value="尚未选择音频")
        self.audio_path = tk.StringVar()
        ttk.Label(frame, textvariable=self.audio_name, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(12, 4))
        ttk.Entry(frame, textvariable=self.audio_path, state="readonly").pack(fill="x")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=10)
        self.buttons = {}
        for key, label, action in (
            ("select", "选择音频", self.select_audio),
            ("pipeline", "开始处理", lambda: self.launch("pipeline")),
            ("cancel", "取消处理", self.cancel),
            ("refresh", "刷新文件状态", self.refresh),
            ("folder", "打开任务文件夹", self.open_folder),
            ("import", "导入已有结果", self.import_results),
        ):
            self.buttons[key] = ttk.Button(bar, text=label, command=action)
            self.buttons[key].pack(side="left", padx=(0, 8))
        files = ttk.LabelFrame(frame, text="当前任务文件", padding=10)
        files.pack(fill="x")
        self.file_states = {}
        for name in store.RESULT_NAMES:
            row = ttk.Frame(files)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=name, width=24).pack(side="left")
            state = self.file_states[name] = tk.StringVar(value="未选择任务")
            ttk.Label(row, textvariable=state, wraplength=590).pack(side="left", fill="x", expand=True)
        ttk.Label(frame, text="精修由你手动完成。格式检查不代表翻译质量；测试精修文本不能用于正式生成。", wraplength=850).pack(anchor="w", pady=8)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        for key, label in (("player", "打开字幕播放器"),):
            self.buttons[key] = ttk.Button(actions, text=label, command=lambda mode=key: self.launch(mode))
            self.buttons[key].pack(side="left", padx=(0, 8))
        self.buttons["history"] = ttk.Button(actions, text="播放历史", command=self.show_history)
        self.buttons["history"].pack(side="left", padx=(0, 8))
        self.buttons["stop_player"] = ttk.Button(actions, text="关闭播放器", command=self.stop_player)
        self.buttons["stop_player"].pack(side="left", padx=(0, 8))
        self.buttons["batch"] = ttk.Button(actions, text="批量汉化队列", command=self.show_batch)
        self.buttons["batch"].pack(side="left", padx=(0, 8))
        self.buttons["library"] = ttk.Button(actions, text="媒体库", command=self.show_library)
        self.buttons["library"].pack(side="left", padx=(0, 8))
        ttk.Label(frame, textvariable=self.player_status, wraplength=850).pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, textvariable=self.pipeline_status, wraplength=850).pack(anchor="w", pady=(4, 0))
        self.status = tk.StringVar(value="请选择音频。首次识别需读取文件，界面会保持响应。")
        ttk.Label(frame, textvariable=self.status, wraplength=850).pack(anchor="w", pady=(12, 4))
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 8))
        self.progress_text = tk.StringVar(value="尚未开始处理")
        ttk.Label(frame, textvariable=self.progress_text, wraplength=850).pack(anchor="w", pady=(0, 6))
        log_bar = ttk.Frame(frame)
        log_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(log_bar, text="运行日志").pack(side="left")
        self.buttons["clear_log"] = ttk.Button(log_bar, text="清空日志", command=self.clear_log)
        self.buttons["clear_log"].pack(side="right")
        self.log = ScrolledText(frame, height=12, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)
        footer = ttk.Frame(root, padding=(12, 0, 12, 6))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.player_status, wraplength=1100).pack(anchor="w")
        ttk.Label(footer, textvariable=self.pipeline_status, wraplength=1100).pack(anchor="w")
        self.notebook.bind("<<NotebookTabChanged>>", self.page_changed)
        self.show_library()
        self.update_buttons()
        self._drain_id = root.after(100, self.drain)

    def select_page(self, key):
        if getattr(self, "notebook", None) is not None and self.notebook.select() != str(self.pages[key]):
            self.notebook.select(self.pages[key])

    def page_changed(self, event=None):
        selected = self.notebook.select()
        if selected == str(self.pages["library"]): self.show_library()
        elif selected == str(self.pages["batch"]): self.show_batch()
        elif selected == str(self.pages["history"]): self.show_history()

    def update_buttons(self):
        pipeline = getattr(self, "pipeline_job", None)
        player = getattr(self, "player_job", None)
        for key, button in self.buttons.items():
            enabled = not self.busy and not getattr(self, "confirming", False) and not getattr(self, "exiting", False) and (key in ("select", "clear_log", "history", "batch", "library") or self.task is not None)
            if key == "pipeline":
                enabled = enabled and pipeline is None and not getattr(self, "batch_running", False) and not getattr(self, "batch_checking", False) and not self.task_in_use(self.task, "player")
            if key == "cancel":
                enabled = pipeline is not None and not self.cancelling and not (getattr(pipeline, "archive_started", None) and pipeline.archive_started.is_set())
            if key == "clear_log":
                enabled = enabled and pipeline is None and player is None and not getattr(self, "batch_running", False) and not getattr(self, "batch_checking", False)
            if key == "srt":
                enabled = enabled and any(self.task.result(name).exists() for name in
                                          ("final_bilingual.txt", "draft_bilingual.txt")) and not self.task_in_use(self.task)
            if key == "import":
                enabled = enabled and not self.task_in_use(self.task)
            if key == "player":
                enabled = enabled and Path(self.task.identity["path"]).is_file() and not self.task_in_use(self.task, "pipeline")
            if key == "stop_player":
                enabled = player is not None and not player.stop_requested
            button.configure(state="normal" if enabled else "disabled")
        for key, button in getattr(self, "player_buttons", {}).items():
            button.configure(state=self.buttons[key].cget("state"))
        if getattr(self, "player_page", None) is not None:
            self.player_page.update_controls()

    def task_in_use(self, task, mode=None):
        if task is None:
            return False
        if task.identity["task_id"] in getattr(self, "library_busy_tasks", set()):
            return True
        pending = getattr(self, "pending_player_task", None)
        def same(other):
            return (other.identity["task_id"] == task.identity["task_id"] or
                    os.path.normcase(os.path.abspath(other.identity["path"])) == os.path.normcase(os.path.abspath(task.identity["path"])))
        if mode in (None, "player") and pending is not None and same(pending):
            return True
        target = getattr(self, "batch_target", None)
        if mode in (None, "pipeline") and target is not None and same(target):
            return True
        return any(job is not None and same(job.task)
                   for kind in ((mode,) if mode else ("pipeline", "player"))
                   for job in (getattr(self, kind + "_job", None),))

    def background(self, title, operation, mode=None):
        if self.busy or getattr(self, "exiting", False):
            return
        self.busy = True
        self.active_mode = mode
        self.status.set(title)
        if getattr(self, "pipeline_job", None) is None:
            self.progress_text.set(title)
            self.progress.stop()
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)
        self.update_buttons()
        def execute():
            try:
                result = operation()
                self.events.put(("done", result))
            except Exception as error:
                unsafe = self.tree is not None and not self.tree.closed
                if unsafe:
                    try:
                        self.tree.finish()
                        unsafe = False
                    except Exception:
                        pass
                self.events.put(("unsafe" if unsafe else "error", str(error)))
        threading.Thread(target=execute, daemon=True).start()

    def select_audio(self):
        if self.busy or getattr(self, "exiting", False):
            return
        selected = filedialog.askopenfilename(parent=self.root, title="选择 ASMR 音频", initialdir=runtime.audio_directory(), filetypes=[("音频", "*.mp3 *.m4a *.wav *.flac *.ogg"), ("全部文件", "*.*")])
        if selected:
            def select():
                task = store.open_task(selected)
                self.register_library_inbox(task)
                return task, inspect_task(task), "任务已打开。可导入旧结果，或开始处理。"
            self.background("正在核对音频身份并打开任务……", select)

    def refresh(self):
        task = self.task
        self.background("正在刷新文件状态……", lambda: (task, inspect_task(task), "文件状态已刷新。"))

    def register_library_inbox(self, task):
        library = getattr(self, "library", None)
        if library is not None and library.in_inbox(task.identity["path"]):
            library.register(task)

    def show_library(self):
        self.select_page("library")
        if self.exiting:
            return
        try:
            from library_ui import LibraryWindow
            if self.library_window is None:
                self.library_window = LibraryWindow(self)
        except Exception as error:
            messagebox.showerror("媒体库暂不可用", str(error), parent=self.root)

    def show_batch(self):
        self.select_page("batch")
        if self.exiting:
            return
        if self.batch is None or self.batch_error:
            messagebox.showerror("批量队列不可用", self.batch_error, parent=self.root)
            return
        if self.batch_window is not None and self.batch_window.winfo_exists():
            return
        dialog = self.batch_window = self.pages["batch"]
        ttk.Label(dialog, text="按添加顺序逐部转写 → 粗翻；停止队列只停止后续任务，当前任务继续。", padding=10).pack(anchor="w")
        self.batch_summary = tk.StringVar()
        ttk.Label(dialog, textvariable=self.batch_summary, wraplength=1050, padding=8).pack(anchor="w")
        area = ttk.Frame(dialog)
        area.pack(fill="both", expand=True, padx=10)
        self.batch_list = ttk.Treeview(area, columns=("name", "state", "stage", "progress", "detail", "audio"), show="headings", selectmode="extended")
        for column, title, width in (("name", "作品", 160), ("state", "状态", 85), ("stage", "阶段", 100),
                                    ("progress", "实际进度", 110), ("detail", "说明 / 失败原因", 300), ("audio", "音频路径", 260)):
            self.batch_list.heading(column, text=title)
            self.batch_list.column(column, width=width, minwidth=60)
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.batch_list.yview)
        self.batch_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.batch_list.pack(side="left", fill="both", expand=True)
        horizontal = ttk.Scrollbar(dialog, orient="horizontal", command=self.batch_list.xview)
        self.batch_list.configure(xscrollcommand=horizontal.set)
        horizontal.pack(fill="x", padx=10)
        controls = ttk.Frame(dialog, padding=10)
        controls.pack(fill="x")
        self.batch_buttons = {}
        for key, label, callback in (("add", "添加音频", self.add_batch_audio), ("start", "开始队列", self.start_batch),
                                     ("stop", "停止队列（完成当前）", self.stop_batch),
                                     ("cancel", "取消选中任务", self.cancel_batch_selected),
                                     ("retry", "重试选中任务", self.retry_batch_selected),
                                     ("clear", "清空任务队列", self.clear_batch)):
            button = self.batch_buttons[key] = ttk.Button(controls, text=label, command=callback)
            button.pack(side="left", padx=(0, 8))
        self.batch_list.bind("<<TreeviewSelect>>", lambda event: self.render_batch(force=True))
        self._batch_rendered = None
        self.render_batch(force=True)

    def can_clear_batch(self):
        return (self.batch is not None and bool(self.batch.entries)
                and not (self.busy or self.exiting or self.confirming or self.batch_error
                         or self.batch_running or self.batch_checking or self.pipeline_job is not None)
                and not any(entry["state"] in batches.ACTIVE for entry in self.batch.entries))

    def clear_batch(self):
        if not self.can_clear_batch():
            return
        self.confirming = True
        self.update_buttons()
        self.render_batch(force=True)
        try:
            confirmed = messagebox.askyesno(
                "清空任务队列",
                f"清空全部 {len(self.batch.entries)} 条队列记录？\n"
                "只移除队列记录，不删除音频、TXT、SRT、任务目录或媒体库作品。\n"
                "播放历史和断点续播也会保留。",
                parent=self.batch_window or self.root, default="no")
        finally:
            self.confirming = False
            self.update_buttons()
            self.render_batch(force=True)
        if not confirmed or not self.can_clear_batch():
            return
        original = self.batch.entries
        self.batch.entries = []
        if not self.save_batch():
            self.batch.entries = original
        else:
            self.batch_current = self.batch_target = None
            self.batch_generation += 1
            self.batch_failures = 0
            self.batch_notice = "任务队列已清空；音频及全部成果文件保留。"
        self.update_buttons()
        self.render_batch(force=True)

    def save_batch(self):
        if self.batch_error:
            return False
        try:
            self.batch.save()
            return True
        except (OSError, batches.QueueError) as error:
            self.batch_running = False
            self.batch_error = f"队列写入失败：{error}。已停止自动启动后续任务；作品文件保留。"
            self.batch_notice = self.batch_error
            self.append_log(self.batch_error + "\n")
            messagebox.showerror("队列保存失败", self.batch_error, parent=self.root)
            return False

    def add_batch_audio(self):
        if self.busy or self.exiting or self.confirming or self.batch_error:
            return
        selected = filedialog.askopenfilenames(parent=self.batch_window, title="批量选择 ASMR 音频",
            initialdir=runtime.audio_directory(), filetypes=[("音频", "*.mp3 *.m4a *.wav *.flac *.ogg")])
        if not selected:
            return
        self.busy = True
        self.batch_notice = "正在后台核对音频身份并创建独立任务……"
        self.update_buttons()
        self.render_batch(force=True)
        def prepare():
            tasks, errors = [], []
            for audio in selected:
                try:
                    if Path(audio).suffix.lower() not in batches.AUDIO_SUFFIXES:
                        raise ValueError("不支持的音频格式")
                    task = store.open_task(audio)
                    self.register_library_inbox(task)
                    tasks.append(task)
                except Exception as error:
                    errors.append(f"{audio}：{error}")
            self.events.put(("batch_added", (tasks, errors)))
        threading.Thread(target=prepare, daemon=True).start()

    def start_batch(self):
        if self.busy or self.exiting or self.confirming or self.batch_error or self.batch_running or self.batch_checking:
            return
        if self.pipeline_job is not None:
            messagebox.showinfo("已有后台汉化", "请先等待或取消当前单部任务，再开始队列。", parent=self.root)
            return
        count = sum(entry["state"] == "pending" for entry in self.batch.entries)
        if not count:
            messagebox.showinfo("没有等待任务", "请添加音频，或选中失败 / 已取消 / 中断任务点击重试。", parent=self.root)
            return
        if not messagebox.askyesno("开始串行汉化", f"按列表顺序自动处理 {count} 部等待作品？\n每部先转写再粗翻；已有完整结果直接跳过。\n单项失败继续，服务 / 显存 / 磁盘错误或连续两项失败暂停。\n不会自动导入旧文件、生成精修或 SRT。", parent=self.batch_window or self.root, default="no"):
            return
        self.batch_running = True
        self.batch_failures = 0
        self.batch_notice = "队列运行中。"
        self.update_buttons()
        self.advance_batch()

    def stop_batch(self):
        if getattr(self, "batch", None) is None:
            return
        self.batch_running = False
        if self.batch_checking:
            entry = self.batch.get(self.batch_current)
            if entry is not None:
                entry.update(state="pending", stage="待核对", detail="队列已停止；未启动此任务。")
            self.batch_generation += 1
            self.batch_checking = False
            self.batch_current = self.batch_target = None
            self.save_batch()
        self.batch_notice = "队列已停止；当前任务继续完成，不再启动下一部。"
        self.update_buttons()
        self.render_batch(force=True)

    def cancel_batch_selected(self):
        if self.busy or self.exiting or self.confirming or self.batch_error:
            return
        selected = tuple(self.batch_list.selection())
        if not selected:
            return
        if not messagebox.askyesno("取消选中任务", "等待任务会标记为取消；当前任务会安全终止其进程树。\n其他等待任务继续，已完成结果保留。是否继续？", parent=self.batch_window, default="no"):
            return
        for task_id in selected:
            entry = self.batch.get(task_id)
            if task_id == self.batch_current and self.pipeline_job is not None:
                self.cancel()
            elif entry is not None and entry["state"] in ("pending", "checking", "interrupted", "failed"):
                if task_id == self.batch_current:
                    self.batch_generation += 1
                    self.batch_checking = False
                    self.batch_current = self.batch_target = None
                entry.update(state="cancelled", stage="未执行", progress="", detail="用户取消；已有文件保留。")
        self.save_batch()
        self.update_buttons()
        self.render_batch(force=True)

    def retry_batch_selected(self):
        if self.busy or self.exiting or self.confirming or self.batch_error:
            return
        selected = [self.batch.get(task_id) for task_id in self.batch_list.selection()]
        selected = [entry for entry in selected if entry and entry["state"] in ("failed", "cancelled", "interrupted")]
        if not selected:
            return
        if not messagebox.askyesno("重试选中任务", "将选中任务重新设为等待，启动前重新核对已有结果。\n有效阶段会复用；有 pending 标记或文件冲突时仍会阻止处理。\n队列运行中会自动处理，否则需点击开始队列。", parent=self.batch_window, default="no"):
            return
        for entry in selected:
            entry.update(state="pending", stage="待核对", progress="", detail="用户确认重试。")
        self.save_batch()
        self.render_batch(force=True)

    def advance_batch(self):
        if (not getattr(self, "batch_running", False) or self.batch_checking or self.pipeline_job is not None
                or self.busy or self.confirming or self.exiting or self.batch_error):
            return
        entry = self.batch.pending()
        if entry is None:
            self.batch_running = False
            self.batch_notice = "队列已结束；请查看各项完成、失败或取消状态。"
            self.update_buttons()
            return
        self.batch_current = entry["task_id"]
        self.batch_target = store.Task(Path(entry["directory"]), {"task_id": entry["task_id"], "path": entry["audio"]})
        if self.task_in_use(self.batch_target, "player"):
            self.batch_running = False
            self.batch_notice = f"队列已暂停：{Path(entry['audio']).name} 正在播放，请切换 / 关闭该播放器后重新开始队列。"
            self.batch_current = self.batch_target = None
            self.append_log(self.batch_notice + "\n")
            self.update_buttons()
            return
        self.batch_checking = True
        self.batch_generation += 1
        token = self.batch_generation
        entry.update(state="checking", stage="核对身份与结果", detail="")
        if not self.save_batch():
            self.batch_checking = False
            self.batch_current = self.batch_target = None
            return
        self.update_buttons()
        snapshot = entry.copy()
        def check():
            try:
                result = batches.check_entry(snapshot)
                self.events.put(("batch_checked", (token, snapshot["task_id"], result, None)))
            except Exception as error:
                self.events.put(("batch_checked", (token, snapshot["task_id"], None, str(error))))
        threading.Thread(target=check, daemon=True).start()

    def batch_failed(self, entry, reason):
        entry.update(state="failed", detail=reason, stage="失败")
        self.batch_failures += 1
        self.append_log(f"[队列 {Path(entry['audio']).name}] {reason}\n")
        if batches.global_failure(reason) or self.batch_failures >= 2:
            self.batch_running = False
            self.batch_notice = "队列已暂停：检测到全局错误或连续两项失败，请查看日志并修复原因后重试。"
            if not self.exiting:
                messagebox.showwarning("队列已暂停", self.batch_notice + "\n" + reason[-1500:], parent=self.root)

    def consume_batch_event(self, kind, value):
        if kind == "batch_added":
            tasks, errors = value
            self.busy = False
            added = sum(self.batch.add(task) for task in tasks)
            self.batch_notice = f"新增 {added} 项，重复 {len(tasks) - added} 项，无法添加 {len(errors)} 项。"
            self.save_batch()
            if errors:
                self.append_log("\n".join(errors) + "\n")
                messagebox.showwarning("部分音频无法添加", "\n".join(errors), parent=self.root)
        elif kind == "batch_checked":
            token, task_id, result, error = value
            if token != self.batch_generation or task_id != self.batch_current or not self.batch_checking:
                return  # Stopping/cancelling invalidates late preflight results.
            self.batch_checking = False
            entry = self.batch.get(task_id)
            if error:
                self.batch_failed(entry, error)
            elif result[1]:
                entry.update(audio=result[0].identity["path"], directory=str(result[0].directory),
                             results={name: str(result[0].result(name)) for name in store.RESULT_NAMES})
                entry.update(state="completed", stage="转写及粗翻完成", progress=result[3], detail="复用已有完整结果；未启动模型。")
                entry["completed_stages"] = ["Whisper", "Qwen"]
                self.batch_failures = 0
                if getattr(self, "library", None) is not None and Path(result[0].identity["path"]).parent.name == "待处理音频":
                    entry.update(state="checking", stage="复用结果并核对归档")
                    if self.save_batch():
                        self.start_job("pipeline", result[0])  # Worker skips models; parent archives.
                        if self.pipeline_job is not None:
                            self.batch_notice = "已有完整结果，正在核对归档。"
                            self.update_buttons()
                            return
            else:
                task, _, stage, progress = result
                entry.update(audio=task.identity["path"], directory=str(task.directory),
                             results={name: str(task.result(name)) for name in store.RESULT_NAMES})
                entry["completed_stages"] = ["Whisper"] if stage == "Qwen" else []
                entry.update(state="translating" if stage == "Qwen" else "transcribing", stage=stage, progress=progress, detail="")
                if self.save_batch():
                    self.start_job("pipeline", task)
                    if self.pipeline_job is not None:
                        self.batch_notice = f"正在处理：{Path(entry['audio']).name}"
                        self.update_buttons()
                        return
                    self.batch_failed(entry, "无法启动安全工作进程；未启动模型。")
            self.batch_current = self.batch_target = None
            self.save_batch()
        self.update_buttons()
        self.render_batch(force=True)

    def batch_progress(self, job):
        if getattr(self, "batch_current", None) != job.task.identity["task_id"]:
            return
        entry = self.batch.get(self.batch_current)
        progress = self.processing_progress
        state = ("translating" if progress.stage in ("Qwen", "准备粗翻") else
                 "transcribing" if progress.stage == "Whisper" else entry["state"])
        changed = entry["state"] != state or entry["stage"] != progress.stage
        entry.update(state=state, stage=progress.stage, progress=progress.label)
        if progress.stage in ("准备粗翻", "Qwen"):
            entry["completed_stages"] = ["Whisper"]
        if changed:
            self.save_batch()

    def batch_finished(self, job, kind, value):
        entry = self.batch.get(self.batch_current)
        if kind == "job_done" and value[1].startswith("取消完成"):
            entry.update(state="cancelled", stage="已安全取消", detail=value[1])
        elif kind == "job_done" and value[0] is not None and value[0][1]:
            entry.update(state="completed", stage="转写及粗翻完成", progress=value[0][0]["draft_bilingual.txt"], detail="结果完整性核对通过。")
            entry["completed_stages"] = ["Whisper", "Qwen"]
            self.batch_failures = 0
        else:
            reason = value if kind == "job_error" else "工作进程退出，但没有完整匹配的转写与粗翻结果。"
            self.batch_failed(entry, reason + "\n" + "".join(job.log_tail))
        self.batch_current = self.batch_target = None
        self.save_batch()

    def render_batch(self, force=False):
        window = getattr(self, "batch_window", None)
        if window is None or not window.winfo_exists():
            return
        now = time.monotonic()
        if not force and now - self._batch_draw_time < 0.5:
            return
        self._batch_draw_time = now
        entries = self.batch.entries
        rows = tuple((entry["task_id"], Path(entry["audio"]).name, batches.LABELS[entry["state"]],
                      entry["stage"], entry["progress"], entry["detail"], entry["audio"]) for entry in entries)
        if rows != self._batch_rendered:
            existing = set(self.batch_list.get_children())
            for task_id in existing - {row[0] for row in rows}:
                self.batch_list.delete(task_id)
            for row in rows:
                if row[0] in existing:
                    self.batch_list.item(row[0], values=row[1:])
                else:
                    self.batch_list.insert("", "end", iid=row[0], values=row[1:])
            self._batch_rendered = rows
        pending = sum(entry["state"] == "pending" for entry in entries)
        completed = sum(entry["state"] == "completed" for entry in entries)
        self.batch_summary.set(f"{self.batch_notice}　共 {len(entries)} 项，等待 {pending} 项，完成 {completed} 项。")
        idle = not self.busy and not self.confirming and not self.exiting and not self.batch_error
        selected = [self.batch.get(key) for key in self.batch_list.selection()]
        states = {"add": idle, "start": idle and pending > 0 and not self.batch_running and not self.batch_checking and self.pipeline_job is None,
                  "stop": not self.exiting and (self.batch_running or self.batch_checking),
                  "cancel": idle and any(entry and entry["state"] not in ("completed", "cancelled") for entry in selected),
                  "retry": idle and any(entry and entry["state"] in ("failed", "cancelled", "interrupted") for entry in selected),
                  "clear": self.can_clear_batch()}
        for key, button in self.batch_buttons.items():
            button.configure(state="normal" if states[key] else "disabled")

    def show_history(self):
        self.select_page("history")
        if self.exiting:
            return
        if self.history_window is not None and self.history_window.winfo_exists():
            self.reload_history()
            return
        dialog = self.history_window = self.pages["history"]
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="按最近播放时间排序；双击作品或选择后点击“打开并播放”。").pack(anchor="w", pady=(0, 8))
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        table = ttk.Treeview(table_frame, columns=("name", "time", "path"), show="headings", selectmode="browse")
        for key, label, width in (("name", "ASMR 文件", 230), ("time", "最近播放时间（本地）", 170), ("path", "原始音频路径", 420)):
            table.heading(key, text=label)
            table.column(key, width=width, minwidth=100, stretch=key != "time")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        message = tk.StringVar()
        ttk.Label(frame, textvariable=message).pack(anchor="w", pady=8)
        entries = {}

        def reload_entries():
            try:
                rows = self.history.list_entries()
            except HistoryError as error:
                message.set("历史暂不可用，原数据已保留。")
                messagebox.showwarning("无法读取播放历史", str(error), parent=dialog)
                return
            selected = table.selection()
            children = table.get_children()
            if children:
                table.delete(*children)
            entries.clear()
            for entry in rows:
                entries[entry["work_id"]] = entry
                table.insert("", "end", iid=entry["work_id"], values=(entry["audio_name"], display_time(entry["last_played_utc"]), entry["audio_path"]))
            if selected and selected[0] in entries: table.selection_set(selected[0])
            message.set(f"共 {len(rows)} 部作品" if rows else "暂无播放历史。实际开始播放后会自动记录。")
        self.reload_history = reload_entries

        def open_selected(event=None):
            selected = table.selection()
            if selected:
                self.open_history_entry(entries[selected[0]], dialog)

        def clear():
            if self.clear_history(dialog):
                reload_entries()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="打开并播放", command=open_selected).pack(side="left")
        ttk.Button(buttons, text="清空播放历史", command=clear).pack(side="left", padx=8)
        ttk.Button(buttons, text="刷新", command=reload_entries).pack(side="right")
        table.bind("<Double-1>", open_selected)
        table.bind("<Return>", open_selected)
        reload_entries()

    def clear_history(self, parent):
        try:
            return clear_confirmed(self.history, lambda title, text: messagebox.askyesno(title, text, parent=parent, default="no"))
        except HistoryError as error:
            messagebox.showwarning("无法清空播放历史", str(error), parent=parent)
            return False

    def open_history_entry(self, entry, dialog):
        if self.busy or self.confirming or getattr(self, "exiting", False):
            return
        if not Path(entry["audio_path"]).is_file():
            messagebox.showwarning("音频文件不存在", "原音频可能已移动或删除，历史记录已保留。\n"
                                   + entry["audio_path"] + "\n可用“选择音频”打开新位置。", parent=dialog)
            return
        self.select_page("player")
        def load():
            task = reopen_task(entry)
            return task, inspect_task(task), "历史作品已加载。"
        self.background("正在核对历史作品身份……", load, mode="history")

    def open_folder(self):
        try:
            os.startfile(str(self.task.directory))
        except OSError as error:
            messagebox.showerror("无法打开文件夹", str(error), parent=self.root)

    def import_results(self):
        task = self.task
        if self.task_in_use(task):
            messagebox.showinfo("任务正在使用", "请先结束此作品的播放或汉化，再导入结果。", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("导入已有结果（可只选一项）")
        dialog.transient(self.root)
        dialog.grab_set()
        selections = {}
        ttk.Label(dialog, text="分别选择需要导入的文件；未选择的项目保持不变。", padding=10).pack(anchor="w")
        def browse(name, variable):
            path = filedialog.askopenfilename(parent=dialog, title=f"选择 {name}",
                initialdir=str(Path(task.identity["path"]).parent),
                filetypes=[("字幕" if name == "output.srt" else "文本", "*.srt" if name == "output.srt" else "*.txt")])
            if not path:
                return
            try:
                mapping = store.map_legacy_selection(task, [path])
                if name not in mapping:
                    raise store.TaskError(f"此栏需要 {name}。")
            except store.TaskError as error:
                messagebox.showerror("无法选择", str(error), parent=dialog)
                return
            selections[name] = Path(path)
            variable.set(path)
        for name in store.RESULT_NAMES:
            row = ttk.Frame(dialog, padding=5)
            row.pack(fill="x")
            variable = tk.StringVar(value="未选择")
            ttk.Label(row, text=name, width=23).pack(side="left")
            ttk.Entry(row, textvariable=variable, width=55, state="readonly").pack(side="left")
            ttk.Button(row, text="选择…", command=lambda n=name, v=variable: browse(n, v)).pack(side="left")
            def clear(n=name, v=variable):
                selections.pop(n, None)
                v.set("未选择")
            ttk.Button(row, text="取消此项", command=clear).pack(side="left")
        def confirm():
            if not selections:
                messagebox.showinfo("尚未选择", "至少选择一个文件。", parent=dialog)
                return
            sources = dict(selections)
            detail = "\n".join(f"{p} → {n}" for n, p in sources.items())
            if not messagebox.askyesno("确认文件归属", f"以下文件确实属于音频：\n{task.identity['path']}\n\n{detail}\n\n仅复制，原文件不变；任务冲突时整批停止。", parent=dialog, default="no"):
                return
            dialog.destroy()
            def importing():
                store.import_legacy(task, sources)
                return task, inspect_task(task), "所选结果已导入，原文件保留。"
            self.background("正在导入并核对音频身份……", importing)
        ttk.Button(dialog, text="确认所选文件并导入", command=confirm).pack(pady=10)

    def confirm_start(self):
        task = self.task
        self.confirming = True
        self.update_buttons()
        dialog = tk.Toplevel(self.root)
        dialog.title("开始处理前确认")
        dialog.transient(self.root)
        dialog.grab_set()
        body = tk.StringVar(value=f"音频：{Path(task.identity['path']).name}\n\n正在读取已有结果与时长……")
        ttk.Label(dialog, textvariable=body, padding=18, wraplength=650, justify="left").pack(fill="both")
        controls = ttk.Frame(dialog, padding=12)
        controls.pack(fill="x")
        state = {"open": True, "info": None}
        results = queue.Queue()

        def dismiss():
            state["open"] = False
            self.confirming = False
            dialog.destroy()
            self.update_buttons()

        def accept():
            if state["info"] is None or state["info"]["blocked"]:
                return
            dismiss()
            self.launch("pipeline", confirmed=True)

        start = ttk.Button(controls, text="确认开始", command=accept, state="disabled")
        start.pack(side="left", padx=(0, 10))
        ttk.Button(controls, text="取消", command=dismiss).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", dismiss)
        dialog.bind("<Escape>", lambda event: dismiss())

        def read():
            try:
                results.put((processing_preview(task), None))
            except Exception as error:
                results.put((None, str(error)))

        def poll():
            if not state["open"]:
                return
            try:
                info, error = results.get_nowait()
            except queue.Empty:
                self.root.after(100, poll)
                return
            if error:
                body.set(f"无法核对已有结果：{error}\n\n未启动处理，请取消后检查。")
                return
            state["info"] = info
            body.set(f"音频：{info['filename']}\n音频总时长：{info['duration']}\n\n" +
                "\n".join(f"{name}：{value}" for name, value in info["states"].items()) +
                "\n\n预计执行：\n" + info["plan"] +
                "\n\n仅转写和粗翻属于本次处理阶段；精修和字幕不会被替换。\n开始后仍按原规则重新核对任务与文件保护。")
            start.configure(state="disabled" if info["blocked"] else "normal")

        threading.Thread(target=read, daemon=True).start()
        self.root.after(100, poll)

    def launch(self, mode, confirmed=False):
        if self.busy or getattr(self, "confirming", False) or getattr(self, "exiting", False):
            return
        if mode == "pipeline" and (getattr(self, "pipeline_job", None) is not None or getattr(self, "batch_running", False) or getattr(self, "batch_checking", False)):
            messagebox.showinfo("已有后台汉化", "本版一次只允许一部作品汉化。当前任务及目标保持不变。", parent=self.root)
            return
        if self.task_in_use(getattr(self, "task", None), "player" if mode == "pipeline" else "pipeline"):
            messagebox.showinfo("同作品正在使用", "为保护文件，本版不同时播放与处理同一作品；可以选择另一部音频。", parent=self.root)
            return
        if mode == "pipeline" and not confirmed:
            self.confirm_start()
            return
        task = self.task
        if mode == "player":
            self.start_player(task)
            return
        if mode == "pipeline":
            self.start_job("pipeline", task)
            return
        srt_plan = None
        if mode == "srt":
            if self.task_in_use(task):
                messagebox.showinfo("任务正在使用", "请先结束此作品的播放或汉化，再生成字幕。", parent=self.root)
                return
            from make_srt import prepare_srt, confirm_srt
            try:
                srt_plan = prepare_srt(task)
            except (ValueError, OSError, UnicodeError) as error:
                messagebox.showerror("无法生成 SRT", f"{error}\n\n没有更改字幕或 TXT 文件。", parent=self.root)
                return
            if not confirm_srt(srt_plan, lambda title, text: messagebox.askyesno(title, text, parent=self.root, default="no")):
                return
        title = {"pipeline": "正在核对任务并准备处理……", "srt": "正在生成 SRT……", "player": "播放器启动中；关闭悬浮窗口后可继续其他操作。"}[mode]
        try:
            self.tree = tree = ProcessTree()
        except OSError as error:
            messagebox.showerror("无法建立安全进程范围", f"未启动处理：{error}", parent=self.root)
            return
        def execute():
            log_dir = runtime.data_root(ROOT) / "gui_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + mode + "-" + uuid.uuid4().hex[:8] + ".log")
            self.events.put(("line", f"日志：{log_path}\n"))
            env = os.environ.copy()
            env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
            process = None
            try:
                with log_path.open("x", encoding="utf-8") as log:
                    command = worker_command(mode, task.identity["path"], gated=True)
                    if srt_plan is not None:
                        command += ["--srt-plan", json.dumps(srt_plan, ensure_ascii=False)]
                    process = tree.start(command, cwd=ROOT, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        self.events.put(("line", line))
                    code = process.wait()
            except Cancelled:
                code = 1223
            finally:
                tree.finish()
                if process is not None:
                    process.wait()
                    process.stdout.close()
            if tree.cancel_requested.is_set():
                with store.task_lock(task.directory):
                    pass
                return task, inspect_task(task), "取消完成：本任务进程树已退出，任务锁可用；完整阶段保留，中断结果见文件状态。"
            if code:
                raise store.TaskError(f"操作未完成（退出码 {code}）。已有结果已保留。\n详细日志：{log_path}")
            return task, inspect_task(task), {"pipeline": "转写与粗翻完成（已完成的结果自动复用）。", "srt": "SRT 已生成。", "player": "播放器已关闭。"}[mode]
        self.background(title, execute, mode=mode)

    def start_job(self, mode, task):
        if getattr(self, "exiting", False) or getattr(self, mode + "_job", None) is not None:
            return
        try:
            job = ActiveJob(mode, task, ProcessTree())
        except OSError as error:
            messagebox.showerror("无法建立安全进程范围", str(error), parent=self.root)
            return
        setattr(self, mode + "_job", job)
        is_batch = mode == "pipeline" and getattr(self, "batch_current", None) == task.identity["task_id"]
        name = Path(task.identity["path"]).name
        if mode == "pipeline":
            self.cancelling = self.recovery_required = False
            self.processing_progress = ProcessingProgress()
            self._rendered_progress = None
            self.pipeline_status.set(f"后台汉化：{name}；正在准备")
            self.progress_text.set("准备处理，等待实际进度……")
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)
        else:
            self.player_status.set(f"当前播放：{name}；播放器启动中")
            if getattr(self, "player_page", None) is not None:
                self.player_page.reset(clear=True)
        self.update_buttons()

        def execute():
            process = None
            def library_state(state, detail=""):
                if mode != "pipeline" or getattr(self, "library", None) is None:
                    return
                try:
                    work = self.library.get(task_id=task.identity["task_id"])
                    if work:
                        self.library.update(work["work_id"], auto_state=state, detail=detail)
                except (ValueError, OSError) as error:
                    self.events.put(("job_line", (job, f"[媒体库状态未更新] {error}\n")))
            try:
                library_state("translating" if task.result("japanese.txt").is_file() else "transcribing")
                translation_seen = False
                log_dir = runtime.data_root(ROOT) / "gui_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + mode + "-" + uuid.uuid4().hex[:8] + ".log")
                self.events.put(("job_line", (job, f"日志：{log_path}\n")))
                env = os.environ.copy()
                env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
                with log_path.open("x", encoding="utf-8") as log:
                    command = worker_command(mode, task.identity["path"], gated=True)
                    if is_batch:
                        command += ["--expected-task", task.identity["task_id"]]
                    if mode == "player":
                        command += ["--player-control"]
                    process = job.process = job.tree.start(command, keep_stdin=mode == "player", cwd=ROOT, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                    if mode == "player" and job.stop_requested:
                        self.send_player_close(job)
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        self.events.put(("job_line", (job, line)))
                        if mode == "pipeline" and not translation_seen and ("Starting Qwen translation" in line or line.startswith("Translation progress:")):
                            translation_seen = True
                            library_state("translating")
                    code = process.wait()
                job.tree.finish()
                if job.tree.cancel_requested.is_set():
                    if mode == "pipeline":
                        with store.task_lock(task.directory):
                            pass
                    status = "取消完成：本任务进程树已退出，任务锁可用；完整阶段保留。" if mode == "pipeline" else "播放器已强制关闭；恢复以最近成功保存的位置为准。"
                elif code:
                    raise store.TaskError(f"{name} 操作未完成（退出码 {code}）。已有结果已保留。\n详细日志：{log_path}")
                else:
                    status = "转写与粗翻完成（已完成的结果自动复用）。" if mode == "pipeline" else "播放器已关闭，最后保存流程已结束。"
                result = inspect_task(task) if mode == "pipeline" else None
                if mode == "pipeline":
                    library_state("cancelled" if job.tree.cancel_requested.is_set() or job.stop_requested else "completed")
                if mode == "pipeline" and result[1] and not job.tree.cancel_requested.is_set() and getattr(self, "library", None) is not None:
                    with job.control_lock:
                        stopped = job.stop_requested
                        if not stopped: job.archive_started.set()
                    if stopped:
                        status = "取消完成：处理阶段已退出，已完成结果保留；未自动归档。"
                    else:
                        archived = task
                        try:
                            if self.library.in_inbox(task.identity["path"]):
                                self.events.put(("job_archiving", (job, "正在校验并自动归档……")))
                                from library_files import archive_if_ready
                                archived = archive_if_ready(task, self.library)
                        except Exception as error:
                            self.events.put(("job_archive_error", (job, str(error))))
                            # A metadata failure cannot turn valid model output
                            # into a failed pipeline. Journal/alias allow recovery.
                            try:
                                archived = self.library.resolve(task.identity["task_id"]) or store.load_task(task.directory)
                            except (ValueError, OSError):
                                pass
                            try:
                                work = self.library.get(task_id=task.identity["task_id"])
                                if work:
                                    self.library.update(work["work_id"], detail="归档未完成：" + str(error),
                                        **({"archive_state": "failed"} if work["archive_state"] == "pending" else {}))
                            except (ValueError, OSError):
                                pass
                            status += " 自动归档未完成，请在媒体库恢复或重试。"
                        if archived.directory != task.directory:
                            self.events.put(("job_relocated", (job, archived)))
                        result = inspect_task(archived)
                self.events.put(("job_done", (job, (result, status))))
            except Cancelled:
                library_state("cancelled", "在启动前取消，原结果保留。")
                try:
                    job.tree.finish()
                    with store.task_lock(task.directory):
                        pass
                    self.events.put(("job_done", (job, (inspect_task(task), "取消完成：已在启动前取消，进程树已退出。"))))
                except Exception as error:
                    self.events.put(("job_unsafe", (job, str(error))))
            except Exception as error:
                library_state("failed", str(error))
                try:
                    job.tree.finish()
                except Exception as cleanup_error:
                    self.events.put(("job_unsafe", (job, f"{error}\n清理尚未确认：{cleanup_error}")))
                else:
                    self.events.put(("job_error", (job, str(error))))
            finally:
                if process is not None and (process.poll() is not None or job.tree.closed):
                    process.wait()
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
                    process.stdout.close()
        threading.Thread(target=execute, daemon=True).start()

    def start_player(self, task):
        if self.task_in_use(task, "pipeline"):
            messagebox.showinfo("此作品正在汉化", "请选择另一部已有字幕的作品播放。", parent=self.root)
            return
        job = getattr(self, "player_job", None)
        if job is not None:
            if job.task.identity["task_id"] == task.identity["task_id"] and not job.stop_requested:
                messagebox.showinfo("播放器已打开", "此作品已在播放器中，可使用悬浮窗口和全局快捷键。", parent=self.root)
                return
            if messagebox.askyesno("切换播放作品", "保存当前播放进度并关闭播放器，再打开所选作品？\n后台汉化继续运行。", parent=self.root, default="no"):
                self.pending_player_task = task
                self.stop_player(keep_pending=True)
            return
        self.start_job("player", task)

    def send_player_close(self, job):
        if job.process is None:
            return
        try:
            with job.control_lock:
                job.process.stdin.write("CLOSE\n")
                job.process.stdin.flush()
        except (OSError, ValueError) as error:
            if job.process.poll() is None:
                self.events.put(("player_control_error", (job, str(error))))

    def player_command(self, command, **fields):
        job = getattr(self, "player_job", None)
        if job is None or job.stop_requested or self.exiting:
            return
        try:
            message = encode_command(command, **fields)
        except ValueError as error:
            messagebox.showerror("播放器命令无效", str(error), parent=self.root)
            return
        def send():
            try:
                with job.control_lock:
                    if self.player_job is not job or job.stop_requested or job.process is None or job.process.poll() is not None:
                        return
                    job.process.stdin.write(message)
                    job.process.stdin.flush()
            except (OSError, ValueError) as error:
                if job.process is not None and job.process.poll() is None:
                    self.events.put(("player_control_error", (job, str(error))))
        threading.Thread(target=send, daemon=True).start()

    def stop_player(self, keep_pending=False):
        if not keep_pending:
            self.pending_player_task = None
        job = getattr(self, "player_job", None)
        if job is None or job.stop_requested:
            return
        job.stop_requested = True
        self.player_status.set(f"当前播放：{Path(job.task.identity['path']).name}；正在保存进度并关闭")
        self.update_buttons()
        threading.Thread(target=lambda: self.send_player_close(job), daemon=True).start()
        self.root.after(8000, lambda: self.check_player_stop(job))

    def check_player_stop(self, job):
        if self.player_job is not job:
            return
        if messagebox.askyesno("播放器尚未关闭", "尚未确认播放器保存并退出。是否只强制结束本次播放器？\n可能丢失最近未保存的进度；后台汉化不会被取消。", parent=self.root, default="no"):
            def force():
                try:
                    job.tree.cancel()
                    if job.recovery_required:
                        job.tree.finish()
                        self.events.put(("job_done", (job, (None, "播放器已强制关闭，使用最近成功保存的进度。"))))
                except Exception as error:
                    self.events.put(("player_control_error", (job, str(error))))
            threading.Thread(target=force, daemon=True).start()
        else:
            self.exiting = False
            job.stop_requested = False
            self.update_buttons()

    def cancel(self):
        job = getattr(self, "pipeline_job", None)
        if job is None or self.cancelling:
            return
        with job.control_lock:
            if job.archive_started.is_set():
                messagebox.showinfo("正在归档", "模型已退出，正在校验与提交文件；请等待归档结束。关闭主窗口也会等待此步骤。", parent=self.root)
                return
            job.stop_requested = True
        self.cancelling = True
        self.pipeline_status.set(f"后台汉化：{Path(job.task.identity['path']).name}；正在取消")
        self.progress_text.set("正在取消（尚未确认结束）……")
        self.update_buttons()
        tree = job.tree
        recovery = job.recovery_required
        def request():
            try:
                tree.cancel()
                if recovery:
                    tree.finish()
                    with store.task_lock(job.task.directory):
                        pass
                    self.events.put(("job_done", (job, (inspect_task(job.task), "取消完成：本任务进程树已退出，任务锁可用。"))))
            except Exception as error:
                self.events.put(("job_cancel_error", (job, str(error))))
        threading.Thread(target=request, daemon=True).start()

    def clear_log(self):
        if self.busy or getattr(self, "pipeline_job", None) is not None or getattr(self, "player_job", None) is not None or getattr(self, "batch_running", False) or getattr(self, "batch_checking", False):
            return
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "501.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def render_progress(self):
        job = getattr(self, "pipeline_job", None)
        if job is None or self.cancelling or self.recovery_required:
            return
        if getattr(job, "archive_started", None) and job.archive_started.is_set():
            return
        progress = self.processing_progress
        snapshot = (progress.label, progress.percent)
        if snapshot == self._rendered_progress:
            return
        self._rendered_progress = snapshot
        self.pipeline_status.set(f"后台汉化：{Path(job.task.identity['path']).name}；{progress.stage}")
        self.progress_text.set(progress.label)
        self.progress.stop()
        if progress.percent is None:
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)
        else:
            self.progress.configure(mode="determinate", maximum=100, value=progress.percent)

    def finish_progress(self, kind, status, was_pipeline):
        if was_pipeline:
            outcome = pipeline_outcome(kind, status)
            self.progress_text.set(outcome)
            self.progress.configure(mode="determinate", value=100 if outcome == "处理完成" else 0)
        elif kind == "error":
            self.progress_text.set("操作失败")
        else:
            self.progress_text.set(status)

    def consume_job_event(self, kind, job, value):
        if getattr(self, job.mode + "_job", None) is not job:
            return False  # A late event cannot complete or cancel another run.
        name = Path(job.task.identity["path"]).name
        if kind == "job_archiving":
            self.pipeline_status.set(f"后台汉化：{name}；归档校验中")
            self.progress.stop()
            self.progress_text.set(value)
            self.update_buttons()
            return False
        if kind == "job_relocated":
            job.task = value
            if self.task is not None and self.task.identity["task_id"] == value.identity["task_id"]:
                self.task = value
                self.audio_path.set(value.identity["path"])
            batch = getattr(self, "batch", None)
            if batch is not None:
                entry = batch.get(value.identity["task_id"])
                if entry:
                    entry.update(audio=value.identity["path"], directory=str(value.directory), results={name: str(value.result(name)) for name in store.RESULT_NAMES})
                    self.save_batch()
            return False
        if kind == "job_archive_error":
            self.append_log(f"[归档未完成 {name}] {value}\n")
            if getattr(self, "batch_running", False):
                self.batch_running = False
                self.batch_notice = "归档未完成，队列已暂停；请在媒体库恢复或重试归档。"
            return False
        if kind == "job_line":
            if job.mode == "pipeline":
                job.log_tail.append(value[-2000:])
                del job.log_tail[:-40]
            if job.mode == "player" and value.startswith("STUDIO_PLAYER "):
                if job.stop_requested:
                    return False
                try:
                    info = json.loads(value[len("STUDIO_PLAYER "):])
                    labels = {"Playing": "正在播放", "Paused": "已暂停", "Ended": "已播放完", "Stopped": "已停止",
                              "Opening": "正在加载", "Buffering": "缓冲中", "Error": "播放异常", "等待恢复选择": "等待恢复选择"}
                    position, duration = info["position_ms"], info["duration_ms"]
                    if type(position) is not int or type(duration) is not int:
                        return False
                    def stamp(ms):
                        seconds = max(0, ms) // 1000
                        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
                    self.player_status.set(f"当前播放：{name}；{labels.get(info['state'], '准备中')}；{stamp(position)} / {stamp(duration)}")
                    if getattr(self, "player_page", None) is not None:
                        self.player_page.observe(info)
                except (KeyError, ValueError, TypeError):
                    pass
            else:
                if job.mode == "pipeline" and not self.cancelling:
                    for line in value.splitlines():
                        self.processing_progress.feed(line)
                    self.batch_progress(job)
                self.append_log(f"[{'汉化' if job.mode == 'pipeline' else '播放'} {name}] {value}")
            return False
        if kind in ("job_unsafe", "job_cancel_error", "player_control_error"):
            if job.mode == "pipeline" and getattr(self, "batch_current", None) == job.task.identity["task_id"]:
                self.batch_running = False
                self.batch_notice = "清理尚未确认；队列已暂停，请重试取消当前任务。"
            self.exiting = False
            job.recovery_required = job.recovery_required or kind == "job_unsafe"
            if job.mode == "pipeline":
                self.cancelling = False
                self.recovery_required = job.recovery_required
                self.pipeline_status.set(f"后台汉化：{name}；清理尚未确认，请重试取消")
                self.progress_text.set("尚未确认安全结束，不会报告取消成功。")
            else:
                self.player_status.set(f"当前播放：{name}；控制或关闭尚未确认，请查看日志或悬浮窗口")
            self.append_log(value + "\n")
            self.update_buttons()
            return False
        if kind not in ("job_done", "job_error"):
            return False
        setattr(self, job.mode + "_job", None)
        if job.mode == "pipeline":
            self.cancelling = self.recovery_required = False
            self.progress.stop()
            status = value[1] if kind == "job_done" else value
            outcome = pipeline_outcome("done" if kind == "job_done" else "error", status)
            self.pipeline_status.set(f"后台汉化：{name}；{outcome}")
            self.finish_progress("done" if kind == "job_done" else "error", status, True)
            if kind == "job_done" and value[0] is not None and not self.busy and self.task is not None and self.task.identity["task_id"] == job.task.identity["task_id"]:
                states, *self.flags = value[0]
                for filename, state in states.items():
                    self.file_states[filename].set(state)
        else:
            self.player_status.set(f"当前播放：{name}；{'已关闭' if kind == 'job_done' else '播放失败'}")
            if getattr(self, "player_page", None) is not None: self.player_page.reset()
        in_batch = job.mode == "pipeline" and getattr(self, "batch_current", None) == job.task.identity["task_id"]
        if in_batch:
            self.batch_finished(job, kind, value)
        if kind == "job_error":
            self.append_log(value + "\n")
            if not in_batch:
                messagebox.showerror("后台汉化未完成" if job.mode == "pipeline" else "播放器未完成", value, parent=self.root)
        self.update_buttons()
        if self.maybe_exit():
            return True
        if job.mode == "player" and self.pending_player_task is not None and not self.exiting:
            task, self.pending_player_task = self.pending_player_task, None
            self.start_player(task)
        return False

    def maybe_exit(self):
        if getattr(self, "exiting", False) and not self.busy and self.pipeline_job is None and self.player_job is None and not (getattr(self, "library_window", None) and self.library_window.busy):
            self.destroy_root()
            return True
        return False

    def drain(self):
        for _ in range(150):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind.startswith("batch_"):
                self.consume_batch_event(kind, value)
                continue
            if kind.startswith("job_") or kind == "player_control_error":
                job, payload = value
                if self.consume_job_event(kind, job, payload):
                    return
                continue
            if kind == "line":
                self.append_log(value)
                continue
            if kind in ("unsafe", "cancel_error"):
                if not self.busy:
                    continue
                self.exiting = False
                self.status.set("文件操作清理尚未确认；后台汉化和播放器状态保持独立。")
                self.append_log(value + "\n")
                self.update_buttons()
                continue
            was_pipeline = self.active_mode == "pipeline"
            was_history = self.active_mode == "history"
            self.busy = False
            self.active_mode = None
            self.tree = None
            if getattr(self, "pipeline_job", None) is None:
                self.progress.stop()
            if kind == "done":
                self.task, result, status = value
                states, *self.flags = result
                self.audio_name.set(Path(self.task.identity["path"]).name)
                self.audio_path.set(self.task.identity["path"])
                for name, state in states.items():
                    self.file_states[name].set(state)
                self.status.set(status)
                if getattr(self, "pipeline_job", None) is None:
                    self.finish_progress(kind, status, was_pipeline)
            else:
                self.status.set("处理失败，请查看日志；可点击刷新文件状态。")
                if getattr(self, "pipeline_job", None) is None:
                    self.finish_progress(kind, value, was_pipeline)
                self.append_log(value + "\n")
                messagebox.showerror("操作未完成", value, parent=self.root)
            self.update_buttons()
            if self.maybe_exit():
                return
            self.close_after_cancel = False
            if was_history and kind == "done":
                self.launch("player")
        self.render_progress()
        self.advance_batch()
        self.render_batch()
        self._drain_id = self.root.after(100, self.drain)

    def destroy_root(self):
        if getattr(self, "library_window", None) is not None:
            self.library_window.shutdown()
        if getattr(self, "_drain_id", None):
            self.root.after_cancel(self._drain_id)
            self._drain_id = None
        if getattr(self, "batch", None) is not None: self.batch.close()
        self.root.destroy()

    def close(self):
        if getattr(self, "library_window", None) is not None and self.library_window.busy:
            messagebox.showinfo("媒体库操作仍在运行", "请等待当前文件校验或数据库操作结束后再退出。", parent=self.root)
            return
        if self.busy:
            messagebox.showinfo("文件操作仍在运行", "请等待当前文件核对或写入结束后再退出。", parent=self.root)
            return
        if self.exiting:
            return
        if self.pipeline_job is not None or self.player_job is not None or getattr(self, "batch_running", False) or getattr(self, "batch_checking", False):
            if not messagebox.askyesno("结束操作并退出", "取消仍在运行的后台汉化，并保存播放进度、关闭播放器后退出？\n已完成结果保留，只处理本次启动的进程。", parent=self.root, default="no"):
                return
            self.exiting = True
            self.stop_batch()
            self.pending_player_task = None
            self.cancel()
            self.stop_player()
            self.update_buttons()
            self.maybe_exit()
        else:
            self.destroy_root()


def main():
    parser = argparse.ArgumentParser(description="EarlySakura ASMR Studio GUI")
    parser.add_argument("--worker", choices=("pipeline", "srt", "player", "transcribe", "translate"))
    parser.add_argument("--await-job", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--srt-plan", help=argparse.SUPPRESS)
    parser.add_argument("--player-control", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expected-task", help=argparse.SUPPRESS)
    parser.add_argument("--diagnose", action="store_true", help="检查运行依赖，不加载模型或创建任务")
    parser.add_argument("audio", nargs="?")
    args = parser.parse_args()
    runtime.setup_runtime()
    if args.diagnose:
        from packaging_checks import diagnose
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2), flush=True)
        return
    if args.worker:
        if not args.audio:
            parser.error("工作进程需要音频路径")
        if args.await_job and sys.stdin.readline() != "START\n":
            return
        worker(args.worker, args.audio, json.loads(args.srt_plan) if args.srt_plan else None, args.player_control, args.expected_task)
    else:
        root = tk.Tk()
        Studio(root)
        root.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
