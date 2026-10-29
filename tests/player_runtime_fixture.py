"""Run the actual floating player with isolated test media/history and its real IPC."""
import argparse
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
from playback_history import HistoryStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--database", required=True)
    parser.add_argument("--player-control", action="store_true")
    parser.add_argument("--hold-resume", action="store_true")
    args = parser.parse_args()
    if sys.stdin.readline() != "START\n": return
    boundary = (ROOT / "work/parallel_runtime_checks").resolve()
    for filename in (args.audio, args.database):
        if not Path(filename).resolve().is_relative_to(boundary):
            raise RuntimeError("Runtime fixture only accepts its own test directory")
    history = HistoryStore(args.database)
    script = ROOT / "floating_subtitle_player.py"
    sys.argv = [str(script), args.audio, "--studio-control"]
    import tkinter as tk
    from tkinter import ttk
    namespace = {"__name__": "__main__", "__file__": str(script)}
    original_tk = tk.Tk
    count = [0]
    def widgets(parent):
        for child in parent.winfo_children():
            yield child
            yield from widgets(child)
    def make_root(*tk_args, **kwargs):
        root = original_tk(*tk_args, **kwargs)
        count[0] += 1
        if count[0] == 2:
            volume_requested = [False]
            def audit():
                for widget in widgets(root):
                    if isinstance(widget, ttk.Button) and widget.cget("text") == "继续上次播放":
                        if args.hold_resume:
                            print("FIXTURE_WAITING_FOR_CLOSE", flush=True)
                            return
                        widget.invoke()  # This fixture alone accepts its test resume.
                        break
                resume = namespace.get("resume")
                if resume is not None and resume.ready:
                    if namespace["subtitle_label"].cget("text") != "开头字幕 A":
                        root.after(50,audit); return
                    if not volume_requested[0]:
                        volume_requested[0]=True; namespace["volume_scale"].set(37)
                        root.after(50,audit); return
                    assert namespace["player"].audio_get_volume() == 37
                    assert namespace["subtitle_label"].cget("text") == "开头字幕 A"
                    print("FIXTURE_PLAYER_READY subtitle=A volume=37 resumed=" + str(resume.target), flush=True)
                else:
                    root.after(50, audit)
            root.after(300, audit)
        return root
    with patch("playback_history.HistoryStore", return_value=history), patch("tkinter.Tk", side_effect=make_root):
        exec(compile(script.read_text(encoding="utf-8-sig"), str(script), "exec"),
             namespace)


if __name__ == "__main__":
    main()
