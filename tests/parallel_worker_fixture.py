"""Owned-process parallel fixtures. No models, real media, or real history DB."""
import argparse
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import json

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import task_store as store
from playback_history import HistoryStore
from playback_resume import ResumePlayback


class ClockPlayer:
    def __init__(self):
        self.position, self.updated, self.status = 25000, time.monotonic(), "Playing"
    def get_state(self): return self.status
    def get_time(self):
        now = time.monotonic()
        if self.status == "Playing": self.position += int((now - self.updated) * 1000)
        self.updated = now
        return self.position
    def get_length(self): return 120000
    def is_seekable(self): return True
    def set_time(self, value): self.position = value; self.updated = time.monotonic()
    def pause(self): self.get_time(); self.status = "Paused"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("pipeline", "player"))
    parser.add_argument("audio")
    parser.add_argument("--database", required=True)
    parser.add_argument("--behavior", default="normal")
    parser.add_argument("--player-control", action="store_true")
    parser.add_argument("--expected-task")
    args = parser.parse_args()
    if sys.stdin.readline() != "START\n": return
    task = store.open_task(args.audio)
    if args.expected_task and task.identity["task_id"] != args.expected_task:
        raise RuntimeError("fixture identity mismatch")
    if args.mode == "player":
        if args.behavior == "fail-player": raise RuntimeError("fixture player failed")
        commands = queue.Queue()
        def read():
            for line in sys.stdin: commands.put(line.strip())
            commands.put("CLOSE")
        threading.Thread(target=read, daemon=True).start()
        player = ClockPlayer()
        resume = ResumePlayback(player, task, history=HistoryStore(args.database))
        while True:
            try: command = commands.get_nowait()
            except queue.Empty: command = None
            if command == "CLOSE":
                resume.close()
                assert resume.writer.done.wait(3)
                return
            resume.tick()
            print("STUDIO_PLAYER " + json.dumps({"state": player.get_state(), "position_ms": player.get_time(), "duration_ms": player.get_length()}), flush=True)
            time.sleep(0.1)
    else:
        # A descendant must belong to this pipeline's Job, not the player's.
        child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
        try:
            if not task.result("japanese.txt").exists():
                with store.task_lock(task.directory):
                    print("Starting Whisper process...", flush=True)
                    partial = store.partial_path(task.result("japanese.txt"))
                    jp = "[00:00:00,000 --> 00:00:01,000] テスト。\n"
                    partial.write_text(jp, encoding="utf-8")
                    print("FIXTURE_READY " + str(task.directory), flush=True)
                    if args.behavior == "wait": time.sleep(30)
                    time.sleep(0.35)
                    if args.behavior == "fail-whisper": raise RuntimeError("fixture Whisper failed")
                    store.publish_new(partial, task.result("japanese.txt"))
            print("Whisper process exited.", flush=True)
            print("Starting Qwen translation...", flush=True)
            with store.task_lock(task.directory):
                if args.behavior == "service-down":
                    raise RuntimeError("HTTPConnectionPool: Connection refused; 模型服务不可用")
                if args.behavior == "fail-qwen":
                    (task.directory / "draft_bilingual.txt.pending.json").write_text("{}")
                    raise RuntimeError("fixture Qwen failed")
                time.sleep(0.35)
                store.write_new(task.result("draft_bilingual.txt"), "[00:00:00,000 --> 00:00:01,000]\nJP: テスト。\nZH_DRAFT: 模拟翻译。\n\n".encode("utf-8"))
                print("Translation progress: 1/1", flush=True)
        finally:
            child.terminate(); child.wait(timeout=3)


if __name__ == "__main__":
    main()
