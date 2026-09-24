#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_toc.py —— 从 content/ 生成 metadata.md 的「目录索引」。

目录索引是**派生物**：阅读顺序的唯一真相是 content/ 下的文件名字典序，本工具只是
把它渲染成一张表。手写目录会长歪，所以这里生成，由 lint 的 `FM011` 校验是否过期。

用法:
    python tools/build_toc.py 在            # 就地更新目录索引
    python tools/build_toc.py 在 --check    # 只检查是否过期（不写）
    python tools/build_toc.py --all         # 所有作品
    python tools/build_toc.py 在 --json     # 附带打印阅读器用的 manifest（不落盘）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CHAPTER_RE = re.compile(r"^(\d{3})-(.+)\.md$")
TOC_BEGIN = "<!-- toc:begin"
TOC_END = "<!-- toc:end -->"
META_FILE = "metadata.md"
STYLE_FILE = "style.md"
CONTENT_DIR = "content"


def setup_console() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def frontmatter(text: str) -> dict[str, str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return {}
    data: dict[str, str] = {}
    for ln in lines[1:close]:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.+?)\s*$", ln)
        if m:
            data[m.group(1).lower()] = m.group(2).strip().strip("\"'")
    return data


def count_chars(text: str) -> int:
    """正文字数：去 front matter、去标题行，计中日韩字符 + 西文词。

    这是全仓库唯一的字数定义，`lint_md.py --stats` 也用它 —— 两个工具给出同一个数字。
    """
    lines = text.split("\n")
    body = lines
    if lines and lines[0].strip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if close is not None:
            body = lines[close + 1:]
    body_text = re.sub(r"^#+[ \t]+.*$", "", "\n".join(body), flags=re.M)
    return len(re.findall(r"[%s]" % CJK, body_text)) + len(re.findall(r"[A-Za-z0-9]+", body_text))


def collect_chapters(work_dir: Path) -> list[tuple[int, str, Path]]:
    """按文件名字典序返回 [(序号, front matter title, 路径)]。"""
    content = work_dir / CONTENT_DIR
    if not content.is_dir():
        return []
    out: list[tuple[int, str, Path]] = []
    for p in sorted(content.glob("*.md")):
        m = CHAPTER_RE.match(p.name)
        if not m:
            continue
        out.append((int(m.group(1)), frontmatter(read_text(p)).get("title", ""), p))
    return out


def render_toc(chapters: list[tuple[int, str, Path]], work_dir: Path) -> str:
    """生成目录索引区域的正文（含两侧标记）。"""
    lines = [TOC_BEGIN + " 由 tools/build_toc.py 生成，请勿手改 -->", ""]
    if not chapters:
        lines.append("（暂无章节）")
    else:
        lines.append("| # | 章节 | 字数 | 文件 |")
        lines.append("|---|---|---|---|")
        total = 0
        for no, title, path in chapters:
            rel = f"{CONTENT_DIR}/{path.name}"
            count = count_chars(read_text(path))
            total += count
            lines.append(f"| {no:03d} | {title} | {count} | [{path.name}]({rel}) |")
        lines.append("")
        lines.append(f"共 {len(chapters)} 章，正文合计 {total} 字。")
    lines.append("")
    lines.append(TOC_END)
    return "\n".join(lines)


def replace_region(text: str, rendered: str) -> str | None:
    """替换 metadata.md 里 toc 标记之间的内容。找不到标记时返回 None。"""
    lines = text.split("\n")
    begin = next((i for i, ln in enumerate(lines) if ln.strip().startswith(TOC_BEGIN)), None)
    end = next((i for i, ln in enumerate(lines) if ln.strip() == TOC_END), None)
    if begin is None or end is None or end < begin:
        return None
    return "\n".join(lines[:begin] + rendered.split("\n") + lines[end + 1:])


def manifest(work_dir: Path, chapters: list[tuple[int, str, Path]]) -> dict:
    meta = frontmatter(read_text(work_dir / META_FILE))
    return {
        "title": meta.get("title", ""),
        "subtitle": meta.get("subtitle", ""),
        "status": meta.get("status", ""),
        "tags": [t.strip() for t in meta.get("tags", "[]").strip("[]").split(",") if t.strip()],
        "chapters": [
            {
                "order": no,
                "title": title,
                "path": f"{CONTENT_DIR}/{path.name}",
                "chars": count_chars(read_text(path)),
            }
            for no, title, path in chapters
        ],
    }


def build(work_dir: Path, check_only: bool, as_json: bool) -> int:
    meta_path = work_dir / META_FILE
    if not meta_path.is_file():
        print(f"找不到 {meta_path}")
        return 1
    chapters = collect_chapters(work_dir)
    text = read_text(meta_path)
    rendered = render_toc(chapters, work_dir)
    new_text = replace_region(text, rendered)

    if new_text is None:
        print(f"{META_FILE}: 找不到 `{TOC_BEGIN} ... -->` / `{TOC_END}` 标记对，无法写入目录索引")
        return 1

    stale = new_text != text
    if check_only:
        print(f"{work_dir.as_posix()}: 目录索引" + ("已过期（需重跑 build_toc.py）" if stale else "是最新的"))
    elif stale:
        meta_path.write_bytes(new_text.encode("utf-8"))
        print(f"{work_dir.as_posix()}: 目录索引已更新（{len(chapters)} 章）")
    else:
        print(f"{work_dir.as_posix()}: 目录索引已是最新，无改动")

    if as_json:
        print(json.dumps(manifest(work_dir, chapters), ensure_ascii=False, indent=2))
    return 0 if not (check_only and stale) else 1


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="从 content/ 生成 metadata.md 的目录索引")
    ap.add_argument("work", nargs="?", help="作品目录名（= 标题）")
    ap.add_argument("--all", action="store_true", help="处理 works/ 下所有作品")
    ap.add_argument("--check", action="store_true", help="只检查是否过期，不写入")
    ap.add_argument("--json", action="store_true", help="附带打印阅读器用的 manifest")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    works = root / "works"
    if args.all:
        dirs = sorted(d for d in works.iterdir()
                      if d.is_dir() and not d.name.startswith("_") and (d / META_FILE).is_file())
    elif args.work:
        dirs = [works / args.work]
    else:
        ap.error("需要指定作品目录名，或加 --all")

    rc = 0
    for d in dirs:
        rc |= build(d, args.check, args.json)
    return rc


if __name__ == "__main__":
    sys.exit(main())
