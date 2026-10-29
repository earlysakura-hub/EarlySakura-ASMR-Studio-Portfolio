"""Only synthetic data and harmless processes; no ASMR inference or VLC."""
from pathlib import Path
import os
import queue
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import studio
import task_store as store
from windows_job import ProcessTree, Cancelled

STAMP = "[00:00:00,123 --> 00:00:01,456]"
JP = STAMP + " hello\n"
DRAFT = STAMP + "\nJP: hello\nZH_DRAFT: translated\n\n"
FINAL = STAMP + "\nJP: hello\nZH_FINAL: final\n\n"


class ImportCancelTest(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work/gui_import_cancel_checks"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.audio = self.folder / "legacy-work.m4a"
        self.audio.write_bytes(b"synthetic-audio-no-decoder")
        self.task = store.open_task(self.audio)
        aliases = patch.object(store, "configuration", return_value={"legacy_srt_aliases": {"legacy-work.m4a": "legacy-work.zh.srt"}})
        aliases.start(); self.addCleanup(aliases.stop)

    def legacy(self, name, text):
        p = self.folder / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_single_subset_alias_and_original_preservation(self):
        p = self.legacy("legacy-work.zh.srt", "exact\r\nsubtitle bytes\n")
        before = p.read_bytes(), p.stat().st_mtime_ns
        selection = store.map_legacy_selection(self.task, [p])
        self.assertEqual(list(selection), ["output.srt"])
        store.import_legacy(self.task, selection)
        self.assertEqual(self.task.result("output.srt").read_bytes(), before[0])
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)
        self.assertFalse(studio.inspect_task(self.task)[1])
        self.assertFalse(self.task.result("japanese.txt").exists())

    def test_alias_limited_to_one_work_and_duplicates_rejected(self):
        alias = self.legacy("legacy-work.zh.srt", "srt")
        canonical = self.legacy("output.srt", "srt")
        with self.assertRaises(store.TaskError):
            store.map_legacy_selection(self.task, [alias, canonical])
        other = self.legacy("other.m4a", "other audio")
        with self.assertRaises(store.TaskError):
            store.map_legacy_selection(store.open_task(other), [alias])

    def test_conflict_preflights_entire_selection(self):
        jp = self.legacy("japanese.txt", JP)
        final = self.legacy("final_bilingual.txt", FINAL)
        store.write_new(self.task.result("japanese.txt"), b"different previous result")
        before = self.task.result("japanese.txt").read_bytes()
        with self.assertRaises(FileExistsError):
            store.import_legacy(self.task, {"final_bilingual.txt": final, "japanese.txt": jp})
        self.assertFalse(self.task.result("final_bilingual.txt").exists())
        self.assertEqual(before, self.task.result("japanese.txt").read_bytes())

    def test_declining_ownership_does_not_import(self):
        p = self.legacy("legacy-work.zh.srt", "srt")
        with patch("tkinter.filedialog.askopenfilenames", return_value=(str(p),)), \
                patch("tkinter.messagebox.askyesno", return_value=False), \
                patch.object(store, "import_legacy") as importing:
            self.assertFalse(store.choose_import(self.task, parent=object()))
            importing.assert_not_called()

    def test_cancel_ui_waits_for_confirmation_then_restores_or_closes(self):
        app = studio.Studio.__new__(studio.Studio)
        app.events = queue.Queue()
        app.busy, app.cancelling, app.close_after_cancel = False, True, False
        app.active_mode, app.recovery_required = "pipeline", False
        app.tree = object()
        job = studio.ActiveJob("pipeline", self.task, app.tree)
        app.pipeline_job, app.player_job, app.pending_player_task = job, None, None
        app.task, app.exiting = self.task, False
        app.pipeline_status = Mock()
        app.root = types.SimpleNamespace(after=Mock(), after_cancel=Mock(), destroy=Mock())
        app.progress = types.SimpleNamespace(stop=Mock(), configure=Mock())
        app.progress_text = Mock()
        app.status = app.audio_name = app.audio_path = Mock()
        app.file_states = {name: Mock() for name in store.RESULT_NAMES}
        app.append_log, app.update_buttons = Mock(), Mock()
        app.events.put(("job_unsafe", (job, "synthetic safety failure")))
        app.drain()
        self.assertFalse(app.busy)
        self.assertIs(app.pipeline_job, job)
        self.assertTrue(app.recovery_required)
        app.root.destroy.assert_not_called()
        app.exiting = True
        app.events.put(("job_done", (job, (studio.inspect_task(self.task), "取消完成"))))
        app.drain()
        self.assertFalse(app.busy)
        self.assertIsNone(app.pipeline_job)
        app.root.destroy.assert_called_once()

    def test_full_import_skips_models_partial_does_not_claim_complete(self):
        jp = self.legacy("japanese.txt", JP)
        draft = self.legacy("draft_bilingual.txt", DRAFT)
        store.import_legacy(self.task, {"japanese.txt": jp})
        self.assertFalse(studio.inspect_task(self.task)[1])
        with patch("run_pipeline.main") as pipeline:
            with self.assertRaises(store.TaskError):
                studio.worker("pipeline", self.audio)
            pipeline.assert_not_called()
        store.import_legacy(self.task, {"draft_bilingual.txt": draft})
        with patch("run_pipeline.main") as pipeline:
            studio.worker("pipeline", self.audio)
            pipeline.assert_not_called()

    def test_existing_final_prevents_accidental_inference(self):
        store.write_new(self.task.result("final_bilingual.txt"), FINAL.encode())
        with patch("run_pipeline.main") as pipeline:
            with self.assertRaises(store.TaskError):
                studio.worker("pipeline", self.audio)
            pipeline.assert_not_called()

    def test_legacy_seconds_final_recognized_without_rewriting(self):
        text = FINAL.replace(",123", "").replace(",456", "").replace("00:00:01", "00:00:00")
        path = self.task.result("final_bilingual.txt")
        store.write_new(path, text.encode())
        before = path.read_bytes(), path.stat().st_mtime_ns
        states, complete, final_ok, _ = studio.inspect_task(self.task)
        self.assertIn("旧版秒级精修", states["final_bilingual.txt"])
        self.assertFalse(complete)
        self.assertFalse(final_ok)  # Existing converter requires milliseconds.
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_pending_blocks_even_apparently_complete_draft(self):
        store.write_new(self.task.result("japanese.txt"), JP.encode())
        store.write_new(self.task.result("draft_bilingual.txt"), DRAFT.encode())
        store.write_new(self.task.directory / "draft_bilingual.txt.pending.json", b"{}")
        self.assertFalse(studio.inspect_task(self.task)[1])
        with self.assertRaises(store.TaskError):
            store.check_draft_progress(self.task.result("japanese.txt"), self.task.result("draft_bilingual.txt"))

    def run_translation_fixture(self):
        response = types.SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": "1|translated"}}]})
        fake_requests = types.SimpleNamespace(post=lambda *args, **kwargs: response)
        with patch.dict(sys.modules, {"requests": fake_requests}), \
                patch.object(sys, "argv", ["translate.py", str(self.task.result("japanese.txt"))]):
            runpy.run_path(str(ROOT / "translate.py"), run_name="__main__")

    def test_real_translation_write_marker_success_without_api(self):
        store.write_new(self.task.result("japanese.txt"), JP.encode())
        self.run_translation_fixture()
        self.assertEqual(store.check_draft_progress(self.task.result("japanese.txt"), self.task.result("draft_bilingual.txt")), (1, 1))
        self.assertFalse((self.task.directory / "draft_bilingual.txt.pending.json").exists())

    def test_interrupted_flush_does_not_claim_complete(self):
        store.write_new(self.task.result("japanese.txt"), JP.encode())
        original = os.fsync
        calls = []
        def fail_output_flush(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("simulated interruption after batch write")
            return original(fd)
        with patch.object(os, "fsync", side_effect=fail_output_flush):
            with self.assertRaises(OSError):
                self.run_translation_fixture()
        self.assertTrue((self.task.directory / "draft_bilingual.txt.pending.json").exists())
        self.assertFalse(studio.inspect_task(self.task)[1])
        with store.task_lock(self.task.directory):
            pass

    def test_cancel_before_start_and_assignment_failure_do_no_work(self):
        tree = ProcessTree()
        try:
            tree.cancel()
            with self.assertRaises(Cancelled):
                tree.start([sys.executable, "-c", "raise RuntimeError('must not execute')"], text=True)
        finally:
            tree.finish()
        tree = ProcessTree()
        sentinel = self.folder / "should-not-exist"
        try:
            with patch.object(tree._api, "AssignProcessToJobObject", return_value=0):
                with self.assertRaises(OSError):
                    tree.start([sys.executable, "-c", f"open({str(sentinel)!r},'w').close()"], text=True)
            self.assertFalse(sentinel.exists())
        finally:
            tree.finish()

    def test_cancel_tree_keeps_unrelated_process_and_releases_lock(self):
        survivor = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
        tree = ProcessTree()
        process = None
        messages = queue.Queue()
        try:
            store.write_new(self.task.result("final_bilingual.txt"), FINAL.encode())
            before = self.task.result("final_bilingual.txt").read_bytes()
            process = tree.start([sys.executable, "-B", "-u", str(ROOT / "tests/cancel_worker_fixture.py"), str(self.task.directory)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", cwd=ROOT)
            def read():
                for line in process.stdout:
                    messages.put(line.strip())
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            seen = set()
            deadline = time.monotonic() + 12
            while not {"FIXTURE_LOCKED", "FIXTURE_CHILD", "FIXTURE_GRANDCHILD"} <= seen:
                seen.add(messages.get(timeout=max(0.1, deadline-time.monotonic())))
                if time.monotonic() > deadline:
                    self.fail(str(seen))
            self.assertGreaterEqual(tree.active_count(), 3)
            with self.assertRaises(store.TaskError):
                with store.task_lock(self.task.directory):
                    pass
            tree.cancel()
            tree.finish()
            process.wait(timeout=5)
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive())
            self.assertEqual(tree.active_count(), 0)
            self.assertIsNone(survivor.poll())
            with store.task_lock(self.task.directory):
                pass
            self.assertEqual(before, self.task.result("final_bilingual.txt").read_bytes())
            self.assertTrue((self.task.directory / "japanese.txt.partial-cancel-fixture").exists())
            self.assertFalse(studio.inspect_task(self.task)[1])
        finally:
            tree.cancel()
            tree.finish()
            if process:
                process.wait(timeout=5)
                process.stdout.close()
            survivor.terminate()
            survivor.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
