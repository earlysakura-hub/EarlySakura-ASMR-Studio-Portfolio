"""Isolated column preferences and Tk event/controller checks; no user data."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
from gui_preferences import GuiPreferences, PreferencesError, DEFAULT_LIBRARY_COLUMN_WIDTHS
from library_ui import LibraryWindow, fit_library_columns, resize_library_columns

WIDTHS = {'title': 310, 'rj': 145, 'state': 95, 'favorite': 65}


class LibraryColumnsTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'work/library_column_checks'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'gui_preferences.json'
        self.preferences = GuiPreferences(self.path)

    def view(self):
        view = LibraryWindow.__new__(LibraryWindow)
        view.preferences = self.preferences
        view._saved_column_widths = self.preferences.library_column_widths()
        view._column_widths = dict(view._saved_column_widths)
        view._column_drag_start = None
        view._column_layout_id = None
        view._column_drag = False; view._column_save_id = None; view.closing = False
        view.window = Mock(); view.window.after.return_value = 'save-timer'
        view._display_widths = dict(view._column_widths)
        view.list = Mock(); view.list.identify_column.return_value = '#2'
        view.list.winfo_width.return_value = 482
        def column(key, option=None, **options):
            if 'width' in options: view._display_widths[key] = options['width']
            if option == 'width': return view._display_widths[key]
        view.list.column.side_effect = column
        view.notice = Mock(); view.app = SimpleNamespace(append_log=Mock(), library=Mock())
        return view

    def test_missing_config_uses_original_widths_without_writing(self):
        self.assertEqual(self.preferences.library_column_widths(), {'title':245,'rj':115,'state':75,'favorite':45})
        self.assertFalse(self.path.exists())

    def test_invalid_entries_fall_back_individually(self):
        for invalid in (True, None, '100', 100.0, 0, -2, 39, 10001, [], {}):
            self.path.write_text(json.dumps({'library_column_widths': dict(WIDTHS, title=invalid)}), encoding='utf-8')
            self.assertEqual(self.preferences.library_column_widths(), dict(WIDTHS, title=245))
        for invalid in (None, [], 'wide', 5):
            self.path.write_text(json.dumps({'library_column_widths': invalid}), encoding='utf-8')
            self.assertEqual(self.preferences.library_column_widths(), DEFAULT_LIBRARY_COLUMN_WIDTHS)

    def test_corrupt_config_falls_back_without_overwriting(self):
        for content in (b'{bad', b'null', b'[]', b'\xff'):
            self.path.write_bytes(content)
            self.assertEqual(self.preferences.library_column_widths(), DEFAULT_LIBRARY_COLUMN_WIDTHS)
            with self.assertRaises(PreferencesError): self.preferences.save_library_column_widths(WIDTHS)
            self.assertEqual(self.path.read_bytes(), content)

    def test_fresh_instances_and_font_saves_preserve_all_settings(self):
        self.path.write_text(json.dumps({'plot_font_size':17,'future':{'日本語':'保持'}}), encoding='utf-8-sig')
        self.preferences.save_library_column_widths(WIDTHS)
        reopened = GuiPreferences(self.path)
        self.assertEqual(reopened.library_column_widths(), WIDTHS)
        self.assertEqual(reopened.plot_font_size(), 17)
        reopened.save_plot_font_size(18)
        self.assertEqual(self.preferences.library_column_widths(), WIDTHS)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['future'], {'日本語':'保持'})

    def test_identical_widths_do_not_write(self):
        self.preferences.save_library_column_widths(WIDTHS)
        with patch.object(self.preferences, '_write') as write:
            self.preferences.save_library_column_widths(dict(WIDTHS))
            write.assert_not_called()

    def test_atomic_failure_keeps_existing_config(self):
        self.preferences.save_plot_font_size(17)
        before = self.path.read_bytes()
        with patch('gui_preferences.os.replace', side_effect=OSError('read only')):
            with self.assertRaises(OSError): self.preferences.save_library_column_widths(WIDTHS)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('*.tmp-*')), [])

    def test_invalid_writes_are_rejected(self):
        for value in (None, [], {}, {'title':100}, dict(WIDTHS, extra=100), dict(WIDTHS, title=True), dict(WIDTHS, state=0)):
            with self.assertRaises(PreferencesError): self.preferences.save_library_column_widths(value)
        self.assertFalse(self.path.exists())

    def test_only_separator_release_schedules_save(self):
        view = self.view(); event = SimpleNamespace(x=100, y=10)
        for region in ('cell', 'heading', 'nothing'):
            view.list.identify_region.return_value = region
            view.column_press(event); view.column_release(event)
        view.window.after.assert_not_called()
        view.list.identify_region.return_value = 'separator'
        view.column_press(event); view.window.after.assert_not_called()
        view.column_release(event)
        view.window.after.assert_called_once_with(350, view.save_column_widths)
        self.assertFalse(self.path.exists())

    def test_repeated_drags_coalesce_and_do_not_save_during_drag(self):
        view = self.view(); event = SimpleNamespace(x=100, y=10)
        view.list.identify_region.return_value = 'separator'
        view.column_press(event); view.column_release(event)
        view.column_press(event)
        view.window.after_cancel.assert_called_once_with('save-timer')
        self.assertIsNone(view._column_save_id)
        view.column_release(SimpleNamespace(x=130, y=10)); view.save_column_widths()
        expected = dict(DEFAULT_LIBRARY_COLUMN_WIDTHS, rj=145, state=45)
        self.assertEqual(self.preferences.library_column_widths(), expected)
        self.assertEqual(view._saved_column_widths, expected)
        self.assertEqual(view.app.library.mock_calls, [])

    def test_unchanged_controller_does_not_access_disk(self):
        view = self.view()
        view._display_widths['title'] = 800  # Automatic expansion is not a user change.
        with patch.object(self.preferences, 'save_library_column_widths') as save:
            view.save_column_widths(); save.assert_not_called()

    def test_write_failure_keeps_adjustment_and_reports_without_dialog(self):
        view = self.view()
        view._column_widths = dict(WIDTHS)
        with patch.object(self.preferences, 'save_library_column_widths', side_effect=OSError('disk full')):
            view.save_column_widths()
        self.assertEqual(view._saved_column_widths, DEFAULT_LIBRARY_COLUMN_WIDTHS)
        self.assertIn('未保存', view.notice.set.call_args.args[0])
        view.app.append_log.assert_called_once()
        self.assertEqual(view.app.library.mock_calls, [])

    def test_shutdown_flushes_pending_save_and_cancels_timers(self):
        view = self.view(); view._column_save_id = 'pending-save'; view._pump_id = 'pump'; view.duration_reader = Mock()
        view._column_widths = dict(WIDTHS)
        view.shutdown()
        self.assertEqual(self.preferences.library_column_widths(), WIDTHS)
        view.window.after_cancel.assert_any_call('pending-save')
        view.window.after_cancel.assert_any_call('pump')
        view.duration_reader.close.assert_called_once()
        self.assertIsNone(view._column_save_id); self.assertTrue(view.closing)

    def test_shutdown_while_dragging_saves_last_width(self):
        view = self.view(); view._column_drag = True
        view._column_drag_start = ('rj', 100, dict(DEFAULT_LIBRARY_COLUMN_WIDTHS))
        view._display_widths.update(rj=145,state=45)
        view.shutdown()
        self.assertEqual(self.preferences.library_column_widths(), dict(DEFAULT_LIBRARY_COLUMN_WIDTHS, rj=145, state=45))

    def test_other_column_drag_records_the_visible_pair_not_window_resize(self):
        view = self.view(); event = SimpleNamespace(x=100, y=10)
        view._display_widths['title'] = 800; view.list.winfo_width.return_value = 1037
        view.list.identify_region.return_value = 'separator'
        view.column_press(event)
        view.column_release(SimpleNamespace(x=130,y=10)); view.save_column_widths()
        self.assertEqual(self.preferences.library_column_widths(), dict(DEFAULT_LIBRARY_COLUMN_WIDTHS, title=800,rj=145,state=45))

    def test_title_drag_preserves_total_and_never_locks_its_minimum(self):
        view = self.view(); event = SimpleNamespace(x=100, y=10)
        view.list.identify_region.return_value = 'separator'; view.list.identify_column.return_value = '#1'
        self.assertEqual(view.column_press(event), 'break')
        self.assertEqual(view.column_motion(SimpleNamespace(x=10000)), 'break')
        self.assertEqual(sum(view._display_widths.values()), 480)
        self.assertTrue(all(width >= 40 for width in view._display_widths.values()))
        view.column_release(SimpleNamespace(x=10000)); view.save_column_widths()
        self.assertEqual(self.preferences.library_column_widths(), {'title':360,'rj':40,'state':40,'favorite':40})
        self.assertFalse(any('minwidth' in call.kwargs for call in view.list.column.call_args_list))

    def test_separator_click_without_motion_does_not_save_expanded_title(self):
        view = self.view(); event = SimpleNamespace(x=100, y=10)
        view._display_widths['title'] = 800
        view.list.winfo_width.return_value = 1037
        view.list.identify_region.return_value = 'separator'; view.list.identify_column.return_value = '#1'
        view.column_press(event); view.column_release(event)
        with patch.object(self.preferences, 'save_library_column_widths') as save:
            view.save_column_widths(); save.assert_not_called()

    def test_next_drag_keeps_previous_unsaved_manual_width(self):
        view = self.view(); event = SimpleNamespace(x=100, y=10)
        view.list.identify_region.return_value = 'separator'
        view.column_press(event); view.column_release(SimpleNamespace(x=110,y=10))
        view.list.identify_column.return_value = '#3'
        view.column_press(event); view.column_release(SimpleNamespace(x=105,y=10))
        view.save_column_widths()
        self.assertEqual(self.preferences.library_column_widths(), dict(DEFAULT_LIBRARY_COLUMN_WIDTHS, rj=125, state=70, favorite=40))

    def test_saved_oversized_widths_fit_without_rewriting_preferences(self):
        view = self.view(); view._column_widths = {'title':483,'rj':180,'state':138,'favorite':77}
        before = dict(view._column_widths); view.list.winfo_width.return_value = 702
        with patch.object(view.preferences, 'save_library_column_widths') as save:
            view.layout_columns()
            self.assertEqual(sum(view._display_widths.values()), 700)
            self.assertEqual(view._display_widths['title'], 305)
            self.assertEqual(view._column_widths, before); save.assert_not_called()

    def test_layout_events_coalesce_and_shutdown_cancels_layout(self):
        view = self.view(); view.window.after_idle.return_value = 'layout'
        view.schedule_column_layout(); view.schedule_column_layout()
        view.window.after_idle.assert_called_once_with(view.layout_columns)
        view.shutdown(); view.window.after_cancel.assert_any_call('layout')

    def test_fit_large_and_small_viewports_keeps_four_columns_visible(self):
        for preferred in (DEFAULT_LIBRARY_COLUMN_WIDTHS, {'title':483,'rj':180,'state':138,'favorite':77},dict.fromkeys(DEFAULT_LIBRARY_COLUMN_WIDTHS,10000)):
            for available in (160,161,250,480,700,1500):
                before = dict(preferred); fitted = fit_library_columns(preferred,available)
                self.assertEqual(sum(fitted.values()),available)
                self.assertTrue(all(width>=40 for width in fitted.values()))
                self.assertEqual(preferred,before)

    def test_every_drag_direction_is_bounded_and_preserves_other_headers(self):
        for key in WIDTHS:
            for delta in (-10000,-1,0,1,10000):
                before = dict(WIDTHS); result = resize_library_columns(WIDTHS,key,delta)
                self.assertEqual(sum(result.values()),sum(WIDTHS.values()))
                self.assertTrue(all(width>=40 for width in result.values()))
                self.assertEqual(WIDTHS,before)

    def test_double_click_separator_is_resize_not_play(self):
        view = self.view(); view.play = Mock(); event = SimpleNamespace(x=100, y=10)
        view.list.identify_region.return_value = 'separator'
        view.column_double_click(event)
        self.assertTrue(view._column_drag); view.play.assert_not_called()
        view.list.identify_region.return_value = 'cell'
        view.column_double_click(event); view.play.assert_called_once()

    def test_closed_or_unrelated_release_does_not_schedule(self):
        view = self.view()
        view.column_release(None)
        view._column_drag = True; view.closing = True; view.column_release(None)
        view.window.after.assert_not_called()

    def test_refresh_only_changes_rows_not_columns_or_preferences(self):
        view = self.view(); view.busy = False; view.current = None; view.controls = Mock(); view.set_plot = Mock()
        for name in ('search','status_filter','tag_filter','tag_box','title','info','duration_reader'):
            setattr(view, name, Mock())
        view.list.get_children.return_value = ('old-row',)
        view.library = Mock(); view.library.tags.return_value = []; view.library.list.return_value = []
        view.run = lambda title, operation, callback: callback(operation())
        with patch.object(self.preferences, 'save_library_column_widths') as save:
            view.refresh()
            view.list.column.assert_not_called(); save.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
