import os
import sys
import requests
import json
from pathlib import Path
from tkinter import Tk, filedialog
from task_store import task_for_text, task_lock, check_draft_progress, file_hash, write_new, TaskError
from app_runtime import lm_studio_settings, lm_studio_headers

_settings = lm_studio_settings()
API_URL = _settings['base_url'] + '/chat/completions'
MODEL = _settings['model']

# =========================
# 输入文件
# =========================

if len(sys.argv) > 1:
    # 被 run_pipeline.py 调用时
    INPUT_FILE = Path(sys.argv[1])

    print(f"Input file received: {INPUT_FILE}")

else:
    # 单独运行 translate.py 时
    root = Tk()
    root.withdraw()

    input_file = filedialog.askopenfilename(
        title="Select japanese.txt",
        filetypes=[
            ("Text files", "*.txt"),
            ("All files", "*.*")
        ]
    )

    if not input_file:
        print("No input file selected.")
        root.destroy()
        exit()

    INPUT_FILE = Path(input_file)

    root.destroy()


if not INPUT_FILE.exists():
    print(f"Input file not found: {INPUT_FILE}")
    exit()

task = task_for_text(INPUT_FILE, "japanese.txt")
INPUT_FILE = task.result("japanese.txt")
OUTPUT_FILE = task.result("draft_bilingual.txt")

BATCH_SIZE = 10


SYSTEM_PROMPT = """
你是一名专业的日中剧情本地化翻译。

任务：将日文 ASMR 台词忠实翻译为自然的简体中文。

规则：
1. 日文原文是唯一依据，不得擅自增加原文没有的信息。
2. 中文必须自然，符合人物对白习惯。
3. 保留敬语、亲密程度、人物语气和情绪。
4. 忠实保留原文含义、人物语气和表达细节，不擅自删减、弱化或改写。
5. 不得漏译任何一句。
6. 不输出解释、注释或额外说明。
7. 每一句输入都有编号。
8. 必须严格返回：
1|中文翻译
2|中文翻译
3|中文翻译
"""


def extract_text(line):
    if "] " in line:
        timestamp, text = line.split("] ", 1)
        return timestamp + "]", text

    return "", line


def ask_qwen(user_text, max_tokens=2000):
    headers = lm_studio_headers()
    try:
        response = requests.post(
            API_URL,
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text}
                ],
                "temperature": 0.2,
                "max_tokens": max_tokens
            },
            timeout=600, headers=headers
        )
        response.raise_for_status()
    except OSError as error:
        raise ConnectionError("模型服务不可用：LM Studio 请求失败。请核对 API 服务、端口、模型标识及认证设置；已有粗翻保留。") from error
    try:
        return response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise TaskError("LM Studio 返回内容无效，请检查服务和所加载模型；本批未写入。") from error


def translate_single(japanese):
    prompt = (
        "只翻译下面这一句日文。\n"
        "只输出中文翻译，不要解释，不要加引号：\n\n"
        + japanese
    )

    return ask_qwen(prompt, 500).strip()


def translate_batch(lines):
    numbered_text = ""

    for i, line in enumerate(lines, start=1):
        _, japanese = extract_text(line)
        numbered_text += f"{i}|{japanese}\n"

    answer = ask_qwen(numbered_text)

    translations = {}

    for line in answer.splitlines():
        if "|" not in line:
            continue

        number, chinese = line.split("|", 1)

        if number.strip().isdigit():
            number = int(number.strip())

            if 1 <= number <= len(lines):
                translations[number] = chinese.strip()

    # 如果批量翻译漏句，单独补翻
    for i, original_line in enumerate(lines, start=1):
        if i not in translations:
            _, japanese = extract_text(original_line)

            print(f"第 {i} 句漏译，正在单独补翻...")

            translations[i] = translate_single(japanese)

    return translations


with task_lock(task.directory):
    with open(INPUT_FILE, "r", encoding="utf-8-sig") as file:
        japanese_lines = [
            line.strip()
            for line in file
            if line.strip()
        ]


    translated_count, total = check_draft_progress(INPUT_FILE, OUTPUT_FILE)
    input_digest = file_hash(INPUT_FILE)


    print(f"共有 {len(japanese_lines)} 句日文")
    print(f"已经翻译 {translated_count} 句")
    print(f"从第 {translated_count + 1} 句继续")


    with open(OUTPUT_FILE, "a", encoding="utf-8") as output:

        for start in range(
            translated_count,
            len(japanese_lines),
            BATCH_SIZE
        ):
            batch = japanese_lines[start:start + BATCH_SIZE]

            print(
                f"\n正在翻译 "
                f"{start + 1} - "
                f"{start + len(batch)}"
            )

            translations = translate_batch(batch)
            for value in translations.values():
                if not value.strip() or len(value.splitlines()) != 1:
                    raise TaskError("译文为空或包含多行；本批未写入，已有内容保留。")
            if file_hash(INPUT_FILE) != input_digest:
                raise TaskError("翻译期间日文输入发生变化；本批未写入。")
            # A killed append can end on apparently valid text. Keep a durable
            # marker until the entire batch has been flushed to disk.
            pending = OUTPUT_FILE.with_name("draft_bilingual.txt.pending.json")
            write_new(pending, json.dumps({"bytes_before": OUTPUT_FILE.stat().st_size,
                      "completed_before": start, "input_sha256": input_digest},
                      ensure_ascii=False).encode("utf-8"))
            # Imported drafts may lack the final blank line; preserve their bytes.
            if OUTPUT_FILE.stat().st_size and not OUTPUT_FILE.read_bytes().endswith((b"\n\n", b"\r\n\r\n")):
                output.write("\n\n")

            for i, original_line in enumerate(batch, start=1):
                timestamp, japanese = extract_text(original_line)
                chinese = translations[i]

                output.write(f"{timestamp}\n")
                output.write(f"JP: {japanese}\n")
                output.write(f"ZH_DRAFT: {chinese}\n")
                output.write("\n")

            output.flush()
            os.fsync(output.fileno())
            pending.unlink()  # Only this batch's own marker; never a user result.

            print("这一批已保存。")


    print("\n全文粗翻完成。")
