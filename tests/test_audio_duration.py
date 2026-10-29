"""Short isolated metadata, cache and stale-result tests; never play or infer."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).absolute().parents[1]; sys.path.insert(0, str(ROOT))
import audio_duration as duration
import studio
from library_ui import LibraryWindow


class AudioDurationTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'work/library_duration_checks'; base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base); self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.audio = self.folder / '中文 日本語 & 空格.wav'
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000); stream.writeframes(b'\0\0' * 8000 * 2)

    def formatted_probe(self, output, returncode=0):
        with patch.object(studio.runtime, 'external_program', return_value='ffprobe'), \
                patch.object(studio.subprocess, 'run', return_value=SimpleNamespace(stdout=output, returncode=returncode)):
            return duration.DurationCache().read(self.audio)

    def test_format_under_hour(self): self.assertEqual(self.formatted_probe('1725'), '00:28:45')
    def test_format_multiple_hours(self): self.assertEqual(self.formatted_probe('15424'), '04:17:04')
    def test_format_over_24_and_over_99_hours(self):
        self.assertEqual(self.formatted_probe('90061'), '25:01:01')
        self.assertEqual(self.formatted_probe('360001'), '100:00:01')

    def test_real_wav_duration_and_unicode_path_no_file_changes(self):
        before = self.audio.read_bytes(), self.audio.stat().st_mtime_ns
        self.assertEqual(duration.DurationCache().read(self.audio), '00:00:02')
        self.assertEqual((self.audio.read_bytes(), self.audio.stat().st_mtime_ns), before)

    def test_real_short_mp3_and_m4a_metadata(self):
        import av
        import numpy as np
        for suffix, codec in (('.mp3', 'libmp3lame'), ('.m4a', 'aac')):
            path = self.folder / ('短音频 日本語' + suffix)
            with av.open(str(path), 'w') as output:
                stream = output.add_stream(codec, rate=44100); stream.layout = 'mono'
                frame = av.AudioFrame.from_ndarray(np.zeros((1, 44100), dtype=np.float32), format='fltp', layout='mono')
                frame.sample_rate = 44100
                for packet in stream.encode(frame): output.mux(packet)
                for packet in stream.encode(None): output.mux(packet)
            self.assertEqual(duration.DurationCache().read(path), '00:00:01', suffix)

    def test_probe_command_is_bounded_and_path_is_one_argument(self):
        with patch.object(studio.runtime, 'external_program', return_value='probe.exe'), \
                patch.object(studio.subprocess, 'run', return_value=SimpleNamespace(stdout='2', returncode=0)) as run:
            duration.DurationCache().read(self.audio)
        self.assertEqual(run.call_args.args[0][-1], os.path.abspath(self.audio))
        self.assertEqual(run.call_args.kwargs['timeout'], 2)
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_missing_file_or_directory_never_probes(self):
        probe = Mock(); cache = duration.DurationCache(probe)
        self.assertEqual(cache.read(self.folder / 'missing.mp3'), duration.UNKNOWN)
        self.assertEqual(cache.read(self.folder), duration.UNKNOWN); probe.assert_not_called()

    def test_real_corrupt_audio_is_unknown(self):
        bad = self.folder / '坏文件.mp3'; bad.write_bytes(b'invalid audio')
        self.assertEqual(duration.DurationCache().read(bad), duration.UNKNOWN)

    def test_unknown_and_invalid_outputs_are_not_zero(self):
        for value in ('N/A', '', 'nan', 'inf', '0', '-1'):
            self.assertEqual(self.formatted_probe(value), duration.UNKNOWN, value)
        self.assertEqual(self.formatted_probe('123', returncode=1), duration.UNKNOWN)

    def test_missing_tool_and_timeout_safe_fallback(self):
        with patch.object(studio.runtime, 'external_program', return_value=None):
            self.assertEqual(duration.DurationCache().read(self.audio), duration.UNKNOWN)
        with patch.object(studio.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffprobe', 2)):
            self.assertEqual(duration.DurationCache().read(self.audio), duration.UNKNOWN)

    def test_repeated_reads_and_path_alias_share_cache(self):
        probe = Mock(return_value='00:00:02'); cache = duration.DurationCache(probe)
        self.assertEqual(cache.read(self.audio), cache.read(self.folder / '..' / self.folder.name / self.audio.name))
        probe.assert_called_once()

    def test_size_or_mtime_change_invalidates_cache(self):
        probe = Mock(return_value='00:00:02'); cache = duration.DurationCache(probe); cache.read(self.audio)
        self.audio.write_bytes(b'replaced'); cache.read(self.audio)
        info = self.audio.stat(); os.utime(self.audio, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000)); cache.read(self.audio)
        self.assertEqual(probe.call_count, 3); self.assertEqual(len(cache.entries), 1)

    def test_change_during_probe_does_not_cache_wrong_result(self):
        def changed(path): self.audio.write_bytes(b'changed'); return '01:00:00'
        cache = duration.DurationCache(changed)
        self.assertEqual(cache.read(self.audio), duration.UNKNOWN); self.assertFalse(cache.entries)

    def test_failed_probe_is_cached_briefly_then_retried(self):
        probe = Mock(side_effect=['未知', '00:00:02']); cache = duration.DurationCache(probe, failure_ttl=30)
        with patch.object(duration.time, 'monotonic', return_value=0): cache.read(self.audio)
        with patch.object(duration.time, 'monotonic', return_value=20): self.assertEqual(cache.read(self.audio), duration.UNKNOWN)
        with patch.object(duration.time, 'monotonic', return_value=31): self.assertEqual(cache.read(self.audio), '00:00:02')
        self.assertEqual(probe.call_count, 2)

    def test_lru_cache_is_bounded(self):
        cache = duration.DurationCache(Mock(return_value='00:00:02'), max_entries=2)
        for index in range(3):
            path = self.folder / f'{index}.wav'; path.write_bytes(b'test'); cache.read(path)
        self.assertEqual(len(cache.entries), 2)

    def test_frozen_probe_uses_configured_tool_without_python_or_cwd(self):
        probe = self.folder / 'probe.exe'; probe.write_bytes(b'test stub')
        (self.folder / 'runtime_paths.json').write_text(json.dumps({'schema_version': 1, 'ffprobe': str(probe)}), encoding='utf-8')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(self.folder / 'EarlySakura.exe')), \
                patch.object(studio.shutil, 'which', return_value=None), \
                patch.object(studio.subprocess, 'run', return_value=SimpleNamespace(stdout='2', returncode=0)) as run:
            self.assertEqual(duration.DurationCache().read(self.audio), '00:00:02')
            self.assertEqual(run.call_args.args[0][0], str(probe))

    def test_reader_nonblocking_coalesces_pending_and_only_worker_probes(self):
        results = queue.Queue(); entered = threading.Event(); release = threading.Event(); calls = []; main = threading.get_ident()
        def read(path):
            self.assertNotEqual(threading.get_ident(), main); calls.append(path)
            if path == 'A': entered.set(); release.wait(3)
            return '00:00:02'
        reader = duration.DurationReader(results, SimpleNamespace(read=read))
        try:
            reader.request('work-A', 'A'); self.assertTrue(entered.wait(1))
            start = time.monotonic(); reader.request('work-B', 'B'); reader.request('work-C', 'C')
            self.assertLess(time.monotonic() - start, .2); release.set()
            a, c = results.get(timeout=2), results.get(timeout=2)
            self.assertEqual(calls, ['A', 'C']); self.assertEqual((a[1], c[1]), ('work-A', 'work-C'))
        finally: release.set(); reader.close(); reader.thread.join(3)
        self.assertFalse(reader.thread.is_alive())

    def test_close_discards_inflight_and_pending_results(self):
        results = queue.Queue(); entered = threading.Event(); release = threading.Event()
        def read(path): entered.set(); release.wait(3); return '00:00:02'
        reader = duration.DurationReader(results, SimpleNamespace(read=read))
        try:
            reader.request('A', 'A'); self.assertTrue(entered.wait(1)); reader.request('B', 'B'); reader.close(); release.set()
            reader.thread.join(3); self.assertTrue(results.empty()); self.assertFalse(reader.thread.is_alive())
        finally: release.set(); reader.close(); reader.thread.join(3)

    def view(self):
        view = LibraryWindow.__new__(LibraryWindow); view.closing = False
        view.current = {'work_id': 'B', 'audio_path': 'B.wav'}; view._duration_request = (2, 'B', 'B.wav')
        view.info = Mock(); view.info.get.return_value = '文件：B\n音频时长：--:--:--\n字幕：存在'
        view.app = SimpleNamespace(player_job=object(), pipeline_job=object(), history=Mock())
        return view

    def test_old_work_or_version_cannot_update_current_details(self):
        view = self.view()
        for result in ((1, 'A', 'A.wav', '01:00:00'), (1, 'B', 'B.wav', '01:00:00'), (2, 'B', 'other.wav', '01:00:00')):
            view.apply_duration(result)
        view.info.set.assert_not_called()

    def test_current_result_updates_only_details_not_player_or_history(self):
        view = self.view(); old = (view.app.player_job, view.app.pipeline_job)
        view.apply_duration((2, 'B', 'B.wav', '00:28:45'))
        self.assertIn('音频时长：00:28:45', view.info.set.call_args.args[0])
        self.assertEqual((view.app.player_job, view.app.pipeline_job), old); self.assertEqual(view.app.history.mock_calls, [])

    def test_empty_or_closed_view_ignores_result(self):
        view = self.view(); view.current = None; view.apply_duration((2, 'B', 'B.wav', '00:00:02'))
        view.current = {'work_id': 'B', 'audio_path': 'B.wav'}; view.closing = True; view.apply_duration((2, 'B', 'B.wav', '00:00:02'))
        view.info.set.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
