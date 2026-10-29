"""Actual Tk/floating VLC tests on isolated silence; never formal works/models."""
import os
from pathlib import Path
import sys
import tempfile
import time
import wave
from unittest.mock import patch

ROOT=Path(__file__).absolute().parents[1]; sys.path.insert(0,str(ROOT))


def main():
    base=ROOT/'work/library_runtime_checks'; base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as temporary:
        directory=Path(temporary); os.environ['EARLYSAKURA_DATA_DIR']=str(directory/'data')
        import studio,task_store
        from media_library import LibraryStore
        from playback_history import HistoryStore
        import tkinter as tk
        from tkinter import ttk
        library=LibraryStore(); library.configure(directory/'Library')
        works=[]; title='标题 日本語 ❤'; plot='完整剧情\n中文と日本語。\n'*400
        for index in range(7):
            audio=directory/('A.wav' if index==0 else f'RJ{100000+index}.wav')
            with wave.open(str(audio),'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
                stream.writeframes(b'\0\0'*8000*40)
            task=task_store.open_task(audio); work_id=library.register(task)
            task.result('japanese.txt').write_text('[00:00:00,000 --> 00:00:30,000] こんにちは。\n',encoding='utf-8')
            task.result('draft_bilingual.txt').write_text('[00:00:00,000 --> 00:00:30,000]\nJP: こんにちは。\nZH_DRAFT: 你好。\n\n',encoding='utf-8')
            task.result('final_bilingual.txt').write_text('[00:00:00,000 --> 00:00:30,000]\nJP: こんにちは。\nZH_FINAL: 你好。\n\n',encoding='utf-8')
            task.result('output.srt').write_text('1\n00:00:00,000 --> 00:00:30,000\n你好。\n\n',encoding='utf-8')
            library.update(work_id,auto_state='completed',archive_state='archived',refinement='refined',title=title if index==0 else '')
            tag=library.save_tag('自定义・耳语'); library.set_tags(work_id,[tag])
            works.append((task,work_id))
        library.update(works[0][1],favorite=1)
        (works[0][0].directory/'full_plot_summary.txt').write_text(plot,encoding='utf-8')
        b_audio=directory/'B.wav'; b_audio.write_bytes(b'synthetic pipeline input'); b=task_store.open_task(b_audio)
        def command(mode,audio,gated=False):
            if mode=='player':return [sys.executable,'-B','-u','-X','utf8',str(ROOT/'tests/check_library_runtime.py'),'--player',str(audio)]
            return [sys.executable,'-B','-u','-X','utf8',str(ROOT/'tests/parallel_worker_fixture.py'),mode,str(audio),
                    '--database',str(library.history),'--behavior','wait']
        with patch.object(studio,'worker_command',side_effect=command),patch.object(studio.messagebox,'askyesno',return_value=True), \
                patch.object(studio.messagebox,'showerror'),patch.object(studio.messagebox,'showwarning'),patch.object(studio.messagebox,'showinfo'):
            root=tk.Tk(); app=studio.Studio(root); view=app.library_window
            failures=[]; stage=[0]; deadline=time.monotonic()+38; initial_pages=tuple(app.notebook.tabs()); ticks=[0]
            before_resume=[None]; before_mtime={str(p):p.read_bytes() for task,_ in works for p in task.directory.glob('*.txt')}
            def children(parent):
                for child in parent.winfo_children():
                    yield child; yield from children(child)
            def no_formal_popup():
                assert not [widget for widget in root.winfo_children() if isinstance(widget,tk.Toplevel)]
                assert root.title()=='EarlySakura ASMR Studio'
                assert tuple(app.notebook.tabs())==initial_pages and len(initial_pages)==5
            def finish():
                if app.pipeline_job and not app.cancelling: app.cancel()
                if app.player_job and not app.player_job.stop_requested: app.stop_player()
                if app.pipeline_job or app.player_job or view.busy: root.after(50,finish); return
                app.close()
            root.report_callback_exception=lambda kind,error,trace:(failures.append(str(error)),finish())
            def step():
                try:
                    ticks[0]+=1; assert time.monotonic()<deadline,f'tab GUI timeout at stage {stage[0]}'
                    no_formal_popup()
                    if stage[0]==0 and not view.busy and len(view.rows)==7:
                        view.list.selection_set(works[0][1]); view.select(); stage[0]=1
                    elif stage[0]==1 and not view.busy:
                        assert view.plot.get('1.0','end-1c')==plot
                        assert view.title.get()==title
                        root.geometry('1000x680'); root.update_idletasks()
                        view.title.set('尚未保存的标题'); view.search.set('A.wav'); view.status_filter.set('已精修')
                        before_count=len(root.tk.call('after','info'))
                        for _ in range(15):
                            for key in app.pages: app.select_page(key); root.update_idletasks()
                        assert view.title.get()=='尚未保存的标题' and view.search.get()=='A.wav'
                        assert view.status_filter.get()=='已精修' and view.list.selection()==(works[0][1],)
                        assert len(root.tk.call('after','info'))<=before_count+1
                        app.select_page('pipeline'); root.update_idletasks()
                        assert app.task_canvas.bbox('all')[3] >= app.task_canvas.winfo_height()
                        assert len(list(children(root)))>30
                        app.show_history(); app.show_batch(); app.show_library()
                        no_formal_popup(); view.play(); stage[0]=2
                    elif stage[0]==2 and 'LIBRARY_PLAYER_READY 你好。' in app.log.get('1.0','end') and app.player_page.ready:
                        assert str(app.notebook.select())==str(app.pages['player'])
                        app.start_job('pipeline',b)
                        app.player_command('toggle'); stage[0]=3
                    elif stage[0]==3 and app.player_page.last_info.get('state')=='Paused':
                        assert app.pipeline_job
                        app.player_page.volume.set(27); app.player_page.set_volume(); stage[0]=4
                    elif stage[0]==4 and app.player_page.last_info.get('volume')==27:
                        app.player_page.progress.set(60); app.player_page.seek(); stage[0]=5
                    elif stage[0]==5 and abs(app.player_page.last_info.get('position_ms',0)-24000)<750:
                        app.player_command('toggle'); app.player_command('visibility'); stage[0]=6
                    elif stage[0]==6 and app.player_page.last_info.get('state')=='Playing':
                        job=app.player_job; pipeline=app.pipeline_job
                        for _ in range(8):
                            for key in app.pages:app.select_page(key)
                        assert app.player_job is job and app.pipeline_job is pipeline
                        assert view.title.get()=='尚未保存的标题'
                        app.player_command('visibility'); app.cancel(); stage[0]=7
                    elif stage[0]==7 and app.pipeline_job is None:
                        assert app.player_job and app.player_page.last_info.get('state')=='Playing'
                        app.stop_player(); stage[0]=8
                    elif stage[0]==8 and app.player_job is None:
                        before_resume[0]=app.history.resume_info(works[0][0].identity['task_id'])
                        assert before_resume[0]['position_ms']>=23000
                        app.show_history()
                        table=next(widget for widget in children(app.history_window) if isinstance(widget,ttk.Treeview))
                        assert table.get_children()==(works[0][0].identity['task_id'],)
                        table.selection_set(works[0][0].identity['task_id'])
                        next(widget for widget in children(app.history_window) if isinstance(widget,ttk.Button) and widget.cget('text')=='打开并播放').invoke()
                        stage[0]=9
                    elif stage[0]==9 and app.player_page.ready and app.player_page.last_info.get('position_ms',0)>=23000:
                        app.stop_player(); stage[0]=10
                    elif stage[0]==10 and app.player_job is None:
                        assert library.get(works[0][1])['title']==title  # Unsaved editor text never changed metadata.
                        assert len(library.list())==7 and library.get(works[0][1])['favorite']
                        assert all(p.read_bytes()==before_mtime[str(p)] for task,_ in works for p in task.directory.glob('*.txt'))
                        no_formal_popup()
                        print('PASS actual Tk fixed five tabs: single formal root, repeated navigation, draft editor/search/filter/selection/plot preserved, no duplicate pages/timers',flush=True)
                        print('PASS actual floating VLC: player tab pause/resume/volume/seek/visibility pipe controls; tab switching while fixture pipeline runs; isolated cancel; history reopen/resume; clean exit',flush=True)
                        finish(); return
                except Exception:
                    import traceback
                    failures.append(traceback.format_exc()); print('TEST_FAILURE',failures[-1],app.player_page.last_info,app.log.get('1.0','end')[-2800:],flush=True)
                    finish(); return
                root.after(50,step)
            root.after(100,step)
            try:root.mainloop()
            finally:
                for job in (app.pipeline_job,app.player_job):
                    if job:job.tree.cancel();job.tree.finish()
                if app.batch:app.batch.close()
            assert not failures,failures
            assert app.player_job is None and app.pipeline_job is None


if __name__=='__main__':main()
