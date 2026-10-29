"""Windows-only, standard-library tests. No models, network, audio decoding or VLC."""
import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_store as store

STAMP = "[00:00:00,123 --> 00:00:01,456]"
JP = STAMP + " こんにちは。\n"
DRAFT = STAMP + "\nJP: こんにちは。\nZH_DRAFT: 你好。\n\n"
FINAL = STAMP + "\nJP: こんにちは。\nZH_FINAL: 你好。\n\n"
SRT = "1\n00:00:00,123 --> 00:00:01,456\n你好。\n\n"


class TasksTest(unittest.TestCase):
    def setUp(self):
        # All temporary files stay under the caller's chosen temp directory.
        self.temp = tempfile.TemporaryDirectory(prefix="earlysakura-test-")
        self.root = Path(self.temp.name)
        self.audio = self.root / "音频 one.wav"
        self.audio.write_bytes(b"synthetic-audio-A")
        self.task = store.open_task(self.audio)

    def tearDown(self):
        self.temp.cleanup()

    def text(self, name, value):
        path = self.root / name
        path.write_text(value, encoding="utf-8")
        return path

    def managed_text(self, name, value):
        store.import_legacy(self.task, {name: self.text(name, value)})
        return self.task.result(name)

    def execute(self, script, args=(), modules=None):
        with patch.object(sys, "argv", [str(ROOT / script), *map(str, args)]), \
                patch.dict(sys.modules, modules or {}), redirect_stdout(io.StringIO()):
            return runpy.run_path(str(ROOT / script), run_name="__main__")

    def fake_tk(self, selection):
        module = types.ModuleType("tkinter")
        module.Tk = lambda: types.SimpleNamespace(withdraw=lambda: None, destroy=lambda: None)
        module.filedialog = types.SimpleNamespace(askopenfilename=lambda **kwargs: str(selection))
        module.messagebox = types.SimpleNamespace(askyesno=lambda *args, **kwargs: True)
        return module

    def test_same_directory_different_audio_isolated(self):
        second = self.root / "another.wav"
        second.write_bytes(b"synthetic-audio-B")
        other = store.open_task(second)
        self.assertNotEqual(self.task.directory, other.directory)
        store.write_new(self.task.result("japanese.txt"), JP.encode())
        self.assertFalse(other.result("japanese.txt").exists())

    def test_same_filename_different_directories_isolated_even_same_bytes(self):
        folder = self.root / "folder"
        folder.mkdir()
        second = folder / self.audio.name
        second.write_bytes(self.audio.read_bytes())
        other = store.open_task(second)
        self.assertNotEqual(self.task.identity["task_id"], other.identity["task_id"])
        self.assertEqual(self.task.identity["sha256"], other.identity["sha256"])

    def test_reopen_preserves_identity_and_manifest(self):
        before = (self.task.directory / "task.json").read_bytes()
        reopened = store.open_task(self.audio)
        self.assertEqual(self.task.directory, reopened.directory)
        self.assertEqual(before, (reopened.directory / "task.json").read_bytes())

    def test_dot_path_and_windows_case_reopen_same_task(self):
        alias = str(self.root / "unused" / ".." / self.audio.name)
        self.assertEqual(self.task.directory, store.open_task(alias).directory)
        self.assertEqual(self.task.directory, store.open_task(str(self.audio).upper()).directory)

    def test_same_size_and_mtime_content_replacement_creates_new_task(self):
        stat = self.audio.stat()
        self.audio.write_bytes(b"synthetic-audio-B")
        os.utime(self.audio, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        other = store.open_task(self.audio)
        self.assertNotEqual(self.task.directory, other.directory)
        self.assertTrue((self.task.directory / "task.json").exists())
        with self.assertRaises(store.TaskError):
            store.load_task(self.task.directory)

    def test_audio_changed_while_hashing_rejected(self):
        original = store.file_hash
        def changing(path):
            value = original(path)
            Path(path).write_bytes(b"changed during hash")
            return value
        with patch.object(store, "file_hash", changing), self.assertRaises(store.TaskError):
            store.audio_identity(self.audio)

    def test_import_four_legacy_formats_preserves_sources(self):
        sources = {name: self.text(name, value) for name, value in zip(store.RESULT_NAMES, (JP, DRAFT, FINAL, SRT))}
        before = {name: path.read_bytes() for name, path in sources.items()}
        store.import_legacy(self.task, sources)
        for name, path in sources.items():
            self.assertEqual(before[name], path.read_bytes())
            self.assertEqual(before[name], self.task.result(name).read_bytes())
        receipt = json.loads(next(self.task.directory.glob("import-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(4, len(receipt["files"]))
        self.assertEqual(self.task.identity["task_id"], receipt["audio_task_id"])

    def test_import_repeat_is_idempotent(self):
        source = self.text("japanese.txt", JP)
        store.import_legacy(self.task, {"japanese.txt": source})
        before = self.task.result("japanese.txt").stat().st_mtime_ns
        store.import_legacy(self.task, {"japanese.txt": source})
        self.assertEqual(before, self.task.result("japanese.txt").stat().st_mtime_ns)

    def test_import_conflict_preflight_writes_no_other_result(self):
        self.managed_text("output.srt", SRT)
        before = self.task.result("output.srt").read_bytes()
        with self.assertRaises(FileExistsError):
            store.import_legacy(self.task, {"japanese.txt": self.text("japanese.txt", JP),
                                         "output.srt": self.text("different.srt", "different")})
        self.assertFalse(self.task.result("japanese.txt").exists())
        self.assertEqual(before, self.task.result("output.srt").read_bytes())

    def test_results_from_another_task_rejected(self):
        second = self.root / "other.wav"
        second.write_bytes(b"other")
        other = store.open_task(second)
        self.managed_text("japanese.txt", JP)
        with self.assertRaises(store.TaskError):
            store.import_legacy(other, {"japanese.txt": self.task.result("japanese.txt")})

    def test_interrupted_import_can_retry_without_overwriting_completed_copy(self):
        sources = {"japanese.txt": self.text("japanese.txt", JP), "final_bilingual.txt": self.text("final_bilingual.txt", FINAL)}
        original = store.write_new
        def fail_second(path, content):
            if Path(path).name == "final_bilingual.txt":
                raise OSError("simulated interruption")
            original(path, content)
        with patch.object(store, "write_new", fail_second), self.assertRaises(OSError):
            store.import_legacy(self.task, sources)
        before = self.task.result("japanese.txt").stat().st_mtime_ns
        store.import_legacy(self.task, sources)
        self.assertEqual(before, self.task.result("japanese.txt").stat().st_mtime_ns)
        self.assertEqual(FINAL, self.task.result("final_bilingual.txt").read_text(encoding="utf-8"))

    def test_interrupted_initial_manifest_can_recover(self):
        other_audio = self.root / "new.wav"
        other_audio.write_bytes(b"new")
        directory = self.task.directory.parent / store.audio_identity(other_audio)["task_id"][:24]
        directory.mkdir()
        partial = directory / "task.json.partial-test"
        partial.write_text('{"schema_version":', encoding="utf-8")
        task = store.open_task(other_audio)
        self.assertTrue(partial.exists())
        self.assertTrue((task.directory / "task.json").is_file())

    def test_orphan_results_without_manifest_not_claimed(self):
        (self.task.directory / "task.json").unlink()  # only synthetic fixture
        store.write_new(self.task.result("japanese.txt"), JP.encode())
        with self.assertRaises(store.TaskError):
            store.open_task(self.audio)
        self.assertEqual(JP, self.task.result("japanese.txt").read_text(encoding="utf-8"))

    def test_corrupt_manifest_not_overwritten(self):
        manifest = self.task.directory / "task.json"
        manifest.write_bytes(b"corrupt")
        with self.assertRaises(store.TaskError):
            store.open_task(self.audio)
        self.assertEqual(b"corrupt", manifest.read_bytes())

    def test_publish_conflict_preserves_both_files(self):
        target = self.task.result("output.srt")
        target.write_bytes(b"old")
        partial = store.partial_path(target)
        partial.write_bytes(b"new")
        with self.assertRaises(FileExistsError):
            store.publish_new(partial, target)
        self.assertEqual(b"old", target.read_bytes())
        self.assertEqual(b"new", partial.read_bytes())

    def test_publish_race_does_not_replace_target(self):
        target = self.task.result("output.srt")
        partial = store.partial_path(target)
        partial.write_bytes(b"new")
        original = Path.rename
        def raced(path, destination):
            Path(destination).write_bytes(b"racing writer")
            return original(path, destination)
        with patch.object(Path, "rename", raced), self.assertRaises(FileExistsError):
            store.publish_new(partial, target)
        self.assertEqual(b"racing writer", target.read_bytes())
        self.assertTrue(partial.exists())

    def test_lock_blocks_other_process_and_recovers_after_kill(self):
        code = ("import sys; sys.path.insert(0,sys.argv[1]); from task_store import task_lock; "
                "from pathlib import Path; import time\nwith task_lock(Path(sys.argv[2])):\n print('LOCKED',flush=True)\n time.sleep(30)")
        child = subprocess.Popen([sys.executable, "-B", "-I", "-X", "utf8", "-c", code, str(ROOT), str(self.task.directory)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            self.assertEqual("LOCKED", child.stdout.readline().strip())
            with self.assertRaises(store.TaskError), store.task_lock(self.task.directory):
                pass
        finally:
            child.kill()
            child.communicate(timeout=5)
        with store.task_lock(self.task.directory):
            pass

    def test_cancel_legacy_confirmation_copies_nothing(self):
        source = self.text("japanese.txt", JP)
        tk = self.fake_tk(source)
        tk.messagebox = types.SimpleNamespace(askyesno=lambda *args, **kwargs: False)
        with patch.dict(sys.modules, {"tkinter": tk}):
            self.assertFalse(store.choose_import(self.task, {"japanese.txt": source}))
        self.assertFalse(self.task.result("japanese.txt").exists())

    def test_legacy_text_explicitly_associated_and_copied(self):
        source = self.text("japanese.txt", JP)
        self.text("draft_bilingual.txt", DRAFT)
        tk = self.fake_tk(self.audio)
        tk.messagebox = types.SimpleNamespace(askyesno=lambda *args, **kwargs: True)
        with patch.dict(sys.modules, {"tkinter": tk}):
            task = store.task_for_text(source, "japanese.txt")
        self.assertEqual(task.directory, self.task.directory)
        self.assertEqual(JP, source.read_text(encoding="utf-8"))
        self.assertEqual(DRAFT, task.result("draft_bilingual.txt").read_text(encoding="utf-8"))

    def test_transcription_mock_preserves_legacy_and_partial_outputs(self):
        self.text("japanese.txt", "old work")
        old_partial = self.task.directory / "japanese.txt.partial-old"
        old_partial.write_bytes(b"incomplete earlier attempt")
        segment = types.SimpleNamespace(words=[], start=.123, end=1.456, text="こんにちは。")
        module = types.ModuleType("faster_whisper")
        module.WhisperModel = lambda *args, **kwargs: types.SimpleNamespace(transcribe=lambda *args, **kwargs: ([segment], None))
        self.execute("transcribe.py", [self.audio], {"faster_whisper": module})
        self.assertEqual(JP, self.task.result("japanese.txt").read_text(encoding="utf-8"))
        self.assertEqual("old work", (self.root / "japanese.txt").read_text(encoding="utf-8"))
        self.assertEqual(b"incomplete earlier attempt", old_partial.read_bytes())
        # No model call when a completed result already exists.
        module.WhisperModel = lambda *args, **kwargs: self.fail("unnecessary model load")
        with self.assertRaises(SystemExit) as stopped:
            self.execute("transcribe.py", [self.audio], {"faster_whisper": module})
        self.assertEqual(0, stopped.exception.code)

    def test_transcription_interrupted_then_restart_preserves_partial(self):
        segment = types.SimpleNamespace(words=[], start=.123, end=1.456, text="こんにちは。")
        def interrupted():
            yield segment
            raise RuntimeError("simulated transcription interruption")
        module = types.ModuleType("faster_whisper")
        module.WhisperModel = lambda *args, **kwargs: types.SimpleNamespace(transcribe=lambda *args, **kwargs: (interrupted(), None))
        with self.assertRaises(RuntimeError):
            self.execute("transcribe.py", [self.audio], {"faster_whisper": module})
        self.assertFalse(self.task.result("japanese.txt").exists())
        partials = list(self.task.directory.glob("japanese.txt.partial-*"))
        self.assertEqual(1, len(partials))
        before = partials[0].read_bytes()
        module.WhisperModel = lambda *args, **kwargs: types.SimpleNamespace(transcribe=lambda *args, **kwargs: ([segment], None))
        self.execute("transcribe.py", [self.audio], {"faster_whisper": module})
        self.assertEqual(before, partials[0].read_bytes())
        self.assertTrue(self.task.result("japanese.txt").exists())

    def test_audio_replaced_during_transcription_is_not_published(self):
        segment = types.SimpleNamespace(words=[], start=.123, end=1.456, text="こんにちは。")
        def changed():
            yield segment
            self.audio.write_bytes(b"replacement audio")
        module = types.ModuleType("faster_whisper")
        module.WhisperModel = lambda *args, **kwargs: types.SimpleNamespace(transcribe=lambda *args, **kwargs: (changed(), None))
        with self.assertRaises(store.TaskError):
            self.execute("transcribe.py", [self.audio], {"faster_whisper": module})
        self.assertFalse(self.task.result("japanese.txt").exists())
        self.assertEqual(1, len(list(self.task.directory.glob("japanese.txt.partial-*"))))

    def test_bom_legacy_draft_and_final_text_compatible(self):
        jp = self.managed_text("japanese.txt", "\ufeff" + JP)
        draft = self.managed_text("draft_bilingual.txt", "\ufeff" + DRAFT)
        self.assertEqual((1, 1), store.check_draft_progress(jp, draft))
        final = self.managed_text("final_bilingual.txt", "\ufeff" + FINAL)
        self.execute("make_srt.py", modules={"tkinter": self.fake_tk(final)})
        self.assertEqual(SRT, self.task.result("output.srt").read_text(encoding="utf-8"))

    def fake_requests(self, answer):
        module = types.ModuleType("requests")
        module.post = lambda *args, **kwargs: types.SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": answer}}]})
        return module

    def test_translation_mock_continues_valid_draft_without_changing_prefix(self):
        second = "[00:00:02,000 --> 00:00:03,000] おやすみ。\n"
        jp = self.managed_text("japanese.txt", JP + second)
        draft = self.managed_text("draft_bilingual.txt", DRAFT.rstrip())
        before = draft.read_bytes()
        self.execute("translate.py", [jp], {"requests": self.fake_requests("1|晚安。")})
        self.assertTrue(draft.read_bytes().startswith(before))
        self.assertEqual((2, 2), store.check_draft_progress(jp, draft))
        self.assertEqual(DRAFT.rstrip(), (self.root / "draft_bilingual.txt").read_text(encoding="utf-8"))

    def test_translation_refuses_incomplete_draft_without_request(self):
        jp = self.managed_text("japanese.txt", JP)
        draft = self.managed_text("draft_bilingual.txt", STAMP + "\nJP: こんにちは。\n")
        before = draft.read_bytes()
        requests = types.ModuleType("requests")
        requests.post = lambda *args, **kwargs: self.fail("API must not be called")
        with self.assertRaises(ValueError):
            self.execute("translate.py", [jp], {"requests": requests})
        self.assertEqual(before, draft.read_bytes())

    def test_translation_network_failure_preserves_valid_prefix(self):
        jp = self.managed_text("japanese.txt", JP + "[00:00:02,000 --> 00:00:03,000] おやすみ。\n")
        draft = self.managed_text("draft_bilingual.txt", DRAFT)
        before = draft.read_bytes()
        requests = types.ModuleType("requests")
        def fail(*args, **kwargs):
            raise ConnectionError("simulated offline API")
        requests.post = fail
        with self.assertRaises(ConnectionError):
            self.execute("translate.py", [jp], {"requests": requests})
        self.assertEqual(before, draft.read_bytes())

    def test_srt_generation_uses_task_and_cancelled_replace_preserves_existing(self):
        final = self.managed_text("final_bilingual.txt", FINAL)
        self.text("output.srt", "old original subtitle")
        self.execute("make_srt.py", modules={"tkinter": self.fake_tk(final)})
        self.assertEqual(SRT, self.task.result("output.srt").read_text(encoding="utf-8"))
        before = self.task.result("output.srt").read_bytes()
        tk = self.fake_tk(final)
        answers = iter((True, False))
        tk.messagebox.askyesno = lambda *args, **kwargs: next(answers)
        self.execute("make_srt.py", modules={"tkinter": tk})
        self.assertEqual(before, self.task.result("output.srt").read_bytes())
        self.assertEqual("old original subtitle", (self.root / "output.srt").read_text(encoding="utf-8"))

    def test_player_subtitle_path_uses_selected_audio_task(self):
        self.managed_text("output.srt", SRT)
        self.text("output.srt", "wrong adjacent subtitle")
        # Execute only the original player's file-selection section. No VLC/Tk
        # player construction or keyboard hooks run during this routing test.
        source = (ROOT / "floating_subtitle_player.py").read_text(encoding="utf-8")
        source = source[:source.index("# 读取 SRT")]
        tk = self.fake_tk(self.audio)
        tk.ttk = types.SimpleNamespace()
        pynput = types.ModuleType("pynput")
        pynput.keyboard = types.SimpleNamespace()
        with patch.dict(sys.modules, {"tkinter": tk, "vlc": types.ModuleType("vlc"), "pynput": pynput}), patch.object(sys, 'argv', ['player']), redirect_stdout(io.StringIO()):
            namespace = {}
            exec(compile(source, "player_selection", "exec"), namespace)
        self.assertEqual(self.task.result("output.srt"), namespace["SUBTITLE_FILE"])

    def test_pipeline_paths_and_subprocess_audio_argument(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("staged_pipeline", ROOT / "run_pipeline.py")
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            if "ps" in args:
                return types.SimpleNamespace(stdout="")
            if "status" in args:
                return types.SimpleNamespace(stdout='{"running": true, "port": 1234}')
            if any(str(value).endswith("transcribe.py") for value in args):
                self.assertEqual(str(self.audio), args[-1])
                store.write_new(self.task.result("japanese.txt"), JP.encode())
            if any(str(value).endswith("translate.py") for value in args):
                self.assertEqual(str(self.task.result("japanese.txt")), args[-1])
                store.write_new(self.task.result("draft_bilingual.txt"), DRAFT.encode())
            return types.SimpleNamespace(stdout="")
        with patch.object(pipeline, "select_audio", return_value=self.audio), \
                patch.object(pipeline.shutil, "which", return_value="mock-lms"), \
                patch.object(pipeline.subprocess, "run", side_effect=fake_run), redirect_stdout(io.StringIO()):
            pipeline.main()
        self.assertTrue(self.task.result("draft_bilingual.txt").exists())
        self.assertFalse((self.root / "japanese.txt").exists())
        self.assertFalse((self.root / "draft_bilingual.txt").exists())

    def test_all_python_sources_parse(self):
        for path in ROOT.glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
