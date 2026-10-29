"""Automated Shell/PE checks + actual windowless-parent worker integration.

This does not claim human Explorer double-click/Windows Terminal acceptance.
All test data and the disposable package copy stay in work/console_free_checks.
"""
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

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))
from app_runtime import BUILD_LABEL
from check_exe import windows, wait_until


def console_windows():
    user = ctypes.WinDLL('user32')
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [w.HWND]
    found = set()
    def visit(handle, argument):
        text = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(handle, text, 256)
        if user.IsWindowVisible(handle) and (text.value == 'ConsoleWindowClass' or 'CASCADIA' in text.value.upper()):
            found.add(int(handle))
        return True
    user.EnumWindows(callback_type(visit), 0)
    return found


def subsystem(exe):
    with Path(exe).open('rb') as stream:
        stream.seek(0x3c); pe = struct.unpack('<I', stream.read(4))[0]
        stream.seek(pe); assert stream.read(4) == b'PE\0\0'
        stream.seek(pe + 24 + 68)
        return struct.unpack('<H', stream.read(2))[0]


def shell_gui(exe, inspect=None):
    class ShellInfo(ctypes.Structure):
        _fields_ = [('cbSize', w.DWORD), ('fMask', w.ULONG), ('hwnd', w.HWND), ('lpVerb', w.LPCWSTR),
                    ('lpFile', w.LPCWSTR), ('lpParameters', w.LPCWSTR), ('lpDirectory', w.LPCWSTR),
                    ('nShow', ctypes.c_int), ('hInstApp', w.HINSTANCE), ('lpIDList', ctypes.c_void_p),
                    ('lpClass', w.LPCWSTR), ('hkeyClass', w.HKEY), ('dwHotKey', w.DWORD),
                    ('hIcon', w.HANDLE), ('hProcess', w.HANDLE)]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32')
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellInfo)]
    kernel.GetProcessId.argtypes = [w.HANDLE]; kernel.GetProcessId.restype = w.DWORD
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    info = ShellInfo(); info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = 'open'; info.lpFile = str(exe); info.lpDirectory = str(exe.parent)
    info.nShow = 1  # Visible product GUI is the subject of this test.
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    assert info.hProcess
    try:
        pid = kernel.GetProcessId(info.hProcess)
        handle = wait_until(lambda: windows(pid, BUILD_LABEL))[0][0]
        time.sleep(1)
        if inspect is not None: inspect(handle, pid)
        user.PostMessageW(handle, 0x10, 0, 0)
        assert kernel.WaitForSingleObject(info.hProcess, 10000) == 0
        code = w.DWORD(); assert kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        assert code.value == 0
        return pid
    finally:
        if kernel.WaitForSingleObject(info.hProcess, 0) != 0:
            kernel.TerminateProcess(info.hProcess, 1)  # Only the retained test GUI HANDLE.
        kernel.CloseHandle(info.hProcess)


def windowless_parent(directory, exe):
    """pythonw has no stdio/console: run original Tk Studio with frozen workers."""
    import app_runtime as runtime
    # Establish safe diagnostic streams before importing GUI modules.
    os.environ['EARLYSAKURA_DATA_DIR'] = str(directory / 'parent-data')
    original_streams_missing = [sys.stdin is None, sys.stdout is None, sys.stderr is None]
    runtime.ensure_gui_streams()
    import tkinter as tk
    from unittest.mock import patch
    import studio
    import task_store as store
    from make_srt import prepare_srt
    import wave
    helper = exe.with_name('EarlySakuraWorker.exe')
    result = {'original_streams_missing': original_streams_missing, 'passed': []}
    user_callbacks = []
    scopes = []
    root = tk.Tk(); root.withdraw()
    root.report_callback_exception = lambda kind, error, trace: user_callbacks.append(str(error))
    def command(mode, audio, gated=False):
        return [str(helper), *(['--await-job'] if gated else []), '--worker', mode, str(audio)]
    try:
        assert all(original_streams_missing)
        assert not ctypes.windll.kernel32.GetConsoleWindow()
        with patch('studio.ROOT', directory), patch('studio.worker_command', side_effect=command):
            app = studio.Studio(root)
            def pump(condition, timeout=30):
                deadline = time.monotonic() + timeout
                while not condition():
                    root.update()
                    assert not user_callbacks, user_callbacks
                    if time.monotonic() > deadline: raise AssertionError('windowless-parent worker timeout')
                    time.sleep(.02)
                root.update()
            audio = directory / 'silent.wav'
            with wave.open(str(audio), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                output.writeframes(b'\0\0' * 16000 * 60)
            task = store.open_task(audio)
            store.write_new(task.result('japanese.txt'), '[00:00:00,000 --> 00:01:00,000] テスト。\n'.encode('utf-8'))
            store.write_new(task.result('draft_bilingual.txt'), '[00:00:00,000 --> 00:01:00,000]\nJP: テスト。\nZH_DRAFT: 隔离测试字幕。\n\n'.encode('utf-8'))
            app.task = task
            app.flags = (True, False, False)
            app.start_job('pipeline', task)
            job = app.pipeline_job; scopes.append(job)
            pump(lambda: app.pipeline_job is None)
            assert job.process.returncode == 0 and job.tree.closed
            logs = list((directory / 'parent-data' / 'gui_logs').glob('*pipeline*.log'))
            assert logs and '已有完整转写和粗翻' in logs[0].read_text(encoding='utf-8')
            result['passed'].append('windowless_parent_PIPE_START_stdout_log_exit_monitor')
            # Current source GUI SRT control starts the frozen SRT worker.
            with patch('studio.messagebox.askyesno', return_value=True):
                app.launch('srt')
                pump(lambda: not app.busy)
            assert task.result('output.srt').is_file()
            result['passed'].append('windowless_parent_srt_child_stdin_stdout_stderr')
            app.start_job('player', task)
            player = app.player_job; scopes.append(player)
            def playing():
                logs = list((directory / 'parent-data' / 'gui_logs').glob('*player*.log'))
                return logs and '"state": "Playing"' in logs[0].read_text(encoding='utf-8')
            pump(playing)
            assert player.process.poll() is None
            result['passed'].append('windowless_parent_actual_frozen_player_telemetry')
            # Cancel a real pipeline targeting a disposable 60s silence input.
            # It either waits on lms, starts transcribe, or exits quickly on a local
            # service error; cancel while owned process is still active.
            other = directory / 'cancel.wav'; shutil.copy2(audio, other)
            cancel_task = store.open_task(other)
            app.start_job('pipeline', cancel_task)
            job = app.pipeline_job; scopes.append(job)
            pump(lambda: job.process is not None)
            app.cancel()
            pump(lambda: app.pipeline_job is None)
            assert job.tree.closed and job.tree.active_count() == 0
            with store.task_lock(cancel_task.directory): pass
            assert player.process.poll() is None
            result['passed'].append('windowless_parent_cancel_pipeline_own_job_lock_release_player_survives')
            app.stop_player()
            pump(lambda: app.player_job is None)
            assert player.tree.closed and player.process.returncode == 0
            result['passed'].append('windowless_parent_CLOSE_pipe_player_normal_exit')
            app.close()
    except BaseException as error:
        result['error'] = repr(error)
        raise
    finally:
        for job in scopes:
            job.tree.cancel(); job.tree.finish()
            if job.process is not None: job.process.wait(timeout=5)
        (directory / 'windowless_parent.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        try: root.destroy()
        except tk.TclError: pass


def check(exe):
    directory = ROOT / 'work' / 'console_free_checks' / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True)
    assert subsystem(exe) == 2
    assert subsystem(exe.with_name('EarlySakuraWorker.exe')) == 3
    # Own copy/config ensures Shell cannot write to formal user metadata even if
    # environment inheritance differs between Windows Shell configurations.
    application = directory / 'package'
    shutil.copytree(exe.parent, application)
    data = directory / 'shell-data'; data.mkdir()
    config = json.loads((application / 'runtime_paths.json').read_text(encoding='utf-8'))
    config['data_root'] = str(data)
    (application / 'runtime_paths.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    baseline = console_windows(); new = set(); stop = threading.Event()
    def monitor():
        while not stop.wait(.05):
            new.update(console_windows() - baseline)
    thread = threading.Thread(target=monitor, daemon=True); thread.start()
    report = {'directory': str(directory), 'gui_PE_subsystem': 2, 'worker_PE_subsystem': 3,
              'human_explorer_double_click': 'not performed', 'passed': []}
    try:
        report['shell_gui_pid'] = shell_gui(application / 'EarlySakura.exe')
        report['passed'].append('ShellExecuteEx_visible_GUI_normal_close_no_console_suppression_flags')
        env = os.environ.copy()
        env.update(EARLYSAKURA_DATA_DIR=str(directory / 'parent-data'), HF_HUB_OFFLINE='1', HF_XET_CACHE=str(directory / 'xet'))
        process = subprocess.Popen([str(Path(sys.executable).with_name('pythonw.exe')), '-B', '-X', 'utf8',
                                   str(Path(__file__).absolute()), '--parent', str(directory), str(application / 'EarlySakura.exe')],
                                  cwd=directory, env=env, creationflags=subprocess.DETACHED_PROCESS)
        assert process.wait(timeout=55) == 0, directory / 'parent-data' / 'gui_logs' / 'gui_startup.log'
        parent = json.loads((directory / 'windowless_parent.json').read_text(encoding='utf-8'))
        assert 'error' not in parent, parent
        report['windowless_parent'] = parent
        assert not new, f'New visible console/Terminal handles: {new}'
        report['passed'].append('no_new_visible_console_or_Windows_Terminal_detected_during_Shell_and_workers')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        stop.set(); thread.join(3)
        report['new_visible_console_handles'] = list(new)
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('--parent', type=Path)
    args = parser.parse_args()
    if args.parent:
        windowless_parent(args.parent.absolute(), args.exe.absolute())
    else:
        check(args.exe.absolute())
