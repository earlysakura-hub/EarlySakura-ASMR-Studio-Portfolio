"""Shared entry for a windowed GUI EXE and a console worker EXE."""
import multiprocessing
import os
from pathlib import Path
import sys
import traceback

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        import app_runtime
        worker_program = app_runtime.frozen() and Path(sys.executable).name.lower() == "earlysakuraworker.exe"
        if app_runtime.frozen() and not worker_program:
            app_runtime.ensure_gui_streams()
        app_runtime.setup_runtime()
        explicit_mode = "--worker" in sys.argv or "--diagnose" in sys.argv
        if app_runtime.frozen() and worker_program != explicit_mode:
            raise ValueError("EarlySakura.exe 仅用于 GUI；工作或诊断模式请使用 EarlySakuraWorker.exe。")
        import studio
        studio.main()
    except Exception:
        detail = traceback.format_exc()
        if sys.stderr is not None:
            print(detail, file=sys.stderr, flush=True)
        if "--worker" not in sys.argv and "--diagnose" not in sys.argv:
            folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "EarlySakura" / "outputs"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "startup_errors.log").open("a", encoding="utf-8") as log:
                log.write(detail + "\n")
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk(); root.withdraw()
            messagebox.showerror("EarlySakura 启动失败", f"{detail}\n日志：{folder / 'startup_errors.log'}", parent=root)
            root.destroy()
        sys.exit(1)
