"""Legacy preview reports, plus explicitly approved, journaled migration API.

Preview does not move, overwrite or delete original works. It may write tag
metadata and generates JSON/Markdown report files; it is not fully read-only.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

import task_store as tasks
from batch_queue import AUDIO_SUFFIXES
from media_library import LibraryError, LibraryStore
from library_files import archive, safe_name
from app_runtime import configuration

OLD_ROOT = Path(configuration().get('legacy_source_directory') or Path.home() / 'EarlySakuraLegacy')
LEGACY_TAG = configuration().get('legacy_tag') or 'Imported'


def scan_preview(source_root=OLD_ROOT, library=None):
    library = library or LibraryStore()
    root = tasks._plain_path(tasks.absolute_path(source_root))
    if not root.is_dir(): raise LibraryError('旧分类目录不存在。')
    inventory, unknown = [], []
    for parent, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(parent) / name
            if path.is_symlink() or path.is_junction():
                dirs.remove(name); unknown.append({'path': str(path), 'reason': '链接/目录联接，不自动跟随或迁移'})
        for name in names:
            path = Path(parent) / name
            if path.is_symlink():
                unknown.append({'path': str(path), 'reason': '符号链接，不迁移'}); continue
            stat = path.stat()
            item = {'path': str(path), 'relative': str(path.relative_to(root)), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                item['sha256'] = tasks.file_hash(path)
            inventory.append(item)
    audios = [Path(item['path']) for item in inventory if Path(item['path']).suffix.lower() in AUDIO_SUFFIXES]
    manifests = []
    for item in inventory:
        if Path(item['path']).name == 'task.json':
            try: manifests.append(tasks.load_task(Path(item['path']).parent, verify_audio=False))
            except ValueError as error: unknown.append({'path': item['path'], 'reason': str(error)})
    history_rows = []
    if library.history.is_file():
        connection = sqlite3.connect(library.history.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            history_rows = [dict(row) for row in connection.execute('SELECT * FROM playback_history')]
        finally: connection.close()
    queue_path = library.database.parent / 'batch_queue.json'
    queue_rows = json.loads(queue_path.read_text(encoding='utf-8'))['entries'] if queue_path.is_file() else []
    rows = []; grouped = set()
    for audio in sorted(audios):
        group = [item for item in inventory if Path(item['path']).is_relative_to(audio.parent)]
        # A nested test audio has its own group. Do not silently absorb it into
        # the primary work's attachments or claim it is formally refined.
        nested_audio_dirs = {other.parent for other in audios if other != audio and other.is_relative_to(audio.parent) and other.parent != audio.parent}
        group = [item for item in group if not any(Path(item['path']).is_relative_to(folder) for folder in nested_audio_dirs)]
        grouped.update(item['path'] for item in group)
        associated = [task for task in manifests if os.path.normcase(task.identity['path']) == os.path.normcase(str(audio))]
        selected_task = associated[0] if len(associated) == 1 else None
        source_map = {}; warnings = []; decisions = []
        if sum(other.parent == audio.parent for other in audios) > 1:
            decisions.append('同目录有多部音频，标准 TXT/SRT 归属不能自动认定，需逐部确认。')
        if len(associated) > 1: decisions.append('同一音频有多个任务清单，需明确选择身份/结果版本。')
        for name in (*tasks.RESULT_NAMES, 'full_plot_summary.txt'):
            primary = audio.parent / name
            alias = tasks.legacy_srt_name(audio)
            if name == 'output.srt' and alias and not primary.exists():
                primary = audio.parent / alias
            alternatives = [task.directory / name for task in associated if (task.directory / name).is_file()]
            candidate = primary if primary.is_file() else alternatives[0] if len(alternatives) == 1 else None
            if candidate is not None:
                source_map[name] = str(candidate)
                if any(tasks.file_hash(path) != tasks.file_hash(candidate) for path in alternatives):
                    decisions.append(f'{name} 音频旁/任务内版本不同，默认主文件为 {candidate}；其他版本保留，需确认。')
        test = audio.name.lower() == 'test.mp3' or audio.parent.name.lower() in ('test', 'tests')
        if test: decisions.append('测试音频及其精修归属需单独确认，不自动标为正式已精修。')
        final = source_map.get('final_bilingual.txt')
        if not final or 'output.srt' not in source_map:
            decisions.append('缺少可确定的 final_bilingual.txt 或 output.srt 来源。')
        elif final:
            try:
                from studio import validate_final
                validate_final(final)
            except (ValueError, OSError, UnicodeError) as error:
                try:
                    validate_final(final, allow_legacy_seconds=True)
                    warnings.append('旧版秒级精修：保持原文件和现有 SRT，不重新生成。')
                except (ValueError, OSError, UnicodeError): decisions.append('精修格式/测试标记需人工检查：' + str(error))
        if not source_map.get('full_plot_summary.txt'):
            warnings.append('没有 full_plot_summary.txt；不影响用户确认的精修状态。')
        for item in group:
            path = Path(item['path'])
            if path.suffix.lower() == '.docx': warnings.append(f'保留 DOCX 附属资料，不自动转换成剧情 TXT：{path.name}')
            if path.name == 'final_bilingual_claude.txt': warnings.append('存在替代精修版本，作为原始资料保留，不覆盖标准 final。')
            historical_srt = path.name == tasks.legacy_srt_name(audio)
            if not historical_srt and path.name not in (*tasks.RESULT_NAMES, 'full_plot_summary.txt', 'task.json', '.task.lock') and path.suffix.lower() not in (*AUDIO_SUFFIXES, '.py', '.docx') and not path.name.startswith(('import-', 'japanese.txt.partial-', 'draft_bilingual.txt.pending')):
                unknown.append({'path': str(path), 'reason': '非标准附属文件，需确认归属；不自动清理'})
        task_id = selected_task.identity['task_id'] if selected_task else None
        proposed_id = uuid.uuid5(uuid.NAMESPACE_URL, 'EarlySakura:' + (task_id or str(audio))).hex
        destination = library.root() / '已处理作品' / safe_name(audio.stem)
        if destination.exists():
            warnings.append('默认目标已存在，不能覆盖；需核对或使用作品 ID 后缀。')
            destination = destination.with_name(destination.name + '-' + proposed_id[:8])
        rows.append({'audio': str(audio), 'source_folder': str(audio.parent), 'audio_bytes': audio.stat().st_size,
                     'audio_sha256': selected_task.identity['sha256'] if selected_task else None,
                     'audio_hash_note': '来自任务清单，执行前重新读取完整音频验证；本次预览不重读大音频。',
                     'task_id': task_id, 'task_directory': str(selected_task.directory) if selected_task else None,
                     'work_id': proposed_id, 'display_title': audio.stem, 'old_title_reference': audio.parent.name,
                     'destination': str(destination), 'sources': source_map, 'files': group,
                     'planned_tags': [LEGACY_TAG], 'planned_state': '待确认' if test else '已精修（用户确认）',
                     'warnings': warnings, 'decisions_required': decisions,
                     'history_refs': [row for row in history_rows if row['audio_path'] == str(audio) or row['work_id'] == task_id],
                     'queue_refs': [row for row in queue_rows if row['audio'] == str(audio) or row['task_id'] == task_id]})
    for item in inventory:
        if item['path'] not in grouped: unknown.append({'path': item['path'], 'reason': '没有确定的音频分组，不自动清理'})
    plan = {'schema_version': 1, 'source_root': str(root), 'library_root': str(library.root()), 'rows': rows,
            'inventory': inventory, 'unknown_files': unknown, 'file_count': len(inventory),
            'total_bytes': sum(item['bytes'] for item in inventory), 'migration_executed': False,
            'cleanup_authorized': False}
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True).encode('utf-8')
    plan['approval_sha256'] = hashlib.sha256(encoded).hexdigest()
    return plan


def write_preview(source_root=OLD_ROOT, library=None, output=None):
    library = library or LibraryStore()
    plan = scan_preview(source_root, library)
    # This creates only a metadata tag. No old works are registered/moved yet.
    library.save_tag(LEGACY_TAG)
    output = Path(output or library.database.parent / 'migration_previews' / ('preview-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]))
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path = output.with_suffix('.json')
    with json_path.open('x', encoding='utf-8') as stream: json.dump(plan, stream, ensure_ascii=False, indent=2)
    lines = [f'# {LEGACY_TAG}：迁移预览', '', f"来源：{plan['source_root']}", f"目标：{plan['library_root']}",
             f"共 {len(plan['rows'])} 个音频候选、{plan['file_count']} 个文件、{plan['total_bytes']:,} 字节。",
             '', '**尚未执行任何旧作品移动、覆盖或删除。实际迁移与最后目录清理分别需要明确确认。**', '',
             '预览可能写入标签元数据，并生成 JSON/Markdown 报告文件；不会移动、覆盖或删除原始作品。', '',
             '音频摘要取自现有清单，执行前会重新完整校验。完整逐文件清单、大小、时间、非音频校验值及路径引用在配套 JSON。']
    for index, row in enumerate(plan['rows'], 1):
        lines += ['', f"## {index}. {Path(row['audio']).name}", '', f"- 原音频：{row['audio']}",
                  f"- 计划目录：{row['destination']}", f"- 内部作品 ID：{row['work_id']}", f"- 原任务 ID：{row['task_id'] or '待执行前建立'}",
                  f"- 标签：{LEGACY_TAG}；状态：{row['planned_state']}",
                  f"- 历史引用：{len(row['history_refs'])}；队列引用：{len(row['queue_refs'])}"]
        for name, source in row['sources'].items(): lines.append(f'- {name} ← {source}')
        lines += ['- 注意：' + warning for warning in row['warnings']]
        lines += ['- **待确认：' + issue + '**' for issue in row['decisions_required']]
    lines += ['', '## 未确定归属/非标准文件', '']
    lines += [f"- {item['path']}：{item['reason']}" for item in plan['unknown_files']] or ['- 无。']
    lines += ['', '## 执行和回退原则', '',
              '确认精确清单及版本选择后，执行前重新校验音频、文件大小/时间和来源摘要；复制到目标同卷暂存目录，逐文件核对，再发布目录、更新媒体库/历史/队列引用，最后仅清理已确认且内容未变的源文件。',
              '原任务清单和全部选定版本/附属资料保留于迁移日志及 originals；复制失败保留原件，已发布但未提交可恢复提交，精修替换有旧版本备份。旧分类目录不递归删除，未知文件、测试子目录及残留元数据必须另行确认。',
              '本轮不运行执行接口。模拟目录的执行/中断测试不代表真实作品迁移已获批准。',
              '', f"预览确认摘要：{plan['approval_sha256']}"]
    markdown = output.with_suffix('.md')
    markdown.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return markdown


def execute_preview(plan, approved_sha256, *, library=None, queue=None):
    """Explicit Stage B API. Never invoked by scan/preview or this development turn."""
    library = library or LibraryStore()
    value = dict(plan); digest = value.pop('approval_sha256', None)
    expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    if not approved_sha256 or digest != expected or approved_sha256 != digest:
        raise LibraryError('没有对这份精确迁移清单的明确确认，未移动。')
    if plan['unknown_files'] or any(row['decisions_required'] for row in plan['rows']):
        raise LibraryError('清单仍有未知文件、版本或测试作品待确认，未执行迁移。')
    if str(library.root()) != plan['library_root']: raise LibraryError('目标根目录已变化，需重新预览。')
    # Verify the entire approved batch before moving its first work.
    for row in plan['rows']:
        existing = library.get(task_id=row['task_id']) if row['task_id'] else library.get(row['work_id'])
        if existing and existing['archive_state'] == 'archived':
            continue
        for item in row['files']:
            path = tasks._plain_path(Path(item['path'])); stat = path.stat()
            if stat.st_size != item['bytes'] or stat.st_mtime_ns != item['mtime_ns']:
                raise LibraryError('预览后来源文件变化，停止迁移。')
            if item.get('sha256') and tasks.file_hash(path) != item['sha256']: raise LibraryError('来源摘要变化，停止迁移。')
    tag = library.save_tag(LEGACY_TAG); moved = []
    for row in plan['rows']:
        existing = library.get(task_id=row['task_id']) if row['task_id'] else library.get(row['work_id'])
        if existing and existing['archive_state'] == 'archived':
            target = tasks.load_task(existing['task_directory'])
            library.set_tags(existing['work_id'], {tag, *[item['id'] for item in library.tags() if item['name'] in existing['tags'] and not item['system']]})
            moved.append(target); continue
        task = tasks.load_task(row['task_directory']) if row['task_directory'] else tasks.open_task(row['audio'])
        sources = dict(row['sources'])
        canonical = set(sources.values())
        for item in row['files']:
            source = Path(item['path'])
            if source == Path(row['audio']) or source.name == '.task.lock': continue
            # Preserve original names of aliases, alternate versions and metadata.
            if str(source) not in canonical or source.name not in tasks.RESULT_NAMES:
                relative = str(source.relative_to(Path(row['source_folder'])))
                sources['originals/old/' + relative] = str(source)
        target = archive(task, library, legacy_sources=sources, confirmed_refined=True, work_id=row['work_id'])
        work = library.get(task_id=task.identity['task_id'])
        library.set_tags(work['work_id'], [tag])
        if queue is not None:
            entry = queue.get(task.identity['task_id'])
            if entry:
                entry.update(audio=target.identity['path'], directory=str(target.directory), results={name: str(target.result(name)) for name in tasks.RESULT_NAMES})
                queue.save()
        moved.append(target)
    # Deliberately no rmtree/remove-directory phase. Cleanup needs a second,
    # separate authorization after verifying all unknown/residual files.
    return moved


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='旧作品迁移预览：可能写入标签元数据并生成报告；不会移动、覆盖或删除原始作品。')
    parser.add_argument('--source', type=Path, default=OLD_ROOT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print(write_preview(args.source, output=args.output))
