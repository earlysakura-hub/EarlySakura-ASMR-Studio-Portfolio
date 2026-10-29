# Testing and data safety

Use Windows x64 and the project's virtual environment. The default automated suite is:

```powershell
& .\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p 'test_*.py'
```

The suite uses generated text/audio, mocks and disposable directories under `work`. Windows process tests start harmless fixture workers to test Job Object cancellation and lock release. `cancel_worker_fixture.py`, `parallel_worker_fixture.py` and `player_runtime_fixture.py` are test infrastructure, not abandoned application copies.

The regression areas include task identity/import/output protection, SRT selection and atomic replacement, serial queues, independent playback/processing state, history/resume persistence, media metadata/archive/recovery, GUI preferences, duration/cache/races, missing-audio filtering, worker dispatch and build-path validation.

## Opt-in native checks

These create their own isolated library and settings and can display temporary Tk windows:

```powershell
& .\.venv\Scripts\python.exe -B -X utf8 tests\check_library_actions.py
& .\.venv\Scripts\python.exe -B -X utf8 tests\check_library_columns.py
& .\.venv\Scripts\python.exe -B -X utf8 tests\check_library_duration.py
& .\.venv\Scripts\python.exe -B -X utf8 tests\check_plot_runtime.py
& .\.venv\Scripts\python.exe -B -X utf8 tests\check_tabs_runtime.py
```

Read each other `check_*.py` script before running it: VLC checks need installed VLC; EXE checks need a separately built package and may perform additional work. In particular, `check_exe.py` contains explicitly invoked real-transcription paths. Do not treat all files named `check_*` as safe inference-free smoke tests.

For a disposable main GUI, set `EARLYSAKURA_DATA_DIR` to an absolute test directory before launch. A private configuration can also be selected with `EARLYSAKURA_CONFIG`. Never use a production library to test deletion, overwrite, migration or recovery.

## Manual acceptance

- Start with one formal main window and the five fixed tabs; changing tabs preserves selection, playback and active processing.
- Test narrow/wide library panes: all four list columns and full action buttons remain visible; plot text gets the remaining height.
- Adjust list widths and plot font, close and reopen, then verify preference recovery. Test without overwriting real preferences.
- Play a short authorized file with synthetic subtitles; check pause, seek, volume, overlay visibility, shortcut handling and audio/subtitle synchronization.
- Save/reopen progress and check resume/from-start/cancel. Normal end should clear the resume suggestion; abnormal exit can only recover the last saved position.
- In isolated data, check cancelled processing leaves complete stages, clears the owned process tree and releases the task lock.
- Only with explicit authorization, run a short real Whisper/Qwen job. Verify the service/model, output identity and actual translation; unit mocks cannot establish inference success.

Current-model service availability, long-running GPU memory behavior, actual audio quality, arbitrary desktop scaling and human Explorer/Windows Terminal observations must be reported separately from automated test counts.

Three old checks against a particular machine's real databases/directories are intentionally not distributed. The library itself does not require them. Original local reports and safety snapshots are also not repository fixtures.
