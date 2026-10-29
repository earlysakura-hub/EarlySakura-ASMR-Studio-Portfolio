from pathlib import Path
from tkinter import Tk, filedialog
import subprocess
import sys
import shutil
import json
from app_runtime import run_script_process, external_program, run_external, lm_studio_settings
from task_store import prepare_audio_task, check_draft_progress, pipeline_block_reason, TaskError


# =========================
# 选择音频
# =========================

def select_audio():
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
        return None

    return Path(audio_file)

# =========================
# 主程序
# =========================

def lm_command(command, **kwargs):
    kwargs.setdefault('timeout', 180)
    try:
        return run_external(command, **kwargs)
    except (OSError, subprocess.SubprocessError) as error:
        raise TaskError("模型服务不可用：LM Studio 命令失败或超时。请先打开 LM Studio，核对 lms、模型下载和服务状态；已有结果保留。") from error


def main(audio_path=None, offer_legacy=True):

    print("EarlySakura ASMR Studio")
    print("=======================")

    audio_path = Path(audio_path) if audio_path is not None else select_audio()

    if audio_path is None:
        print("No audio file selected.")
        return

    task = prepare_audio_task(audio_path, offer_legacy=offer_legacy)
    japanese_path = task.result("japanese.txt")
    draft_path = task.result("draft_bilingual.txt")

    # Completed files need neither a model nor an LM Studio CLI installation.
    if japanese_path.is_file():
        done, total = check_draft_progress(japanese_path, draft_path)
        if done == total and total > 0:
            print("已有完整转写和粗翻，未启动模型。")
            return
    reason = pipeline_block_reason(task)
    if reason:
        raise TaskError(reason)

    # =========================
    # 检查已有文件
    # =========================

    if draft_path.exists() and not japanese_path.is_file():
        print("\nDraft exists, but japanese.txt is missing.")
        print("Pipeline stopped to prevent data mismatch.")
        return

    if japanese_path.is_file():

        if japanese_path.stat().st_size == 0:
            print("\njapanese.txt is empty.")
            return

        need_transcription = False
        print("\nExisting japanese.txt found.")
        print("Whisper transcription can be skipped.")

    else:

        need_transcription = True
        print("\nNo complete japanese.txt found.")
        print("Whisper transcription is required.")

    # =========================
    # LM Studio 配置
    # =========================

    lms_command = external_program("lms")

    if not lms_command:
        raise TaskError("LM Studio CLI not found. 请安装并打开 LM Studio，确保 lms 在 PATH 或运行配置中。")

    settings = lm_studio_settings()
    if settings['host'] not in ('localhost', '127.0.0.1', '::1'):
        raise TaskError("自动流程只管理本机 LM Studio；远程服务请先手动准备日文并使用 translate.py。")
    model_id, server_port = settings['model'], settings['port']

    print(f"\nAudio: {audio_path}")
    print(f"Japanese output: {japanese_path}")
    print(f"Draft output: {draft_path}")

    # =========================
    # 根据需要执行 Whisper
    # =========================

    if need_transcription:

        # 检查并卸载 Qwen
        print("\nChecking loaded LM Studio models...")

        loaded_models = lm_command(
            [lms_command, "ps"],
            capture_output=True,
            text=True,
            check=True
        )

        if model_id in loaded_models.stdout:

            print("\nQwen 4B is loaded. Unloading...")

            lm_command(
                [
                    lms_command,
                    "unload",
                    model_id
                ],
                check=True
            )

            print("Qwen 4B unloaded successfully.")

        else:
            print("Qwen 4B is not loaded.")

        # 独立进程运行 Whisper
        transcribe_script = Path(__file__).parent / "transcribe.py"

        if not transcribe_script.is_file():
            print("transcribe.py not found.")
            return

        print("\nStarting Whisper process...")

        run_script_process("transcribe.py", audio_path)

        print("\nWhisper process exited.")
        print("CUDA resources released.")

    else:

        print("\nSkipping Whisper.")
        print("Using existing japanese.txt.")

    # =========================
    # 检查 Whisper 输出
    # =========================

    if not japanese_path.is_file() or japanese_path.stat().st_size == 0:
        print("Japanese transcription is missing or empty.")
        return

    # =========================
    # 检查粗翻进度
    # =========================

    try:
        translated_count, total = check_draft_progress(
            japanese_path,
            draft_path
        )

    except (ValueError, OSError, UnicodeError) as error:
        print(f"\nDraft validation failed: {error}")
        print("Pipeline stopped to protect existing files.")
        return

    print(f"\nTranslation progress: {translated_count}/{total}")

    if translated_count == total:
        print("\nRough translation already completed.")
        print("No translation required.")
        return

    print(f"\nResuming translation from sentence {translated_count + 1}.")

    # =========================
    # 启动 LM Studio 服务
    # =========================

    server_status = lm_command(
        [lms_command, "server", "status", "--json", "--quiet"],
        capture_output=True,
        text=True,
        check=True
    )

    try:
        server_info = json.loads(server_status.stdout)
        if not isinstance(server_info, dict): raise ValueError('Expected server status object')
    except (ValueError, TypeError) as error:
        raise TaskError('模型服务不可用：LM Studio CLI 返回了无效服务状态，请检查 CLI 版本和后台服务。') from error

    if server_info.get("running"):

        if server_info.get("port") != server_port:
            raise TaskError(f"LM Studio is running on a different port. 请将服务端口与运行配置一致（{server_port}）。")

        print("\nLM Studio server is already running.")

    else:

        print("\nStarting LM Studio server...")

        lm_command(
            [lms_command, "server", "start", "--port", str(server_port)],
            check=True
        )

    # =========================
    # 加载 Qwen 4B
    # =========================

    loaded_models = lm_command(
        [lms_command, "ps"],
        capture_output=True,
        text=True,
        check=True
    )

    if model_id not in loaded_models.stdout:

        print("\nLoading Qwen 4B...")

        lm_command(
            [
                lms_command,
                "load",
                model_id,
                "--gpu", "max",
                "--context-length", "8192"
            ],
            check=True
        )

    else:
        print("\nQwen 4B is already loaded.")

    print("\nLM Studio is ready.")

    # =========================
    # 自动启动粗翻
    # =========================

    translate_script = Path(__file__).parent / "translate.py"

    if not translate_script.is_file():
        print("translate.py not found.")
        return

    print("\nStarting Qwen translation...")

    run_script_process("translate.py", japanese_path)

    print("\nTranslation process finished.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="转写并粗翻；不传音频时打开选择框。")
    parser.add_argument("audio", nargs="?")
    main(parser.parse_args().audio)
