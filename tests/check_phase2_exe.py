"""Real frozen GUI/worker checks on disposable data. Never infer model success."""
import argparse
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
import wave
import zlib

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))
from check_console_free import console_windows, shell_gui, subsystem
from check_exe import wait_until, windows
from windows_job import ProcessTree
import task_store
from media_library import LibraryStore
from playback_history import HistoryStore
from batch_queue import BatchQueue
from gui_preferences import GuiPreferences
from player_ipc import encode_command
from make_srt import prepare_srt


def capture(handle, path):
    """Capture only the owned test GUI to PNG, using Windows GDI + stdlib."""
    user = ctypes.WinDLL('user32'); gdi = ctypes.WinDLL('gdi32')
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user.GetWindowDC.argtypes = [w.HWND]; user.GetWindowDC.restype = w.HDC
    user.ReleaseDC.argtypes = [w.HWND, w.HDC]
    user.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
    gdi.CreateCompatibleDC.argtypes = [w.HDC]; gdi.CreateCompatibleDC.restype = w.HDC
    gdi.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]; gdi.CreateCompatibleBitmap.restype = w.HBITMAP
    gdi.SelectObject.argtypes = [w.HDC, w.HANDLE]; gdi.SelectObject.restype = w.HANDLE
    gdi.DeleteObject.argtypes = [w.HANDLE]; gdi.DeleteDC.argtypes = [w.HDC]
    gdi.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT]
    rect = w.RECT(); assert user.GetWindowRect(handle, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    source = user.GetWindowDC(handle); target = gdi.CreateCompatibleDC(source)
    bitmap = gdi.CreateCompatibleBitmap(source, width, height); old = gdi.SelectObject(target, bitmap)
    try:
        assert user.PrintWindow(handle, target, 2), 'PrintWindow failed'
        gdi.SelectObject(target, old)
        stride = (width * 3 + 3) & ~3
        header = struct.pack('<IiiHHIIiiII', 40, width, height, 1, 24, 0, stride * height, 0, 0, 0, 0)
        info = ctypes.create_string_buffer(header + b'\0' * 16); pixels = ctypes.create_string_buffer(stride * height)
        assert gdi.GetDIBits(target, bitmap, 0, height, pixels, info, 0) == height
        rows = []
        for index in range(height - 1, -1, -1):
            row = pixels.raw[index * stride:index * stride + width * 3]
            rgb = bytearray(len(row)); rgb[0::3] = row[2::3]; rgb[1::3] = row[1::3]; rgb[2::3] = row[0::3]
            rows.append(b'\0' + bytes(rgb))
        def chunk(name, content):
            return struct.pack('>I', len(content)) + name + content + struct.pack('>I', zlib.crc32(name + content))
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                         + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))
    finally:
        gdi.DeleteObject(bitmap); gdi.DeleteDC(target); user.ReleaseDC(handle, source)


def check(exe, gui_only=False):
    directory = ROOT / 'work/phase2_packaging' / ('frozen-' + uuid.uuid4().hex[:10]); directory.mkdir(parents=True)
    package = directory / 'package'; shutil.copytree(exe.parent, package)
    data = directory / 'data'; data.mkdir()
    config = json.loads((package / 'runtime_paths.json').read_text(encoding='utf-8'))
    assert config['data_root'] == str(ROOT / 'outputs'), 'Delivery package must use original user outputs'
    config['data_root'] = str(data)
    (package / 'runtime_paths.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    old_override = os.environ.get('EARLYSAKURA_DATA_DIR'); os.environ['EARLYSAKURA_DATA_DIR'] = str(data)
    report = {'delivery_exe': str(exe), 'test_directory': str(directory), 'passed': [], 'real_models': 'not run',
              'human_double_click_and_audio': 'not performed', 'isolated_package': str(package)}
    trees = []; processes = []; readers = []; console_baseline = console_windows(); new_consoles = set(); done = threading.Event()
    def monitor():
        while not done.wait(.05): new_consoles.update(console_windows() - console_baseline)
    monitor_thread = threading.Thread(target=monitor, daemon=True); monitor_thread.start()
    env = os.environ.copy()
    for name in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'): env.pop(name, None)
    env.update(PATH=os.path.join(os.environ['SystemRoot'], 'System32'), HF_HUB_OFFLINE='1')
    helper = package / 'EarlySakuraWorker.exe'
    def run(arguments, label):
        result = subprocess.run([str(helper), *arguments], cwd=directory, env=env, capture_output=True,
            text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
        (directory / (label + '.log')).write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
        assert result.returncode == 0, (label, result.returncode, result.stdout[-1500:], result.stderr[-1500:])
        report['passed'].append(label); return result.stdout
    try:
        assert subsystem(exe) == 2 and subsystem(exe.with_name('EarlySakuraWorker.exe')) == 3
        report['passed'].append('GUI_PE_windowed_Worker_PE_console')
        sys.path.insert(0, str(ROOT / 'work/packaging_tools'))
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(exe)); pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith('.pyz')))
        required = ('studio', 'task_store', 'batch_queue', 'library_ui', 'media_library', 'library_files', 'library_migration',
                    'player_page', 'player_ipc', 'gui_preferences', 'playback_history', 'playback_resume', 'make_srt', 'run_pipeline')
        assert all(name in pyz.toc for name in required)
        report['bundled_modules'] = list(required); report['passed'].append('new_modules_in_actual_frozen_PYZ')
        library = LibraryStore(data / 'media_library.sqlite3', data / 'playback_history.sqlite3'); library.configure(directory / '隔离媒体库')
        works = []
        for index in range(7):
            audio = directory / f'RJ{100000 + index} 中文・日本語.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000); stream.writeframes(b'\0\0' * 8000 * 45)
            task = task_store.open_task(audio); work_id = library.register(task)
            task.result('japanese.txt').write_text('[00:00:00,000 --> 00:00:45,000] テスト。\n', encoding='utf-8')
            task.result('draft_bilingual.txt').write_text('[00:00:00,000 --> 00:00:45,000]\nJP: テスト。\nZH_DRAFT: 隔离测试字幕。\n\n', encoding='utf-8')
            library.update(work_id, title=f'隔离作品 {index + 1} 日本語', auto_state='completed', archive_state='archived', refinement='refined', favorite=index == 0)
            tag = library.save_tag('测试标签'); library.set_tags(work_id, [tag])
            (task.directory / 'full_plot_summary.txt').write_text('完整剧情・日本語・English 123\n\n' + '长篇中文剧情与日文の紹介。\n' * 80, encoding='utf-8')
            works.append((task, work_id))
        queue = BatchQueue(data / 'batch_queue.json'); queue.add(works[1][0]); queue.save(); queue.close()
        preferences = GuiPreferences(data / 'gui_preferences.json'); preferences.save_plot_font_size(17)
        expected = library.list(); queue_bytes = (data / 'batch_queue.json').read_bytes()
        report['diagnostic'] = json.loads(run(['--diagnose'], 'actual_frozen_diagnostic'))
        diagnostic = report['diagnostic']; assert diagnostic['frozen'] and diagnostic['executable'] == str(helper)
        assert all(value['ok'] for value in diagnostic['libraries'].values()), diagnostic['libraries']
        assert diagnostic['data_root'] == str(data) and all(diagnostic['scripts'].values())
        for size in (17, 18):
            preferences.save_plot_font_size(size)
            assert preferences.plot_font_size() == size
            before = preferences.path.read_bytes()
            def inspect(handle, pid):
                time.sleep(1); capture(handle, directory / f'actual-frozen-GUI-{size}pt.png')
                assert len(windows(pid, 'EarlySakura ASMR Studio')) == 1
            shell_gui(package / 'EarlySakura.exe', inspect)
            assert library.list() == expected
            assert preferences.path.read_bytes() == before
            assert (data / 'batch_queue.json').read_bytes() == queue_bytes
        report['passed'].append('actual_Shell_GUI_start_restart_normal_close_isolated_metadata_kept')
        report['preference_test'] = '17/18 pt seeded in isolated preferences; actual frozen GUI images captured; GUI button-save still needs manual acceptance'
        if gui_only:
            assert not new_consoles, list(new_consoles)
            print(json.dumps(report, ensure_ascii=False, indent=2)); return
        task = works[0][0]
        run(['--worker', 'pipeline', str(task.identity['path'])], 'frozen_pipeline_complete_skip_no_models')
        run(['--worker', 'transcribe', str(task.identity['path'])], 'frozen_transcribe_existing_japanese_dispatch_no_models')
        run(['--worker', 'srt', str(task.identity['path']), '--srt-plan', json.dumps(prepare_srt(task), ensure_ascii=False)], 'frozen_srt_from_synthetic_draft')
        assert task.result('output.srt').is_file()
        failure = subprocess.run([str(helper), '--worker', 'pipeline', str(directory / '不存在.wav')],
            cwd=directory, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
        (directory / 'expected-worker-error.log').write_text(failure.stdout + '\n' + failure.stderr, encoding='utf-8')
        assert failure.returncode != 0 and 'Traceback' in failure.stderr
        report['passed'].append('frozen_worker_invalid_input_stderr_nonzero_clean_exit_no_models')
        def start(mode, target=None):
            target = target or task
            tree = ProcessTree(); trees.append(tree)
            process = tree.start([str(helper), '--await-job', '--worker', mode, str(target.identity['path']),
                                 *(['--player-control'] if mode == 'player' else [])], keep_stdin=True,
                cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
            processes.append(process); lines = []
            def read():
                for line in process.stdout: lines.append(line)
            reader = threading.Thread(target=read, daemon=True); reader.start(); readers.append(reader)
            return tree, process, lines, reader
        tree, player, lines, reader = start('player')
        def latest(predicate):
            for line in reversed(lines.copy()):
                if line.startswith('STUDIO_PLAYER '):
                    info = json.loads(line[len('STUDIO_PLAYER '):])
                    if predicate(info): return info
                    break
            assert player.poll() is None, ''.join(lines)
        def send(command, **fields): player.stdin.write(encode_command(command, **fields)); player.stdin.flush()
        wait_until(lambda: latest(lambda info: info['state'] == 'Playing' and info['position_ms'] > 500), 25)
        send('toggle'); wait_until(lambda: latest(lambda info: info['state'] == 'Paused'))
        send('volume', value=31); wait_until(lambda: latest(lambda info: info['volume'] == 31))
        send('seek', position_ms=20000); wait_until(lambda: latest(lambda info: abs(info['position_ms'] - 20000) < 1000))
        send('relative', seconds=5); wait_until(lambda: latest(lambda info: abs(info['position_ms'] - 25000) < 1000))
        send('relative', seconds=-5); wait_until(lambda: latest(lambda info: abs(info['position_ms'] - 20000) < 1000))
        user = ctypes.WinDLL('user32'); user.IsWindowVisible.argtypes = [w.HWND]
        def visible(): return any(user.IsWindowVisible(handle) for handle, _ in windows(player.pid, 'EarlySakura ASMR Studio — 悬浮字幕'))
        wait_until(visible); send('visibility'); wait_until(lambda: not visible()); send('visibility'); wait_until(visible)
        send('toggle'); wait_until(lambda: latest(lambda info: info['state'] == 'Playing'))
        # Completed synthetic inputs guarantee this cancellation test cannot launch a model.
        cancel_task = works[1][0]
        cancel_tree, cancelled, cancel_lines, cancel_reader = start('pipeline', cancel_task)
        cancel_tree.cancel(); cancelled.wait(timeout=8); cancel_tree.finish(); cancel_reader.join(3)
        assert cancel_tree.closed and cancel_tree.active_count() == 0 and player.poll() is None
        with task_store.task_lock(cancel_task.directory): pass
        report['passed'].append('frozen_owned_pipeline_tree_cancel_lock_available_other_player_survives')
        send('close'); assert player.wait(timeout=12) == 0; tree.finish(); reader.join(3)
        saved = HistoryStore(data / 'playback_history.sqlite3').resume_info(task.identity['task_id'])
        assert saved and saved['position_ms'] >= 19000
        report['saved_resume'] = saved
        report['passed'].append('actual_frozen_VLC_pause_volume_seek_plus_minus_5_visibility_continue_CLOSE_progress_saved')
        (directory / 'actual-player.log').write_text(''.join(lines), encoding='utf-8')
        tree, player, lines, reader = start('player')
        wait_until(lambda: windows(player.pid, '恢复播放'), 20)
        player.stdin.write('CLOSE\n'); player.stdin.flush(); assert player.wait(timeout=12) == 0; tree.finish(); reader.join(3)
        assert HistoryStore(data / 'playback_history.sqlite3').resume_info(task.identity['task_id']) == saved
        report['passed'].append('frozen_player_reopen_resume_prompt_cancel_preserves_position')
        assert not new_consoles, list(new_consoles)
        report['passed'].append('no_new_visible_console_Terminal_sampled_50ms')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    except BaseException as error:
        report['error'] = repr(error); raise
    finally:
        for tree in trees: tree.cancel(); tree.finish()
        for process in processes: process.wait(timeout=8)
        for reader in readers: reader.join(3)
        for process in processes:
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed: stream.close()
        done.set(); monitor_thread.join(3); report['new_console_handles'] = list(new_consoles)
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if old_override is None: os.environ.pop('EARLYSAKURA_DATA_DIR', None)
        else: os.environ['EARLYSAKURA_DATA_DIR'] = old_override


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('exe', type=Path); parser.add_argument('--gui-only', action='store_true')
    args = parser.parse_args(); check(args.exe.absolute(), args.gui_only)
