"""Real Tk/real VLC checks with harmless pipeline fixtures, never real models.

--gui checks the main window using simulated player workers.
Default checks actual VLC with a silent WAV on local audio output alongside a fixture pipeline.
"""
from pathlib import Path
import ast
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import studio
import task_store as store
from playback_history import HistoryStore, HistoryError
from playback_resume import ResumePlayback
from windows_job import ProcessTree


def make_tasks(directory, real_audio=False):
    tasks = []
    for name in ("A", "B", "C"):
        audio = directory / (name + ".wav")
        if real_audio and name == "A":
            with wave.open(str(audio), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
                stream.writeframes(b"\0\0" * 8000 * 60)
        else:
            audio.write_bytes(b"synthetic-" + name.encode())
        task = store.open_task(audio)
        if name == "A":
            store.write_new(task.result("output.srt"), "1\n00:00:00,000 --> 00:00:20,000\n开头字幕 A\n\n2\n00:00:20,000 --> 00:00:40,000\n字幕 A\n".encode())
        tasks.append(task)
    return tasks


def fixture_command(mode, audio, database, behavior="normal"):
    return [sys.executable, "-B", "-u", "-X", "utf8", str(ROOT / "tests/parallel_worker_fixture.py"),
            mode, str(audio), "--database", str(database), "--behavior", behavior]


def check_gui(directory):
    import tkinter as tk
    from tkinter import ttk
    real_player = "--real-player-gui" in sys.argv
    tasks = make_tasks(directory, real_audio=real_player)
    history = HistoryStore(directory / "history.sqlite3")
    player_starts = [0]
    def command(mode, audio, gated=False):
        assert mode in ("player", "pipeline")
        if mode == "player" and real_player:
            player_starts[0] += 1
            return [sys.executable, "-B", "-u", "-X", "utf8", str(ROOT / "tests/player_runtime_fixture.py"),
                    str(audio), "--database", str(history.database), *(["--hold-resume"] if player_starts[0] == 3 else [])]
        return fixture_command(mode, audio, history.database, "wait" if mode == "pipeline" else "normal")
    with patch.object(studio, "worker_command", side_effect=command), patch.object(studio, "ROOT", directory), \
            patch.object(studio.messagebox, "askyesno", return_value=True):
        root = tk.Tk()
        app = studio.Studio(root)
        app.history = history
        failures, complete = [], [False]
        stage = [0]
        before_cancel = [None]
        deadline = time.monotonic() + 15
        def widgets(parent):
            for widget in parent.winfo_children():
                yield widget
                yield from widgets(widget)
        def select(task):
            app.task = task
            states, *app.flags = studio.inspect_task(task)
            app.audio_name.set(Path(task.identity["path"]).name)
            app.audio_path.set(task.identity["path"])
            for name, state in states.items(): app.file_states[name].set(state)
            app.update_buttons()
        def step():
            try:
                assert time.monotonic() < deadline, "native GUI check timed out"
                if stage[0] == 0:
                    select(tasks[0]); app.buttons["player"].invoke(); stage[0] = 1
                elif stage[0] == 1 and "正在播放" in app.player_status.get():
                    select(tasks[1]); app.buttons["pipeline"].invoke(); stage[0] = 2
                elif stage[0] == 2:
                    buttons = [widget for widget in widgets(root) if isinstance(widget, ttk.Button) and widget.cget("text") == "确认开始"]
                    if buttons and str(buttons[0].cget("state")) != "disabled":
                        buttons[0].invoke(); stage[0] = 3
                elif stage[0] == 3 and app.processing_progress.stage == "Whisper":
                    assert "A.wav" in app.player_status.get()
                    assert "B.wav" in app.pipeline_status.get()
                    assert str(app.buttons["select"].cget("state")) == "normal"
                    assert str(app.buttons["history"].cget("state")) == "normal"
                    app.buttons["history"].invoke()
                    table = next(widget for widget in widgets(app.history_window) if isinstance(widget, ttk.Treeview))
                    assert table.get_children() == (tasks[0].identity["task_id"],)
                    app.select_page('pipeline')
                    app.buttons["stop_player"].invoke(); stage[0] = 4
                elif stage[0] == 4 and app.player_job is None:
                    assert app.pipeline_job is not None and app.pipeline_job.process.poll() is None
                    assert history.resume_info(tasks[0].identity["task_id"]) is not None
                    if real_player:
                        assert "FIXTURE_PLAYER_READY" in app.log.get("1.0", "end")
                        app.buttons["history"].invoke()
                        table = next(widget for widget in widgets(app.history_window) if isinstance(widget, ttk.Treeview))
                        table.selection_set(tasks[0].identity["task_id"])
                        next(widget for widget in widgets(app.history_window) if isinstance(widget, ttk.Button) and widget.cget("text") == "打开并播放").invoke()
                        stage[0] = 5
                    else:
                        complete[0] = True; app.close(); return
                elif stage[0] == 5 and "正在播放" in app.player_status.get() and app.log.get("1.0", "end").count("FIXTURE_PLAYER_READY") >= 2:
                    assert app.pipeline_job is not None and app.pipeline_job.process.poll() is None
                    app.buttons["stop_player"].invoke(); stage[0] = 6
                elif stage[0] == 6 and app.player_job is None:
                    before_cancel[0] = history.resume_info(tasks[0].identity["task_id"])
                    app.buttons["history"].invoke()
                    table = next(widget for widget in widgets(app.history_window) if isinstance(widget, ttk.Treeview))
                    table.selection_set(tasks[0].identity["task_id"])
                    next(widget for widget in widgets(app.history_window) if isinstance(widget, ttk.Button) and widget.cget("text") == "打开并播放").invoke()
                    stage[0] = 7
                elif stage[0] == 7 and "等待恢复选择" in app.player_status.get() and "FIXTURE_WAITING_FOR_CLOSE" in app.log.get("1.0", "end"):
                    app.buttons["stop_player"].invoke(); stage[0] = 8
                elif stage[0] == 8 and app.player_job is None:
                    assert history.resume_info(tasks[0].identity["task_id"]) == before_cancel[0]
                    complete[0] = True; app.close(); return
                root.after(70, step)
            except Exception as error:
                failures.append(f"{error}; stage={stage[0]}; player={app.player_status.get()}; pipeline={app.pipeline_status.get()}; log={app.log.get('1.0', 'end')[-1400:]}")
                app.close()
        root.after(100, step)
        try:
            root.mainloop()
        finally:
            for job in (app.pipeline_job, app.player_job):
                if job is not None:
                    job.tree.cancel(); job.tree.finish()
            try:
                if root.winfo_exists(): root.destroy()
            except tk.TclError:
                pass  # The normal exit already destroyed the application.
        assert not failures and complete[0], failures
    print("PASS native Tk" + (" + actual floating player" if real_player else " + simulated player") +
          ": independent labels/buttons, confirmation, history during pipeline, graceful player close, combined exit")
    if real_player:
        print("PASS actual floating player: subtitle label A, volume slider, history reopen/resume while B continues")
        print("PASS actual floating player: parent close cancels pending resume without changing saved position")


def check_vlc(directory):
    import vlc
    import re
    tasks = make_tasks(directory, real_audio=True)
    history = HistoryStore(directory / "history.sqlite3")
    engine = vlc.Instance("--intf=dummy", "--no-video")
    player = engine.media_player_new()
    player.set_media(engine.media_new(tasks[0].identity["path"]))
    resume = ResumePlayback(player, tasks[0], 25000, history)
    source = ast.parse((ROOT / "floating_subtitle_player.py").read_text(encoding="utf-8-sig"))
    names = {"update_subtitle", "format_time", "load_subtitles", "time_to_ms", "report_save_errors"}
    namespace = dict(player=player, resume=resume, closing_player=False, dragging_progress=False,
                     save_warning_shown=False, current_subtitle="", subtitle_offset_ms=0,
                     queue=queue, re=re, root=Mock(), subtitle_label=Mock(), time_label=Mock(),
                     progress_bar={}, messagebox=Mock(), HistoryError=HistoryError, report_player_status=Mock())
    exec(compile(ast.Module(body=[node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[]), "parallel_subtitles", "exec"), namespace)
    namespace["subtitles"] = namespace["load_subtitles"](tasks[0].result("output.srt"))
    def tick_until(check, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            namespace["update_subtitle"]()
            if check(): return
            time.sleep(0.05)
        raise AssertionError("real VLC check timed out")
    def start_pipeline(task, behavior):
        tree = ProcessTree()
        process = tree.start(fixture_command("pipeline", task.identity["path"], history.database, behavior),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", cwd=ROOT)
        lines = queue.Queue()
        def read():
            for line in process.stdout: lines.put(line)
        reader = threading.Thread(target=read, daemon=True); reader.start()
        return tree, process, lines, reader
    scopes = []
    try:
        player.play()
        tick_until(lambda: resume.ready)
        before = player.get_time()
        tree, process, lines, reader = start_pipeline(tasks[1], "normal")
        scopes.append((tree, process, reader))
        tick_until(lambda: process.poll() is not None)
        tree.finish(); reader.join(3)
        assert process.returncode == 0
        tick_until(lambda: player.get_time() - before >= 500)
        assert player.is_playing() and namespace["current_subtitle"] == "字幕 A"
        assert store.check_draft_progress(tasks[1].result("japanese.txt"), tasks[1].result("draft_bilingual.txt")) == (1, 1)
        print("PASS real VLC + fixture: playback clock/subtitles continue while B publishes Japanese and draft")
        tree, process, lines, reader = start_pipeline(tasks[2], "wait")
        scopes.append((tree, process, reader))
        tick_until(lambda: tree.active_count() >= 3)
        player.audio_set_volume(37)
        tick_until(lambda: player.audio_get_volume() == 37, timeout=2)
        resume.save_now(); player.pause()
        tick_until(lambda: player.get_state() == vlc.State.Paused)
        assert process.poll() is None
        player.play()
        tick_until(lambda: player.is_playing())
        resume.jump_to(30000)
        tick_until(lambda: resume.pending_seek is None)
        tree.cancel(); tree.finish(); process.wait(timeout=3); reader.join(3)
        before = player.get_time()
        tick_until(lambda: player.get_time() - before >= 500)
        assert player.is_playing() and namespace["current_subtitle"] == "字幕 A"
        with store.task_lock(tasks[2].directory): pass
        resume.close(); assert resume.writer.done.wait(3)
        saved = history.resume_info(tasks[0].identity["task_id"])
        assert saved and 30000 <= saved["position_ms"] < 40000
        assert [row["work_id"] for row in history.list_entries()] == [tasks[0].identity["task_id"]]
        print("PASS real VLC + fixture: pause/play, volume, seek, isolated cancellation, A resume saved without B/C history")
    finally:
        resume.close(); resume.writer.done.wait(3)
        player.stop(); player.release(); engine.release()
        for tree, process, reader in scopes:
            tree.cancel(); tree.finish(); process.wait(timeout=3); reader.join(3); process.stdout.close()


if __name__ == "__main__":
    work = ROOT / "work" / "parallel_runtime_checks"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        (check_gui if any(flag in sys.argv for flag in ("--gui", "--real-player-gui")) else check_vlc)(Path(temporary))
