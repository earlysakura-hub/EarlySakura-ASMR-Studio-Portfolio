"""Synthetic old works only. Real legacy directory is never modified."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))
import test_media_library as fixtures
JP, DRAFT, FINAL = fixtures.JP, fixtures.DRAFT, fixtures.FINAL
import task_store as tasks
from media_library import LibraryError
import library_files as files
import library_migration as migration
from playback_history import HistoryStore


class MigrationTest(unittest.TestCase):
    setUp = fixtures.LibraryTest.setUp

    def legacy(self, stem='RJ654321'):
        directory = self.root / 'Old' / stem; directory.mkdir(parents=True)
        audio = directory / (stem + '.wav'); audio.write_bytes(b'old-' + stem.encode())
        task = tasks.open_task(audio)
        for name, text in [('japanese.txt', JP), ('draft_bilingual.txt', DRAFT), ('final_bilingual.txt', FINAL),
                           ('output.srt', '1\n00:00:00,123 --> 00:00:01,456\n你好。\n')]:
            (directory / name).write_text(text, encoding='utf-8')
        return task

    def test_preview_is_read_only_and_successful_migration_preserves_files_and_resume(self):
        a, b = self.legacy(), self.legacy('RJ654322')
        (Path(a.identity['path']).parent / '附属资料.docx').write_bytes(b'docx preserved')
        history = HistoryStore(self.library.history); history.begin_playback(a, 's', 1000, 10000)
        before = {str(path): path.read_bytes() for path in (self.root / 'Old').rglob('*') if path.is_file()}
        plan = migration.scan_preview(self.root / 'Old', self.library)
        self.assertEqual(plan['unknown_files'], [])
        self.assertEqual(len(plan['rows']), 2)
        self.assertFalse(any(row['decisions_required'] for row in plan['rows']))
        self.assertEqual(before, {str(path): path.read_bytes() for path in (self.root / 'Old').rglob('*') if path.is_file()})
        with self.assertRaises(LibraryError): migration.execute_preview(plan, 'wrong', library=self.library)
        moved = migration.execute_preview(plan, plan['approval_sha256'], library=self.library)
        self.assertEqual(len(moved), 2)
        for task in moved:
            work = self.library.get(task_id=task.identity['task_id'])
            self.assertEqual(work['state'], '已精修'); self.assertIn(migration.LEGACY_TAG, work['tags'])
            self.assertEqual(task.result('final_bilingual.txt').read_text(encoding='utf-8'), FINAL)
        self.assertEqual((moved[0].directory / 'originals/old/附属资料.docx').read_bytes(), b'docx preserved')
        self.assertEqual(history.resume_info(a.identity['task_id'])['position_ms'], 1000)
        self.assertEqual(len(migration.execute_preview(plan, plan['approval_sha256'], library=self.library)), 2)
        self.assertEqual(len(self.library.list()), 3)  # Includes initial inbox fixture.
        self.assertTrue((self.root / 'Old').exists())  # No directory cleanup.

    def test_unknown_and_shared_audio_results_block_execution(self):
        task = self.legacy(); folder = Path(task.identity['path']).parent
        unknown = folder / 'unowned.bin'; unknown.write_bytes(b'keep')
        plan = migration.scan_preview(self.root / 'Old', self.library)
        with self.assertRaises(LibraryError): migration.execute_preview(plan, plan['approval_sha256'], library=self.library)
        self.assertEqual(unknown.read_bytes(), b'keep'); self.assertTrue(Path(task.identity['path']).exists())
        unknown.unlink()  # Only synthetic test data.
        (folder / 'other.wav').write_bytes(b'second')
        plan = migration.scan_preview(self.root / 'Old', self.library)
        self.assertTrue(all(any('同目录' in text for text in row['decisions_required']) for row in plan['rows']))

    def test_changes_in_last_work_block_entire_batch_before_first_move(self):
        a = self.legacy(); b = self.legacy('RJ654322')
        plan = migration.scan_preview(self.root / 'Old', self.library)
        (Path(b.identity['path']).parent / 'final_bilingual.txt').write_text('changed', encoding='utf-8')
        with self.assertRaises(LibraryError): migration.execute_preview(plan, plan['approval_sha256'], library=self.library)
        self.assertTrue(Path(a.identity['path']).exists()); self.assertTrue(Path(b.identity['path']).exists())

    def test_published_legacy_migration_recovers_without_changing_audio_or_srt(self):
        task = self.legacy(); original = Path(task.identity['path']).read_bytes()
        plan = migration.scan_preview(self.root / 'Old', self.library)
        with patch.object(self.library, 'relocate', side_effect=LibraryError('SQLite unavailable')):
            with self.assertRaises(LibraryError): migration.execute_preview(plan, plan['approval_sha256'], library=self.library)
        self.assertEqual(Path(task.identity['path']).read_bytes(), original)
        op = self.library.pending_operations()[0]
        moved = files.recover_archive(op['op_id'], self.library)
        self.assertEqual(Path(moved.identity['path']).read_bytes(), original)
        migration.execute_preview(plan, plan['approval_sha256'], library=self.library)
        self.assertIn(migration.LEGACY_TAG, self.library.get(task_id=task.identity['task_id'])['tags'])

    def test_recovery_refuses_busy_source_without_removing_any_source_text(self):
        task = self.legacy(); plan = migration.scan_preview(self.root / 'Old', self.library)
        with patch.object(self.library, 'relocate', side_effect=LibraryError('offline')):
            with self.assertRaises(LibraryError): migration.execute_preview(plan, plan['approval_sha256'], library=self.library)
        source = Path(task.identity['path']).parent / 'final_bilingual.txt'
        with Path(task.identity['path']).open('rb'):
            with self.assertRaises(LibraryError): files.recover_archive(self.library.pending_operations()[0]['op_id'], self.library)
        self.assertEqual(source.read_text(encoding='utf-8'), FINAL)

    def test_test_audio_cannot_be_claimed_as_formally_refined(self):
        self.legacy('test')
        plan = migration.scan_preview(self.root / 'Old', self.library)
        self.assertEqual(plan['rows'][0]['planned_state'], '待确认')
        with self.assertRaises(LibraryError): migration.execute_preview(plan, plan['approval_sha256'], library=self.library)

    def test_refinement_hard_interruption_rolls_back_on_explicit_recovery(self):
        self.task.result('japanese.txt').write_text(JP, encoding='utf-8')
        self.task.result('draft_bilingual.txt').write_text(DRAFT, encoding='utf-8')
        moved = files.archive(self.task, self.library)
        source = self.root / 'final_bilingual.txt'; source.write_text(FINAL, encoding='utf-8')
        with patch.object(self.library, 'update', side_effect=KeyboardInterrupt('simulated hard exit')):
            with self.assertRaises(KeyboardInterrupt): files.import_refinement(self.work_id, source, confirmed=True, library=self.library)
        self.assertTrue(moved.result('final_bilingual.txt').exists())
        op = self.library.pending_operations()[0]
        files.recover_import(op['op_id'], self.library)
        self.assertFalse(moved.result('final_bilingual.txt').exists()); self.assertFalse(moved.result('output.srt').exists())
        self.assertEqual(self.library.get(self.work_id)['state'], '待精修')
        self.assertEqual(source.read_text(encoding='utf-8'), FINAL)


if __name__ == '__main__': unittest.main(verbosity=2)
