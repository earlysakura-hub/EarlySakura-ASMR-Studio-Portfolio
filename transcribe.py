from pathlib import Path
from tkinter import Tk, filedialog
import sys
import os
from task_store import prepare_audio_task, task_lock, partial_path, publish_new, TaskError


def format_time(seconds):
    total_milliseconds = round(seconds * 1000)

    hours = total_milliseconds // 3600000
    minutes = (total_milliseconds % 3600000) // 60000
    secs = (total_milliseconds % 60000) // 1000
    milliseconds = total_milliseconds % 1000

    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

# =========================
# 获取音频文件
# =========================

if len(sys.argv) > 1:

    # 从 run_pipeline.py 接收音频路径
    audio_path = Path(sys.argv[1])

    print(f"Audio received: {audio_path}")

else:

    # 单独运行时仍然可以手动选择
    root = Tk()
    root.withdraw()

    audio_file = filedialog.askopenfilename(
        title="Select ASMR audio file",
        filetypes=[
            ("Audio files", "*.m4a *.mp3 *.wav"),
            ("All files", "*.*")
        ]
    )

    root.destroy()

    if not audio_file:
        print("No audio file selected.")
        sys.exit(1)

    audio_path = Path(audio_file)


# 检查音频文件是否存在
if not audio_path.is_file():
    print(f"Audio file not found: {audio_path}")
    sys.exit(1)


# 输出绑定到音频身份；已有结果和历史中断文件均保留。
task = prepare_audio_task(audio_path, offer_legacy=(len(sys.argv) == 1))
output_path = task.result("japanese.txt")
with task_lock(task.directory):
    if output_path.exists():
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise TaskError("已有日文结果无效；为保护数据未覆盖。")
        print(f"已有日文结果，未重新转写：{output_path}")
        sys.exit(0)
    temp_output_path = partial_path(output_path)
    from faster_whisper import WhisperModel

    model = WhisperModel(
        "medium",
        device="cuda",
        compute_type="int8_float16"
    )

    segments, info = model.transcribe(
        str(audio_path),
        language="ja",
        word_timestamps=True,
        log_progress=True
    )

    with open(temp_output_path, "x", encoding="utf-8") as file:
        for segment in segments:
            if segment.words:
                start = format_time(segment.words[0].start)
                end = format_time(segment.words[-1].end)
            else:
                start = format_time(segment.start)
                end = format_time(segment.end)

            text = segment.text.strip()

            line = f"[{start} --> {end}] {text}"

            print(line)
            file.write(line + "\n")
            file.flush()
        os.fsync(file.fileno())

    # =========================
    # 转写完成后保存正式文件
    # =========================

    if temp_output_path.stat().st_size == 0:
        raise TaskError("未识别出有效文本；保留临时文件，不生成正式日文结果。")
    task.verify_audio()
    publish_new(temp_output_path, output_path)

    print("\nWhisper transcription completed.")
    print(f"Japanese text saved to: {output_path}")