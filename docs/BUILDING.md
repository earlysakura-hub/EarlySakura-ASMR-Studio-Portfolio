# Optional Windows onedir build

Source operation is the primary setup documented in the README. Build only after configuring and testing the source version. Model weights, LM Studio, VLC and CUDA/cuDNN are external; a Python EXE is not a self-contained GPU/model installation.

Install the separate pinned build tools without replacing the runtime environment:

```powershell
& .\.venv\Scripts\python.exe -m pip install --target work\packaging_tools -r packaging-requirements.txt
& .\.venv\Scripts\python.exe -B -X utf8 build_exe.py
```

`build_exe.cmd` invokes the same builder. Optional `--output-name` selects a new safe directory name under `dist`; an existing directory is refused. The builder does not clean old builds or personal files.

The PyInstaller spec produces an onedir application containing:

- `EarlySakura.exe`: Windows GUI subsystem, no console.
- `EarlySakuraWorker.exe`: console-capable helper with explicit `--worker` modes and redirected standard streams.
- `_internal`: Python/package resources, including the raw transcription, translation and floating-player scripts needed by runpy-based workers.
- `runtime_paths.json`: generated machine-specific configuration.
- `BUILDING.md`: this guide.

Keep the **entire** directory when testing a build. Do not distribute just one EXE. GUI-to-worker dispatch, START gating, Windows Job Objects, stream handling, task locks and cancellation must remain intact; hiding/killing Windows Terminal is not a substitute for GUI-subsystem startup.

Build logs, source hashes and environment details are saved under a unique `build` directory. Model/GUI/user data are never collected by the spec. The generated configuration can contain local paths and must be reviewed before sharing any executable package; `dist`, `build`, `work` and real configuration are ignored by Git.

Read the package check scripts' `--help` and source before use. In an isolated package/data directory, verify:

1. Explorer startup and normal GUI shutdown; no persistent extra console window.
2. All tabs, full library controls, selected duration, preference persistence and plot display.
3. A player worker loads a short authorized audio/SRT, responds to controls, saves resume, and exits cleanly.
4. A synthetic pipeline handles logs, failure and cancellation without affecting a separate player.
5. Missing external dependencies produce actionable errors and no false completion.
6. A separately authorized short real transcription/translation test, if required for release acceptance.

An older executable does not use newer source files automatically. Passing unit tests is not a claim that a newly built EXE or a different Windows computer has passed manual acceptance. Do not commit binaries, DLL bundles, build caches, generated runtime paths, credentials or model weights to this source repository.
