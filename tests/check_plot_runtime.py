"""Real Tk reading controls with isolated works/settings; no VLC or AI."""
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    base = ROOT / 'work/plot_reading_checks'
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as temporary:
        folder = Path(temporary)
        os.environ['EARLYSAKURA_DATA_DIR'] = str(folder / 'data')
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter.scrolledtext import ScrolledText
        import studio
        import task_store
        from media_library import LibraryStore
        library = LibraryStore(); library.configure(folder / 'Library')
        text = '中文剧情，日本語の紹介。English 123，标点！\n\n' + ('中文と日本語・English 123。' * 400) + '\n结尾。\n'
        works = []
        for index in range(7):
            audio = folder / f'RJ{100000 + index}.wav'; audio.write_bytes(b'synthetic only')
            task = task_store.open_task(audio); work_id = library.register(task)
            library.update(work_id, title=f'测试作品 {index}', auto_state='completed', refinement='refined', favorite=index == 0)
            tag = library.save_tag('隔离测试'); library.set_tags(work_id, [tag])
            if index == 0: (task.directory / 'full_plot_summary.txt').write_text(text, encoding='utf-8')
            works.append((task, work_id))
        expected = library.list()
        protected = {p: p.read_bytes() for task, _ in works for p in task.directory.iterdir() if p.is_file()}
        protected.update({Path(task.identity['path']): Path(task.identity['path']).read_bytes() for task, _ in works})
        failures = []

        def start():
            root = tk.Tk(); app = studio.Studio(root)
            root.report_callback_exception = lambda kind, error, trace: failures.append(str(error))
            return root, app, app.library_window

        def settle(root, view):
            deadline = time.monotonic() + 30; quiet = 0
            while quiet < 3:
                root.update()
                assert not failures, failures
                assert time.monotonic() < deadline, f'Reading page timeout: busy={view.busy}, events={view.events.qsize()}, pump={view._pump_id}, notice={view.notice.get()}'
                quiet = quiet + 1 if not view.busy else 0
                time.sleep(.02)

        def cleanup(root, app, view):
            view.shutdown(); app.batch.close()
            try: root.destroy()
            except tk.TclError: pass

        root, app, view = start()
        try:
            settle(root, view)
            # Same unconfigured widget as before this change; inspect its real font.
            original = ScrolledText(root)
            old_font = tkfont.Font(root=root, font=original.cget('font')).actual()
            original.destroy()
            print('Original default Text font:', old_font, 'Tk scaling:', root.tk.call('tk', 'scaling'))
            assert view.plot_font.cget('size') == 14
            default_font = tkfont.nametofont('TkTextFont', root=root).actual()
            view.list.selection_set(works[0][1]); view.select(); settle(root, view)
            assert view.plot.get('1.0', 'end-1c') == text
            view.plot_font_plus.invoke(); assert view.plot_font.cget('size') == 15
            view.plot_font_minus.invoke(); assert view.plot_font.cget('size') == 14
            view.set_plot_font_size(17)
            assert tkfont.nametofont('TkTextFont', root=root).actual() == default_font
            for _, work_id in works:
                print('Checking work switch:', work_id, flush=True)
                view.list.selection_set(work_id); view.select(); settle(root, view)
                assert view.plot_font.cget('size') == 17
            assert view.plot.get('1.0', 'end-1c') == '暂无剧情介绍'
            for key in app.pages: app.select_page(key); root.update()
            app.select_page('library'); settle(root, view)
            assert app.library_window is view and view.plot_font.cget('size') == 17
            view.list.selection_set(works[0][1]); view.select(); settle(root, view)
            for geometry in ('1200x850', '1000x680'):
                root.geometry(geometry); root.update()
                for size in (10, 14, 24):
                    print('Checking layout:', geometry, size, flush=True)
                    view.set_plot_font_size(size); root.update()
                    assert view.plot.get('1.0', 'end-1c') == text
                    assert view.plot.cget('wrap') == 'word' and view.plot.winfo_height() > 40
                    view.plot.yview_moveto(0); root.update()
                    assert view.plot.compare('3.0 display lineend', '<', '3.end'), 'Long paragraph must wrap'
                    assert view.plot.xview() == (0.0, 1.0)
                    for button in (view.plot_font_plus, view.plot_font_minus):
                        header = button.master.master
                        assert button.winfo_rootx() + button.winfo_width() <= header.winfo_rootx() + header.winfo_width()
                    view.plot.yview_moveto(1); root.update()
                    assert view.plot.yview()[0] > 0, 'Vertical scroll must move'
            view.set_plot_font_size(17)
            assert library.list() == expected
            for p, content in protected.items(): assert p.read_bytes() == content, str(p)
            app.close()
        finally:
            cleanup(root, app, view)

        root, app, view = start()
        try:
            settle(root, view); assert view.plot_font.cget('size') == 17
            view.reset_plot_font(); assert view.plot_font.cget('size') == 14
            app.close()
        finally:
            cleanup(root, app, view)
        root, app, view = start()
        try:
            settle(root, view); assert view.plot_font.cget('size') == 14
            assert library.list() == expected
            for p, content in protected.items(): assert p.read_bytes() == content, str(p)
            app.close()
        finally:
            cleanup(root, app, view)
        assert not failures, failures
        print('PASS native Tk: mixed text/control/bounds/wrap/scroll/resize/work+tab switching/restart+reset; seven isolated works unchanged; no AI/VLC.')


if __name__ == '__main__': main()
