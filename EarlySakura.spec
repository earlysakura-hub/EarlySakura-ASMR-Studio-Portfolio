# Explicit runtime packages and script resources. Never collect project outputs/models.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

root = Path(SPECPATH)
datas = [(str(root / name), '.') for name in ('transcribe.py', 'translate.py', 'floating_subtitle_player.py')]
binaries = []
hiddenimports = ['studio', 'task_store', 'batch_queue', 'playback_history', 'gui_preferences', 'run_pipeline', 'make_srt', 'packaging_checks', 'playback_resume', 'media_library', 'library_files', 'library_ui', 'library_migration', 'player_page', 'player_ipc', 'pynput.keyboard._win32', 'pynput.mouse._win32']
for package in ('faster_whisper', 'onnxruntime', 'tokenizers', 'av', 'huggingface_hub', 'hf_xet'):
    datas += collect_data_files(package)
    binaries += collect_dynamic_libs(package)
    hiddenimports += collect_submodules(package)
datas += collect_data_files('ctranslate2')
binaries += collect_dynamic_libs('ctranslate2')
hiddenimports += ['ctranslate2', 'ctranslate2._ext', 'ctranslate2.models', 'vlc', 'requests', 'pynput']
for distribution in ('faster-whisper', 'ctranslate2', 'av', 'onnxruntime', 'huggingface_hub', 'tokenizers',
                     'hf-xet', 'numpy', 'requests', 'python-vlc', 'pynput'):
    datas += copy_metadata(distribution)
a = Analysis([str(root / 'exe_entry.py')], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
# GUI has no console from process creation; worker retains real redirected stdio.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='EarlySakura', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name='EarlySakuraWorker', debug=False,
             bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, worker, a.binaries, a.datas, strip=False, upx=False, name='EarlySakura')
