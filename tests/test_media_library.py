"""Isolated synthetic library checks: no models or formal user data."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import task_store as tasks
from media_library import LibraryStore, LibraryError
import library_files as files
from playback_history import HistoryStore, reopen_task
from batch_queue import check_entry

JP = '[00:00:00,123 --> 00:00:01,456] こんにちは。\n'
DRAFT = '[00:00:00,123 --> 00:00:01,456]\nJP: こんにちは。\nZH_DRAFT: 你好。\n\n'
FINAL = DRAFT.replace('ZH_DRAFT:', 'ZH_FINAL:')


class LibraryTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'work' / 'library_checks'; base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = LibraryStore(self.root / 'data' / 'library.sqlite3', self.root / 'data' / 'history.sqlite3')
        self.library.configure(self.root / 'Library')
        self.audio = self.library.root() / '待处理音频' / 'RJ123456.wav'
        self.audio.write_bytes(b'synthetic audio; no decoder')
        self.task = tasks.open_task(self.audio)
        self.work_id = self.library.register(self.task)

    def completed(self):
        tasks.write_new(self.task.result('japanese.txt'), JP.encode())
        tasks.write_new(self.task.result('draft_bilingual.txt'), DRAFT.encode())

    def test_metadata_titles_tags_favorite_search_persistence_do_not_touch_files(self):
        original = self.audio.read_bytes()
        self.library.update(self.work_id, title='中文・日本語 ❤', favorite=1)
        a = self.library.save_tag('耳语'); b = self.library.save_tag('剧情向')
        self.library.set_tags(self.work_id, [a, b])
        self.library.save_tag('囁き', a)
        self.assertEqual(len(self.library.list(search='RJ123456')), 1)
        self.assertEqual(len(self.library.list(search='日本語')), 1)
        self.assertEqual(len(self.library.list(status='喜爱')), 1)
        self.assertEqual(len(self.library.list(tag_id=a)), 1)
        self.library.set_tags(self.work_id, [b]); self.library.delete_tag(a)
        with self.assertRaises(LibraryError): self.library.delete_tag(1)
        with self.assertRaises(LibraryError): self.library.save_tag('喜爱', 1)
        reopened = LibraryStore(self.library.database, self.library.history)
        self.assertEqual(reopened.get(self.work_id)['tags'], ['剧情向'])
        self.assertEqual(reopened.get(self.work_id)['display_title'], '中文・日本語 ❤')
        self.assertEqual(self.audio.read_bytes(), original)
        self.assertEqual(self.library.register(self.task), self.work_id)

    def test_archive_identity_paths_history_resume_old_queue_and_idempotence(self):
        self.completed()
        tasks.write_new(self.task.directory / 'partial-note.txt', b'keep intermediate')
        history = HistoryStore(self.library.history)
        history.begin_playback(self.task, 'session', 20000, 60000)
        entry = history.list_entries()[0]
        old_task, old_audio = self.task, self.audio
        original = self.audio.read_bytes()
        moved = files.archive(self.task, self.library)
        self.assertEqual(moved.directory, self.library.root() / '已处理作品' / 'RJ123456')
        self.assertEqual(moved.identity['task_id'], old_task.identity['task_id'])
        self.assertEqual(self.library.get(self.work_id)['state'], '待精修')
        self.assertFalse(old_audio.exists())
        self.assertEqual(Path(moved.identity['path']).read_bytes(), original)
        self.assertEqual((moved.directory / 'partial-note.txt').read_bytes(), b'keep intermediate')
        self.assertEqual(tasks.open_task(moved.identity['path']).identity['task_id'], moved.identity['task_id'])
        self.assertEqual(tasks.load_task(old_task.directory).directory, moved.directory)
        self.assertEqual(history.resume_info(moved.identity['task_id']), {'position_ms': 20000, 'duration_ms': 60000})
        self.assertEqual(reopen_task(entry).directory, moved.directory)
        self.assertEqual(history.list_entries()[0]['audio_path'], moved.identity['path'])
        queued = {'task_id': old_task.identity['task_id'], 'directory': str(old_task.directory), 'audio': str(old_audio)}
        self.assertEqual(check_entry(queued)[0].directory, moved.directory)
        self.assertEqual(files.archive(old_task, self.library).directory, moved.directory)
        self.assertEqual(len(self.library.list()), 1)

    def test_incomplete_cancelled_and_busy_task_never_move_audio(self):
        tasks.write_new(self.task.result('japanese.txt'), JP.encode())
        with self.assertRaises(ValueError): files.archive(self.task, self.library)
        self.assertTrue(self.audio.exists())
        tasks.write_new(self.task.result('draft_bilingual.txt'), DRAFT.encode())
        with tasks.task_lock(self.task.directory):
            with self.assertRaises(tasks.TaskError): files.archive(self.task, self.library)
        self.assertTrue(self.audio.exists())

    def test_audio_reader_blocks_exclusive_archive_and_does_not_delete(self):
        self.completed()
        with self.audio.open('rb'):
            with self.assertRaises(LibraryError): files.archive(self.task, self.library)
        self.assertTrue(self.audio.exists())

    def test_collision_uses_unique_folder_without_overwriting(self):
        self.completed()
        conflict = self.library.root() / '已处理作品' / 'RJ123456'
        conflict.mkdir(); (conflict / 'user.txt').write_bytes(b'untouched')
        moved = files.archive(self.task, self.library)
        self.assertNotEqual(moved.directory, conflict)
        self.assertEqual((conflict / 'user.txt').read_bytes(), b'untouched')

    def test_archive_metadata_failure_keeps_original_then_recovers_published_copy(self):
        self.completed()
        with patch.object(self.library, 'relocate', side_effect=LibraryError('simulated SQLite failure')):
            with self.assertRaises(LibraryError): files.archive(self.task, self.library)
        self.assertTrue(self.audio.exists())
        self.assertTrue(self.task.result('japanese.txt').exists())
        operation = self.library.pending_operations()[0]
        moved = files.recover_archive(operation['op_id'], self.library)
        self.assertFalse(self.audio.exists())
        self.assertEqual(tasks.load_task(moved.directory).identity['task_id'], self.task.identity['task_id'])
        self.assertEqual(self.library.pending_operations(), [])

    def test_copy_interruption_recovers_without_claiming_complete_or_deleting(self):
        self.completed()
        with patch.object(files, 'verified_copy', side_effect=OSError('simulated interrupted copy')):
            with self.assertRaises(OSError): files.archive(self.task, self.library)
        self.assertTrue(self.audio.exists())
        op = self.library.pending_operations()[0]
        self.assertIsNone(files.recover_archive(op['op_id'], self.library))
        self.assertEqual(self.library.get(self.work_id)['state'], '待处理')
        self.assertTrue(self.audio.exists())
        self.assertIsNotNone(files.archive(self.task, self.library))

    def test_refinement_plot_optional_backup_srt_and_failure_rollback(self):
        self.completed(); moved = files.archive(self.task, self.library)
        inputs = self.root / 'inputs'; inputs.mkdir()
        final = inputs / 'final_bilingual.txt'; final.write_text(FINAL, encoding='utf-8')
        with self.assertRaises(LibraryError): files.import_refinement(self.work_id, final, library=self.library)
        files.import_refinement(self.work_id, final, confirmed=True, library=self.library)
        work = self.library.get(self.work_id)
        self.assertEqual(work['state'], '已精修')
        self.assertIn('你好。', moved.result('output.srt').read_text(encoding='utf-8'))
        self.assertEqual(files.read_plot(work), '暂无剧情介绍')
        plot = inputs / 'full_plot_summary.txt'; content = '完整剧情（含剧透）\n中文と日本語。\n' * 300
        plot.write_text(content, encoding='utf-8')
        files.import_refinement(self.work_id, plot=plot, confirmed=True, library=self.library)
        self.assertEqual(files.read_plot(self.library.get(self.work_id)), content)
        old_final = moved.result('final_bilingual.txt').read_bytes(); old_srt = moved.result('output.srt').read_bytes()
        final.write_text(FINAL.replace('你好。', '您好。'), encoding='utf-8')
        with self.assertRaises(LibraryError): files.import_refinement(self.work_id, final, confirmed=True, library=self.library)
        with patch.object(self.library, 'update', side_effect=LibraryError('simulated metadata failure')):
            with self.assertRaises(LibraryError): files.import_refinement(self.work_id, final, confirmed=True, replace=True, library=self.library)
        self.assertEqual(moved.result('final_bilingual.txt').read_bytes(), old_final)
        self.assertEqual(moved.result('output.srt').read_bytes(), old_srt)
        self.assertTrue(list(moved.directory.glob('.library_imports/*/backup/final_bilingual.txt')))

    def test_invalid_or_test_final_never_marks_refined_or_changes_srt(self):
        self.completed(); moved = files.archive(self.task, self.library)
        final = self.root / 'final_bilingual.txt'
        for text in ('bad format', FINAL + '\n非正式精修', FINAL.replace('01,456', '00,123')):
            final.write_text(text, encoding='utf-8')
            with self.assertRaises(ValueError): files.import_refinement(self.work_id, final, confirmed=True, library=self.library)
            self.assertEqual(self.library.get(self.work_id)['state'], '待精修')
            self.assertFalse(moved.result('output.srt').exists())

    def test_bad_database_and_bad_plot_are_preserved(self):
        bad = self.root / 'corrupt.sqlite3'; bad.write_bytes(b'not a SQLite database')
        with self.assertRaises(LibraryError): LibraryStore(bad).list()
        self.assertEqual(bad.read_bytes(), b'not a SQLite database')
        plot = self.task.directory / 'full_plot_summary.txt'; plot.write_bytes(b'\xff\xfeinvalid')
        with self.assertRaises(UnicodeError): files.read_plot(self.library.get(self.work_id))
        self.assertEqual(plot.read_bytes(), b'\xff\xfeinvalid')

    def test_existing_srt_requires_explicit_replacement_and_success_keeps_backup(self):
        self.completed(); moved = files.archive(self.task, self.library)
        moved.result('output.srt').write_bytes(b'existing subtitle')
        source = self.root / 'final_bilingual.txt'; source.write_text(FINAL, encoding='utf-8')
        with self.assertRaises(LibraryError): files.import_refinement(self.work_id, source, confirmed=True, library=self.library)
        self.assertEqual(moved.result('output.srt').read_bytes(), b'existing subtitle')
        files.import_refinement(self.work_id, source, confirmed=True, replace=True, library=self.library)
        backups = list(moved.directory.glob('.library_imports/*/backup/output.srt'))
        self.assertEqual(backups[0].read_bytes(), b'existing subtitle')
        self.assertEqual(self.library.get(self.work_id)['state'], '已精修')


if __name__ == '__main__': unittest.main(verbosity=2)
