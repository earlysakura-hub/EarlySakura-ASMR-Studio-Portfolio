"""Actual Tk selected-duration checks on isolated short WAVs, no playback."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import wave

ROOT = Path(__file__).absolute().parents[1]; sys.path.insert(0, str(ROOT))


def wav(path, seconds):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000); stream.writeframes(b'\0\0' * 8000 * seconds)


def main():
    base = ROOT / 'work/library_duration_checks'; base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as temporary:
        folder = Path(temporary); os.environ['EARLYSAKURA_DATA_DIR'] = str(folder / 'data')
        import tkinter as tk
        import studio, task_store, library_ui
        from audio_duration import DurationReader, DurationCache, probe_duration
        from media_library import LibraryStore
        library = LibraryStore(); library.configure(folder / 'Library')
        works = []
        for index, seconds in enumerate((2, 5, 1)):
            audio = folder / f'中文 日本語 {index}.wav'; wav(audio, seconds)
            task = task_store.open_task(audio); work_id = library.register(task)
            (task.directory / 'full_plot_summary.txt').write_text('完整剧情・日本語\n' * 40, encoding='utf-8')
            works.append((audio, work_id))
        expected = library.list(); before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p, _ in works[:2]}
        entered = threading.Event(); release = threading.Event(); calls = []; blocked = [True]
        def counted(path):
            calls.append(path)
            if path == str(works[0][0]) and blocked[0]: entered.set(); release.wait(4)
            return probe_duration(path)
        def reader(results): return DurationReader(results, DurationCache(counted))
        failures = []; ticks = [0]
        with patch.object(library_ui, 'DurationReader', side_effect=reader), patch.object(library_ui.messagebox, 'showerror') as errors:
            root = tk.Tk(); app = studio.Studio(root); view = app.library_window
            root.report_callback_exception = lambda kind, error, trace: failures.append(str(error))
            timer = [None]
            def tick(): ticks[0] += 1; timer[0] = root.after(20, tick)
            tick()
            def wait(condition, timeout=8):
                end = time.monotonic() + timeout
                while not condition():
                    root.update(); assert not failures, failures; assert time.monotonic() < end, 'Tk duration timeout'
                    time.sleep(.01)
                root.update()
            def select(index): view.list.selection_set(works[index][1]); view.select()
            try:
                wait(lambda: not view.busy and len(view.rows) == 3)
                assert tuple(view.list.cget('columns')) == ('title', 'rj', 'state', 'favorite')
                select(1); wait(lambda: '音频时长：00:00:05' in view.info.get() and not view.busy)
                assert view.info.get() == '音频时长：00:00:05\n标签：无'
                compact_info = view.info.get(); compact_height = view.plot.winfo_height()
                view.info.set('文件：测试.wav\n' + compact_info + '\n状态：已精修\n音频：旧版路径\n作品目录：旧版目录\n字幕：存在')
                root.update()
                assert compact_height > view.plot.winfo_height(), 'Compact details should give the plot more height.'
                view.info.set(compact_info); root.update()
                self_reader = view.duration_reader
                select(0); wait(entered.is_set); start_ticks = ticks[0]
                select(1)
                wait(lambda: ticks[0] >= start_ticks + 5)
                assert view.current['work_id'] == works[1][1] and '--:--:--' in view.info.get()
                release.set(); wait(lambda: '音频时长：00:00:05' in view.info.get())
                assert app.player_job is None and app.pipeline_job is None and app.batch.entries == []
                for key in app.pages: app.select_page(key); root.update()
                app.select_page('library'); wait(lambda: not view.busy)
                assert '音频时长：00:00:05' in view.info.get() and view.duration_reader is self_reader
                for geometry in ('1200x850', '1000x680'):
                    root.geometry(geometry); root.update()
                    assert view.plot.winfo_height() > 40
                    for button in view.work_buttons:
                        assert button.winfo_rootx() + button.winfo_width() <= button.master.winfo_rootx() + button.master.winfo_width()
                select(0); wait(lambda: '音频时长：00:00:02' in view.info.get())
                select(1); wait(lambda: '音频时长：00:00:05' in view.info.get())
                assert calls.count(str(works[0][0])) == 1 and calls.count(str(works[1][0])) == 1
                for p, value in before.items(): assert (p.read_bytes(), p.stat().st_mtime_ns) == value
                blocked[0] = False; wav(works[0][0], 3)
                select(0); wait(lambda: '音频时长：00:00:03' in view.info.get())
                assert calls.count(str(works[0][0])) == 2
                works[2][0].unlink()  # This test's own disposable file only.
                select(2); wait(lambda: '--:--:--' in view.info.get() and not view.busy)
                assert '音频时长：00:00:00' not in view.info.get(); errors.assert_not_called()
                view.refresh(); wait(lambda: len(view.rows) == 2 and not view.busy)
                assert works[2][1] not in view.rows and view.current['work_id'] != works[2][1]
                wav(works[2][0], 1)
                view.refresh(); wait(lambda: len(view.rows) == 3 and not view.busy)
                assert works[2][1] in view.rows
                assert library.list() == expected
                root.after_cancel(timer[0]); timer[0] = None
                app.close(); view.duration_reader.thread.join(3)
                assert not view.duration_reader.thread.is_alive()
                print('PASS native Tk: deleted audio hidden on refresh, old selection cleared, same work ID restored with file, database unchanged; compact details/duration/cache/GUI ticks/tabs/1200x850+1000x680; no AI/VLC/user-data writes.')
            finally:
                release.set()
                if not view.closing: view.shutdown()
                view.duration_reader.thread.join(3); app.batch.close()
                try:
                    if timer[0]: root.after_cancel(timer[0])
                    root.destroy()
                except tk.TclError: pass


if __name__ == '__main__': main()
