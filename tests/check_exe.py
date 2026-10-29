"""Actual frozen executable checks, isolated data, owned processes, clean PATH."""
import argparse
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
import task_store as store
from app_runtime import BUILD_LABEL
from windows_job import ProcessTree
from playback_history import HistoryStore
from make_srt import prepare_srt
from batch_queue import BatchQueue


def windows(pid, title=None):
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    found = []
    def visit(handle, argument):
        owner = w.DWORD(); user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid:
            text = ctypes.create_unicode_buffer(512); user.GetWindowTextW(handle, text, 512)
            if text.value and (title is None or text.value == title): found.append((handle, text.value))
        return True
    user.EnumWindows(callback_type(visit), 0)
    return found


def wait_until(condition, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value: return value
        time.sleep(.05)
    raise AssertionError('timeout waiting for executable state')


def check(exe, player=False, clip_source=None):
    helper = exe.with_name('EarlySakuraWorker.exe')
    directory = ROOT / 'work' / 'exe_checks' / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True)
    env = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(name, None)
    env.update(PATH=os.path.join(os.environ['SystemRoot'], 'System32'),
               EARLYSAKURA_DATA_DIR=str(directory / 'data'), HF_XET_CACHE=str(directory / 'xet'))
    report = {'exe': str(exe), 'test_directory': str(directory), 'clean_python_environment': True, 'passed': []}
    def run(args, label):
        result = subprocess.run([str(helper), *args], cwd=directory, env=env, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=60,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        (directory / (label + '.log')).write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
        assert result.returncode == 0, (label, result.returncode, result.stdout[-3000:], result.stderr[-3000:])
        report['passed'].append(label)
        return result.stdout
    try:
        report['diagnostic'] = json.loads(run(['--diagnose'], 'diagnostic'))
        assert all(item['ok'] for item in report['diagnostic']['libraries'].values()), report['diagnostic']
        assert all(report['diagnostic']['scripts'].values())
        # True EXE main window; close through its normal WM_DELETE_WINDOW handler.
        gui = subprocess.Popen([str(exe)], cwd=directory, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            handle = wait_until(lambda: windows(gui.pid, BUILD_LABEL))[0][0]
            user = ctypes.WinDLL('user32'); user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            user.PostMessageW(handle, 0x10, 0, 0)
            assert gui.wait(timeout=10) == 0
            report['passed'].append('actual_exe_gui_start_close')
        finally:
            if gui.poll() is None: gui.terminate(); gui.wait(timeout=5)
        audio = directory / 'silent.wav'
        with wave.open(str(audio), 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
            output.writeframes(b'\0\0' * 16000 * 60)
        task = store.open_task(audio)
        jp = '[00:00:00,000 --> 00:01:00,000] テスト。\n'
        draft = '[00:00:00,000 --> 00:01:00,000]\nJP: テスト。\nZH_DRAFT: 独立测试字幕。\n\n'
        store.write_new(task.result('japanese.txt'), jp.encode('utf-8'))
        store.write_new(task.result('draft_bilingual.txt'), draft.encode('utf-8'))
        before = {name: task.result(name).read_bytes() for name in ('japanese.txt', 'draft_bilingual.txt')}
        run(['--worker', 'pipeline', str(audio)], 'pipeline_complete_skip')
        run(['--worker', 'srt', str(audio), '--srt-plan', json.dumps(prepare_srt(task), ensure_ascii=False)], 'srt_frozen_worker')
        assert task.result('output.srt').is_file()
        for name, content in before.items(): assert task.result(name).read_bytes() == content
        # Actual frozen transcribe worker sees complete JP and skips before model.
        run(['--worker', 'transcribe', str(audio)], 'transcribe_dispatch_complete_skip')
        if player:
            tree = ProcessTree(); process = None; lines = queue.Queue(); seen = []
            try:
                process = tree.start([str(helper), '--await-job', '--worker', 'player', str(audio), '--player-control'],
                    keep_stdin=True, cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace')
                def read():
                    for line in process.stdout:
                        seen.append(line); lines.put(line)
                reader = threading.Thread(target=read, daemon=True); reader.start()
                def playing():
                    for line in list(seen):
                        if line.startswith('STUDIO_PLAYER '):
                            value = json.loads(line[len('STUDIO_PLAYER '):])
                            if value['state'] == 'Playing' and value['position_ms'] >= 5500: return value
                    assert process.poll() is None, ''.join(seen)
                playback = wait_until(playing, 25)
                assert playback['duration_ms'] == 60000
                assert windows(process.pid, 'EarlySakura ASMR Studio')
                process.stdin.write('CLOSE\n'); process.stdin.flush()
                assert process.wait(timeout=12) == 0
                tree.finish(); reader.join(3)
                history = HistoryStore(directory / 'data' / 'playback_history.sqlite3')
                saved = history.resume_info(task.identity['task_id'])
                assert saved and saved['position_ms'] >= 5000
                report['saved_resume'] = saved
                report['passed'].append('actual_frozen_vlc_play_clock_close_history_resume_saved')
                (directory / 'player.log').write_text(''.join(seen), encoding='utf-8')
            finally:
                tree.cancel(); tree.finish()
                if process:
                    process.wait(timeout=5)
                    if process.stdin and not process.stdin.closed: process.stdin.close()
                    process.stdout.close()
        if clip_source:
            # Decode only the first 19 seconds; retain 7..19s as a new test input.
            import av
            digest = store.file_hash(clip_source)
            resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
            chunks, samples = [], 0
            with av.open(str(clip_source)) as source:
                for frame in source.decode(audio=0):
                    for converted in resampler.resample(frame):
                        chunks.append(converted.to_ndarray().tobytes()); samples += converted.samples
                    if samples >= 19 * 16000: break
            assert store.file_hash(clip_source) == digest
            clip = directory / 'speech-12seconds.wav'
            with wave.open(str(clip), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                output.writeframes(b''.join(chunks)[7 * 16000 * 2:19 * 16000 * 2])
            env['HF_HUB_OFFLINE'] = '1'  # Existing cache only; no forced download.
            run(['--worker', 'transcribe', str(clip)], 'actual_whisper_gpu_12s')
            clip_task = store.open_task(clip)
            assert clip_task.result('japanese.txt').stat().st_size > 0
            report['whisper_output'] = str(clip_task.result('japanese.txt'))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def follow_up(exe, directory):
    helper = exe.with_name('EarlySakuraWorker.exe')
    directory = directory.resolve()
    assert directory.is_relative_to((ROOT / 'work' / 'exe_checks').resolve())
    env = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'): env.pop(name, None)
    env.update(PATH=os.path.join(os.environ['SystemRoot'], 'System32'), EARLYSAKURA_DATA_DIR=str(directory / 'data'),
               HF_XET_CACHE=str(directory / 'xet'), HF_HUB_OFFLINE='1')
    passed = []
    scopes = []
    def start(mode, audio, player=False):
        tree = ProcessTree()
        args = [str(helper), '--await-job', '--worker', mode, str(audio)]
        if player: args.append('--player-control')
        process = tree.start(args, keep_stdin=player, cwd=directory, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        lines = []
        def read():
            for line in process.stdout: lines.append(line)
        thread = threading.Thread(target=read, daemon=True); thread.start()
        scopes.append((tree, process, lines, thread))
        return tree, process, lines, thread
    def finish(scope, name):
        tree, process, lines, thread = scope
        process.wait(timeout=12); tree.finish(); thread.join(3)
        (directory / (name + '.log')).write_text(''.join(lines), encoding='utf-8')
    try:
        # Real frozen player reopens the previously saved DB and offers resume.
        task = store.open_task(directory / 'silent.wav')
        history = HistoryStore(directory / 'data' / 'playback_history.sqlite3')
        before = history.resume_info(task.identity['task_id'])
        scope = start('player', task.identity['path'], player=True)
        tree, process, lines, thread = scope
        wait_until(lambda: windows(process.pid, '恢复播放'), 20)
        wait_until(lambda: any('等待恢复选择' in line for line in lines), 5)
        process.stdin.write('CLOSE\n'); process.stdin.flush()
        finish(scope, 'resume_reopen_cancel')
        assert process.returncode == 0
        assert history.resume_info(task.identity['task_id']) == before
        passed.append('frozen_restart_reads_resume_prompt_close_preserves_progress')
        # Queue metadata survives real EXE main-window restart without auto-start.
        queue_path = directory / 'data' / 'batch_queue.json'
        entries = BatchQueue(queue_path)
        entries.add(task); entries.save(); entries.close()
        queue_before = queue_path.read_bytes()
        gui = subprocess.Popen([str(exe)], cwd=directory, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            handle = wait_until(lambda: windows(gui.pid, BUILD_LABEL))[0][0]
            user = ctypes.WinDLL('user32'); user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            user.PostMessageW(handle, 0x10, 0, 0)
            assert gui.wait(timeout=10) == 0
            assert queue_path.read_bytes() == queue_before
            passed.append('frozen_gui_restart_keeps_pending_queue_without_automatic_processing')
        finally:
            if gui.poll() is None: gui.terminate(); gui.wait(timeout=5)
        # Cancel actual frozen transcribe during its protected setup; no fake mode.
        clip = directory / 'cancel-12seconds.wav'
        shutil.copy2(directory / 'speech-12seconds.wav', clip)
        cancelled_task = store.open_task(clip)
        scope = start('transcribe', clip)
        tree, process, lines, thread = scope
        def locked():
            assert process.poll() is None, ''.join(lines)
            try:
                with store.task_lock(cancelled_task.directory): pass
            except store.TaskError:
                return True
        wait_until(locked, 15)
        tree.cancel(); finish(scope, 'actual_frozen_cancel')
        assert tree.closed and tree.active_count() == 0
        with store.task_lock(cancelled_task.directory): pass
        assert not cancelled_task.result('japanese.txt').exists()
        passed.append('actual_frozen_transcribe_cancel_tree_exit_lock_released_no_completed_output')
        # True translator worker on a service known to be offline: error, not success.
        import requests
        try:
            requests.get('http://localhost:1234/v1/models', timeout=2)
        except requests.RequestException:
            scope = start('translate', store.open_task(directory / 'speech-12seconds.wav').result('japanese.txt'))
            finish(scope, 'qwen_unavailable')
            tree, process, lines, thread = scope
            assert process.returncode != 0 and any('ConnectionError' in line for line in lines)
            passed.append('frozen_translate_offline_service_error_nonzero_clean_exit')
        else:
            passed.append('offline_service_check_skipped_service_is_now_available')
        print(json.dumps({'directory': str(directory), 'passed': passed}, ensure_ascii=False, indent=2))
    finally:
        for tree, process, lines, thread in scopes:
            tree.cancel(); tree.finish(); process.wait(timeout=5); thread.join(3)
            if process.stdin and not process.stdin.closed: process.stdin.close()
            process.stdout.close()
        (directory / 'follow_up_report.json').write_text(json.dumps({'passed': passed}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('--player', action='store_true')
    parser.add_argument('--clip-source', type=Path, help='只读源短音频，复制其中 12 秒测试 GPU，不改原文件')
    parser.add_argument('--follow-up', type=Path, help='继续已有的独立 EXE 检查目录，不重复完整检查')
    args = parser.parse_args()
    if args.follow_up:
        follow_up(args.exe.absolute(), args.follow_up)
    else:
        check(args.exe.absolute(), args.player, args.clip_source)
