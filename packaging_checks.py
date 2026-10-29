"""Read-only build diagnostics; imports runtime libraries, never loads a model."""
import importlib
import importlib.metadata as metadata
import json
from pathlib import Path
import sys

import app_runtime as runtime


def diagnose():
    result = {"build": runtime.BUILD_LABEL, "frozen": runtime.frozen(), "python": sys.version,
              "executable": sys.executable, "resources": str(runtime.SOURCE_ROOT),
              "data_root": str(runtime.data_root()), "libraries": {}, "scripts": {}}
    for name in ("tkinter", "sqlite3", "requests", "vlc", "pynput.keyboard", "av", "ctranslate2",
                 "faster_whisper", "onnxruntime", "tokenizers", "huggingface_hub",
                 "studio", "task_store", "batch_queue", "playback_history", "playback_resume",
                 "media_library", "library_files", "library_ui", "library_migration",
                 "player_page", "player_ipc", "gui_preferences", "make_srt", "run_pipeline"):
        try:
            module = importlib.import_module(name)
            result["libraries"][name] = {"ok": True, "version": str(getattr(module, "__version__", "stdlib"))}
        except (Exception, SystemExit) as error:
            result["libraries"][name] = {"ok": False, "error": str(error)}
    if result["libraries"]["ctranslate2"]["ok"]:
        import ctranslate2
        result["cuda_devices"] = ctranslate2.get_cuda_device_count()
        result["cuda_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cuda")) if result["cuda_devices"] else []
    for name in ("transcribe.py", "translate.py", "floating_subtitle_player.py"):
        result["scripts"][name] = runtime.resource(name).is_file()
    try:
        import requests
        response = requests.get(runtime.lm_studio_settings()['base_url'] + '/models', timeout=3, headers=runtime.lm_studio_headers())
        result["qwen_service"] = {"status": response.status_code,
                                  "models": [row.get("id") for row in response.json().get("data", [])]}
    except Exception as error:
        result["qwen_service"] = {"error": str(error)}
    result["external_programs"] = {name: runtime.external_program(name) for name in ("lms", "ffprobe")}
    return result
