"""Recoverable library file operations. No inference and no recursive deletion."""
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

import task_store as tasks
from media_library import LibraryError, LibraryStore, now


def atomic_json(path, data):
    path = Path(path)
    temporary = tasks.partial_path(path)
    with temporary.open('x', encoding='utf-8') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write('\n'); output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)


def verified_copy(source, destination):
    source, destination = tasks._plain_path(Path(source)), tasks._plain_path(Path(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = tasks.file_hash(source)
    with source.open('rb') as incoming, destination.open('xb') as output:
        shutil.copyfileobj(incoming, output, 1024 * 1024)
        output.flush(); os.fsync(output.fileno())
    if tasks.file_hash(destination) != expected or tasks.file_hash(source) != expected:
        raise LibraryError(f'复制期间文件变化或校验失败，原文件保留：{source}')
    return expected


class SealedAudio:
    """Exclusive read+delete handle: refuse active readers/writers (including VLC).

    Retain this exact HANDLE through copy/commit; mark deletion only after all
    copies and metadata are committed. Windows releases it on abnormal exit.
    """
    def __init__(self, path):
        self.path = tasks._plain_path(tasks.absolute_path(path))
        self.stream = None

    def __enter__(self):
        import msvcrt
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        self.kernel.CreateFileW.restype = w.HANDLE
        self.kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        path = str(self.path)
        path = '\\\\?\\UNC\\' + path[2:] if path.startswith('\\\\') else '\\\\?\\' + path
        handle = self.kernel.CreateFileW(path, 0x80000000 | 0x10000, 0, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise LibraryError(f'音频正在使用、只读或不能安全移动，未归档：{self.path}（{ctypes.WinError(ctypes.get_last_error())}）')
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        self.stream = os.fdopen(descriptor, 'rb')
        return self

    def copy(self, destination, expected):
        self.stream.seek(0)
        digest = hashlib.sha256()
        with Path(destination).open('xb') as output:
            while chunk := self.stream.read(1024 * 1024):
                digest.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        if digest.hexdigest() != expected or tasks.file_hash(destination) != expected:
            raise LibraryError('音频复制校验失败，未删除原音频。')

    def delete(self, expected):
        import msvcrt
        self.stream.seek(0)
        if hashlib.file_digest(self.stream, 'sha256').hexdigest() != expected:
            raise LibraryError('原音频校验变化，未清理。')
        class Disposition(ctypes.Structure):
            _fields_ = [('DeleteFile', w.BOOL)]
        value = Disposition(True)
        handle = w.HANDLE(msvcrt.get_osfhandle(self.stream.fileno()))
        if not self.kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(value), ctypes.sizeof(value)):
            raise LibraryError(f'已提交归档，但源音频清理未完成：{ctypes.WinError(ctypes.get_last_error())}')

    def __exit__(self, *args):
        if self.stream: self.stream.close()


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .') or '作品'
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name, re.I): name = '_' + name
    return name


def relocated_manifest(task, destination, work_id):
    manifest = json.loads((task.directory / 'task.json').read_text(encoding='utf-8'))
    identity = dict(task.identity)
    identity['origin_normalized_path'] = identity.get('origin_normalized_path', identity['normalized_path'])
    identity['path'] = str(destination / Path(identity['path']).name)
    identity['normalized_path'] = os.path.normcase(identity['path'])
    manifest.update(audio=identity, library_work_id=work_id)
    manifest.pop('redirect_to', None)
    return manifest


def archive(task, library=None, *, legacy_sources=None, confirmed_refined=False, work_id=None):
    library = library or LibraryStore()
    previous = library.get(task_id=task.identity['task_id'])
    if previous and previous['archive_state'] == 'archived':
        return tasks.load_task(previous['task_directory'])
    if previous and any(op['work_id'] == previous['work_id'] and op['kind'] == 'archive' for op in library.pending_operations()):
        raise LibraryError('该作品有未完成归档，请先恢复中断操作，避免重复复制。')
    # Only the inbox is automatically movable. Legacy calls must provide an
    # explicit source map and confirmed human refinement ownership.
    if legacy_sources is None and not library.in_inbox(task.identity['path']): return task
    if legacy_sources is not None and not confirmed_refined: raise LibraryError('旧作品必须先确认迁移预览。')
    work_id = library.register(task, work_id=work_id)
    with tasks.task_lock(task.directory), SealedAudio(task.identity['path']) as audio:
        if legacy_sources is None:
            done, total = tasks.check_draft_progress(task.result('japanese.txt'), task.result('draft_bilingual.txt'))
            if not total or done != total: raise LibraryError('转写与粗翻未完整完成，未归档。')
        else:
            if not all(Path(legacy_sources[name]).is_file() for name in ('final_bilingual.txt', 'output.srt')):
                raise LibraryError('旧作品缺少确认的精修或 SRT，未迁移。')
        parent = tasks._plain_path(library.root() / '已处理作品')
        parent.mkdir(parents=True, exist_ok=True)
        name = safe_name(Path(task.identity['path']).stem)
        destination = parent / name
        if destination.exists():
            destination = parent / (name + '-' + work_id[:8])
        if destination.exists(): raise LibraryError(f'归档目标冲突，未覆盖：{destination}')
        op_id = uuid.uuid4().hex
        stage = parent / ('.incoming-' + op_id)
        stage.mkdir()
        manifest = relocated_manifest(task, destination, work_id)
        sources = {}
        for source in task.directory.rglob('*'):
            tasks._plain_path(source)
            if source.is_file() and source.name not in ('.task.lock', 'task.json'):
                sources[str(source.relative_to(task.directory))] = source
        # Explicit legacy sources override only staged canonical files. Other
        # task versions are preserved in originals/ rather than overwritten.
        if legacy_sources:
            for name, source in legacy_sources.items():
                if name in sources and tasks.file_hash(sources[name]) != tasks.file_hash(source):
                    sources['originals/task/' + name] = sources[name]
                sources[name] = Path(source)
        payload = {'op_id': op_id, 'work_id': work_id, 'kind': 'archive', 'state': 'copying',
                   'source_audio': task.identity['path'], 'sha256': task.identity['sha256'],
                   'source_directory': str(task.directory), 'destination': str(destination), 'stage': str(stage),
                   'original_manifest': json.loads((task.directory / 'task.json').read_text(encoding='utf-8')),
                   'manifest': manifest, 'refined': confirmed_refined, 'files': {}, 'source_files': {}}
        journal = library.database.parent / 'library_operations' / (op_id + '.json')
        journal.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(journal, payload)
        library.operation(op_id, work_id, 'archive', 'copying', journal)
        library.update(work_id, auto_state='completed', archive_state='archiving')
        try:
            audio.copy(stage / Path(task.identity['path']).name, task.identity['sha256'])
            payload['files'][Path(task.identity['path']).name] = task.identity['sha256']
            for relative, source in sources.items():
                if not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
                    raise LibraryError('无效归档相对路径。')
                if (stage / relative).exists(): raise LibraryError('归档内部文件名冲突。')
                payload['files'][relative] = verified_copy(source, stage / relative)
                payload['source_files'][str(source)] = payload['files'][relative]
            # The original manifest is itself kept in the operation journal.
            (stage / 'task.json').write_bytes(tasks._json_bytes(manifest))
            payload['files']['task.json'] = tasks.file_hash(stage / 'task.json')
            atomic_json(journal, payload)
            stage.rename(destination)  # Windows no-clobber, on destination volume.
            payload['state'] = 'published'; atomic_json(journal, payload)
            library.operation(op_id, work_id, 'archive', 'published', journal)
            moved = tasks.Task(destination, manifest['audio'])
            with tasks.task_lock(destination):
                library.relocate(work_id, moved, refined=confirmed_refined)
                payload['state'] = 'committed'; atomic_json(journal, payload)
                library.operation(op_id, work_id, 'archive', 'committed', journal)
                _cleanup_archive(payload, journal, library, audio)
            return moved
        except Exception as error:
            if payload['state'] == 'copying':
                library.update(work_id, archive_state='failed', detail=str(error))
            else:
                library.update(work_id, detail='归档需要恢复：' + str(error))
            raise


def _verify_archive(payload):
    destination = tasks._plain_path(tasks.absolute_path(payload['destination']))
    if not payload['files'] or 'task.json' not in payload['files']:
        raise LibraryError('归档清单不完整，停止恢复。')
    for relative, digest in payload['files'].items():
        if not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise LibraryError('归档清单相对路径无效，停止恢复。')
        if tasks.file_hash(tasks._plain_path(destination / relative)) != digest:
            raise LibraryError('归档校验变化，停止恢复或清理。')
    task = tasks.load_task(destination)
    if task.identity['task_id'] != payload['manifest']['audio']['task_id']:
        raise LibraryError('归档任务身份变化，停止恢复。')
    return task


def _cleanup_archive(payload, journal, library, sealed=None):
    # On recovery, establish the same exclusive lease before touching ANY source.
    if sealed is None and Path(payload['source_audio']).exists():
        with SealedAudio(payload['source_audio']) as audio:
            return _cleanup_archive(payload, journal, library, audio)
    _verify_archive(payload)
    source_directory = Path(payload['source_directory'])
    old_manifest = payload['original_manifest']
    current = json.loads((source_directory / 'task.json').read_text(encoding='utf-8'))
    alias = dict(old_manifest, redirect_to=payload['destination'])
    if current not in (old_manifest, alias): raise LibraryError('原任务清单被修改，未清理源文件。')
    atomic_json(source_directory / 'task.json', alias)
    # Never delete unknown files or directories. Changed originals are retained.
    for filename, digest in payload['source_files'].items():
        source = tasks._plain_path(Path(filename))
        if not (source.is_relative_to(source_directory) or source.is_relative_to(Path(payload['source_audio']).parent)):
            raise LibraryError('源清理路径不属于该作品，停止清理。')
        if source == source_directory / 'task.json' or source.name == '.task.lock': continue
        if source.exists():
            if tasks.file_hash(source) != digest: raise LibraryError(f'原文件已变化，保留等待检查：{source}')
            source.unlink()
    if Path(payload['source_audio']).exists():
        if sealed is not None:
            sealed.delete(payload['sha256'])
        else:
            with SealedAudio(payload['source_audio']) as audio:
                audio.delete(payload['sha256'])
    payload['state'] = 'done'; atomic_json(journal, payload)
    library.operation(payload['op_id'], payload['work_id'], 'archive', 'done', journal)


def archive_if_ready(task, library=None):
    library = library or LibraryStore()
    if not library.in_inbox(task.identity['path']): return task
    return archive(task, library)


def recover_archive(op_id, library=None):
    library = library or LibraryStore()
    operation = next((op for op in library.pending_operations() if op['op_id'] == op_id), None)
    if not operation or operation['kind'] != 'archive': raise LibraryError('没有可恢复的归档操作。')
    journal = Path(operation['journal']); payload = json.loads(journal.read_text(encoding='utf-8'))
    destination = Path(payload['destination'])
    if destination.parent != library.root() / '已处理作品': raise LibraryError('目标不在当前媒体库中，停止恢复。')
    if payload['state'] == 'copying' and not destination.exists():
        # Nothing was published: original files remain. A future retry gets a new
        # stage; this incomplete stage is retained for diagnosis, never claimed.
        payload['state'] = 'rolled_back'; atomic_json(journal, payload)
        library.operation(op_id, payload['work_id'], 'archive', 'rolled_back', journal)
        library.update(payload['work_id'], archive_state='failed', detail='复制阶段中断，原文件保留，可重新归档。')
        return None
    moved = _verify_archive(payload)
    work = library.get(payload['work_id'])
    with tasks.task_lock(Path(payload['source_directory'])), tasks.task_lock(moved.directory):
        if work['archive_state'] != 'archived': library.relocate(payload['work_id'], moved, refined=payload['refined'])
        _cleanup_archive(payload, journal, library)
    return moved


def _restore_import(payload):
    directory = Path(payload['directory'])
    for name, info in payload['files'].items():
        target = directory / name
        current = tasks.file_hash(target) if target.exists() else None
        if current == info['before']: continue
        if current != info['after']: raise LibraryError('导入目标被外部修改，停止自动回滚，备份保留。')
        if info['before'] is None:
            target.unlink()  # Only the new file this operation itself published.
        else:
            temporary = tasks.partial_path(target)
            verified_copy(Path(payload['backup']) / name, temporary)
            os.replace(temporary, target)


def import_refinement(work_id, final=None, plot=None, *, confirmed=False, replace=False, library=None):
    if not confirmed: raise LibraryError('请先确认精修/剧情属于选中的作品。')
    library = library or LibraryStore(); work = library.get(work_id)
    if not work: raise LibraryError('作品不存在。')
    task = tasks.load_task(work['task_directory'])
    inputs = {}
    if final is not None: inputs['final_bilingual.txt'] = tasks._plain_path(tasks.absolute_path(final))
    if plot is not None: inputs['full_plot_summary.txt'] = tasks._plain_path(tasks.absolute_path(plot))
    if not inputs: raise LibraryError('没有选择导入文件。')
    for name, path in inputs.items():
        if path.name != name: raise LibraryError('请使用 final_bilingual.txt 或 full_plot_summary.txt 标准文件名。')
        if not path.read_text(encoding='utf-8-sig').strip(): raise LibraryError('导入文件为空。')
    with tasks.task_lock(task.directory):
        if final is not None and task.result('output.srt').exists() and not replace:
            raise LibraryError('已有 output.srt，需要确认替换；原字幕保留。')
        for name in inputs:
            if (task.directory / name).exists() and not replace:
                raise LibraryError(f'已有 {name}，需要明确确认替换；原文件保留。')
        op_id = uuid.uuid4().hex
        operation_folder = task.directory / '.library_imports' / op_id
        incoming, backup = operation_folder / 'incoming', operation_folder / 'backup'
        incoming.mkdir(parents=True); backup.mkdir()
        for name, path in inputs.items(): verified_copy(path, incoming / name)
        if final is not None:
            from make_srt import prepare_srt, generate_srt
            if task.result('japanese.txt').exists(): verified_copy(task.result('japanese.txt'), incoming / 'japanese.txt')
            candidate = tasks.Task(incoming, task.identity)
            generate_srt(candidate, prepare_srt(candidate))  # Full validation before live writes.
        names = [*inputs, *(['output.srt'] if final is not None else [])]
        payload = {'op_id': op_id, 'work_id': work_id, 'kind': 'import', 'state': 'prepared',
                   'directory': str(task.directory), 'backup': str(backup), 'files': {},
                   'old_refinement': work['refinement'], 'refinement': 'refined' if final is not None else work['refinement']}
        for name in names:
            target = task.directory / name
            before = verified_copy(target, backup / name) if target.exists() else None
            payload['files'][name] = {'before': before, 'after': tasks.file_hash(incoming / name)}
        journal = operation_folder / 'journal.json'; atomic_json(journal, payload)
        library.operation(op_id, work_id, 'import', 'prepared', journal)
        try:
            for name, info in payload['files'].items():
                target = task.directory / name
                if (tasks.file_hash(target) if target.exists() else None) != info['before']:
                    raise LibraryError('原字幕在导入期间改变，未覆盖。')
                temporary = tasks.partial_path(target)
                verified_copy(incoming / name, temporary)
                os.replace(temporary, target)
            library.update(work_id, refinement=payload['refinement'])
            payload['state'] = 'done'; atomic_json(journal, payload)
            library.operation(op_id, work_id, 'import', 'done', journal)
        except Exception:
            _restore_import(payload)
            library.update(work_id, refinement=payload['old_refinement'])
            payload['state'] = 'rolled_back'; atomic_json(journal, payload)
            library.operation(op_id, work_id, 'import', 'rolled_back', journal)
            raise
    return library.get(work_id)


def recover_import(op_id, library=None):
    library = library or LibraryStore()
    op = next((item for item in library.pending_operations() if item['op_id'] == op_id), None)
    if not op or op['kind'] != 'import': raise LibraryError('没有可恢复的导入操作。')
    journal = Path(op['journal']); payload = json.loads(journal.read_text(encoding='utf-8'))
    work = library.get(payload['work_id'])
    if Path(payload['directory']) != Path(work['task_directory']): raise LibraryError('作品目录变化，停止恢复。')
    with tasks.task_lock(Path(payload['directory'])):
        if payload['state'] == 'done':
            for name, info in payload['files'].items():
                if tasks.file_hash(Path(payload['directory']) / name) != info['after']: raise LibraryError('导入结果变化，停止恢复。')
            library.update(work['work_id'], refinement=payload['refinement'])
            state = 'done'
        else:
            _restore_import(payload)
            library.update(work['work_id'], refinement=payload['old_refinement'])
            state = 'rolled_back'
        payload['state'] = state; atomic_json(journal, payload)
        library.operation(op_id, work['work_id'], 'import', state, journal)
    return library.get(work['work_id'])


def read_plot(work):
    path = Path(work['files']['full_plot_summary.txt'])
    return tasks._plain_path(path).read_text(encoding='utf-8-sig') if path.is_file() else '暂无剧情介绍'
