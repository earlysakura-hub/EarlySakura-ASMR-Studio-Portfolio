"""Real local VLC with generated WAV, dummy audio output and isolated metadata.

This checks decoding/clock/seeking, not audible output or native GUI interaction.
"""
import ast
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import wave
from unittest.mock import Mock

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import vlc
from playback_history import HistoryStore, HistoryError, reopen_task
from playback_resume import ResumePlayback
from task_store import open_task, load_task


def make_player(task):
    engine = vlc.Instance("--intf=dummy", "--aout=dummy", "--no-video")
    player = engine.media_player_new()
    player.set_media(engine.media_new(task.identity["path"]))
    return engine, player


def until(check, session, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.tick()
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("VLC check timed out")


def finish(session, player, engine):
    session.close()
    player.stop()
    assert session.writer.done.wait(4), "background save not flushed"
    errors = []
    while not session.writer.errors.empty():
        errors.append(session.writer.errors.get())
    player.release()
    engine.release()
    assert not errors, errors


def child(database, task_directory):
    print("CHILD loaded imports", file=sys.stderr, flush=True)
    store = HistoryStore(database)
    task = load_task(task_directory)
    print("CHILD verified task", file=sys.stderr, flush=True)
    engine, player = make_player(task)
    print("CHILD created VLC", file=sys.stderr, flush=True)
    session = ResumePlayback(player, task, 20000, store)  # Production 5-second interval.
    player.play()
    print("CHILD started VLC", file=sys.stderr, flush=True)
    next_report = 0
    while True:
        session.tick()
        if time.monotonic() >= next_report:
            print(f"CHILD {session.state()} position={player.get_time()} duration={player.get_length()} ready={session.ready} pending={session.pending_seek}", file=sys.stderr, flush=True)
            next_report = time.monotonic() + 1
            while not session.writer.errors.empty():
                print(session.writer.errors.get(), file=sys.stderr, flush=True)
        time.sleep(0.05)


def main():
    work = ROOT / "work" / "vlc_resume_checks"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        directory = Path(temporary)
        store = HistoryStore(directory / "history.sqlite3")
        tasks = []
        for name in ("A", "B"):
            audio = directory / (name + ".wav")
            with wave.open(str(audio), "wb") as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
                output.writeframes(b"\0\0" * 8000 * 60)
            task = open_task(audio)
            task.result("output.srt").write_text("1\n00:00:00,000 --> 00:00:10,000\n开头字幕\n\n"
                                                "2\n00:00:20,000 --> 00:00:40,000\n恢复位置字幕\n", encoding="utf-8")
            tasks.append(task)

        # Normal start, real seek, pause and capture before VLC stop.
        for task, target in zip(tasks, (25000, 12000)):
            engine, player = make_player(task)
            session = ResumePlayback(player, task, history=store)
            try:
                player.play()
                until(lambda: session.ready, session)
                assert 0 <= player.get_time() < 2000
                session.jump_to(target)
                until(lambda: session.pending_seek is None, session)
                if task == tasks[0]:
                    until(lambda: (store.resume_info(task.identity["task_id"]) or {}).get("position_ms", 0) >= 29000, session)
                session.save_now(); player.pause(); session.tick()
                paused_position = player.get_time()
            finally:
                finish(session, player, engine)
            saved = store.resume_info(task.identity["task_id"])
            assert saved and abs(saved["position_ms"] - paused_position) <= 750, saved
        print("PASS real VLC: first play, seek, 5-second checkpoint, pause, close, independent A/B progress")

        entry = next(row for row in store.list_entries() if row["work_id"] == tasks[0].identity["task_id"])
        task = reopen_task(entry)
        saved = store.resume_info(task.identity["task_id"])
        engine, player = make_player(task)
        session = ResumePlayback(player, task, saved["position_ms"], store)
        try:
            player.play()
            until(lambda: session.ready, session)
            # Execute the unchanged subtitle selection against the real VLC clock.
            source = ast.parse((ROOT / "floating_subtitle_player.py").read_text(encoding="utf-8-sig"))
            names = {"update_subtitle", "format_time", "load_subtitles", "time_to_ms", "report_save_errors"}
            import re
            namespace = dict(player=player, resume=session, closing_player=False, dragging_progress=False,
                             save_warning_shown=False, current_subtitle="", subtitle_offset_ms=0,
                             queue=queue, re=re, root=Mock(), subtitle_label=Mock(), time_label=Mock(),
                             progress_bar={}, messagebox=Mock(), HistoryError=HistoryError)
            namespace["report_player_status"] = Mock()
            exec(compile(ast.Module(body=[node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[]), "real_vlc_subtitles", "exec"), namespace)
            namespace["subtitles"] = namespace["load_subtitles"](task.result("output.srt"))
            namespace["update_subtitle"]()
            assert namespace["current_subtitle"] == "恢复位置字幕"
            assert 40 < namespace["progress_bar"]["value"] < 65
            assert abs(player.get_time() - saved["position_ms"]) <= 750
        finally:
            finish(session, player, engine)
        print("PASS real VLC: reopen through history, resume time, SRT selection and progress display")

        # Start over after a prior resume; an actual Ended state suppresses resume.
        engine, player = make_player(task)
        session = ResumePlayback(player, task, 0, store)
        try:
            player.play()
            until(lambda: session.ready, session)
            assert player.get_time() < 2000
            session.jump_to(60000)
            until(lambda: session.pending_seek is None, session)
            assert not session.finished
            until(lambda: session.finished, session)
        finally:
            finish(session, player, engine)
        assert store.resume_info(task.identity["task_id"]) is None
        print("PASS real VLC: start over, near-end seek not completed, natural Ended marked completed")

        if "--check-vlc-child" not in sys.argv:
            print("NOT VERIFIED: forced real-VLC child exit; isolated child initialization blocks on this machine."
                  " Reproduce with --check-vlc-child. Periodic-writer termination is tested separately.")
            return

        # Kill only the child created here after a real 5-second periodic save.
        process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(Path(__file__).absolute()),
                                    "--crash-child", str(store.database), str(tasks[1].directory)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 12
            saved = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read().decode("utf-8", errors="replace"))
                saved = store.resume_info(tasks[1].identity["task_id"])
                if saved and saved["position_ms"] >= 24000:
                    break
                time.sleep(0.1)
            if not saved or saved["position_ms"] < 24000:
                process.kill(); process.wait(timeout=5)
                raise AssertionError(f"{saved}\n{process.stderr.read().decode('utf-8', errors='replace')}")
            position = saved["position_ms"]
            process.kill()
            process.wait(timeout=5)
            assert HistoryStore(store.database).resume_info(tasks[1].identity["task_id"])["position_ms"] == position
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            process.stderr.close()
        print("PASS real VLC: forced child termination retains last 5-second checkpoint")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--crash-child":
        child(sys.argv[2], sys.argv[3])
    else:
        main()
