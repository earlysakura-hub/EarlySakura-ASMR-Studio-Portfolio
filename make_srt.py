"""Validated SRT conversion; importing this module never starts a dialog or job."""
import argparse
import hashlib
import os
from pathlib import Path
import re

from task_store import (task_for_text, task_lock, partial_path, publish_new,
                        check_draft_progress, file_hash, TaskError)

TEST_MARKERS = ("端到端测试字幕", "非正式精修")
TIME = r"\d{2}:[0-5]\d:[0-5]\d,\d{3}"
STAMP = re.compile(rf"\[({TIME}) --> ({TIME})\]")
SOURCE_NAMES = {"final": "final_bilingual.txt", "draft": "draft_bilingual.txt"}


def milliseconds(stamp):
    hours, minutes, seconds, ms = map(int, re.split("[:,]", stamp))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + ms


def parse_bilingual(content, source):
    text = content.decode("utf-8-sig").strip()
    if source == "final" and any(marker in text for marker in TEST_MARKERS):
        raise TaskError("精翻含非正式测试标记，禁止生成；不会自动改用粗翻。")
    if not text:
        raise TaskError("字幕来源为空。")
    tag = "ZH_FINAL:" if source == "final" else "ZH_DRAFT:"
    entries, previous = [], -1
    for index, block in enumerate(re.split(r"\n\s*\n", text), 1):
        lines = [line.strip() for line in block.splitlines()]
        match = STAMP.fullmatch(lines[0]) if lines else None
        if (len(lines) != 3 or not match or not lines[1].startswith("JP:")
                or not lines[1][3:].strip() or not lines[2].startswith(tag)
                or not lines[2][len(tag):].strip()):
            raise TaskError(f"第 {index} 块无效：需要毫秒时间戳、非空 JP: 和非空 {tag} 三行。")
        start, end = match.groups()
        start_ms, end_ms = milliseconds(start), milliseconds(end)
        if end_ms <= start_ms or start_ms < previous:
            raise TaskError(f"第 {index} 块时间范围无效或开始时间倒退。")
        previous = start_ms
        entries.append((start, end, lines[2][len(tag):].strip()))
    return entries


def _fingerprint(path):
    return file_hash(path) if path.exists() else None


def _prepare(task):
    final, draft, japanese = (task.result(name) for name in
                             ("final_bilingual.txt", "draft_bilingual.txt", "japanese.txt"))
    source = "final" if final.exists() else "draft"
    path = final if source == "final" else draft
    if not path.is_file():
        raise TaskError("精翻文件不是有效文件，不能回退粗翻。" if source == "final" else "没有可用的精翻或完整粗翻。")
    content = path.read_bytes()
    entries = parse_bilingual(content, source)
    jp_bytes = japanese.read_bytes() if japanese.exists() else None
    if source == "draft":
        if jp_bytes is None:
            raise TaskError("缺少 japanese.txt，不能确认粗翻完整性。")
        done, total = check_draft_progress(japanese, draft)
        if done != total or total <= 0:
            raise TaskError(f"粗翻尚未完成（{done}/{total}），未生成字幕。")
    elif jp_bytes is not None:
        originals = [line.strip() for line in jp_bytes.decode("utf-8-sig").splitlines() if line.strip()]
        if len(originals) != len(entries):
            raise TaskError("精翻条数与 japanese.txt 不一致，可能不完整；不会改用粗翻。")
        for index, (line, (start, end, _)) in enumerate(zip(originals, entries), 1):
            prefix, separator, text = line.partition("] ")
            if not separator or not text.strip() or prefix + "]" != f"[{start} --> {end}]":
                raise TaskError(f"精翻第 {index} 块时间戳与日文原文不一致，未生成。")
    inputs = {path.name: hashlib.sha256(content).hexdigest(),
              "japanese.txt": hashlib.sha256(jp_bytes).hexdigest() if jp_bytes is not None else None}
    for name, digest in inputs.items():
        if _fingerprint(task.result(name)) != digest:
            raise TaskError("校验期间来源文件发生变化，请重新确认。")
    plan = {"source": source, "inputs": inputs, "count": len(entries),
            "output_sha256": _fingerprint(task.result("output.srt"))}
    return plan, entries


def prepare_srt(task):
    """Read-only preview. A bad final never permits draft fallback."""
    return _prepare(task)[0]


def confirm_srt(plan, ask):
    label = "精翻" if plan["source"] == "final" else "粗翻"
    if plan["source"] == "final":
        accepted = ask("确认精修完成", "你已人工检查当前 final_bilingual.txt，确认它是此音频的正式精修文本吗？\n程序只能校验格式，不能判断译文质量。")
    else:
        accepted = ask("使用 Qwen 粗翻", "不存在 final_bilingual.txt，当前 Qwen 粗翻已完整。\n是否直接使用 ZH_DRAFT 生成字幕？粗翻未经人工精修，不会创建 final_bilingual.txt。")
    if not accepted:
        return False
    if plan["output_sha256"] is not None:
        return ask("替换已有字幕", f"已有 output.srt。本次将使用【{label}】重新生成，并替换现有字幕。是否继续？")
    return True


def validate_srt(path, entries):
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", text.strip())
    if len(blocks) != len(entries):
        raise TaskError("临时 SRT 条数不完整，原字幕未替换。")
    for index, (block, (start, end, translation)) in enumerate(zip(blocks, entries), 1):
        if block.splitlines() != [str(index), f"{start} --> {end}", translation]:
            raise TaskError(f"临时 SRT 第 {index} 块验证失败，原字幕未替换。")


def generate_srt(task, approved_plan):
    """Publish only a fully validated temporary file, under the existing task lock."""
    with task_lock(task.directory):
        task.verify_audio()
        plan, entries = _prepare(task)
        if plan != approved_plan:
            raise TaskError("来源、完整性或已有字幕已变化，请重新确认；原字幕未替换。")
        output = task.result("output.srt")
        temporary = partial_path(output)
        content = "".join(f"{i}\n{start} --> {end}\n{text}\n\n"
                          for i, (start, end, text) in enumerate(entries, 1))
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        validate_srt(temporary, entries)
        if prepare_srt(task) != approved_plan:
            raise TaskError("生成期间来源或原字幕发生变化，请重新确认；未替换。")
        if plan["output_sha256"] is None:
            publish_new(temporary, output)
        else:
            os.replace(temporary, output)  # Same directory / volume, atomic replace.
    print(f"完成！使用{'精翻' if plan['source'] == 'final' else '粗翻'}生成 {len(entries)} 条字幕。")
    print(f"文件：{output}")
    return output


def main():
    from tkinter import Tk, filedialog, messagebox
    parser = argparse.ArgumentParser(description="选择精翻或粗翻，确认后生成 SRT。")
    parser.add_argument("input", nargs="?")
    args = parser.parse_args()
    root = Tk()
    root.withdraw()
    try:
        selected = args.input or filedialog.askopenfilename(parent=root, title="选择 final_bilingual.txt 或 draft_bilingual.txt", filetypes=[("文本", "*.txt")])
        if not selected:
            return
        name = Path(selected).name
        if name not in SOURCE_NAMES.values():
            raise TaskError("请选择 final_bilingual.txt 或 draft_bilingual.txt。")
        task = task_for_text(Path(selected), name)
        plan = prepare_srt(task)
        if confirm_srt(plan, lambda title, text: messagebox.askyesno(title, text, parent=root, default="no")):
            generate_srt(task, plan)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
