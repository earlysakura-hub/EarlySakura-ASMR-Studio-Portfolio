"""Synthetic TXT/SRT only. No models, VLC, real audio decoding or GUI windows."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import make_srt as srt
import studio
import task_store as store

STAMP = "[00:00:00,123 --> 00:00:01,456]"
JP = STAMP + " hello\n"
DRAFT = STAMP + "\nJP: hello\nZH_DRAFT: draft translation\n\n"
FINAL = STAMP + "\nJP: hello\nZH_FINAL: final translation\n\n"


class SrtSourcesTest(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work/srt_source_checks"
        work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        audio = Path(self.temp.name) / "synthetic.wav"
        audio.write_bytes(b"synthetic-no-decoder")
        self.task = store.open_task(audio)

    def put(self, name, text):
        path = self.task.result(name)
        path.write_text(text, encoding="utf-8")
        return path

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.task.directory.iterdir() if p.is_file()}

    def test_final_preferred_and_timestamps_preserved(self):
        self.put("japanese.txt", JP)
        self.put("draft_bilingual.txt", DRAFT)
        self.put("final_bilingual.txt", FINAL)
        before = self.snapshot()
        plan = srt.prepare_srt(self.task)
        self.assertEqual(plan["source"], "final")
        output = srt.generate_srt(self.task, plan)
        self.assertEqual(output.read_text(encoding="utf-8"), "1\n00:00:00,123 --> 00:00:01,456\nfinal translation\n\n")
        for name, state in before.items():
            self.assertEqual(self.snapshot()[name], state)

    def test_complete_draft_used_without_fake_final(self):
        self.put("japanese.txt", JP)
        self.put("draft_bilingual.txt", DRAFT)
        plan = srt.prepare_srt(self.task)
        self.assertEqual(plan["source"], "draft")
        output = srt.generate_srt(self.task, plan)
        self.assertIn("draft translation", output.read_text())
        self.assertFalse(self.task.result("final_bilingual.txt").exists())

    def test_bad_final_never_falls_back(self):
        self.put("japanese.txt", JP)
        self.put("draft_bilingual.txt", DRAFT)
        for bad in ("", FINAL.replace("final translation", ""), FINAL.replace("final translation", "非正式精修"),
                    FINAL.replace("ZH_FINAL", "ZH_DRAFT"), FINAL + "incomplete tail"):
            self.put("final_bilingual.txt", bad)
            with self.assertRaises(store.TaskError):
                srt.prepare_srt(self.task)
        self.assertFalse(self.task.result("output.srt").exists())

    def test_missing_final_blocks_are_rejected_against_japanese(self):
        self.put("japanese.txt", JP + JP.replace("00:00:00,123", "00:00:02,123").replace("00:00:01,456", "00:00:03,456"))
        self.put("final_bilingual.txt", FINAL)
        with self.assertRaises(store.TaskError):
            srt.prepare_srt(self.task)

    def test_incomplete_and_pending_drafts_rejected(self):
        self.put("japanese.txt", JP + JP)
        self.put("draft_bilingual.txt", DRAFT)
        with self.assertRaises(store.TaskError):
            srt.prepare_srt(self.task)
        self.put("japanese.txt", JP)
        (self.task.directory / "draft_bilingual.txt.pending.json").write_text("{}")
        with self.assertRaises(store.TaskError):
            srt.prepare_srt(self.task)

    def test_bad_timestamps_and_empty_translations_rejected(self):
        for bad in (FINAL.replace("00:00:01,456", "00:00:00,123"),
                    FINAL.replace("00:00:01,456", "00:61:01,456"), FINAL.replace("final translation", "")):
            with self.assertRaises(store.TaskError):
                srt.parse_bilingual(bad.encode(), "final")

    def test_no_source_or_draft_without_original_rejected(self):
        with self.assertRaises(store.TaskError):
            srt.prepare_srt(self.task)
        self.put("draft_bilingual.txt", DRAFT)
        with self.assertRaises(store.TaskError):
            srt.prepare_srt(self.task)

    def test_cancel_at_each_prompt_does_not_start_or_write(self):
        self.put("japanese.txt", JP)
        self.put("draft_bilingual.txt", DRAFT)
        self.put("output.srt", "old subtitle")
        app = studio.Studio.__new__(studio.Studio)
        app.busy, app.confirming, app.task, app.root = False, False, self.task, object()
        before = self.snapshot()
        for answers in ([False], [True, False]):
            with patch.object(studio.messagebox, "askyesno", side_effect=answers), patch.object(studio, "ProcessTree") as jobs:
                app.launch("srt")
                jobs.assert_not_called()
            self.assertEqual(self.snapshot(), before)

    def test_overwrite_prompt_names_correct_source(self):
        for source in ("final", "draft"):
            ask = Mock(side_effect=[True, True])
            self.assertTrue(srt.confirm_srt({"source": source, "output_sha256": "oldhash"}, ask))
            expected = "精翻" if source == "final" else "粗翻"
            self.assertEqual(ask.call_args.args[1], f"已有 output.srt。本次将使用【{expected}】重新生成，并替换现有字幕。是否继续？")

    def test_successful_replace_validates_temp_before_replacement(self):
        self.put("final_bilingual.txt", FINAL)
        output = self.put("output.srt", "old subtitle")
        old = output.read_bytes()
        replace = srt.os.replace
        def checked_replace(src, dst):
            self.assertEqual(Path(dst).read_bytes(), old)
            srt.validate_srt(Path(src), [("00:00:00,123", "00:00:01,456", "final translation")])
            return replace(src, dst)
        with patch.object(srt.os, "replace", side_effect=checked_replace):
            srt.generate_srt(self.task, srt.prepare_srt(self.task))
        self.assertIn("final translation", output.read_text())
        self.assertFalse(list(self.task.directory.glob("output.srt.partial-*")))

    def test_validation_and_replace_failures_keep_old_subtitle(self):
        self.put("final_bilingual.txt", FINAL)
        output = self.put("output.srt", "old subtitle")
        before = output.read_bytes(), output.stat().st_mtime_ns
        for target, error in (("validate_srt", store.TaskError("bad temporary SRT")), ("os.replace", PermissionError("in use"))):
            plan = srt.prepare_srt(self.task)
            with patch("make_srt." + target, side_effect=error):
                with self.assertRaises((store.TaskError, PermissionError)):
                    srt.generate_srt(self.task, plan)
            self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), before)

    def test_changed_source_or_old_output_requires_new_confirmation(self):
        final = self.put("final_bilingual.txt", FINAL)
        output = self.put("output.srt", "old subtitle")
        plan = srt.prepare_srt(self.task)
        final.write_text(FINAL.replace("final translation", "edited"))
        with self.assertRaises(store.TaskError):
            srt.generate_srt(self.task, plan)
        self.assertEqual(output.read_text(), "old subtitle")
        plan = srt.prepare_srt(self.task)
        output.write_text("externally changed")
        with self.assertRaises(store.TaskError):
            srt.generate_srt(self.task, plan)
        self.assertEqual(output.read_text(), "externally changed")

    def test_final_arriving_after_draft_confirmation_stops_generation(self):
        self.put("japanese.txt", JP)
        self.put("draft_bilingual.txt", DRAFT)
        plan = srt.prepare_srt(self.task)
        self.put("final_bilingual.txt", FINAL)
        with self.assertRaises(store.TaskError):
            srt.generate_srt(self.task, plan)
        self.assertFalse(self.task.result("output.srt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
