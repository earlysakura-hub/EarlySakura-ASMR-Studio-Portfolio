"""Headless protocol/lifecycle checks; no formal metadata or audio writes."""
import ast
import json
from pathlib import Path
import queue
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT=Path(__file__).absolute().parents[1]; sys.path.insert(0,str(ROOT))
import studio
from player_ipc import decode_command,encode_command


class TabsTest(unittest.TestCase):
    def test_fixed_pages_and_only_temporary_toplevels_in_main_modules(self):
        tree=ast.parse((ROOT/'studio.py').read_text(encoding='utf-8'))
        functions={node.name:node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef)}
        for name in ('show_library','show_batch','show_history','select_page'):
            calls=[node for node in ast.walk(functions[name]) if isinstance(node,ast.Call)]
            self.assertFalse(any(isinstance(call.func,ast.Attribute) and call.func.attr in ('Tk','Toplevel','destroy') for call in calls),name)
        source=(ROOT/'library_ui.py').read_text(encoding='utf-8')
        tree=ast.parse(source)
        init=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='__init__')
        self.assertFalse(any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr in ('Tk','Toplevel') for node in ast.walk(init)))

    def test_select_same_page_is_idempotent_and_does_not_touch_jobs(self):
        app=studio.Studio.__new__(studio.Studio); app.notebook=Mock(); app.pages={'library':'library-frame','player':'player-frame'}
        app.notebook.select.return_value='library-frame'; app.pipeline_job=object(); app.player_job=object()
        before=(app.pipeline_job,app.player_job)
        app.select_page('library'); app.notebook.select.assert_called_once_with()
        app.notebook.select.reset_mock(); app.select_page('player')
        app.notebook.select.assert_any_call('player-frame'); self.assertEqual(before,(app.pipeline_job,app.player_job))

    def test_protocol_round_trip_and_close_compatibility(self):
        for command,fields in [('toggle',{}),('visibility',{}),('seek',{'position_ms':123456}),('relative',{'seconds':-5}),('volume',{'value':37})]:
            self.assertEqual(decode_command(encode_command(command,**fields)),{'command':command,**fields})
        self.assertEqual(encode_command('close'),'CLOSE\n')
        self.assertEqual(decode_command('CLOSE\n'),{'command':'close'})

    def test_invalid_commands_cannot_reach_engine(self):
        for value in [{'command':'volume','value':101},{'command':'volume','value':True},{'command':'seek','position_ms':-1},
                      {'command':'relative','seconds':500},{'command':'toggle','extra':1},{'command':'arbitrary'},[], 'x'*2050]:
            line=value if isinstance(value,str) else json.dumps(value)
            with self.assertRaises(ValueError):decode_command(line)

    def test_player_command_uses_its_owned_pipe_from_worker_thread(self):
        done=threading.Event(); main=threading.get_ident()
        process=Mock(); process.poll.return_value=None
        process.stdin.flush.side_effect=lambda:done.set()
        def write(line):
            self.assertNotEqual(threading.get_ident(),main)
            self.assertEqual(decode_command(line),{'command':'volume','value':31})
        process.stdin.write.side_effect=write
        job=SimpleNamespace(stop_requested=False,process=process,control_lock=threading.Lock())
        app=studio.Studio.__new__(studio.Studio); app.player_job=job; app.exiting=False; app.events=queue.Queue()
        app.player_command('volume',value=31); self.assertTrue(done.wait(2))
        job.stop_requested=True; process.stdin.write.reset_mock(); app.player_command('toggle'); process.stdin.write.assert_not_called()

    def test_worker_dispatches_controls_on_tk_tick_to_existing_functions(self):
        source=ast.parse((ROOT/'floating_subtitle_player.py').read_text(encoding='utf-8-sig'))
        node=next(node for node in source.body if isinstance(node,ast.FunctionDef) and node.name=='poll_studio_commands')
        resume=Mock(ready=True,failed=False); resume.jump_to.return_value=True
        player=Mock(); player.get_length.return_value=60000
        namespace=dict(closing_player=False,control_commands=queue.Queue(),queue=queue,root=Mock(),resume=resume,player=player,
                       startup_closed=False,toggle_play=Mock(),seek=Mock(),toggle_subtitle_visibility=Mock(),volume_scale=Mock(),
                       close_player=Mock(),current_subtitle='',last_status_report=0)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'player-control-tick','exec'),namespace)
        for command,fields in [('toggle',{}),('relative',{'seconds':5}),('visibility',{}),('volume',{'value':44}),('seek',{'position_ms':20000})]:
            namespace['control_commands'].put(decode_command(encode_command(command,**fields))); namespace['poll_studio_commands']()
        namespace['toggle_play'].assert_called_once(); namespace['seek'].assert_called_once_with(5)
        namespace['toggle_subtitle_visibility'].assert_called_once(); namespace['volume_scale'].set.assert_called_once_with(44)
        resume.jump_to.assert_called_once_with(20000); self.assertIsNone(namespace['current_subtitle'])
        namespace['control_commands'].put('CLOSE'); namespace['poll_studio_commands'](); namespace['close_player'].assert_called_once()

    def test_shutdown_cancels_each_page_timer_without_destroying_business_early(self):
        app=studio.Studio.__new__(studio.Studio); app.root=Mock(); app.batch=Mock(); app.library_window=Mock(); app._drain_id='after-id'
        app.destroy_root(); app.library_window.shutdown.assert_called_once(); app.root.after_cancel.assert_called_once_with('after-id')
        app.batch.close.assert_called_once(); app.root.destroy.assert_called_once(); self.assertIsNone(app._drain_id)


if __name__=='__main__':unittest.main(verbosity=2)
