"""Repeatable onedir builder. Output/cache/logs are unique; never clean user data."""
from datetime import datetime
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import winreg
from app_runtime import configuration

ROOT = Path(__file__).absolute().parent


def output_directory(name=None):
    name = name or ('phase2-build-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', name):
        raise ValueError('构建目录名只能包含英文字母、数字、下划线和连字符。')
    output = ROOT / 'dist' / name
    if output.exists():
        raise FileExistsError(f'构建目录已存在，未覆盖或清理：{output}')
    return output


def build(name=None):
    tools = ROOT / 'work' / 'packaging_tools'
    if not (tools / 'PyInstaller').is_dir():
        raise SystemExit('先执行 .venv\\Scripts\\python.exe -m pip install --target work\\packaging_tools -r packaging-requirements.txt')
    output = output_directory(name)
    cache = ROOT / 'build' / (output.name + '-' + uuid.uuid4().hex[:6])
    output.mkdir(parents=True); cache.mkdir(parents=True)
    env = os.environ.copy()
    env.update(PYTHONPATH=str(tools), PYTHONUTF8='1', PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
    env['HF_XET_CACHE'] = str(cache / 'xet')
    env['PYINSTALLER_CONFIG_DIR'] = str(cache / 'pyinstaller_config')
    command = [sys.executable, '-B', '-m', 'PyInstaller', '--distpath', str(output), '--workpath', str(cache), str(ROOT / 'EarlySakura.spec')]
    print('构建输出：' + str(output), flush=True)
    with (cache / 'build.log').open('x', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace')
        for line in process.stdout:
            log.write(line); log.flush()
        code = process.wait()
        process.stdout.close()
    if code:
        raise SystemExit(f'构建失败（{code}），日志：{cache / "build.log"}')
    application = output / 'EarlySakura'
    vlc = None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r'SOFTWARE\VideoLAN\VLC') as key:
                vlc = winreg.QueryValueEx(key, 'InstallDir')[0]
                break
        except OSError:
            pass
    gpu = [path for path in os.environ.get('PATH', '').split(os.pathsep)
           if path and Path(path).is_dir() and any(word in path.lower() for word in ('cuda', 'cudnn'))]
    config = dict(configuration())
    config.update({'schema_version': 1, 'data_root': str(ROOT / 'outputs'), 'vlc_directory': vlc,
              'gpu_dll_directories': gpu, 'lms': shutil.which('lms'), 'ffprobe': shutil.which('ffprobe')}
    )
    (application / 'runtime_paths.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    versions = {d.metadata['Name']: d.version for d in metadata.distributions()}
    tool_versions = {d.metadata['Name']: d.version for d in metadata.distributions(path=[str(tools)])}
    sources = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in [*ROOT.glob('*.py'), ROOT / 'EarlySakura.spec']}
    (cache / 'build_environment.json').write_text(json.dumps({'python': sys.version, 'packages': versions,
        'build_tools': tool_versions, 'build_command': command, 'external_runtime_paths': config}, ensure_ascii=False, indent=2), encoding='utf-8')
    (cache / 'source_hashes.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.copy2(ROOT / 'docs' / 'BUILDING.md', application / 'BUILDING.md')
    (ROOT / 'build' / 'latest_build.json').write_text(json.dumps({'directory': str(output), 'exe': str(application / 'EarlySakura.exe'),
        'worker': str(application / 'EarlySakuraWorker.exe'), 'build_directory': str(cache)}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('构建完成：' + str(application / 'EarlySakura.exe'), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='独立 onedir 构建，不覆盖旧版本。')
    parser.add_argument('--output-name', help='dist 下的新目录名；已存在时拒绝构建。')
    build(parser.parse_args().output_name)
