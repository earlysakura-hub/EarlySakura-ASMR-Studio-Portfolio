"""Missing-audio display filtering without deleting metadata or resume records."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import task_store
from library_ui import LibraryWindow, audio_is_missing
from media_library import LibraryStore
from playback_history import HistoryStore


class MissingAudioTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'work/library_missing_audio_checks'; base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base); self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.audio = self.folder / '中文 日本語 作品' / 'RJ123456.wav'
        self.audio.parent.mkdir(); self.audio.write_bytes(b'synthetic audio')
        self.task = task_store.open_task(self.audio)
        self.library = LibraryStore(self.folder / 'data/media.sqlite3', self.folder / 'data/history.sqlite3')
        self.library.configure(self.folder / 'Library')
        self.work_id = self.library.register(self.task)
        self.library.update(self.work_id, title='保留标题', favorite=1)
        self.tag = self.library.save_tag('保留标签'); self.library.set_tags(self.work_id,[self.tag])
        self.history = HistoryStore(self.library.history)
        self.history.begin_playback(self.task,'test-session',20000,60000)
        self.work_before = self.library.get(self.work_id)
        self.history_before = self.history.list_entries()

    def view(self):
        view = LibraryWindow.__new__(LibraryWindow)
        view.library = self.library; view.busy = False; view.current = self.work_before
        view.rows = {self.work_id:self.work_before}; view.list = Mock()
        view.list.get_children.return_value = (self.work_id,)
        for name, value in (('search',''),('status_filter','全部'),('tag_filter','全部标签')):
            setattr(view,name,Mock(get=Mock(return_value=value)))
        for name in ('tag_box','title','info','notice','duration_reader','controls','set_plot'):
            setattr(view,name,Mock())
        view._duration_request = (1,self.work_id,str(self.audio))
        view.run = lambda title,operation,callback: callback(operation())
        return view

    def assert_metadata_unchanged(self):
        self.assertEqual(self.library.get(self.work_id),self.work_before)
        self.assertEqual(self.history.list_entries(),self.history_before)
        self.assertEqual(self.history.resume_info(self.task.identity['task_id']),{'position_ms':20000,'duration_ms':60000})

    def test_existing_unicode_path_is_visible_without_decoding(self):
        self.assertFalse(audio_is_missing(self.audio))
        view = self.view(); view.refresh()
        self.assertIn(self.work_id,view.rows); self.assert_metadata_unchanged()

    def test_deleted_audio_hidden_and_selection_cleared_without_metadata_deletion(self):
        self.audio.unlink()
        view = self.view(); view.refresh()
        self.assertEqual(view.rows,{})
        self.assertIsNone(view.current); self.assertIsNone(view._duration_request)
        view.duration_reader.request.assert_called_once_with(None,None)
        view.list.selection_set.assert_not_called()
        self.assertIn('已隐藏 1 部',view.notice.set.call_args.args[0])
        self.assert_metadata_unchanged()

    def test_whole_folder_disappearing_hides_work_and_restore_uses_same_id(self):
        original = self.audio.parent; held = self.folder / '暂存'
        self.assertTrue(original.is_relative_to(self.folder) and held.is_relative_to(self.folder))
        original.rename(held)  # Only this test's own temporary directory.
        view = self.view(); view.refresh(); self.assertEqual(view.rows,{})
        self.assert_metadata_unchanged()
        held.rename(original); view.refresh()
        self.assertIn(self.work_id,view.rows)
        view.list.selection_set.assert_called_with(self.work_id)
        self.assert_metadata_unchanged()

    def test_restoring_file_reappears_without_reregistering(self):
        content = self.audio.read_bytes(); self.audio.unlink()
        view = self.view(); view.refresh(); self.assertFalse(view.rows)
        self.audio.write_bytes(content); view.refresh()
        self.assertEqual(list(view.rows),[self.work_id]); self.assert_metadata_unchanged()

    def test_directory_instead_of_audio_is_not_a_playable_work(self):
        self.audio.unlink(); self.audio.mkdir()
        self.assertTrue(audio_is_missing(self.audio))
        view = self.view(); view.refresh(); self.assertFalse(view.rows)
        self.assert_metadata_unchanged()

    def test_permission_error_is_not_treated_as_deletion(self):
        with patch('library_ui.Path.stat',side_effect=PermissionError('access denied')):
            self.assertFalse(audio_is_missing(self.audio))

    def test_temporary_io_error_is_not_treated_as_deletion(self):
        with patch('library_ui.Path.stat',side_effect=OSError('device not ready')):
            self.assertFalse(audio_is_missing(self.audio))

    def test_invalid_parent_path_is_missing(self):
        with patch('library_ui.Path.stat',side_effect=NotADirectoryError('not a directory')):
            self.assertTrue(audio_is_missing(self.audio))

    def test_search_favorite_and_tag_filters_do_not_bring_missing_work_back(self):
        self.audio.unlink()
        for search,status,tag in (('RJ123456','全部','全部标签'),('','喜爱','全部标签'),('','全部','保留标签')):
            view = self.view()
            view.search.get.return_value=search; view.status_filter.get.return_value=status; view.tag_filter.get.return_value=tag
            view.refresh(); self.assertFalse(view.rows)
        self.assert_metadata_unchanged()

    def test_unknown_access_keeps_registration_visible(self):
        view = self.view()
        with patch('library_ui.audio_is_missing',return_value=False): view.refresh()
        self.assertIn(self.work_id,view.rows); self.assert_metadata_unchanged()


if __name__ == '__main__': unittest.main(verbosity=2)
