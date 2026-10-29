"""Actual Tk and VLC on generated silent WAVs; AI stages are fixtures.

All media, databases, progress and queue entries stay in this project's work/.
This does not certify audible output or human interaction quality.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
import wave
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))


def player_fixture(audio):
    import tkinter as tk
    import task_store
    script = ROOT / 'floating_subtitle_player.py'
    if sys.stdin.readline() != 'START\n': return
    if not Path(audio).absolute().is_relative_to(ROOT / 'work/library_runtime_checks'):
        raise RuntimeError('Only isolated library test media permitted')
    original = tk.Tk; count = [0]; checked = [False]
    namespace = {'__name__': '__main__', '__file__': str(script)}
    def make_root(*args, **kwargs):
        root = original(*args, **kwargs); count[0] += 1
        if count[0] == 2:
            deadline = time.monotonic() + 8
            last_debug = [0]
            volume_requested = [False]
            def audit():
                try:
                    assert time.monotonic() < deadline, 'Player never reached ready'
                    resume = namespace.get('resume')
                    if resume and time.monotonic()-last_debug[0]>1:
                        last_debug[0]=time.monotonic()
                        print('LIBRARY_PLAYER_DEBUG',resume.ready,resume.failed,namespace['player'].get_state(),namespace['player'].get_time(),repr(namespace['subtitle_label'].cget('text')),flush=True)
                    if resume and resume.ready:
                        expected = '你好。' if namespace['subtitles'] else '暂无字幕'
                        actual = namespace['subtitle_label'].cget('text')
                        if actual != expected:
                            root.after(50, audit)
                            return  # Wait for the existing subtitle refresh tick.
                        if not volume_requested[0]:
                            volume_requested[0]=True
                            namespace['volume_scale'].set(42)
                            root.after(50,audit); return
                        assert namespace['player'].audio_get_volume() == 42, ('volume',namespace['player'].audio_get_volume())
                        resume.save_now()
                        assert namespace['player'].get_time() >= 0, ('position',namespace['player'].get_time())
                        checked[0] = True
                        print('LIBRARY_PLAYER_READY ' + expected, flush=True)
                    else: root.after(50, audit)
                except Exception as error:
                    print('LIBRARY_PLAYER_AUDIT_FAILED ' + repr(error), flush=True)
                    namespace['close_player']()
            root.after(500, audit)
        return root
    sys.argv = [str(script), str(audio), '--studio-control']
    with patch('tkinter.Tk', side_effect=make_root), patch('playback_resume.choose_resume', side_effect=lambda root, saved, format_time: saved['position_ms'] if saved else 0):
        exec(compile(script.read_text(encoding='utf-8-sig'), str(script), 'exec'), namespace)
    assert checked[0], 'No actual player verification'


def main():
    import tkinter as tk
    import studio
    import task_store as tasks
    from library_files import archive, import_refinement
    from library_ui import LibraryWindow
    from media_library import LibraryStore
    base = ROOT / 'work/library_runtime_checks'; base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as temporary:
        directory = Path(temporary)
        os.environ['EARLYSAKURA_DATA_DIR'] = str(directory / 'data')
        library = LibraryStore(); library.configure(directory / 'Library')
        works = []
        jp = '[00:00:00,000 --> 00:00:30,000] こんにちは。\n'
        draft = '[00:00:00,000 --> 00:00:30,000]\nJP: こんにちは。\nZH_DRAFT: 你好。\n\n'
        for name in ('A', 'B', 'C'):
            audio = library.root() / '待处理音频' / (name + '.wav')
            with wave.open(str(audio), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
                stream.writeframes(b'\0\0' * 8000 * 40)
            task = tasks.open_task(audio); work_id = library.register(task)
            if name != 'B':
                task.result('japanese.txt').write_text(jp, encoding='utf-8')
                task.result('draft_bilingual.txt').write_text(draft, encoding='utf-8')
                task = archive(task, library)
            works.append((task, work_id))
        final = directory / 'final_bilingual.txt'; final.write_text(draft.replace('ZH_DRAFT', 'ZH_FINAL'), encoding='utf-8')
        plot = directory / 'full_plot_summary.txt'; content = '完整剧情\n日本語と中文・♥\n' * 400
        plot.write_text(content, encoding='utf-8')
        import_refinement(works[0][1], final, plot, confirmed=True, library=library)
        def command(mode, audio, gated=False):
            if mode == 'player': return [sys.executable, '-B', '-u', '-X', 'utf8', str(Path(__file__)), '--player', str(audio)]
            return [sys.executable, '-B', '-u', '-X', 'utf8', str(ROOT / 'tests/parallel_worker_fixture.py'),
                    mode, str(audio), '--database', str(library.history), '--behavior', 'normal']
        with patch.object(studio, 'worker_command', side_effect=command), patch.object(studio.messagebox, 'askyesno', return_value=True), \
                patch.object(studio.messagebox, 'showerror', side_effect=lambda title, text, **kwargs: print('TEST_GUI_ERROR', title, text, flush=True)):
            from playback_history import HistoryStore
            root = tk.Tk(); app = studio.Studio(root)
            app.history = HistoryStore(library.history)  # Parent imports preceded isolated env setup.
            view = app.library_window
            failures = []; stage = [0]; deadline = time.monotonic() + 25; ticks = [0]
            def select(work_id):
                view.list.selection_set(work_id); view.select()
            def finish():
                if app.pipeline_job: app.cancel()
                if app.player_job: app.stop_player()
                if app.pipeline_job or app.player_job or view.busy: root.after(50, finish); return
                view.close(); app.close()
            def step():
                try:
                    ticks[0] += 1; assert time.monotonic() < deadline, 'Native library test timed out'
                    if stage[0] == 0 and not view.busy and len(view.rows) == 3:
                        select(works[0][1]); stage[0] = 1
                    elif stage[0] == 1 and not view.busy:
                        assert view.plot.get('1.0', 'end-1c') == content
                        view.title.set('测试标题 日本語 ❤'); view.save_title(); stage[0] = 2
                    elif stage[0] == 2 and not view.busy:
                        assert view.current['display_title'] == '测试标题 日本語 ❤'
                        view.favorite(); stage[0] = 3
                    elif stage[0] == 3 and not view.busy:
                        assert view.current['favorite']; view.play(); stage[0] = 4
                    elif stage[0] == 4 and 'LIBRARY_PLAYER_READY 你好。' in app.log.get('1.0', 'end'):
                        app.start_job('pipeline', works[1][0]); stage[0] = 5
                    elif stage[0] == 5 and app.pipeline_job is None:
                        archived = library.get(works[1][1]); assert archived['archive_state'] == 'archived'
                        assert archived['auto_state'] == 'completed'
                        assert app.player_job and 'A.wav' in app.player_status.get()
                        app.stop_player(); stage[0] = 6
                    elif stage[0] == 6 and app.player_job is None:
                        view.refresh(); stage[0] = 7
                    elif stage[0] == 7 and not view.busy:
                        select(works[2][1]); stage[0] = 8
                    elif stage[0] == 8 and not view.busy:
                        assert view.plot.get('1.0','end-1c') == '暂无剧情介绍'
                        view.play(); stage[0] = 9
                    elif stage[0] == 9 and '正在播放' in app.player_status.get():
                        root.after(1000, app.stop_player); stage[0] = 10
                    elif stage[0] == 10 and app.player_job is None:
                        log = app.log.get('1.0', 'end')
                        assert 'LIBRARY_PLAYER_READY 你好。' in log and 'LIBRARY_PLAYER_READY 暂无字幕' in log, log[-2500:]
                        assert 'AUDIT_FAILED' not in log
                        assert len(app.history.list_entries()) == 2
                        assert app.history.resume_info(works[0][0].identity['task_id'])
                        assert ticks[0] > 20
                        print('PASS actual Tk library: list/title/favorite/full plot; actual VLC: with/without SRT, volume/pause/history; concurrent fixture pipeline auto-archive; clean close')
                        finish(); return
                except Exception as error:
                    import traceback
                    print('TEST_STAGE', stage[0], traceback.format_exc(), app.log.get('1.0', 'end'), flush=True)
                    failures.append(repr(error)); finish(); return
                root.after(50, step)
            root.after(100, step); root.mainloop()
            assert not failures, failures


if __name__ == '__main__':
    if '--player' in sys.argv: player_fixture(sys.argv[sys.argv.index('--player') + 1])
    else: main()
