"""Packaging adapter checks without models, external programs or GUI."""
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import app_runtime as runtime
import studio


class PackagingTest(unittest.TestCase):
    def setUp(self):
        folder = ROOT / 'work' / 'packaging_checks'
        folder.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.config = self.folder / 'runtime_paths.json'
        executable = self.folder / 'EarlySakura.exe'
        executable.write_bytes(b'not a real executable')
        (self.folder / 'EarlySakuraWorker.exe').write_bytes(b'not a real executable')
        for patcher in (patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(executable)),
                        patch.dict(os.environ, {'LOCALAPPDATA': str(self.folder)})):
            patcher.start(); self.addCleanup(patcher.stop)
        self.previous_override = os.environ.pop('EARLYSAKURA_DATA_DIR', None)
        self.addCleanup(self.restore_override)

    def restore_override(self):
        if self.previous_override is not None: os.environ['EARLYSAKURA_DATA_DIR'] = self.previous_override

    def write(self, value):
        self.config.write_text(json.dumps(value), encoding='utf-8')

    def test_frozen_worker_is_explicit_not_python_script_or_gui(self):
        self.assertEqual(runtime.worker_command('pipeline', '音频.wav', gated=True),
                         [str(self.folder / 'EarlySakuraWorker.exe'), '--await-job', '--worker', 'pipeline', '音频.wav'])
        self.assertEqual(runtime.script_command('transcribe.py', '音频.wav'),
                         [str(self.folder / 'EarlySakuraWorker.exe'), '--worker', 'transcribe', '音频.wav'])
        self.assertEqual(runtime.script_command('translate.py', 'japanese.txt'),
                         [str(self.folder / 'EarlySakuraWorker.exe'), '--worker', 'translate', 'japanese.txt'])

    def test_missing_worker_never_falls_back_to_windowed_gui(self):
        (self.folder / 'EarlySakuraWorker.exe').unlink()
        with self.assertRaises(FileNotFoundError): runtime.worker_command('pipeline', 'A.wav')

    def test_worker_spawns_next_stage_with_same_console_helper(self):
        with patch.object(sys, 'executable', str(self.folder / 'EarlySakuraWorker.exe')):
            self.assertEqual(runtime.script_command('translate.py', 'japanese.txt')[0], str(self.folder / 'EarlySakuraWorker.exe'))

    def test_substage_has_no_window_empty_input_and_inherited_real_outputs(self):
        with patch.object(runtime.subprocess, 'run') as run:
            runtime.run_script_process('transcribe.py', 'A.wav')
            run.assert_called_once_with([str(self.folder / 'EarlySakuraWorker.exe'), '--worker', 'transcribe', 'A.wav'],
                                        check=True, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_gui_missing_streams_are_safe_and_write_log_only(self):
        old = sys.stdin, sys.stdout, sys.stderr
        created = []
        try:
            sys.stdin = sys.stdout = sys.stderr = None
            runtime.ensure_gui_streams()
            created = [sys.stdin, sys.stdout]
            self.assertEqual(sys.stdin.read(), '')
            self.assertIs(sys.stdout, sys.stderr)
            print('GUI 启动输出', flush=True)
            print('GUI 错误输出', file=sys.stderr, flush=True)
        finally:
            sys.stdin, sys.stdout, sys.stderr = old
            for stream in created: stream.close()
        log = self.folder / 'EarlySakura' / 'outputs' / 'gui_logs' / 'gui_startup.log'
        self.assertIn('GUI 错误输出', log.read_text(encoding='utf-8'))

    def test_source_mode_keeps_original_paths_and_python(self):
        with patch.object(sys, 'frozen', False):
            self.assertEqual(runtime.data_root(self.folder), self.folder / 'outputs')
            self.assertEqual(runtime.script_command('transcribe.py', 'A.wav'),
                             [sys.executable, str(ROOT / 'transcribe.py'), 'A.wav'])

    def test_existing_data_in_place_missing_path_falls_back_no_migration(self):
        old = self.folder / 'existing'; old.mkdir()
        (old / 'history').write_bytes(b'keep')
        self.write({'schema_version': 1, 'data_root': str(old)})
        self.assertEqual(runtime.data_root(), old)
        self.assertEqual((old / 'history').read_bytes(), b'keep')
        missing = self.folder / 'not-created'
        self.write({'schema_version': 1, 'data_root': str(missing)})
        self.assertEqual(runtime.data_root(), self.folder / 'EarlySakura' / 'outputs')
        self.assertFalse(missing.exists())

    def test_override_is_absolute_and_does_not_write_internal_resources(self):
        with patch.dict(os.environ, {'EARLYSAKURA_DATA_DIR': str(self.folder / 'test-data')}):
            self.assertEqual(runtime.data_root(), self.folder / 'test-data')
        with patch.dict(os.environ, {'EARLYSAKURA_DATA_DIR': 'relative'}):
            with self.assertRaises(ValueError): runtime.data_root()

    def test_bad_config_is_preserved_and_not_silently_reset(self):
        self.config.write_bytes(b'broken json')
        with self.assertRaises(ValueError): runtime.data_root()
        self.assertEqual(self.config.read_bytes(), b'broken json')

    def test_raw_script_worker_dispatch_has_no_task_side_effect(self):
        for mode in ('transcribe', 'translate'):
            with patch.object(runtime, 'run_script') as runner, patch.object(studio.store, 'prepare_audio_task') as prepare:
                studio.worker(mode, 'argument')
                runner.assert_called_once_with(mode + '.py', 'argument')
                prepare.assert_not_called()

    def test_new_build_path_is_dist_and_existing_directory_is_never_cleared(self):
        import build_exe
        with patch.object(build_exe, 'ROOT', self.folder):
            target = build_exe.output_directory('EarlySakura_Phase2_NewBuild')
            self.assertEqual(target, self.folder / 'dist/EarlySakura_Phase2_NewBuild')
            target.mkdir(parents=True); original = target / 'keep.txt'; original.write_bytes(b'keep')
            with self.assertRaises(FileExistsError): build_exe.output_directory('EarlySakura_Phase2_NewBuild')
            self.assertEqual(original.read_bytes(), b'keep')

    def test_build_name_cannot_escape_output_directory(self):
        import build_exe
        for name in ('../outside', r'..\outside', r'D:\outside', 'bad/name', '.hidden'):
            with self.assertRaises(ValueError): build_exe.output_directory(name)

    def test_explicit_new_modules_and_gui_worker_subsystems_in_spec(self):
        text = (ROOT / 'EarlySakura.spec').read_text(encoding='utf-8')
        for module in ('library_ui', 'player_page', 'player_ipc', 'gui_preferences', 'media_library', 'batch_queue', 'playback_history'):
            self.assertIn(repr(module), text)
        self.assertIn('console=False', text); self.assertIn('console=True', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
