#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_verbatim.py —— 验证 works/<title>/ 是否逐字保留了 raw/<title>.txt。

规范 docs/md-spec.md §8 的判定标准：去掉新增的结构标记（`# `、front matter、
文件边界）与授权的书名行迁移之后，产物必须逐字等于原文。lint 检查 §1–§7，
本脚本检查 §8 —— 两者互补，lint 看不出内容有没有被改写。

作品结构：works/<title>/{metadata.md, style.md, content/NNN-*.md}
原文：    raw/<title>.txt

用法:
    python tools/verify_verbatim.py 在
    python tools/verify_verbatim.py --all
    python tools/verify_verbatim.py 在 --strict-blank
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CHAPTER_RE = re.compile(r"^(\d{3})-(.+)\.md$")
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


def read_lines(path: Path) -> list[str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def strip_frontmatter(lines: list[str]) -> list[str]:
    if lines and lines[0].strip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if close is not None:
            return lines[close + 1:]
    return lines


def chapter_body(path: Path) -> list[str]:
    """章文件的正文：去 front matter、去首尾空行、去掉加在标题行上的 `# `。

    标题不一定在正文首行（作品可声明 heading-not-first），所以按 front matter 的
    title 定位那一行，而不是假定它是第一行。
    """
    lines = read_lines(path)
    title = read_index_meta(path).get("title", "")
    body = strip_frontmatter(lines)
    for i, ln in enumerate(body):
        if title and ln.strip() == f"# {title}":
            body[i] = ln.replace(f"# {title}", title, 1)
            break
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return body


def read_index_meta(path: Path) -> dict[str, str]:
    """读 index.md 的 front matter，只取简单标量。"""
    meta: dict[str, str] = {}
    if not path.is_file():
        return meta
    lines = read_lines(path)
    if not lines or lines[0].strip() != "---":
        return meta
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return meta
    for ln in lines[1:close]:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.+?)\s*$", ln)
        if m:
            meta[m.group(1).lower()] = m.group(2).strip().strip("\"'")
    return meta


def blank_before(lines: list[str]) -> tuple[list[str], list[int]]:
    """返回 (非空行列表, 每行前面紧邻的空行数)。"""
    nb: list[str] = []
    runs: list[int] = []
    run = 0
    for ln in lines:
        if ln.strip():
            nb.append(ln)
            runs.append(run)
            run = 0
        else:
            run += 1
    return nb, runs


def verify(work: str, root: Path, strict_blank: bool) -> int:
    raw_path = root / "raw" / f"{work}.txt"
    work_dir = root / "works" / work
    if not raw_path.is_file():
        print(f"找不到 raw/{work}.txt")
        return 1
    if not work_dir.is_dir():
        print(f"找不到 works/{work}/")
        return 1

    files = sorted(p for p in (work_dir / CONTENT_DIR).glob("*.md") if CHAPTER_RE.match(p.name))
    if not files:
        print(f"works/{work}/{CONTENT_DIR}/ 下没有 NNN-*.md 章节")
        return 1

    raw = read_lines(raw_path)
    while raw and not raw[-1].strip():
        raw.pop()

    # §8 授权：原文首部的书名行可移入 metadata.md 的 title / subtitle
    index_meta = read_index_meta(work_dir / META_FILE)
    relocated: list[str] = []
    while raw and raw[0].strip():
        head = raw[0].strip()
        if index_meta.get("title") and head == f"《{index_meta['title']}》":
            relocated.append(raw.pop(0))
            continue
        if index_meta.get("subtitle") and head == f"——{index_meta['subtitle']}":
            relocated.append(raw.pop(0))
            continue
        break
    while raw and not raw[0].strip():
        raw.pop(0)

    produced: list[str] = []
    chapter_firsts: set[int] = set()      # 每个章节正文的首个非空行在 produced 里的下标
    for i, p in enumerate(files):
        if i:
            produced.append("")
        body = chapter_body(p)
        chapter_firsts.add(len([l for l in produced if l.strip()]))
        produced.extend(body)

    nb_raw, runs_raw = blank_before(raw)
    nb_prod, runs_prod = blank_before(produced)

    print(f"raw/{work}.txt    {len(raw)} 行（{len(nb_raw)} 非空 / {len(raw)-len(nb_raw)} 空）")
    print(f"works/{work}/      {len(files)} 章")
    print(f"拼接后                 {len(produced)} 行（{len(nb_prod)} 非空 / {len(produced)-len(nb_prod)} 空）")
    if relocated:
        print(f"§8 授权移入 metadata：  {len(relocated)} 行 —— " + " / ".join(relocated))
    print()

    failures: list[str] = []

    # ---- 1. 非空行序列（正文有没有被改写、增删、重排）
    sm = difflib.SequenceMatcher(None, nb_raw, nb_prod, autojunk=False)
    ratio = sm.ratio()
    print(f"[1] 非空行序列  相似度 {ratio:.4%}")
    blocks = [op for op in sm.get_opcodes() if op[0] != "equal"]
    if blocks:
        print(f"    {len(blocks)} 处差异：")
        for tag, i1, i2, j1, j2 in blocks:
            print(f"\n    {tag}  原文[{i1}:{i2}] → 产物[{j1}:{j2}]")
            for x in nb_raw[i1:i2]:
                print(f"        - {x[:64]}")
            for y in nb_prod[j1:j2]:
                print(f"        + {y[:64]}")
        failures.append(f"非空行序列有 {len(blocks)} 处差异")
    else:
        print("    逐字相同。")

    # ---- 2. 空行：按非空行对齐，比较每行前面紧邻的空行数
    print(f"\n[2] 空行        原文 {len(raw)-len(nb_raw)} / 产物 {len(produced)-len(nb_prod)}")
    mapping = []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            mapping.append((blk.a + k, blk.b + k))
    collapsed = []
    inserted = []
    for i_raw, j_prod in mapping:
        if j_prod == 0 or j_prod in chapter_firsts:
            continue                      # 章节边界：原文的空行就是分隔符，不算内容
        delta = runs_raw[i_raw] - runs_prod[j_prod]
        if delta > 0:
            collapsed.append((i_raw, delta, runs_raw[i_raw], runs_prod[j_prod], nb_raw[i_raw]))
        elif delta < 0:
            inserted.append((i_raw, -delta, runs_raw[i_raw], runs_prod[j_prod], nb_raw[i_raw]))
    lost = sum(c[1] for c in collapsed)
    added = sum(c[1] for c in inserted)
    for label, items, amount in (("折叠（原文多、产物少）", collapsed, lost),
                                 ("插入（原文无、产物多）", inserted, added)):
        if not items:
            continue
        print(f"    {label}：{len(items)} 处，共 {amount} 个空行")
        for i_raw, n, a_, b_, text in items[:6]:
            print(f"        raw[{i_raw:>4}] 原 {a_} → 产物 {b_}   后一行: {text[:44]}")
        if len(items) > 6:
            print(f"        ...（另有 {len(items)-6} 处）")
    if not collapsed and not inserted:
        print("    逐字相同（章节边界处的空行不算内容）。")
    if strict_blank:
        if lost:
            failures.append(f"空行被折叠 {lost} 个")
        if added:
            failures.append(f"空行被插入 {added} 个")

    print()
    if failures:
        print("结论：未通过 §8")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("结论：通过 §8（去掉 `# `、front matter、文件边界后逐字等于原文）")
    return 0


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="验证 works/<title>/ 是否逐字保留了 raw/<title>.txt")
    ap.add_argument("work", nargs="?", help="作品目录名（= metadata.md 的 title）")
    ap.add_argument("--all", action="store_true", help="验证 works/ 下所有作品")
    ap.add_argument("--strict-blank", action="store_true", help="连续空行被折叠也算失败")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    works: list[str] = []
    if args.all:
        wdir = root / "works"
        if wdir.is_dir():
            works = sorted(d.name for d in wdir.iterdir()
                           if d.is_dir() and not d.name.startswith("_")
                           and (root / "raw" / f"{d.name}.txt").is_file())
    elif args.work:
        works = [args.work]
    else:
        ap.error("需要指定作品目录名，或加 --all")

    rc = 0
    for i, name in enumerate(works):
        if i:
            print("\n" + "-" * 60 + "\n")
        rc |= verify(name, root, args.strict_blank)
    if not works:
        print("没有可验证的作品（需要 raw/<title>.txt 与 works/<title>/ 同时存在）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
