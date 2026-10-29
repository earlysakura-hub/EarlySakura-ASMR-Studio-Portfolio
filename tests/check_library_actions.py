"""Actual Tk media buttons/threads on disposable works; no Explorer/AI/VLC."""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]; sys.path.insert(0, str(ROOT))


def main():
    base = ROOT / 'work/library_entry_checks'; base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as temporary:
        folder = Path(temporary); os.environ['EARLYSAKURA_DATA_DIR'] = str(folder / 'data')
        import tkinter as tk
        import studio, task_store, library_ui
        from media_library import LibraryStore
        from gui_preferences import GuiPreferences
        library = LibraryStore(); library.configure(folder / 'Library')
        preferences = GuiPreferences(folder / 'data/gui_preferences.json')
        preferences.save_library_column_widths({'title':1600,'rj':180,'state':138,'favorite':77})
        settings_before = preferences.path.read_bytes()
        works = []
        for index in range(2):
            audio = folder / f'日本語 & 中文 {index}.m4a'; audio.write_bytes(b'synthetic' + bytes([index]))
            task = task_store.open_task(audio); work_id = library.register(task)
            library.update(work_id, auto_state='completed', archive_state='archived' if index == 0 else 'pending',
                           refinement='refined' if index == 0 else 'pending')
            task.result('japanese.txt').write_text('[00:00:00,000 --> 00:00:01,000] テスト。\n', encoding='utf-8')
            draft = f'[00:00:00,000 --> 00:00:01,000]\nJP: テスト。\nZH_DRAFT: 粗翻{index}。\n\n'
            task.result('draft_bilingual.txt').write_text(draft, encoding='utf-8')
            if index == 0: task.result('final_bilingual.txt').write_text(draft.replace('ZH_DRAFT', 'ZH_FINAL').replace('粗翻', '精翻'), encoding='utf-8')
            works.append((task, work_id))
        before = library.list(); protected = {p: p.read_bytes() for task, _ in works for p in task.directory.glob('*.txt')}
        failures = []
        with patch.object(library_ui.os, 'startfile') as explorer, patch.object(library_ui.messagebox, 'askyesno', return_value=True) as ask, \
                patch.object(library_ui.messagebox, 'showinfo'), patch.object(library_ui.messagebox, 'showerror'):
            root = tk.Tk(); app = studio.Studio(root); view = app.library_window
            root.report_callback_exception = lambda kind, error, trace: failures.append(str(error))
            def settle():
                deadline = time.monotonic() + 8; quiet = 0
                while quiet < 3:
                    root.update(); assert not failures, failures
                    assert time.monotonic() < deadline, 'Media actions timed out'
                    quiet = quiet + 1 if not view.busy else 0; time.sleep(.02)
            try:
                settle(); assert 'srt' not in app.buttons
                view.current = None; view.controls()
                assert str(view.folder_button['state']) == str(view.srt_button['state']) == 'disabled'
                for geometry in ('1200x850', '1000x680'):
                    root.geometry(geometry); settle()
                    panes = view.list.master.master
                    assert view.list.master.winfo_reqwidth() == 480, 'Saved columns must not enlarge the pane request.'
                    for detail_width in (300,375,500):
                        panes.sashpos(0,panes.winfo_width()-detail_width); settle()
                        for button in view.work_buttons:
                            assert button.winfo_width() >= button.winfo_reqwidth(), ('truncated',button.cget('text'),detail_width)
                            assert button.winfo_height() >= button.winfo_reqheight(), button.cget('text')
                            assert button.winfo_rootx() >= button.master.winfo_rootx()
                            assert button.winfo_rootx()+button.winfo_width() <= button.master.winfo_rootx()+button.master.winfo_width(), button.cget('text')
                            assert button.winfo_rooty()+button.winfo_height() <= button.master.winfo_rooty()+button.master.winfo_height(), button.cget('text')
                            assert button.winfo_rootx()+button.winfo_width() <= root.winfo_rootx()+root.winfo_width(), button.cget('text')
                        assert view.plot.winfo_height() > 10
                        if detail_width == 300: assert int(view.srt_button.grid_info()['row']) > 0
                        if detail_width == 500: assert int(view.srt_button.grid_info()['row']) == 0
                        assert preferences.path.read_bytes() == settings_before
                for task, work_id in works:
                    view.list.selection_set(work_id); view.select(); settle()
                    selected = view.list.selection(); view.folder_button.invoke()
                    explorer.assert_called_with(str(Path(task.identity['path']).parent)); assert view.list.selection() == selected
                    app.task = works[1 if work_id == works[0][1] else 0][0]; old_task = app.task
                    view.srt_button.invoke(); settle()
                    assert task.result('output.srt').is_file(); assert app.task is old_task
                    assert view.list.selection() == selected and 'SRT 已生成' in view.notice.get()
                    assert not view.busy and not app.confirming and not app.library_busy_tasks
                old = works[1][0].result('output.srt').read_bytes()
                with patch.object(library_ui.messagebox, 'askyesno', side_effect=[True, False]):
                    view.srt_button.invoke(); settle()
                assert works[1][0].result('output.srt').read_bytes() == old
                assert library.list() == before
                for p, content in protected.items(): assert p.read_bytes() == content
                app.close()
                print('PASS native Tk: all buttons fully sized/visible at 300/375/500 detail widths and 1000x680/1200x850 windows; SRT wraps/unwraps; huge saved columns do not enlarge pane request; folder/SRT actions, final/draft, overwrite decline, no preference or source changes; no AI/playback/real Explorer.')
            finally:
                if not view.closing: view.shutdown()
                app.batch.close()
                try: root.destroy()
                except tk.TclError: pass


if __name__ == '__main__': main()
