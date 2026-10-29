"""Isolated media actions; Explorer and dialogs mocked, real SRT conversion."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]; sys.path.insert(0, str(ROOT))
import library_ui
import make_srt
import task_store
from media_library import LibraryStore

STAMP = '[00:00:00,123 --> 00:00:01,456]'


class Variable:
    def __init__(self, value=''): self.value = value
    def set(self, value): self.value = value
    def get(self): return self.value


class LibraryActionsTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'work/library_entry_checks'; base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base); self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.library = LibraryStore(self.folder / 'data/media.sqlite3'); self.library.configure(self.folder / 'Library')
        self.tasks, self.works = [], []
        for index, name in enumerate(('已归档 日本語 & 空格.m4a', '待精修 中文.mp3')):
            folder = self.library.root() / ('已处理作品' if index == 0 else '待处理音频')
            folder.mkdir(parents=True, exist_ok=True); audio = folder / name; audio.write_bytes(b'synthetic' + bytes([index]))
            task = task_store.open_task(audio); work_id = self.library.register(task)
            self.library.update(work_id, archive_state='archived' if index == 0 else 'pending',
                                auto_state='completed', refinement='refined' if index == 0 else 'pending')
            task.result('japanese.txt').write_text(STAMP + ' テスト。\n', encoding='utf-8')
            task.result('draft_bilingual.txt').write_text(STAMP + f'\nJP: テスト。\nZH_DRAFT: 粗翻{index}。\n\n', encoding='utf-8')
            (task.directory / 'full_plot_summary.txt').write_text('保留剧情。', encoding='utf-8')
            self.tasks.append(task); self.works.append(self.library.get(work_id))
        self.view = view = library_ui.LibraryWindow.__new__(library_ui.LibraryWindow)
        view.library = self.library; view.current = self.works[0]; view.busy = view.closing = False; view.window = object()
        view.notice = Variable(); view.info = Variable('字幕：暂无字幕'); view.controls = Mock(); view.errors = []
        view.app = SimpleNamespace(exiting=False, confirming=False, task=self.tasks[1], task_in_use=Mock(return_value=False), update_buttons=Mock())
        def run(title, operation, callback=None, task=None):
            if task is not None and view.app.task_in_use(task): return
            try: value = operation()
            except Exception as error: view.errors.append(str(error))
            else:
                if callback: callback(value)
        view.run = Mock(side_effect=run)
        for target, options in (('messagebox.askyesno', {'return_value': True}), ('messagebox.showinfo', {}),
                                ('messagebox.showerror', {}), ('os.startfile', {})):
            patcher = patch('library_ui.' + target, **options); mock = patcher.start(); self.addCleanup(patcher.stop)
            setattr(self, target.split('.')[-1], mock)

    def final(self, index=0):
        self.tasks[index].result('final_bilingual.txt').write_text(STAMP + f'\nJP: テスト。\nZH_FINAL: 正式精翻{index}。\n\n', encoding='utf-8')

    def protected(self):
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for task in self.tasks
                for p in [Path(task.identity['path']), *task.directory.glob('*.txt')]}

    def test_folder_uses_real_audio_parent_for_archived_and_pending_unicode(self):
        before, records = self.protected(), self.library.list()
        for work in self.works:
            self.view.current = work; self.view.open_folder()
            self.startfile.assert_called_with(str(Path(work['audio_path']).parent))
            self.assertIs(self.view.current, work)
        self.assertEqual(self.protected(), before); self.assertEqual(self.library.list(), records)

    def test_folder_missing_audio_or_directory_is_not_opened(self):
        for audio in (str(self.folder / 'missing/file.wav'), str(self.folder / 'missing.wav')):
            self.view.current = dict(self.works[0], audio_path=audio); self.view.open_folder()
        self.startfile.assert_not_called(); self.assertEqual(self.showerror.call_count, 2)

    def test_folder_no_selection_or_relative_path_is_safe(self):
        self.view.current = None; self.view.open_folder(); self.startfile.assert_not_called()
        self.view.current = dict(self.works[0], audio_path='relative.wav'); self.view.open_folder()
        self.startfile.assert_not_called(); self.showerror.assert_called_once()

    def test_folder_os_failure_is_reported(self):
        self.startfile.side_effect = OSError('Explorer failed'); self.view.open_folder()
        self.showerror.assert_called_once()

    def test_srt_uses_selected_work_not_previous_app_task_and_preserves_metadata(self):
        self.final(); before, records, old_task = self.protected(), self.library.list(), self.view.app.task
        self.view.make_srt()
        self.assertIn('正式精翻0', self.tasks[0].result('output.srt').read_text(encoding='utf-8'))
        self.assertFalse(self.tasks[1].result('output.srt').exists()); self.assertIs(self.view.app.task, old_task)
        self.assertEqual(self.protected(), before); self.assertEqual(self.library.list(), records)
        self.assertIn(str(self.tasks[0].result('output.srt')), self.view.notice.get())

    def test_draft_confirmation_and_no_fake_final(self):
        self.view.current = self.works[1]; self.view.make_srt()
        self.assertEqual(self.askyesno.call_args.args[0], '使用 Qwen 粗翻')
        self.assertIn('粗翻1', self.tasks[1].result('output.srt').read_text(encoding='utf-8'))
        self.assertFalse(self.tasks[1].result('final_bilingual.txt').exists())

    def test_invalid_or_test_final_never_falls_back(self):
        for content in ('', '格式错误', STAMP + '\nJP: テスト。\nZH_FINAL: 非正式精修\n'):
            self.tasks[0].result('final_bilingual.txt').write_text(content, encoding='utf-8')
            self.view.make_srt(); self.assertFalse(self.tasks[0].result('output.srt').exists())
        self.assertEqual(len(self.view.errors), 3); self.askyesno.assert_not_called()

    def test_missing_or_incomplete_source_is_rejected(self):
        self.tasks[0].result('draft_bilingual.txt').unlink(); self.view.make_srt()
        self.assertFalse(self.tasks[0].result('output.srt').exists())
        self.view.current = self.works[1]
        self.tasks[1].result('japanese.txt').write_text(STAMP + ' テスト。\n' + STAMP + ' extra\n', encoding='utf-8')
        self.view.make_srt(); self.assertFalse(self.tasks[1].result('output.srt').exists())
        self.assertEqual(len(self.view.errors), 2)

    def test_cancel_source_or_overwrite_keeps_existing_output(self):
        output = self.tasks[0].result('output.srt'); output.write_bytes(b'old formal subtitle')
        before = output.read_bytes(), output.stat().st_mtime_ns
        for answers in ([False], [True, False]):
            self.askyesno.side_effect = answers; self.view.make_srt()
            self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), before)
            self.assertFalse(self.view.app.confirming); self.assertFalse(self.view.busy)

    def test_approved_replace_matches_existing_converter(self):
        self.final(); self.tasks[0].result('output.srt').write_bytes(b'old subtitle'); self.view.make_srt()
        self.assertEqual(self.askyesno.call_count, 2)
        self.assertIn('【精翻】', self.askyesno.call_args.args[1])
        self.assertEqual(self.tasks[0].result('output.srt').read_text(encoding='utf-8'),
                         '1\n00:00:00,123 --> 00:00:01,456\n正式精翻0。\n\n')

    def test_validation_or_write_failure_preserves_old_subtitle(self):
        self.final(); output = self.tasks[0].result('output.srt'); output.write_bytes(b'keep')
        before = output.read_bytes(), output.stat().st_mtime_ns
        for target in ('make_srt.validate_srt', 'make_srt.os.replace'):
            with patch(target, side_effect=OSError('simulated failure')): self.view.make_srt()
            self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), before)
        self.assertEqual(len(self.view.errors), 2)

    def test_selection_change_during_preview_never_generates_previous_work(self):
        def changed(title, operation, callback=None, task=None):
            result = operation(); self.view.current = self.works[1]; callback(result)
        self.view.run.side_effect = changed; self.view.make_srt()
        self.askyesno.assert_not_called()
        self.assertFalse(any(task.result('output.srt').exists() for task in self.tasks))

    def test_selection_change_during_confirmation_stops_generation(self):
        def changed(*args, **kwargs): self.view.current = self.works[1]; return True
        self.askyesno.side_effect = changed; self.view.make_srt()
        self.assertFalse(any(task.result('output.srt').exists() for task in self.tasks))

    def test_another_confirmation_opened_during_preview_keeps_its_flag(self):
        def another_dialog(title, operation, callback=None, task=None):
            result = operation(); self.view.app.confirming = True; callback(result)
        self.view.run.side_effect = another_dialog; self.view.make_srt()
        self.askyesno.assert_not_called(); self.assertTrue(self.view.app.confirming)
        self.assertFalse(self.tasks[0].result('output.srt').exists())

    def test_mismatched_task_or_audio_identity_is_rejected(self):
        for fields in ({'task_id': self.works[1]['task_id']}, {'audio_path': self.works[1]['audio_path']}):
            self.view.current = dict(self.works[0], **fields); self.view.make_srt()
        self.assertEqual(len(self.view.errors), 2); self.askyesno.assert_not_called()

    def test_playing_or_processing_same_work_blocks_write(self):
        self.view.app.task_in_use.return_value = True; self.view.make_srt()
        self.askyesno.assert_not_called(); self.showinfo.assert_called_once()
        self.assertFalse(self.tasks[0].result('output.srt').exists())

    def test_no_selection_does_not_preview_or_generate(self):
        self.view.current = None; self.view.make_srt(); self.view.run.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
