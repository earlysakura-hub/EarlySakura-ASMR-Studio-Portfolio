# EarlySakura ASMR Studio

A Windows desktop application for Japanese ASMR transcription, local Japanese-to-Chinese draft translation, bilingual subtitle preparation, and synchronized audio playback.

The interface is currently in Simplified Chinese. The application uses Tkinter/ttk and VLC, with one main window containing Library, Player, Translation Task, Queue, and Playback History tabs. Floating bilingual subtitles remain in a separate draggable, always-on-top window.

This repository contains application code and synthetic tests. It does **not** contain ASMR recordings, translated works, playback records, model weights, LM Studio, VLC, CUDA libraries, or a prebuilt executable. Use only media you are authorized to process.

## Features

- Japanese transcription with faster-whisper on an NVIDIA GPU.
- Automatic local Qwen draft translation through LM Studio, with validated partial-result recovery.
- Audio-bound task directories, content hashes, task locks, and protection against accidental output replacement.
- A serial batch queue with cancellation, failure handling, persistence, and explicit restart controls.
- A media library with custom titles, tags, favorites, draft/refined states, safe archive/import operations, and full plot-text reading.
- Selected-audio duration display, remembered list widths, responsive action buttons, and a persistent plot font preference.
- SRT generation from formal refinements, or a complete draft after confirmation; existing SRT replacement is confirmed and published safely.
- VLC playback, floating subtitles, global shortcuts, playback history, and resume playback.
- Playback and background processing of another work can run independently. Only one translation pipeline runs at a time.

Refinement remains a manual step. There is no automatic cloud refinement service, diarization, source separation, or CPU fallback workflow in the current GUI.

## Requirements

- Windows x64; Windows 10/11 is the intended environment. Windows-specific file locks, Job Objects, registry access, and process cleanup are used.
- Python **3.13 x64** with Tcl/Tk installed. The development environment was tested with Python **3.13.16**; other Python versions are not validated.
- [VLC](https://www.videolan.org/vlc/) x64 for playback. Installing `python-vlc` alone does not install VLC or `libvlc.dll`.
- An NVIDIA GPU, compatible driver, CUDA 12 cuBLAS and cuDNN 9 runtime libraries for transcription. These are external dependencies, not pip requirements or repository assets. See the [faster-whisper GPU requirements](https://github.com/SYSTRAN/faster-whisper#gpu).
- [LM Studio](https://lmstudio.ai/) and its `lms` CLI for the automatic transcription/translation pipeline.
- `ffprobe` from an FFmpeg distribution is optional for the library's quick duration display. Without it, duration is shown as `--:--:--`. Transcription itself decodes through PyAV; a system `ffmpeg` executable is not required for that decoder.

Playback and subtitle conversion do not require a loaded AI model. First-time Whisper use may download its model from Hugging Face; Qwen must be downloaded separately in LM Studio.

## Install and start

In PowerShell, from the cloned repository:

```powershell
py -3.13 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath '.\runtime_paths.json')) {
    Copy-Item -LiteralPath '.\runtime_paths.example.json' -Destination '.\runtime_paths.json' -ErrorAction Stop
}
& .\.venv\Scripts\python.exe -B -X utf8 studio.py
```

The conditional command copies the example only when `runtime_paths.json` does not exist; an existing configuration is left unchanged. There is no need to activate the virtual environment for these commands.

After setup, `启动 EarlySakura.cmd` launches the source GUI using `.venv\Scripts\pythonw.exe`. To see startup errors directly, use the Python command above. Tkinter, SQLite and unittest are part of Python; do not install similarly named pip packages.

### Configuration

Source mode reads `runtime_paths.json` beside the source files. A frozen application reads it beside the EXE. To select a different configuration, set `EARLYSAKURA_CONFIG` to its **absolute** path. Neither mode depends on the terminal's current directory for configuration lookup.

`runtime_paths.example.json` documents the supported settings:

| Setting | Purpose and default |
|---|---|
| `lm_studio_base_url` | OpenAI-compatible base URL; default `http://localhost:1234/v1`. An address without `/v1` is also accepted. |
| `lm_studio_model` | Model identifier used by both `lms` and translation requests; default `qwen/qwen3-4b-2507`. |
| `lms`, `ffprobe` | Optional absolute executable paths; otherwise discovered through PATH. |
| `vlc_directory` | Optional VLC installation directory containing `libvlc.dll` and `plugins`. Otherwise normal python-vlc discovery applies. |
| `gpu_dll_directories` | Optional list of absolute CUDA/cuDNN DLL directories, added at runtime on Windows. |
| `data_root` | Optional absolute application-data directory. Source mode otherwise uses `outputs`; frozen mode uses a valid configured directory or LocalAppData. |
| `audio_directory` | Audio picker starting directory; defaults to the user's home directory. |
| `library_root` | Initial library root for an unconfigured database; defaults to `EarlySakuraLibrary` under the user's home directory. An existing database's configured root takes precedence. |
| `legacy_source_directory` | Default source for an explicit legacy migration preview; defaults to `EarlySakuraLegacy` under the home directory. |
| `legacy_tag` | Tag applied by the legacy migration workflow; defaults to `Imported`. Existing library tags are not rewritten. |
| `legacy_srt_aliases` | Optional mapping of an audio filename to a historical SRT filename. Values are filenames, not directory paths; canonical output remains `output.srt`. |

Keep local absolute paths in the ignored configuration, not source code. Do not commit real configuration or credentials.

Optional process environment variables are listed in `.env.example`:

- `EARLYSAKURA_CONFIG`: absolute configuration file path.
- `EARLYSAKURA_DATA_DIR`: absolute application-data override; takes precedence over `data_root`.
- `LM_STUDIO_API_TOKEN`: optional token if LM Studio API authentication is enabled.

**The application does not automatically load `.env` files.** Set these variables in the shell that starts the app, or through Windows environment settings. For example, to use a disposable data directory:

```powershell
$env:EARLYSAKURA_DATA_DIR = Join-Path $PWD.Path 'work\sandbox-data'
& .\.venv\Scripts\python.exe -B -X utf8 studio.py
```

Do not use this sandbox override when you intend to open your normal library. Never paste credentials into source files, issues, logs, screenshots, or Git commands.

## LM Studio and models

### Japanese transcription

`transcribe.py` uses `WhisperModel("medium", device="cuda", compute_type="int8_float16")`, Japanese language selection, and word timestamps. The installed faster-whisper mapping resolves `medium` to [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium). Its model card identifies the converted model as MIT-licensed.

The first run can download the model to the Hugging Face cache. Weights and caches must remain outside version control. This application does not use LM Studio for Whisper.

### Chinese draft translation

The verified local model is **Qwen3-4B-Instruct-2507, Q4_K_M GGUF**:

- Download source: [lmstudio-community/Qwen3-4B-Instruct-2507-GGUF](https://huggingface.co/lmstudio-community/Qwen3-4B-Instruct-2507-GGUF).
- File: `Qwen3-4B-Instruct-2507-Q4_K_M.gguf`, approximately 2.50 GB.
- LM Studio catalog identifier used by the app: `qwen/qwen3-4b-2507`.
- Upstream model: [Qwen/Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507), whose model card states Apache-2.0. The quantized repository identifies that upstream and its quantizer, but its visible model card does not separately state a license field. Review the source terms; this repository does not redistribute either model.

Do not substitute a Thinking model or another Qwen version merely because its filename is similar. Other model IDs can be configured, but their translation behavior has not been validated.

1. Install and open LM Studio at least once. Verify `lms --help` in a new terminal, or configure its executable path. See the [official CLI documentation](https://lmstudio.ai/docs/cli).
2. Download the model above using LM Studio's model search. The CLI alternative is `lms get qwen/qwen3-4b-2507`; confirm the intended Q4_K_M variant when prompted. Do not add the download directory to this repository.
3. In LM Studio's Developer page, load the model and start the API server on port 1234. The corresponding CLI operations are:

   ```powershell
   lms load qwen/qwen3-4b-2507 --gpu max --context-length 8192
   lms server start --port 1234
   ```

4. A read-only connection check is:

   ```powershell
   Invoke-RestMethod http://localhost:1234/v1/models
   ```

   If authentication is enabled, provide the token through your local setup and `LM_STUDIO_API_TOKEN`; do not store it in the JSON example.

5. The application sends `POST /v1/chat/completions` using `requests`. It does not need an OpenAI cloud account or the OpenAI Python SDK. See [LM Studio Chat Completions](https://lmstudio.ai/docs/developer/openai-compat/chat-completions).

The **automatic pipeline** manages a local LM Studio instance: it unloads the configured Qwen when needed before Whisper, then starts/checks the server and reloads Qwen with GPU offload and an 8192-token context. Existing complete results bypass model startup. The base URL's port and CLI server port are read from the same setting. For a non-local server, automatic local process management is rejected; prepare the server yourself and use the standalone translator instead. Remote setups are not validated.

Draft generation retains the existing batch size of 10, temperature 0.2, per-request read workflow, and 600-second HTTP timeout. CLI calls have a 180-second bound so a failed service does not wait forever. No weights or inference parameters are changed by repository setup.

Some `lms` commands automatically try to wake the LM Studio background service. If that fails, open LM Studio and resolve the service issue before starting a new task. The application reports missing CLI, CLI failure/timeout, inconsistent ports, connection/authentication failure, and malformed responses without claiming an incomplete task is finished.

## Using the GUI

1. **Library:** choose a library root if necessary. Its two managed subdirectories are `待处理音频` (incoming audio) and `已处理作品` (processed works).
2. Put incoming audio files directly in `待处理音频`, then scan. Scanning registers tasks; it does not start AI inference. MP3, M4A, WAV, FLAC and OGG are accepted by the unified picker/queue; the older standalone pickers primarily list MP3/M4A/WAV.
3. **Translation Task / Queue:** select one audio or add multiple files, review the planned stages, then start. The queue processes one work at a time, Whisper followed by Qwen. Stop-after-current and cancel-current are distinct operations.
4. Completed transcription/draft results in the managed inbox are archived through the existing checked workflow. Such works are still **pending refinement** until a formal refinement is imported.
5. **Library details:** edit titles/tags/favorites, open the real audio folder, import formal refinements or full plot text, and generate SRT. Formal refinement import retains the existing automatic SRT workflow.
6. **Player:** open the selected library work or an audio task. The main player controls and floating subtitles share the same playback time. Use the subtitle offset controls when a subtitle needs an intentional time adjustment.
7. **History:** reopen a work and choose resume/from-start when an unfinished saved position exists. Resume records are saved periodically and at playback control/close events.

Switching tabs does not stop playback or background processing. File-changing operations on the same active work may be refused to protect its task lock. The library's audio duration is metadata, not a playback position. Missing audio is hidden on refresh/startup; database registration and resume metadata remain, allowing the work to reappear if the file is restored to its original path. There is no continuous filesystem watcher.

### Task files and subtitle format

Ordinary audio tasks use an `EarlySakuraTasks/<short-task-id>` directory beside the audio. SHA-256 plus normalized source path identify a task; different paths with the same filename are not silently merged. Controlled library relocation preserves the stable ID and updates references.

```text
audio folder/
  example.wav
  EarlySakuraTasks/<task-id>/
    task.json
    japanese.txt
    draft_bilingual.txt
    final_bilingual.txt       # optional human refinement
    output.srt               # optional generated subtitle
```

Transcription lines use `[HH:MM:SS,mmm --> HH:MM:SS,mmm] Japanese text`. Bilingual files contain blank-line-separated blocks:

```text
[00:00:00,000 --> 00:00:01,000]
JP: テスト。
ZH_FINAL: 测试。
```

Draft blocks use `ZH_DRAFT:` instead. `final_bilingual.txt` takes priority when valid. An invalid, incomplete or test-marked final blocks generation rather than silently falling back to draft. Complete drafts require confirmation. Existing `output.srt` replacement also requires confirmation, and new content is validated in a temporary file before replacement.

Importing old results requires an explicit audio association. Files are copied, conflicting task outputs are not silently overwritten, and incomplete append markers are not treated as completed translation. Do not edit task manifests or remove pending markers just to force a task to continue. Legacy migration requires a separately reviewed preview and explicit approval; scanning does not authorize migration or directory cleanup.

### Standalone entries

These original entries remain available, with file dialogs when their optional argument is omitted:

```powershell
& .\.venv\Scripts\python.exe transcribe.py 'path-to-audio.wav'
& .\.venv\Scripts\python.exe translate.py 'path-to-task\japanese.txt'
& .\.venv\Scripts\python.exe floating_subtitle_player.py 'path-to-audio.wav'
& .\.venv\Scripts\python.exe run_pipeline.py 'path-to-audio.wav'
& .\.venv\Scripts\python.exe make_srt.py --help
```

Use the GUI for SRT source/overwrite confirmation. Never assume an unrelated text file beside an audio file belongs to that work.

## Project layout

```text
studio.py                         Main GUI and worker dispatch
library_ui.py                     Embedded library page
media_library.py / library_files.py  Metadata, checked imports/archive/recovery
library_migration.py              Explicit legacy preview/migration API
task_store.py / batch_queue.py    Task identity, locks, serial queue
transcribe.py / translate.py      Whisper and LM Studio translation
run_pipeline.py / make_srt.py     Pipeline and checked SRT conversion
floating_subtitle_player.py       VLC engine and floating subtitle window
player_page.py / player_ipc.py    Main-window player controls and protocol
playback_history.py / playback_resume.py  History and resume
audio_duration.py / gui_preferences.py   Metadata and GUI preferences
app_runtime.py / windows_job.py   Runtime paths and owned process cleanup
exe_entry.py / EarlySakura.spec / build_exe.py  Optional Windows build
tests/                            Synthetic unit tests and opt-in runtime checks
docs/TESTING.md / docs/BUILDING.md
```

No runtime resource directory is missing: Tcl/Tk comes with Python, fonts come from Windows, and required package assets come from the installed distributions. The EXE spec explicitly includes the raw transcription, translation and floating-player scripts used by workers.

## Troubleshooting

| Symptom | Check |
|---|---|
| GUI does not start | Run `studio.py` from a terminal; verify Python x64 and Tcl/Tk. Check JSON syntax and `schema_version: 1`. |
| `libvlc.dll` not found | Install matching x64 VLC, or set `vlc_directory` to the actual installation directory. |
| CUDA/cuDNN DLL error | Verify the driver, CUDA 12/cuDNN 9 libraries and DLL paths for the pinned CTranslate2 version. Do not solve this by silently changing compute type or model. |
| LM Studio CLI not found | Open LM Studio once, check `lms --help`, PATH or the configured `lms` executable. |
| CLI wake/start timeout | Open LM Studio and check its background service. No successful inference is implied by a CLI installation. |
| API connection / authentication / model error | Check Developer server, configured port, model ID, and optional API token. Keep both pipeline and translator on the same configuration. |
| Final/draft count differs from Japanese text | Verify work ownership, version, timestamps and completeness; do not add blank blocks or delete lines just to match counts. |
| A task is locked | Close its playback or let/cancel its processing through the GUI. A remaining `.task.lock` file alone is not proof of a held OS lock. |
| A draft has a pending marker | An append may have been interrupted. Preserve results and inspect them; do not claim it is complete. |
| Deleted audio remains visible in an old window | Restart the updated source version and refresh the library. An older EXE does not automatically pick up source changes. |
| Another queue window owns the queue | Do not run multiple queue writers against the same data directory. Use a separate sandbox for testing. |

## Tests and optional builds

Run synthetic regression tests with:

```powershell
& .\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p 'test_*.py'
```

Tests create their own small data and harmless processes; they do not prove long-audio GPU inference, audible playback quality, or human Windows Terminal behavior. Explicit GUI/VLC/EXE checks are opt-in. See [testing](docs/TESTING.md) and [Windows builds](docs/BUILDING.md).

## Current limitations and repository policy

- Windows only; no promise of support for other operating systems or untested Python/package versions.
- GPU transcription is the configured workflow. Qwen remains an external LM Studio model/service; neither model is embedded in the app.
- Only one translation job runs at a time. Playback of another work can continue, subject to available CPU/GPU/memory resources.
- Refinement is manual; merged/split final blocks may fail the current strict Japanese/final alignment checks.
- Arbitrary external moves/renames are not automatically relinked. Restore the path or use an explicitly reviewed migration.
- Historical machine-specific checks/reports, local data, models and build products are deliberately excluded from Git. Their exclusion is not a local deletion operation.
- A source update does not rebuild installed EXEs. No new executable is provided by this publication.
- No project open-source license has been selected. Do not infer a project license from dependency/model licenses.
