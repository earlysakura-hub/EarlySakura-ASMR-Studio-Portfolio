"""Harmless three-generation process fixture; never imports models or VLC."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import task_store as store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--role", choices=("parent", "child", "grandchild"), default="parent")
    args = parser.parse_args()
    directory = Path(args.directory)
    if args.role == "parent" and sys.stdin.readline() != "START\n":
        return
    if args.role != "grandchild":
        role = "child" if args.role == "parent" else "grandchild"
        subprocess.Popen([sys.executable, "-B", "-u", str(Path(__file__).absolute()), str(directory), "--role", role], cwd=ROOT)
    if args.role == "parent":
        with store.task_lock(directory):
            store.write_new(directory / "japanese.txt.partial-cancel-fixture", b"unfinished transcription fixture\n")
            print("FIXTURE_LOCKED", flush=True)
            time.sleep(120)
    else:
        print("FIXTURE_" + args.role.upper(), flush=True)
        time.sleep(120)


if __name__ == "__main__":
    main()
