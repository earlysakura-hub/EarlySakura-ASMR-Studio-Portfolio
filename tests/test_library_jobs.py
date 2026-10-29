"""Real owned Windows child processes; inference remains synthetic."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))
import test_parallel as fixtures
import studio
import task_store as tasks
from media_library import LibraryStore, LibraryError
from test_media_library import JP, DRAFT


class LibraryJobsTest(unittest.TestCase):
    setUp = fixtures.ParallelTest.setUp
    pump_until = fixtures.ParallelTest.pump_until
    cleanup_runs = fixtures.ParallelTest.cleanup_runs

    def inbox(self):
        library = LibraryStore(self.directory / 'library.sqlite3', self.history.database)
        library.configure(self.directory / 'Library')
        audio = library.root() / '待处理音频' / 'B.wav'; audio.write_bytes(b'B synthetic')
        task = tasks.open_task(audio); work_id = library.register(task)
        self.app.library = library; self.app.library_busy_tasks = set()
        return library, task, work_id

    def test_successful_pipeline_archives_while_another_player_stays_alive(self):
        library, task, work_id = self.inbox()
        self.app.start_player(self.tasks[0]); self.pump_until(lambda: '正在播放' in str(self.app.player_status.set.call_args))
        player_job = self.app.player_job
        self.app.start_job('pipeline', task); self.pump_until(lambda: self.app.pipeline_job is None)
        work = library.get(work_id)
        self.assertEqual(work['archive_state'], 'archived'); self.assertEqual(work['auto_state'], 'completed')
        self.assertFalse(Path(task.identity['path']).exists()); self.assertTrue(Path(work['audio_path']).exists())
        self.assertIs(self.app.player_job, player_job); self.assertIsNone(player_job.process.poll())

    def test_failure_and_cancel_retain_audio_and_stage_state_without_archive(self):
        for behavior, expected in [('fail-qwen', 'failed'), ('wait', 'cancelled')]:
            library, task, work_id = self.inbox()
            self.behavior['pipeline'] = behavior
            self.app.start_job('pipeline', task)
            if behavior == 'wait':
                self.pump_until(lambda: any('FIXTURE_READY' in str(call) for call in self.app.append_log.call_args_list))
                self.app.cancel()
            self.pump_until(lambda: self.app.pipeline_job is None)
            self.assertEqual(library.get(work_id)['auto_state'], expected)
            self.assertNotEqual(library.get(work_id)['archive_state'], 'archived')
            self.assertTrue(Path(task.identity['path']).exists())
            # Separate synthetic task for second branch without removing any results.
            if behavior == 'fail-qwen':
                library.configure(library.root())
                self.directory = self.directory / 'next'; self.directory.mkdir()

    def test_optional_library_failure_does_not_invalidate_complete_pipeline(self):
        library, task, work_id = self.inbox()
        with patch.object(library, 'in_inbox', side_effect=LibraryError('simulated SQLite failure')):
            self.app.start_job('pipeline', task); self.pump_until(lambda: self.app.pipeline_job is None)
        self.assertTrue(studio.inspect_task(task)[1])
        self.assertEqual(library.get(work_id)['auto_state'], 'completed')
        self.assertTrue(any('处理完成' in str(call) for call in self.app.pipeline_status.set.call_args_list))
        self.assertTrue(Path(task.identity['path']).exists())

    def test_actual_worker_skips_models_for_completed_inbox_and_parent_archives(self):
        library, task, work_id = self.inbox()
        task.result('japanese.txt').write_text(JP, encoding='utf-8')
        task.result('draft_bilingual.txt').write_text(DRAFT, encoding='utf-8')
        def command(mode, audio, gated=False):
            return [sys.executable, '-B', '-u', '-X', 'utf8', str(ROOT / 'studio.py'),
                    '--await-job', '--worker', mode, str(audio)]
        with patch.object(studio, 'worker_command', side_effect=command):
            self.app.start_job('pipeline', task); self.pump_until(lambda: self.app.pipeline_job is None)
        self.assertEqual(library.get(work_id)['archive_state'], 'archived')
        self.assertTrue(any('跳过处理' in str(call) for call in self.app.append_log.call_args_list))


if __name__ == '__main__': unittest.main(verbosity=2)
