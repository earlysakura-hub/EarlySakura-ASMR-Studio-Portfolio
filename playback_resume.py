"""VLC resume coordination and coalesced background writes to playback history."""
import queue
import threading
import time
import uuid

from playback_history import HistoryStore, HistoryError


class ProgressWriter:
    def __init__(self, task, history=None):
        self.task = task
        self.history = history if history is not None else HistoryStore()
        self.session = uuid.uuid4().hex
        self.errors = queue.Queue()
        self.done = threading.Event()
        self.condition = threading.Condition()
        self.pending = None
        self.stopping = False
        self.disabled = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, position, duration, completed=False):
        with self.condition:
            if not self.stopping and not self.disabled:
                # Rapid seeks/pauses replace a waiting snapshot, not the audio.
                self.pending = (position, duration, completed)
                self.condition.notify()

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()

    def _run(self):
        started = False
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.pending is not None or self.stopping)
                    if self.pending is None:
                        return
                    position, duration, completed = self.pending
                    self.pending = None
                try:
                    if not started:
                        self.history.begin_playback(self.task, self.session, position, duration)
                        started = True
                    if not self.history.save_progress(self.task.identity["task_id"], self.session,
                                                      position, duration, completed):
                        with self.condition:
                            self.disabled = True
                            self.pending = None
                        self.errors.put("历史已清空或另一播放器已接管此作品的进度，本窗口不会覆盖新记录。")
                except (HistoryError, OSError) as error:
                    self.errors.put(str(error))
        finally:
            self.done.set()


class ResumePlayback:
    def __init__(self, player, task, position=0, history=None, interval=5.0, clock=time.monotonic):
        self.player, self.target = player, position
        self.writer = ProgressWriter(task, history)
        self.interval, self.clock = interval, clock
        self.ready = False
        self.failed = False
        self.pending_seek = None
        self.last_snapshot = None
        self.last_saved = -float("inf")
        self.last_state = None
        self.finished = False
        self.closing = False

    def state(self):
        return str(self.player.get_state()).rsplit(".", 1)[-1]

    def _submit(self, position, duration, completed=False):
        self.last_snapshot = (position, duration, completed)
        self.last_saved = self.clock()
        self.writer.submit(position, duration, completed)

    def _seek(self, target):
        self.player.set_time(target)
        self.pending_seek = (target, self.clock() + 5.0)

    def tick(self):
        """False while seeking: don't render/save VLC's stale pre-seek time."""
        if self.closing or self.failed:
            return False
        state = self.state()
        position, duration = self.player.get_time(), self.player.get_length()
        valid = type(position) is int and type(duration) is int and 0 <= position <= duration and duration > 0
        if not self.ready and self.pending_seek is None:
            if state != "Playing" or not valid:
                return False
            if self.target > 0:
                if not self.player.is_seekable() or self.target >= duration:
                    self.failed = True
                    self.player.pause()
                    raise HistoryError("无法将此音频恢复到保存位置。已暂停，原进度保留；可关闭后选择从头播放。")
                self._seek(self.target)
                return False
            self.ready = True
        if self.pending_seek is not None:
            target, deadline = self.pending_seek
            if not valid or state not in ("Playing", "Paused") or abs(position - target) > 750:
                if self.clock() >= deadline:
                    self.failed = True
                    self.player.pause()
                    raise HistoryError("VLC 尚未确认跳转成功。已暂停，未用错误位置覆盖保存进度。")
                return False
            self.pending_seek = None
            self.ready = True
            self.finished = False
            self._submit(position, duration)
        if state == "Ended":
            if not self.finished and self.last_snapshot is not None:
                duration = self.last_snapshot[1]
                self._submit(duration, duration, True)
                self.finished = True
        elif state in ("Playing", "Paused") and valid:
            self.finished = False
            self.last_snapshot = (position, duration, False)
            if self.clock() - self.last_saved >= self.interval or (state == "Paused" and self.last_state != "Paused"):
                self._submit(position, duration)
        elif state in ("Stopped", "Error") and self.last_state != state and self.last_snapshot is not None:
            self._submit(*self.last_snapshot)
        self.last_state = state
        return self.ready

    def save_now(self):
        if not self.ready or self.failed or self.pending_seek is not None or self.closing:
            return
        if self.state() in ("Playing", "Paused"):
            position, duration = self.player.get_time(), self.player.get_length()
            if type(position) is int and type(duration) is int and 0 <= position <= duration and duration > 0:
                self._submit(position, duration)
                return
        if self.last_snapshot is not None:
            self._submit(*self.last_snapshot)

    def jump_to(self, position):
        if not self.ready or self.failed or self.closing:
            return False
        duration = self.player.get_length()
        if type(duration) is not int or duration <= 0 or not self.player.is_seekable():
            return False
        # Leave at least one second: dragging to 100% is not an Ended event.
        self._seek(max(0, min(int(position), max(0, duration - 1000))))
        return True

    def close(self):
        if self.closing:
            return
        try:
            self.tick()  # Observe an Ended/acknowledged seek just before stopping.
        except HistoryError as error:
            self.writer.errors.put(str(error))
        self.save_now()
        self.closing = True
        self.writer.close()


def choose_resume(root, saved, formatter):
    """None cancels; 0 starts over. This dialog never writes progress."""
    if saved is None:
        return 0
    import tkinter as tk
    from tkinter import ttk
    dialog = tk.Toplevel(root)
    dialog.title("恢复播放")
    dialog.transient(root)
    dialog.attributes("-topmost", True)
    dialog.resizable(False, False)
    frame = ttk.Frame(dialog, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=f"上次播放至：{formatter(saved['position_ms'])} / {formatter(saved['duration_ms'])}").pack(pady=(0, 14))
    buttons = ttk.Frame(frame)
    buttons.pack()
    choice = [None]
    def accept(position):
        choice[0] = position
        dialog.destroy()
    ttk.Button(buttons, text="继续上次播放", command=lambda: accept(saved["position_ms"])).pack(side="left", padx=4)
    ttk.Button(buttons, text="从头播放", command=lambda: accept(0)).pack(side="left", padx=4)
    ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=4)
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.bind("<Escape>", lambda event: dialog.destroy())
    dialog.grab_set()
    root.wait_window(dialog)
    return choice[0]
