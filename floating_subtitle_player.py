import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
import re
import queue
import threading
import time
from app_runtime import setup_runtime
setup_runtime()
import vlc
from pynput import keyboard
from task_store import prepare_audio_task, choose_import, task_lock
import atexit
from playback_history import HistoryStore, HistoryError
from playback_resume import ResumePlayback, choose_resume
from player_ipc import decode_command


# =========================
# 文件
# =========================

file_root = tk.Tk()
file_root.withdraw()

import argparse
parser = argparse.ArgumentParser(description="悬浮字幕播放器；不传音频时打开选择框。")
parser.add_argument("audio", nargs="?")
parser.add_argument("--studio-control", action="store_true", help=argparse.SUPPRESS)
args = parser.parse_args()
audio_file = args.audio
if audio_file is None:
    audio_file = filedialog.askopenfilename(
        title="Select ASMR audio file",
        filetypes=[("Audio files", "*.m4a *.mp3 *.wav"), ("All files", "*.*")]
    )

if not audio_file:
    print("No audio file selected.")
    exit()

AUDIO_FILE = Path(audio_file)

task = prepare_audio_task(AUDIO_FILE, parent=file_root, offer_legacy=not args.studio_control)
SUBTITLE_FILE = task.result("output.srt")

if not SUBTITLE_FILE.exists() and not args.studio_control:
    selected_srt = filedialog.askopenfilename(parent=file_root, title="选择属于当前音频的 SRT 字幕",
                                            filetypes=[("SRT subtitles", "*.srt")])
    if selected_srt:
        choose_import(task, {"output.srt": Path(selected_srt)}, parent=file_root)

print(f"Task subtitle: {SUBTITLE_FILE}" if SUBTITLE_FILE.exists() else "暂无字幕；仅播放音频。")

file_root.destroy()


# =========================
# 读取 SRT
# =========================

def time_to_ms(time_text):
    """
    00:01:23,456
    转换成毫秒
    """

    hours, minutes, rest = time_text.split(":")
    seconds, milliseconds = rest.split(",")

    return (
        int(hours) * 3600000
        + int(minutes) * 60000
        + int(seconds) * 1000
        + int(milliseconds)
    )


def load_subtitles(filename):
    with open(filename, "r", encoding="utf-8-sig") as file:
        content = file.read()

    blocks = re.split(r"\n\s*\n", content.strip())

    subtitles = []

    for block in blocks:
        lines = block.splitlines()

        if len(lines) < 3:
            continue

        time_line = lines[1]

        match = re.match(
            r"(\d{2}:\d{2}:\d{2},\d{3}) --> "
            r"(\d{2}:\d{2}:\d{2},\d{3})",
            time_line
        )

        if not match:
            continue

        start = time_to_ms(match.group(1))
        end = time_to_ms(match.group(2))

        text = "\n".join(lines[2:])

        subtitles.append((start, end, text))

    return subtitles


_playback_lease = task_lock(task.directory)
_playback_lease.__enter__()  # Relocation/refinement must wait for this player to exit.
atexit.register(lambda: _playback_lease.__exit__(None, None, None))
subtitles = load_subtitles(SUBTITLE_FILE) if SUBTITLE_FILE.is_file() else []


# =========================
# VLC
# =========================

instance = vlc.Instance()

media = instance.media_new(str(AUDIO_FILE))

player = instance.media_player_new()
player.set_media(media)


# =========================
# Tkinter 悬浮字幕
# =========================

root = tk.Tk()

root.title("EarlySakura ASMR Studio — 悬浮字幕")

# 没有普通窗口边框
root.overrideredirect(True)

# 永远保持在最前面
root.attributes("-topmost", True)

# 窗口大小
window_width = 900
window_height = 180

screen_width = root.winfo_screenwidth()
screen_height = root.winfo_screenheight()

x = (screen_width - window_width) // 2
y = screen_height - 220

root.geometry(
    f"{window_width}x{window_height}+{x}+{y}"
)

root.configure(bg="white")

# 窗口透明度
root.attributes("-alpha", 0.50)


# =========================
# 字幕文字
# =========================

subtitle_label = tk.Label(
    root,
    text="",
    font=("Microsoft YaHei UI", 18, "bold"),
    fg="black",
    bg="white",
    wraplength=820,
    justify="center"
)

subtitle_label.pack(
    expand=True,
    fill="both",
    padx=15,
    pady=8
)


# =========================
# 时间显示
# =========================

time_label = tk.Label(
    root,
    text="00:00:00 / 00:00:00",
    font=("Microsoft YaHei UI", 10),
    fg="black",
    bg="white"
)

time_label.pack(
    fill="x",
    padx=15
)


# =========================
# 进度条
# =========================

progress_bar = ttk.Progressbar(
    root,
    orient="horizontal",
    mode="determinate",
    maximum=100
)

progress_bar.pack(
    fill="x",
    padx=15,
    pady=(0, 8)
)

volume_row = ttk.Frame(root)
volume_row.pack(fill="x", padx=15, pady=(0, 6))
ttk.Label(volume_row, text="音量").pack(side="left", padx=(0, 8))
volume = tk.DoubleVar(value=100)
volume_scale = ttk.Scale(volume_row, from_=0, to=100, variable=volume,
                         command=lambda value: player.audio_set_volume(int(float(value))))
volume_scale.pack(side="left", fill="x", expand=True)


# =========================
# 字幕更新
# =========================

current_subtitle = ""

# 字幕偏移量
# 正数 = 字幕提前
# 负数 = 字幕延后
subtitle_offset_ms = 0
resume = None
closing_player = False
dragging_progress = False
save_warning_shown = False
control_commands = queue.Queue()
startup_closed = False
last_status_report = -float("inf")


def format_time(ms):
    if ms < 0:
        ms = 0

    total_seconds = ms // 1000

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return f"{hours:02}:{minutes:02}:{seconds:02}"


def update_subtitle():
    global current_subtitle
    if closing_player:
        return
    report_save_errors()
    try:
        ready = resume.tick()
    except HistoryError as error:
        subtitle_label.config(text="恢复或跳转失败，已暂停；原进度保留。")
        report_player_status(player.get_time(), player.get_length(), "Error")
        messagebox.showwarning("无法恢复播放", str(error), parent=root)
        ready = False
    if not ready or dragging_progress:
        root.after(50, update_subtitle)
        return

    current_time = player.get_time()
    total_time = player.get_length()
    if resume.finished and resume.last_snapshot is not None:
        # VLC can reset its clock at Ended; keep the display at the actual end.
        current_time, total_time, _ = resume.last_snapshot
    report_player_status(current_time, total_time)

    # 只偏移字幕时间
    subtitle_time = current_time + subtitle_offset_ms

    new_text = "暂无字幕" if not subtitles else ""

    if subtitle_time >= 0:

        for start, end, text in subtitles:

            if start <= subtitle_time <= end:
                new_text = text
                break

    if new_text != current_subtitle:

        current_subtitle = new_text
        subtitle_label.config(text=new_text)

    # 当前时间 / 总时长
    if total_time > 0:

        current_text = format_time(current_time)
        total_text = format_time(total_time)

        time_label.config(
            text=f"{current_text} / {total_text}"
        )

        progress = (current_time / total_time) * 100

        progress_bar["value"] = progress

    root.after(50, update_subtitle)


# =========================
# 播放控制
# =========================

def toggle_play(event=None):
    if closing_player:
        return

    if player.is_playing():
        resume.save_now()
        player.pause()

    else:
        player.play()


def seek(seconds):

    current = resume.pending_seek[0] if resume.pending_seek is not None else player.get_time()

    if current < 0:
        return

    new_time = current + seconds * 1000

    if new_time < 0:
        new_time = 0

    resume.jump_to(new_time)


def progress_drag(event):
    global dragging_progress
    dragging_progress = True
    percentage = max(0, min(100, event.x * 100 / max(1, progress_bar.winfo_width())))
    progress_bar["value"] = percentage
    return "break"


def progress_release(event):
    global dragging_progress, current_subtitle
    progress_drag(event)
    dragging_progress = False
    duration = player.get_length()
    if duration > 0 and resume.jump_to(int(duration * progress_bar["value"] / 100)):
        current_subtitle = None
    return "break"


progress_bar.bind("<Button-1>", progress_drag)
progress_bar.bind("<B1-Motion>", progress_drag)
progress_bar.bind("<ButtonRelease-1>", progress_release)


def seek_forward(event=None):
    seek(5)


def seek_backward(event=None):
    seek(-5)


# =========================
# 字幕偏移
# =========================

def subtitle_earlier(event=None):
    global subtitle_offset_ms, current_subtitle

    subtitle_offset_ms += 500
    current_subtitle = None

    print(f"Subtitle offset: {subtitle_offset_ms / 1000:+.1f}s")


def subtitle_later(event=None):
    global subtitle_offset_ms, current_subtitle

    subtitle_offset_ms -= 500
    current_subtitle = None

    print(f"Subtitle offset: {subtitle_offset_ms / 1000:+.1f}s")


def reset_subtitle_offset(event=None):
    global subtitle_offset_ms, current_subtitle

    subtitle_offset_ms = 0
    current_subtitle = None

    print("Subtitle offset: +0.0s")


# =========================
# 关闭播放器
# =========================

def close_player(event=None):
    global closing_player
    if closing_player:
        return
    closing_player = True
    hotkeys.stop()
    resume.close()  # Capture before VLC stop resets its reported clock.
    player.stop()
    time_label.config(text="正在保存播放进度并关闭……")
    wait_for_progress_save()


def report_save_errors():
    global save_warning_shown
    while True:
        try:
            error = resume.writer.errors.get_nowait()
        except queue.Empty:
            break
        print(f"播放进度未保存：{error}", flush=True)
        if not save_warning_shown:
            save_warning_shown = True
            messagebox.showwarning("播放进度未保存", error, parent=root)


def wait_for_progress_save():
    report_save_errors()
    if resume.writer.done.is_set():
        root.destroy()
    else:
        root.after(50, wait_for_progress_save)


def report_player_status(position, duration, state=None):
    global last_status_report
    if not args.studio_control or time.monotonic() - last_status_report < 1:
        return
    last_status_report = time.monotonic()
    import json
    print("STUDIO_PLAYER " + json.dumps({"state": state or resume.state(),
                                         "position_ms": max(0, position), "duration_ms": max(0, duration),
                                         "volume": player.audio_get_volume(),
                                         "controls_ready": bool(resume is not None and resume.ready and not resume.failed)}, ensure_ascii=False), flush=True)


def read_studio_commands():
    # This thread only reads a pipe and queues messages; it never calls Tk.
    for line in sys.stdin:
        if line.strip() == "CLOSE":
            control_commands.put("CLOSE")
        else:
            try:
                control_commands.put(decode_command(line))
            except (ValueError, TypeError) as error:
                print(f"忽略无效播放器命令：{error}", flush=True)
    control_commands.put("CLOSE")  # Parent closed its pipe.


def poll_studio_commands():
    global startup_closed, current_subtitle, last_status_report
    if closing_player:
        return
    try:
        command = control_commands.get_nowait()
    except queue.Empty:
        command = None
    if command == "CLOSE" or isinstance(command, dict) and command.get('command') == 'close':
        if resume is None:
            startup_closed = True
            for child in root.winfo_children():
                if isinstance(child, tk.Toplevel):
                    child.destroy()  # Cancel the resume choice without saving.
        else:
            close_player()
            return
    elif isinstance(command, dict) and resume is not None and resume.ready and not resume.failed:
        action = command['command']
        if action == 'toggle': toggle_play()
        elif action == 'relative': seek(command['seconds'])
        elif action == 'visibility': toggle_subtitle_visibility()
        elif action == 'volume': volume_scale.set(command['value'])
        elif action == 'seek':
            duration = player.get_length()
            if duration > 0 and resume.jump_to(min(command['position_ms'],duration)):
                current_subtitle = None
        last_status_report = -float('inf')
    root.after(100, poll_studio_commands)


root.protocol("WM_DELETE_WINDOW", close_player)


# =========================
# 隐藏字幕
# =========================

subtitle_hidden = False


def toggle_subtitle_visibility():
    global subtitle_hidden

    if subtitle_hidden:

        root.deiconify()
        root.attributes("-topmost", True)

        subtitle_hidden = False

    else:

        root.withdraw()

        subtitle_hidden = True


# =========================
# 隐藏字幕 + 暂停
# =========================

def toggle_hide_and_pause():
    global subtitle_hidden

    if subtitle_hidden:

        root.deiconify()
        root.attributes("-topmost", True)

        player.play()

        subtitle_hidden = False

    else:

        resume.save_now()
        player.pause()
        root.withdraw()

        subtitle_hidden = True


# =========================
# 拖动字幕位置
# =========================

drag_x = 0
drag_y = 0


def start_drag(event):

    global drag_x, drag_y

    drag_x = event.x
    drag_y = event.y


def drag_window(event):

    x = root.winfo_pointerx() - drag_x
    y = root.winfo_pointery() - drag_y

    root.geometry(f"+{x}+{y}")


subtitle_label.bind(
    "<Button-1>",
    start_drag
)

subtitle_label.bind(
    "<B1-Motion>",
    drag_window
)


# =========================
# 普通快捷键
# =========================

# 暂停 / 继续
root.bind("<space>", toggle_play)

# 前进 5 秒
root.bind("<Right>", seek_forward)

# 后退 5 秒
root.bind("<Left>", seek_backward)

# 退出
root.bind("<Escape>", close_player)


# =========================
# 全局快捷键
# =========================

def global_toggle():
    root.after(
        0,
        toggle_subtitle_visibility
    )


def global_hide_and_pause():
    root.after(
        0,
        toggle_hide_and_pause
    )


def global_toggle_play():
    root.after(
        0,
        toggle_play
    )


def global_subtitle_earlier():
    root.after(
        0,
        subtitle_earlier
    )


def global_subtitle_later():
    root.after(
        0,
        subtitle_later
    )


def global_reset_subtitle_offset():
    root.after(
        0,
        reset_subtitle_offset
    )


hotkeys = keyboard.GlobalHotKeys({

    # 隐藏 / 显示字幕
    "<ctrl>+<shift>+h":
        global_toggle,

    # 隐藏字幕并暂停 /
    # 恢复字幕并继续
    "<ctrl>+<shift>+j":
        global_hide_and_pause,

    # 全局暂停 / 继续
    "<ctrl>+<shift>+<space>":
        global_toggle_play,

    # 字幕提前 0.5 秒
    "<ctrl>+<shift>+u":
        global_subtitle_earlier,

    # 字幕延后 0.5 秒
    "<ctrl>+<shift>+i":
        global_subtitle_later,

    # 字幕偏移归零
    "<ctrl>+<shift>+o":
        global_reset_subtitle_offset

})

# =========================
# 开始
# =========================

if args.studio_control:
    import sys
    threading.Thread(target=read_studio_commands, daemon=True).start()
    root.after(100, poll_studio_commands)
history = HistoryStore()
try:
    saved = history.resume_info(task.identity["task_id"])
except HistoryError as error:
    saved = None
    messagebox.showwarning("播放进度不可用", str(error) + "\n本次可从头播放。", parent=root)
if saved is not None:
    report_player_status(saved["position_ms"], saved["duration_ms"], "等待恢复选择")
position = choose_resume(root, saved, format_time)
if position is None or startup_closed:
    player.release()
    instance.release()
    root.destroy()
    raise SystemExit(0)
resume = ResumePlayback(player, task, position, history)
hotkeys.start()
player.play()

root.after(
    100,
    update_subtitle
)

root.mainloop()
