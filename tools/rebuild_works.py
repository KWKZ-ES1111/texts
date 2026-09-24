#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rebuild_works.py —— 按 docs/md-spec.md §8 从 raw/ 重建出版层。

只加结构标记，一字不改：切分文件、给已有的章节标题行加 `# `、补 front matter、
换行归一为 LF。正文的字符与空白都从 raw/ 原样搬过来。

章节切分点只取自**原文已有的标题行**（与现有 content/ 章节 front matter 的 title
逐字匹配，按顺序在 raw 里定位），不发明标题、不改动标题。

用法:
    python tools/rebuild_works.py 在 --dry-run   # 只报告将要做的改动
    python tools/rebuild_works.py 在            # 写入
    python tools/build_toc.py 在                # 重建后刷新目录索引
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CHAPTER_RE = re.compile(r"^(\d{3})-(.+)\.md$")
TITLE_RE = re.compile(r"^title:[ \t]*(.+?)[ \t]*$", re.M)
META_FILE = "metadata.md"
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


def read_raw(path: Path) -> list[str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    lines = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def split_frontmatter(text: str) -> tuple[str, str]:
    """返回 (front matter 块含两侧 `---` 与末尾换行, 剩余正文)。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return "", text
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return "", text
    return "\n".join(lines[:close + 1]) + "\n", "\n".join(lines[close + 1:])


def read_index_titles(index: Path) -> tuple[str, str]:
    if not index.is_file():
        return "", ""
    text = index.read_text(encoding="utf-8", errors="replace")
    fm, _body = split_frontmatter(text)
    title = m.group(1) if (m := re.search(r"^title:[ \t]*(.+?)[ \t]*$", fm, re.M)) else ""
    sub = m.group(1) if (m := re.search(r"^subtitle:[ \t]*(.+?)[ \t]*$", fm, re.M)) else ""
    return title, sub


def rebuild(work: str, root: Path, dry_run: bool) -> int:
    raw_path = root / "raw" / f"{work}.txt"
    work_dir = root / "works" / work
    content = work_dir / CONTENT_DIR
    if not raw_path.is_file() or not work_dir.is_dir():
        print(f"需要 raw/{work}.txt 与 works/{work}/ 同时存在")
        return 1

    files = sorted(p for p in content.glob("*.md") if CHAPTER_RE.match(p.name))
    if not files:
        print(f"works/{work}/{CONTENT_DIR}/ 下没有 NNN-*.md")
        return 1

    raw = read_raw(raw_path)
    index_title, index_sub = read_index_titles(work_dir / META_FILE)

    # ---- 1. 定位每个章节的标题行（顺序查找，只认整行逐字相等）
    cuts: list[tuple[Path, str, int]] = []
    cursor = 0
    for p in files:
        fm, _ = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        m = TITLE_RE.search(fm)
        if not m:
            print(f"{p.name}: front matter 里没有 title，跳过")
            return 1
        title = m.group(1)
        idx = next((i for i in range(cursor, len(raw)) if raw[i].strip() == title), None)
        if idx is None:
            print(f"{p.name}: 在 raw 里找不到标题行 {title!r}（顺序查找自 raw[{cursor}]）")
            return 1
        cuts.append((p, title, idx))
        cursor = idx + 1

    # ---- 2. 题头：书名两行按 §8 授权移入 metadata.md，其余留给第一章
    head_end = cuts[0][2]
    head = raw[:head_end]
    moved: list[str] = []
    rest = list(head)
    if rest and index_title and rest[0].strip() == f"《{index_title}》":
        moved.append(rest.pop(0))
    if rest and index_sub and rest and rest[0].strip() == f"——{index_sub}":
        moved.append(rest.pop(0))
    if moved:
        print(f"题头：{len(moved)} 行按 §8 移入 {META_FILE} front matter —— " + " / ".join(moved))
    if rest and any(l.strip() for l in rest):
        print(f"题头：其余 {len([l for l in rest if l.strip()])} 行非空行将并入 {cuts[0][0].name} 的开头")

    # ---- 3. 组装每章正文
    bodies: list[list[str]] = []
    for k, (p, title, idx) in enumerate(cuts):
        end = cuts[k + 1][2] if k + 1 < len(cuts) else len(raw)
        body = (list(rest) if k == 0 else []) + [f"# {title}"] + raw[idx + 1:end]
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        bodies.append(body)

    # ---- 4. 写出
    for (p, _title, _idx), body in zip(cuts, bodies):
        text = p.read_text(encoding="utf-8", errors="replace")
        fm, old_body = split_frontmatter(text)
        if not fm:
            print(f"{p.name}: 没有 front matter，跳过")
            return 1
        new = fm + "\n" + "\n".join(body) + "\n"
        changed = new != text
        old_n = len([l for l in old_body.split("\n") if l.strip()])
        new_n = len([l for l in body if l.strip()])
        flag = "改写" if changed else "不变"
        if changed or dry_run:
            print(f"  {flag}  {p.name}   非空行 {old_n} → {new_n}")
        if changed and not dry_run:
            p.write_bytes(new.encode("utf-8"))

    print(f"\n章节 {len(files)} 个" + ("（--dry-run，未写入）" if dry_run else " 已重建"))
    if not dry_run:
        print(f"下一步：python tools/build_toc.py {work}   # 刷新目录索引")
    return 0


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="按 §8 从 raw/ 重建 works/ 出版层")
    ap.add_argument("work", help="作品目录名（= metadata.md 的 title）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写入")
    args = ap.parse_args()
    return rebuild(args.work, Path(__file__).resolve().parent.parent, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
