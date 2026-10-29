"""Manual GUI cancellation rehearsal, isolated synthetic task and harmless workers."""
from pathlib import Path
import sys
import tkinter as tk
import uuid

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import studio
import task_store as store


def main():
    directory = ROOT / "work" / ("manual-cancel-" + uuid.uuid4().hex[:10])
    directory.mkdir(parents=True)
    audio = directory / "模拟音频-不进行解码.wav"
    audio.write_bytes(b"not-real-audio-cancel-test-only")
    task = store.open_task(audio)
    # Existing completed artifacts must survive cancellation byte-for-byte.
    store.write_new(task.result("final_bilingual.txt"), b"preserved-fixture-final\n")
    store.write_new(task.result("output.srt"), b"preserved-fixture-subtitle\n")
    def fixture_command(mode, selected_audio, gated=False):
        if mode != "pipeline" or str(selected_audio) != str(audio):
            raise store.TaskError("此验收窗口只允许模拟任务；不会启动任何真实业务。")
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        return [str(python), "-B", "-u", str(ROOT / "tests/cancel_worker_fixture.py"), str(task.directory)]
    studio.worker_command = fixture_command
    original_preview = studio.processing_preview
    def fixture_preview(selected_task):
        info = original_preview(selected_task)
        info.update(blocked=None, plan="仅启动无害模拟进程，供取消验收；不运行 Whisper 或 Qwen。")
        return info
    studio.processing_preview = fixture_preview
    root = tk.Tk()
    app = studio.Studio(root)
    normal_update = app.update_buttons
    def fixture_buttons():
        normal_update()
        for key in ("select", "import", "srt", "player", "history", "stop_player"):
            app.buttons[key].configure(state="disabled")
    app.update_buttons = fixture_buttons
    root.title("EarlySakura — 仅模拟取消验收，不运行模型")
    app.task = task
    app.audio_name.set(audio.name)
    app.audio_path.set(str(audio))
    app.flags = (False, False, False)
    app.refresh()
    print(f"模拟目录（保留供检查）：{directory}", flush=True)
    root.mainloop()


if __name__ == "__main__":
    main()
