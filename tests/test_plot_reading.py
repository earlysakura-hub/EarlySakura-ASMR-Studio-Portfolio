"""Isolated preference and display-controller tests; no GUI or formal data."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
from gui_preferences import GuiPreferences, PreferencesError
from library_ui import LibraryWindow


class PlotReadingTest(unittest.TestCase):
    def setUp(self):
        folder = ROOT / 'work/plot_reading_checks'
        folder.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'gui_preferences.json'
        self.preferences = GuiPreferences(self.path)

    def view(self):
        view = LibraryWindow.__new__(LibraryWindow)
        view.preferences = self.preferences
        view.plot_font_size = self.preferences.plot_font_size()
        for name in ('plot_font', 'plot_font_label', 'plot_font_minus', 'plot_font_plus', 'plot', 'notice'):
            setattr(view, name, Mock())
        view.app = SimpleNamespace(append_log=Mock(), library=Mock())
        return view

    def test_missing_file_defaults_without_creating_it(self):
        self.assertEqual(self.preferences.plot_font_size(), 14)
        self.assertFalse(self.path.exists())

    def test_missing_field_keeps_other_settings(self):
        self.path.write_text('{"other_setting": "日本語"}', encoding='utf-8')
        self.assertEqual(self.preferences.plot_font_size(), 14)
        self.preferences.save_plot_font_size(17)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {'other_setting': '日本語', 'plot_font_size': 17})

    def test_invalid_size_values_fall_back(self):
        for value in (None, True, False, '17', 17.0, -1, 0, 9, 25, [], {}):
            with self.subTest(value=value):
                self.path.write_text(json.dumps({'plot_font_size': value}), encoding='utf-8')
                self.assertEqual(self.preferences.plot_font_size(), 14)

    def test_valid_boundaries_and_restart(self):
        for size in (10, 14, 17, 24):
            self.preferences.save_plot_font_size(size)
            self.assertEqual(GuiPreferences(self.path).plot_font_size(), size)

    def test_corrupt_or_non_object_config_stays_unchanged(self):
        for content in (b'{broken', b'[]', b'null', b'42', b'\xff\xfe'):
            self.path.write_bytes(content)
            self.assertEqual(self.preferences.plot_font_size(), 14)
            with self.assertRaises(PreferencesError): self.preferences.save_plot_font_size(17)
            self.assertEqual(self.path.read_bytes(), content)

    def test_bom_config_is_supported(self):
        self.path.write_text('{"plot_font_size": 18}', encoding='utf-8-sig')
        self.assertEqual(self.preferences.plot_font_size(), 18)

    def test_fresh_read_preserves_new_and_unknown_fields(self):
        self.preferences.save_plot_font_size(14)
        self.path.write_text('{"plot_font_size": 14, "future": {"theme": "test"}}', encoding='utf-8')
        self.preferences.save_plot_font_size(17)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['future'], {'theme': 'test'})

    def test_atomic_save_failure_preserves_original(self):
        self.preferences.save_plot_font_size(17)
        before = self.path.read_bytes()
        with patch('gui_preferences.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.preferences.save_plot_font_size(18)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('gui_preferences.json.tmp-*')), [])

    def test_invalid_save_rejected(self):
        for value in (9, 25, True, 14.0, '14'):
            with self.assertRaises(PreferencesError): self.preferences.save_plot_font_size(value)
        self.assertFalse(self.path.exists())

    def test_plus_minus_reset_updates_private_font_and_saves(self):
        view = self.view()
        view.change_plot_font(1)
        self.assertEqual(view.plot_font_size, 15)
        view.plot_font.configure.assert_called_with(size=15)
        view.change_plot_font(-1)
        self.assertEqual(view.plot_font_size, 14)
        view.set_plot_font_size(17); view.reset_plot_font()
        self.assertEqual(GuiPreferences(self.path).plot_font_size(), 14)
        view.plot.delete.assert_not_called(); view.plot.insert.assert_not_called()
        self.assertEqual(view.app.library.mock_calls, [])

    def test_limits_disable_buttons(self):
        view = self.view()
        view.set_plot_font_size(24); view.change_plot_font(1)
        self.assertEqual(view.plot_font_size, 24)
        view.plot_font_plus.configure.assert_called_with(state='disabled')
        view.set_plot_font_size(10); view.change_plot_font(-1)
        self.assertEqual(view.plot_font_size, 10)
        view.plot_font_minus.configure.assert_called_with(state='disabled')

    def test_write_error_does_not_block_font_or_modify_plot(self):
        view = self.view()
        with patch.object(self.preferences, 'save_plot_font_size', side_effect=OSError('read only')):
            view.change_plot_font(1)
        self.assertEqual(view.plot_font_size, 15)
        view.plot_font.configure.assert_called_with(size=15)
        self.assertIn('未保存', view.notice.set.call_args.args[0])
        view.app.append_log.assert_called_once()
        view.plot.delete.assert_not_called(); view.plot.insert.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
