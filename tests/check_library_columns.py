"""Real Tk separator drags and a fresh-process restart, isolated from user data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import wave

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))


def phase(folder, mode):
    os.environ['EARLYSAKURA_DATA_DIR'] = str(folder / 'data')
    import tkinter as tk
    import studio
    from gui_preferences import DEFAULT_LIBRARY_COLUMN_WIDTHS
    from library_ui import fit_library_columns
    root = tk.Tk(); app = studio.Studio(root); view = app.library_window
    errors = []
    root.report_callback_exception = lambda kind, error, trace: errors.append(str(error))

    def pump(seconds=.1):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update(); assert not errors, errors; time.sleep(.01)

    def settle():
        deadline = time.monotonic() + 12
        while view.busy or not view.events.empty():
            pump(.03); assert time.monotonic() < deadline, view.notice.get()
        pump(.2)
        if view.busy: settle()

    def widths(): return {key: view.list.column(key, 'width') for key in DEFAULT_LIBRARY_COLUMN_WIDTHS}

    def check_widths(expected):
        actual = widths()
        viewport = view.list.winfo_width()
        assert actual == fit_library_columns(expected, viewport-2), (actual, expected, viewport)
        assert sum(actual.values()) == viewport-2
        assert tree_visible(), (actual, view.list.xview())
        for button in view.work_buttons:
            assert button.winfo_width() >= button.winfo_reqwidth(), ('clipped detail button',button.cget('text'))
            assert button.winfo_rootx()+button.winfo_width() <= button.master.winfo_rootx()+button.master.winfo_width(), button.cget('text')

    def tree_visible():
        tree = view.list; children = tree.get_children()
        left = 1
        for index, key in enumerate(DEFAULT_LIBRARY_COLUMN_WIDTHS, start=1):
            width = tree.column(key,'width'); x = left
            if children: x, y, width, height = tree.bbox(children[0], key)
            if width < 40 or x < 0 or x+width > tree.winfo_width()-1: return False
            if tree.identify_column(left+width//2) != f'#{index}': return False
            left += width
        return tree.xview() == (0.0, 1.0)

    def drag(key, delta):
        tree = view.list; tree.xview_moveto(0); pump(.01)
        index = list(DEFAULT_LIBRARY_COLUMN_WIDTHS).index(key) + 1
        candidates = [(x, y) for y in range(5, 25) for x in range(2, tree.winfo_width()-2)
                      if tree.identify_region(x, y) == 'separator' and tree.identify_column(x) == f'#{index}']
        assert candidates, (key, tree.winfo_width(), widths())
        x, y = candidates[len(candidates)//2]
        before = widths()[key]
        tree.event_generate('<ButtonPress-1>', x=x, y=y)
        steps = min(abs(delta), 20)
        for step in range(1, steps+1):
            tree.event_generate('<B1-Motion>', x=x+round(delta*step/steps), y=y, state=0x100)
            root.update_idletasks(); assert tree_visible(), ('hidden during drag',key,widths())
        tree.event_generate('<ButtonRelease-1>', x=x+delta, y=y)
        root.update_idletasks()
        assert (widths()[key]-before)*delta > 0, (key, before, widths(), view._column_widths)
        assert app.player_job is None and app.pipeline_job is None

    try:
        settle()
        assert tuple(view.list.cget('columns')) == ('title','rj','state','favorite')
        assert app.player_job is None and app.pipeline_job is None
        assert view.plot_font_size == 17
        if mode == 'write':
            seed = {'title':483,'rj':180,'state':138,'favorite':77}
            assert view._column_widths == seed
            check_widths(seed)
            # Make all four separators visible; this changes only the test's pane.
            view.list.master.master.sashpos(0, 700); pump(.05)
            with patch.object(view.preferences, '_write', wraps=view.preferences._write) as write:
                for key, delta in zip(DEFAULT_LIBRARY_COLUMN_WIDTHS, (30, 20, 15, 10)):
                    drag(key, delta)
                assert write.call_count == 0, 'Saving must wait until the drag sequence settles.'
                pump(.45)
                assert write.call_count == 1, write.call_count
                expected = dict(view._column_widths)
                assert view.preferences.library_column_widths() == expected
                check_widths(expected)
                for _ in range(2): view.refresh(); settle()
                check_widths(expected); assert write.call_count == 1
                view.search.set('does not match'); view.refresh(); settle()
                assert len(view.rows) == 0; check_widths(expected)
                view.search.set(''); view.refresh(); settle()
                assert len(view.rows) == 2; check_widths(expected)
                view.status_filter.set('已精修'); view.refresh(); settle()
                assert len(view.rows) == 0; check_widths(expected)
                view.status_filter.set('全部'); view.refresh(); settle()
                view.scan_inbox(); settle()  # Empty isolated inbox, no archive.
                assert len(view.rows) == 2; check_widths(expected)
                for page in app.pages: app.select_page(page); pump(.03)
                app.select_page('library'); settle()
                for geometry in ('1000x680', '1200x850'):
                    root.geometry(geometry); pump(.1); check_widths(expected)
                assert write.call_count == 1, 'Refresh/filter/scan/tab/window changes must not save widths.'
                # Widening or narrowing the pane must keep every column visible.
                root.geometry('1600x850'); pump(.1)
                view.list.master.master.sashpos(0, 950); pump(.05)
                check_widths(expected)
                assert view.preferences.library_column_widths() == expected and write.call_count == 1
                view.list.master.master.sashpos(0, 220); pump(.05)
                check_widths(expected)
                assert view.preferences.library_column_widths() == expected and write.call_count == 1
                view.list.master.master.sashpos(0, 700); pump(.03)
                # Reproduce the reported failure with an extreme title drag.
                drag('title', 10000); pump(.45)
                expected = dict(view._column_widths)
                check_widths(expected)
                assert widths()['rj'] >= 40 and widths()['state'] >= 40 and widths()['favorite'] >= 40
                assert view.preferences.library_column_widths() == expected and write.call_count == 2
                for geometry in ('1000x680','1200x850'):
                    root.geometry(geometry); pump(.1); check_widths(expected)
                drag('title', -30)
                expected = dict(view._column_widths)
                assert write.call_count == 2
                app.destroy_root()  # Pending drag save must flush without a wait.
                assert write.call_count == 3
            (folder / 'expected-widths.json').write_text(json.dumps(expected), encoding='utf-8')
            print('PASS first process: oversized legacy preferences fitted; all four columns visible throughout extreme drags, narrow/wide panes and window resizes; refresh/search/filter/scan/tabs stable; coalesced save and shutdown flush.')
        else:
            expected = json.loads((folder / 'expected-widths.json').read_text(encoding='utf-8'))
            assert view._column_widths == expected
            check_widths(expected)
            for geometry in ('1000x680', '1200x850'):
                root.geometry(geometry); pump(.1); view.refresh(); settle()
                check_widths(expected)
            assert app.player_job is None and app.pipeline_job is None
            app.destroy_root()
            print('PASS restarted process: saved preferences restored and fitted to each viewport with all four columns visible; original plot font preserved; no player or model started.')
    finally:
        if not view.closing: view.shutdown()
        view.duration_reader.thread.join(3) if view.duration_reader.started else None
        if app.batch is not None: app.batch.close()
        try: root.destroy()
        except tk.TclError: pass


def main():
    if len(sys.argv) == 3:
        phase(Path(sys.argv[1]), sys.argv[2]); return
    base = ROOT / 'work/library_column_checks'; base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as temporary:
        folder = Path(temporary); os.environ['EARLYSAKURA_DATA_DIR'] = str(folder / 'data')
        import task_store
        from media_library import LibraryStore
        from gui_preferences import GuiPreferences
        library = LibraryStore(); library.configure(folder / 'Library')
        for number in (100001,100002):
            audio = folder / f'RJ{number}.wav'
            with wave.open(str(audio), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000); output.writeframes(b'\0\0'*8000)
            task = task_store.open_task(audio); library.register(task)
        expected_library = library.list()
        protected = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file() and p.suffix in ('.wav','.txt')}
        preferences = GuiPreferences(folder / 'data/gui_preferences.json')
        preferences.save_plot_font_size(17)
        preferences.save_library_column_widths({'title':483,'rj':180,'state':138,'favorite':77})
        for mode in ('write', 'read'):
            result = subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).absolute()),str(folder),mode],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', timeout=60,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            print(result.stdout, end=''); print(result.stderr, end='')
            assert result.returncode == 0, f'Tk {mode} failed: {result.returncode}'
        assert library.list() == expected_library
        assert preferences.plot_font_size() == 17
        for path, content in protected.items(): assert path.read_bytes() == content
        print('PASS isolated native Tk restart check: metadata/audio unchanged; no real works or user preferences touched.')


if __name__ == '__main__': main()
