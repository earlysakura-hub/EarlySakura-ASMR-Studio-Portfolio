"""Portable configuration, unchanged defaults and actionable LM failure checks."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).absolute().parents[1]; sys.path.insert(0, str(ROOT))
import app_runtime as runtime
import run_pipeline
import task_store
from media_library import LibraryStore


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'work/configuration_checks'; base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base); self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name); self.config = self.folder / 'runtime_paths.json'
        for item in (patch.dict(os.environ, {'EARLYSAKURA_CONFIG':str(self.config)}),
                     patch.object(sys,'frozen',False,create=True)):
            item.start(); self.addCleanup(item.stop)
        self.write({})

    def write(self, data):
        self.config.write_text(json.dumps(dict(schema_version=1, **data)), encoding='utf-8')

    def test_source_reads_explicit_file_without_current_directory_dependency(self):
        self.write({'audio_directory':str(self.folder),'lm_studio_model':'test-model'})
        self.assertEqual(runtime.audio_directory(),str(self.folder))
        self.assertEqual(runtime.lm_studio_settings()['model'],'test-model')

    def test_default_model_url_and_port_match_existing_pipeline(self):
        self.assertEqual(runtime.lm_studio_settings(),{'base_url':'http://localhost:1234/v1','model':'qwen/qwen3-4b-2507','port':1234,'host':'localhost'})

    def test_custom_url_and_model_are_shared_and_v1_is_not_duplicated(self):
        for url in ('http://127.0.0.1:3456','http://127.0.0.1:3456/v1/'):
            self.write({'lm_studio_base_url':url,'lm_studio_model':'custom-model'})
            settings=runtime.lm_studio_settings()
            self.assertEqual(settings['base_url'],'http://127.0.0.1:3456/v1')
            self.assertEqual(settings['port'],3456); self.assertEqual(settings['model'],'custom-model')

    def test_credentials_and_invalid_urls_are_rejected_without_echoing_secrets(self):
        for url in ('file:///models','http://user:do-not-echo@localhost:1234/v1','http://localhost:1234/v1?token=do-not-echo','http://localhost:1234/wrong'):
            self.write({'lm_studio_base_url':url})
            with self.assertRaises(ValueError) as error:runtime.lm_studio_settings()
            self.assertNotIn('do-not-echo',str(error.exception))

    def test_relative_override_missing_and_corrupt_file_fail_safely(self):
        with patch.dict(os.environ,{'EARLYSAKURA_CONFIG':'relative.json'}):
            with self.assertRaises(ValueError):runtime.configuration()
        self.config.unlink()
        with self.assertRaises(ValueError):runtime.configuration()
        self.config.write_bytes(b'broken')
        with self.assertRaises(ValueError):runtime.configuration()
        self.assertEqual(self.config.read_bytes(),b'broken')

    def test_no_local_file_uses_portable_defaults_without_creating_files(self):
        with patch.dict(os.environ,{'EARLYSAKURA_CONFIG':''}), patch.object(runtime,'SOURCE_ROOT',self.folder/'absent'):
            self.assertEqual(runtime.configuration(),{})
            self.assertEqual(runtime.audio_directory(),str(Path.home()))
            self.assertFalse((self.folder/'absent').exists())

    def test_environment_data_override_wins_over_json_and_source_root(self):
        self.write({'data_root':str(self.folder/'configured')})
        with patch.dict(os.environ,{'EARLYSAKURA_DATA_DIR':str(self.folder/'override')}):
            self.assertEqual(runtime.data_root(ROOT),self.folder/'override')

    def test_source_data_root_is_consistent_for_default_and_explicit_project(self):
        self.write({'data_root':str(self.folder/'configured')})
        with patch.dict(os.environ,{'EARLYSAKURA_DATA_DIR':''}):
            self.assertEqual(runtime.data_root(),self.folder/'configured')
            self.assertEqual(runtime.data_root(ROOT),self.folder/'configured')
            self.assertEqual(runtime.data_root(self.folder),self.folder/'outputs')

    def test_existing_library_root_is_not_changed_by_new_defaults(self):
        library=LibraryStore(self.folder/'db.sqlite3')
        old=self.folder/'original-library'; library.configure(old)
        self.write({'library_root':str(self.folder/'new-default')})
        self.assertEqual(library.root(),old)
        self.assertFalse((self.folder/'new-default').exists())

    def test_legacy_aliases_are_opt_in_and_scoped_to_one_audio(self):
        self.assertIsNone(task_store.legacy_srt_name('sample.wav'))
        self.write({'legacy_srt_aliases':{'sample.wav':'old-subtitle.srt'}})
        self.assertEqual(task_store.legacy_srt_name('SAMPLE.wav'),'old-subtitle.srt')
        self.assertIsNone(task_store.legacy_srt_name('different.wav'))
        self.write({'legacy_srt_aliases':{'sample.wav':'../outside.srt'}})
        with self.assertRaises(task_store.TaskError):task_store.legacy_srt_name('sample.wav')

    def test_optional_token_is_only_read_from_environment(self):
        with patch.dict(os.environ,{'LM_STUDIO_API_TOKEN':''}):self.assertEqual(runtime.lm_studio_headers(),{})
        with patch.dict(os.environ,{'LM_STUDIO_API_TOKEN':'synthetic-test-value'}):
            self.assertEqual(runtime.lm_studio_headers(),{'Authorization':'Bearer synthetic-test-value'})
        self.assertNotIn('synthetic-test-value',self.config.read_text(encoding='utf-8'))

    def test_cli_missing_timeout_or_failed_exit_is_clear_and_bounded(self):
        for error in (FileNotFoundError('missing'),subprocess.TimeoutExpired('lms',180),subprocess.CalledProcessError(1,'lms')):
            with patch.object(run_pipeline,'run_external',side_effect=error) as command:
                with self.assertRaises(task_store.TaskError) as failure:run_pipeline.lm_command(['lms','ps'])
                self.assertIn('模型服务不可用',str(failure.exception))
                self.assertEqual(command.call_args.kwargs['timeout'],180)

    def test_pipeline_uses_configured_port_for_status_and_start(self):
        self.write({'lm_studio_base_url':'http://127.0.0.1:3456/v1','lm_studio_model':'custom-model'})
        audio=self.folder/'audio.wav'; audio.write_bytes(b'synthetic')
        task=task_store.open_task(audio)
        task.result('japanese.txt').write_text('[00:00:00,000 --> 00:00:01,000] hello\n',encoding='utf-8')
        calls=[]
        def cli(command,**kwargs):
            calls.append(command)
            return Mock(stdout='{"running":false}' if 'status' in command else '')
        def script(name,arg):
            self.assertEqual(name,'translate.py')
            task.result('draft_bilingual.txt').write_text('[00:00:00,000 --> 00:00:01,000]\nJP: hello\nZH_DRAFT: translated\n\n',encoding='utf-8')
        with patch.object(run_pipeline,'external_program',return_value='lms'),patch.object(run_pipeline,'run_external',side_effect=cli),patch.object(run_pipeline,'run_script_process',side_effect=script):
            run_pipeline.main(audio,offer_legacy=False)
        self.assertIn(['lms','server','start','--port','3456'],calls)
        self.assertIn(['lms','load','custom-model','--gpu','max','--context-length','8192'],calls)

    def test_translation_connection_failure_is_actionable_without_loading_module(self):
        # Extract just the request function: no top-level dialogs, task or model work.
        tree=ast.parse((ROOT/'translate.py').read_text(encoding='utf-8'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='ask_qwen')
        requests=Mock(); requests.post.side_effect=ConnectionError('offline')
        scope={'requests':requests,'lm_studio_headers':lambda:{},'API_URL':'http://localhost:1234/v1/chat/completions','MODEL':'test','SYSTEM_PROMPT':'test','TaskError':task_store.TaskError}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'translation-function','exec'),scope)
        with self.assertRaises(ConnectionError) as error:scope['ask_qwen']('synthetic')
        self.assertIn('已有粗翻保留',str(error.exception))


if __name__=='__main__':unittest.main(verbosity=2)
