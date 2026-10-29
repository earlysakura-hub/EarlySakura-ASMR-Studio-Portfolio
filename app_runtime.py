"""Source/frozen paths and explicit subprocess dispatch (no business changes)."""
import ctypes
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from urllib.parse import urlsplit

BUILD_LABEL = "EarlySakura ASMR Studio"
SOURCE_ROOT = Path(__file__).absolute().parent
_dll_handles = []
_configured = False


def frozen():
    return bool(getattr(sys, "frozen", False))


def configuration():
    override = os.environ.get("EARLYSAKURA_CONFIG")
    if override and not Path(override).is_absolute():
        raise ValueError("EARLYSAKURA_CONFIG 必须是绝对路径。")
    path = Path(override) if override else (Path(sys.executable).parent if frozen() else SOURCE_ROOT) / "runtime_paths.json"
    if not path.exists():
        if override: raise ValueError("指定的运行配置文件不存在；未修改用户数据。")
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("runtime_paths.json 格式或版本无效；未修改用户数据。")
    return data


def audio_directory():
    return str(configuration().get("audio_directory") or Path.home())


def lm_studio_settings():
    config = configuration()
    base = config.get("lm_studio_base_url") or "http://localhost:1234/v1"
    model = config.get("lm_studio_model") or "qwen/qwen3-4b-2507"
    if not isinstance(base, str) or not isinstance(model, str) or not model.strip():
        raise ValueError("LM Studio 地址和模型标识必须是非空文本。")
    parsed = urlsplit(base.rstrip('/'))
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/v1')):
        raise ValueError("LM Studio 地址应为 http(s)://主机:端口/v1，不得包含凭据、查询或其他路径。")
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return {'base_url': base.rstrip('/') + ('/v1' if not parsed.path else ''),
            'model': model.strip(), 'port': port, 'host': parsed.hostname}


def lm_studio_headers():
    token = os.environ.get('LM_STUDIO_API_TOKEN')
    return {'Authorization': 'Bearer ' + token} if token else {}


def data_root(source_root=None):
    override = os.environ.get("EARLYSAKURA_DATA_DIR")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError("EARLYSAKURA_DATA_DIR 必须是绝对路径。")
        return path
    if not frozen():
        if source_root is None or Path(source_root) == SOURCE_ROOT:
            configured = configuration().get("data_root")
            if configured:
                if not Path(configured).is_absolute(): raise ValueError("data_root 必须是绝对路径。")
                return Path(configured)
        return Path(source_root or SOURCE_ROOT) / "outputs"
    old = configuration().get("data_root")
    if old and Path(old).is_absolute() and Path(old).is_dir():
        return Path(old)  # Same-machine test build reads the existing data in place.
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ValueError("无法找到可写用户目录，请配置 EARLYSAKURA_DATA_DIR。")
    return Path(local) / "EarlySakura" / "outputs"


def resource(name):
    return SOURCE_ROOT / name


def worker_command(mode, argument, gated=False, source_root=None):
    options = [*(["--await-job"] if gated else []), "--worker", mode, str(argument)]
    if frozen():
        return [str(worker_executable()), *options]
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    return [str(python), "-B", "-u", "-X", "utf8", str(Path(source_root or SOURCE_ROOT) / "studio.py"), *options]


def worker_executable():
    path = Path(sys.executable).with_name("EarlySakuraWorker.exe")
    if not path.is_file():
        raise FileNotFoundError("缺少 EarlySakuraWorker.exe，请保留完整测试包；未启动后台任务。")
    return path


def ensure_gui_streams():
    """Windowed bootloader sets stdio to None; never reuse these for workers."""
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")
    if sys.stdout is None or sys.stderr is None:
        try:
            folder = data_root() / "gui_logs"
            folder.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError):
            folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "EarlySakura" / "outputs"
            folder.mkdir(parents=True, exist_ok=True)
        output = (folder / "gui_startup.log").open("a", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdout is None:
            sys.stdout = output
        if sys.stderr is None:
            sys.stderr = output


def script_command(name, argument):
    modes = {"transcribe.py": "transcribe", "translate.py": "translate"}
    if frozen():
        return worker_command(modes[name], argument)
    return [sys.executable, str(resource(name)), str(argument)]


def run_script_process(name, argument):
    # Substages never read interactive stdin. stdout/stderr inherit the worker's
    # real pipes so native model progress and exceptions reach the GUI/log reader.
    return subprocess.run(script_command(name, argument), check=True, stdin=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def run_script(name, argument, extra=()):
    previous = sys.argv
    try:
        sys.argv = [str(resource(name)), str(argument), *extra]
        runpy.run_path(str(resource(name)), run_name="__main__")
    finally:
        sys.argv = previous


def external_program(name):
    import shutil
    path = configuration().get(name)
    if path and Path(path).is_absolute() and Path(path).is_file():
        return str(path)
    return shutil.which(name)


def run_external(command, **kwargs):
    """LM Studio is an external application, not a bundled Python worker."""
    if frozen() and os.name == "nt":
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return subprocess.run(command, **kwargs)
    finally:
        if frozen() and os.name == "nt":
            ctypes.windll.kernel32.SetDllDirectoryW(str(getattr(sys, "_MEIPASS", SOURCE_ROOT)))


def setup_runtime():
    global _configured
    if _configured:
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    config = configuration()
    vlc = config.get("vlc_directory")
    if vlc and (Path(vlc) / "libvlc.dll").is_file():
        os.environ["PYTHON_VLC_LIB_PATH"] = str(Path(vlc) / "libvlc.dll")
        os.environ["PYTHON_VLC_MODULE_PATH"] = str(Path(vlc) / "plugins")
        os.environ["VLC_PLUGIN_PATH"] = str(Path(vlc) / "plugins")
    directories = config.get("gpu_dll_directories", [])
    if not isinstance(directories, list) or any(not isinstance(item, str) for item in directories):
        raise ValueError("GPU DLL 目录配置无效。")
    for directory in directories:
        if Path(directory).is_absolute() and Path(directory).is_dir():
            _dll_handles.append(os.add_dll_directory(directory))
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
    _configured = True


