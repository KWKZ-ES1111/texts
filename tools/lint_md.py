#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lint_md.py —— 本仓库 Markdown 小说格式校验器。

规范：docs/md-spec.md

作品结构（`works/<title>/`，文件夹名 = metadata.md 的 title）：

    metadata.md     作品元数据 + 生成的目录索引（role = meta）
    style.md        文体与符号约定 + allow: 豁免声明（role = style，可选）
    content/        章节，阅读顺序 = 文件名字典序（role = chapter）

用法:
    python tools/lint_md.py                  # 校验仓库内全部 .md
    python tools/lint_md.py works/在          # 只校验指定路径
    python tools/lint_md.py --strict         # 警告也算失败
    python tools/lint_md.py --stats          # 附带字数统计
    python tools/lint_md.py --scope chapter  # 只跑某一档（chapter/meta/style/doc/work）
    python tools/lint_md.py --list-rules     # 列出全部规则编号
    python tools/lint_md.py --features       # 列出 style.md 可声明的豁免项
    python tools/lint_md.py --self-test      # 造样本验证每条规则都会触发
    python tools/lint_md.py --fix-eol        # 唯一的自动修复：CRLF→LF、去 BOM

约定：默认只读。唯一的写操作是显式 --fix-eol，且永不触碰 raw/ 与 .txt。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_toc  # noqa: E402  （字数与目录索引的唯一实现，避免两个工具漂移）

# ---------------------------------------------------------------- 常量

LINT_SKIP_DIRS = {".git", ".cursor", "node_modules", "__pycache__", ".venv", "venv", ".idea"}
FIX_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea"}

META_FILE = "metadata.md"
STYLE_FILE = "style.md"
CONTENT_DIR = "content"
WORKS_DIR = "works"
RAW_DIR = "raw"
SELFTEST_DIRNAME = "_selftest"

WORK_DIR_ALLOWED = {META_FILE, STYLE_FILE, CONTENT_DIR}

FIX_SUFFIXES = {".md", ".mdc", ".py"}
FIX_NAMES = {".gitattributes", ".editorconfig"}

CHAPTER_RE = re.compile(r"^(\d{3})-(.+)\.md$")
BAD_NAME_CHARS = set("\\/:*?\"<>|\t ·—")
TITLE_FORBIDDEN_CHARS = set("\\/:*?\"<>|")
STATUSES = {"draft", "final", "archived"}
KINDS = {"body", "appendix", "afterword", "extra"}
META_REQUIRED = ("title", "status", "created")
CHAPTER_REQUIRED = ("title", "kind")
KNOWN_FIELDS = set(META_REQUIRED) | set(CHAPTER_REQUIRED) | {
    "subtitle", "updated", "tags", "origin_model", "origin_chat",
    "origin_date", "license", "synopsis",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
H1_RE = re.compile(r"^#[ \t]+\S")
SCENE_BREAK = "* * *"
CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
EMPH_CJK_RE = re.compile(r"[%s]\*{1,2}|\*{1,2}[%s]" % (CJK, CJK))

RULES = {
    "ENC001": "文件含 UTF-8 BOM",
    "ENC002": "含 CRLF/CR 换行",
    "ENC003": "文件末尾缺少换行 / 多余空行",
    "ENC004": "无法以 UTF-8 解码",
    "WS001": "行尾空格",
    "WS002": "连续空行",
    "WS003": "行首缩进",
    "WS004": "全角空格 U+3000",
    "WS005": "段落内硬换行（两行非空行相邻）",
    "FM001": "缺少 front matter",
    "FM002": "front matter 未闭合",
    "FM003": "缺少必填字段",
    "FM004": "枚举值非法（status / kind）",
    "FM005": "日期格式非法（应为 YYYY-MM-DD）",
    "FM006": "front matter 使用了被禁止的 YAML 结构",
    "FM007": "作品文件夹名与 title 不一致，或 title 不能当文件夹名",
    "FM008": "front matter 出现未知字段",
    "FM009": "作品豁免声明无效（未知项或触及不可豁免的硬约束）",
    "FM010": "metadata.md 正文不得出现 `#` 一级标题",
    "FM011": "目录索引缺失或已过期（重跑 tools/build_toc.py）",
    "ST001": "文件名不符合规范",
    "ST002": "章节序号不连续或重复",
    "ST003": "正文第一个非空行不是本章标题",
    "ST004": "`#` 一级标题数量不为 1",
    "ST005": "标题跳级",
    "ST006": "front matter title 与 `#` 标题不一致",
    "ST007": "kind 与标题不符",
    "ST008": "作品目录结构不符合规范",
    "ST009": "找不到与作品同名的 raw 原文",
    "MD001": "正文出现表格",
    "MD002": "正文出现代码块或行内代码",
    "MD003": "正文出现链接",
    "MD004": "正文出现图片",
    "MD005": "正文出现引用式链接定义",
    "MD006": "正文出现脚注",
    "MD007": "正文出现原始 HTML",
    "MD008": "正文出现 `---` 场景分隔（应使用 `* * *`）",
    "MD009": "正文出现 Markdown 列表",
    "MD010": "行内强调紧贴中文（跨渲染器不稳定）",
}

# 各 role 跑哪些检查
ROLES = ("chapter", "meta", "style", "doc")
SCOPE_ALIASES = {"novel": "chapter", "work": "work", "hygiene": "doc"}


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


class Report:
    def __init__(self, suppress: frozenset[str] = frozenset()) -> None:
        self.items: list[tuple[str, int, str, str, str]] = []
        self.suppressed: set[str] = set(suppress)

    def add(self, rel: str, line: int, level: str, code: str, msg: str) -> None:
        if code in self.suppressed:
            return
        self.items.append((rel, max(1, line), level, code, msg))

    @property
    def errors(self) -> int:
        return sum(1 for it in self.items if it[2] == "E")

    @property
    def warnings(self) -> int:
        return sum(1 for it in self.items if it[2] == "W")


# ---------------------------------------------------------------- 作品豁免

FEATURES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "table":            (frozenset({"MD001"}), frozenset()),
    "code":             (frozenset({"MD002"}), frozenset()),
    "link":             (frozenset({"MD003"}), frozenset()),
    "image":            (frozenset({"MD004"}), frozenset()),
    "footnote":         (frozenset({"MD005", "MD006"}), frozenset()),
    "html":             (frozenset({"MD007"}), frozenset()),
    "hr":               (frozenset({"MD008"}), frozenset()),
    "list":             (frozenset({"MD009"}), frozenset()),
    "emphasis-tight":   (frozenset({"MD010"}), frozenset()),
    "heading-not-first": (frozenset({"ST003"}), frozenset()),
    "no-h1":            (frozenset({"ST003", "ST004", "ST006"}), frozenset()),
    "headings-free":    (frozenset({"ST005"}), frozenset()),
    "meta-h1":          (frozenset({"FM010"}), frozenset()),
    "no-toc":           (frozenset({"FM011"}), frozenset()),
    "rich-frontmatter": (frozenset({"FM006"}), frozenset()),
    "enum-free":        (frozenset({"FM004"}), frozenset()),
    "kind-optional":    (frozenset(), frozenset({"kind"})),
    "keep-blank-runs":  (frozenset({"WS002"}), frozenset()),
    "keep-adjacent-lines": (frozenset({"WS005"}), frozenset()),
}

FEATURE_DOC = {
    "table":            "允许表格",
    "code":             "允许代码块与行内代码",
    "link":             "允许链接",
    "image":            "允许图片",
    "footnote":         "允许脚注（含引用式链接定义）",
    "html":             "允许原始 HTML",
    "hr":               "允许 `---` 等自定义场景分隔",
    "list":             "允许 Markdown 列表",
    "emphasis-tight":   "允许强调符紧贴中文，不再告警",
    "heading-not-first": "章节标题可不在正文首行（标题前的装置块不移动）",
    "no-h1":            "章节标题只写在 front matter，正文不出 `#` 标题",
    "headings-free":    "允许标题跳级",
    "meta-h1":          "允许 metadata.md 正文出现 `#` 一级标题",
    "no-toc":           "不要目录索引（作品目录不参与索引）",
    "rich-frontmatter": "允许 front matter 使用缩进/嵌套/多行字符串",
    "enum-free":        "不校验 status / kind 的取值",
    "kind-optional":    "不要求章节写 kind 字段",
    "keep-blank-runs":  "保留原文的连续空行（不压成一个）",
    "keep-adjacent-lines": "保留原文的相邻非空行（不插空行）",
}

EXEMPT_FORBIDDEN = {
    "ENC001", "ENC002", "ENC003", "ENC004",
    "FM001", "FM002",
    "ST001", "ST002",
    "FM009",
}


def parse_list(val: str) -> list[str]:
    v = val.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [p.strip().strip("\"'") for p in v.split(",") if p.strip()]


class Exemption:
    def __init__(self) -> None:
        self.codes: set[str] = set()
        self.optional: set[str] = set()
        self.declared: list[str] = []
        self.problems: list[str] = []


def load_exemption(work_dir: Path) -> Exemption:
    ex = Exemption()
    style = work_dir / STYLE_FILE
    if not style.is_file():
        return ex
    raw = style.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        lines = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    except UnicodeDecodeError:
        return ex
    if not lines or lines[0].strip() != "---":
        return ex
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return ex
    for ln in lines[1:close]:
        m = FM_KEY_RE.match(ln)
        if not m or m.group(1).lower() != "allow":
            continue
        for name in parse_list(m.group(2)):
            key = name.lower()
            if key in FEATURES:
                codes, optional = FEATURES[key]
                ex.codes |= codes
                ex.optional |= optional
                ex.declared.append(key)
            elif key in RULES:
                if key in EXEMPT_FORBIDDEN:
                    ex.problems.append(f"`{key}` 属于不可豁免的硬约束，已忽略")
                else:
                    ex.codes.add(key)
                    ex.declared.append(key)
            else:
                ex.problems.append(
                    f"未知的豁免项 `{name}`（可用：{'、'.join(sorted(FEATURES))}，或直接写规则码）")
    return ex


# ---------------------------------------------------------------- front matter


def parse_frontmatter(block: list[str], rel: str, base_line: int, rep: Report) -> dict[str, str]:
    data: dict[str, str] = {}
    for idx, ln in enumerate(block):
        line_no = base_line + idx
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if ln[:1].isspace():
            rep.add(rel, line_no, "E", "FM006", "front matter 只允许顶层 `key: value`，禁止嵌套/缩进")
            continue
        m = FM_KEY_RE.match(ln)
        if not m:
            rep.add(rel, line_no, "E", "FM006", "front matter 行不符合 `key: value`")
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if val in {"|", ">", "|-", ">-", "|+", ">+"}:
            rep.add(rel, line_no, "E", "FM006", "禁止多行字符串（`|` / `>`）")
            continue
        if val.startswith("&") or val.startswith("*"):
            rep.add(rel, line_no, "E", "FM006", "禁止 YAML 锚点 / 别名")
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        data[key] = val
    return data


def split_frontmatter(text: str) -> tuple[list[str], int]:
    """返回 (front matter 行, 正文起始下标)。无 front matter 时返回 ([], 0)。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return [], 0
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return [], 0
    return lines[1:close], close + 1


# ---------------------------------------------------------------- role


def role_for(path: Path, works: Path) -> str:
    """chapter = works/<title>/content/*.md；meta = metadata.md；
    style = style.md；其余（含 docs/）为 doc。"""
    try:
        path.resolve().relative_to(works)
    except ValueError:
        return "doc"
    if path.parent.name == CONTENT_DIR:
        return "chapter"
    if path.name == META_FILE:
        return "meta"
    if path.name == STYLE_FILE:
        return "style"
    return "doc"


# ---------------------------------------------------------------- 单文件检查


def check_file(path: Path, rel: str, role: str, rep: Report,
               ex: Exemption | None = None) -> tuple[int, int]:
    """返回 (正文字数, 章节号或 -1)。"""
    ex = ex or Exemption()
    strict = role == "chapter"          # 章节里排版问题算错误，其余算警告
    raw = path.read_bytes()

    if raw.startswith(b"\xef\xbb\xbf"):
        rep.add(rel, 1, "E", "ENC001", "含 UTF-8 BOM")
        raw = raw[3:]
    if b"\r\n" in raw or b"\r" in raw:
        rep.add(rel, 1, "E", "ENC002", "含 CRLF/CR 换行，必须 LF")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        rep.add(rel, 1, "E", "ENC004", f"无法以 UTF-8 解码：{exc}")
        return 0, -1

    if not text.endswith("\n"):
        rep.add(rel, len(text.splitlines()) or 1, "E", "ENC003", "文件末尾缺少换行")
    if text.endswith("\n\n"):
        rep.add(rel, len(text.splitlines()), "E", "ENC003", "文件末尾有多余空行")

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    # ---- 通用卫生
    blank_run = 0
    for i, ln in enumerate(lines):
        n = i + 1
        if ln.strip() == "":
            blank_run += 1
            if blank_run >= 2:
                rep.add(rel, n, "E" if strict else "W", "WS002", "连续空行（段间只允许一个空行）")
            continue
        blank_run = 0
        if ln != ln.rstrip():
            rep.add(rel, n, "E" if strict else "W", "WS001", "行尾有空格")
        if "\u3000" in ln:
            rep.add(rel, n, "E" if strict else "W", "WS004", "含全角空格 U+3000")
        if strict and ln[:1] in (" ", "\t"):
            rep.add(rel, n, "E", "WS003", "行首缩进（缩进交给阅读器）")

    # ---- front matter
    fm_block, body_start = split_frontmatter(text)
    fm: dict[str, str] = {}
    if not fm_block:
        if lines and lines[0].strip() == "---":
            rep.add(rel, 1, "E", "FM002", "front matter 未闭合（缺少结束的 `---`）")
        elif role in ("chapter", "meta"):
            rep.add(rel, 1, "E", "FM001", "缺少 front matter")
    else:
        fm = parse_frontmatter(fm_block, rel, 2, rep)

    chapter_no = -1
    if role in ("chapter", "meta"):
        required = [k for k in (META_REQUIRED if role == "meta" else CHAPTER_REQUIRED)
                    if k not in ex.optional]
        for key in required:
            if key not in fm:
                rep.add(rel, 1, "E", "FM003", f"缺少必填字段 `{key}`")
        if role == "meta":
            if fm.get("status") and fm["status"] not in STATUSES:
                rep.add(rel, 1, "E", "FM004",
                        f"status 值非法：{fm['status']}（应为 draft/final/archived）")
        else:
            if fm.get("kind") and fm["kind"] not in KINDS:
                rep.add(rel, 1, "E", "FM004",
                        f"kind 值非法：{fm['kind']}（应为 body/appendix/afterword/extra）")
        for key in ("created", "updated", "origin_date"):
            if key in fm and not DATE_RE.match(fm[key]):
                rep.add(rel, 1, "E", "FM005", f"{key} 日期格式非法：{fm[key]}（应为 YYYY-MM-DD）")
        for key in fm:
            if key not in KNOWN_FIELDS:
                rep.add(rel, 1, "W", "FM008", f"未知字段 `{key}`")

    # ---- metadata.md
    if role == "meta":
        work_dir = path.parent
        title = fm.get("title", "")
        if title:
            if path.parent.name != title:
                rep.add(rel, 1, "E", "FM007",
                        f"作品文件夹名（{path.parent.name}）与 title（{title}）不一致 —— "
                        f"文件夹名就是作品的唯一身份")
            bad = next((c for c in title if c in TITLE_FORBIDDEN_CHARS), None)
            if bad:
                rep.add(rel, 1, "E", "FM007", f"title 含不能作文件夹名的字符 {bad!r}")
            if title != title.strip():
                rep.add(rel, 1, "E", "FM007", "title 首尾有空白，不能作文件夹名")
        for i in range(body_start, len(lines)):
            if H1_RE.match(lines[i]):
                rep.add(rel, i + 1, "E", "FM010",
                        "metadata.md 正文不得出现 `#` 一级标题（用 `##` 及以下）")
        # 目录索引是否与 content/ 一致
        chapters = build_toc.collect_chapters(work_dir)
        rendered = build_toc.render_toc(chapters, work_dir)
        updated = build_toc.replace_region(text, rendered)
        if updated is None:
            rep.add(rel, 1, "E", "FM011",
                    "找不到 `<!-- toc:begin ... -->` / `<!-- toc:end -->` 标记对，"
                    "无法生成目录索引（重跑 tools/build_toc.py）")
        elif updated != text:
            rep.add(rel, 1, "E", "FM011", "目录索引已过期或与实际章节不符（重跑 tools/build_toc.py）")

    # ---- 章节
    if role == "chapter":
        m = CHAPTER_RE.match(path.name)
        if not m:
            rep.add(rel, 1, "E", "ST001", "章节文件名必须为 `NNN-短标题.md`（000 起，3 位零填充）")
        else:
            chapter_no = int(m.group(1))
            bad = next((c for c in m.group(2) if c in BAD_NAME_CHARS), None)
            if bad:
                rep.add(rel, 1, "E", "ST001",
                        f"文件名含禁用字符 {bad!r}（禁 空格 · — : / \\ * ? \" < > |）")

        first_idx = next((i for i in range(body_start, len(lines)) if lines[i].strip()), None)
        if first_idx is None:
            rep.add(rel, len(lines) or 1, "E", "ST003", "章节正文为空")
        elif not H1_RE.match(lines[first_idx]):
            rep.add(rel, first_idx + 1, "E", "ST003", "正文第一个非空行必须是本章标题（`# 标题`）")

        h1 = [i for i in range(body_start, len(lines)) if H1_RE.match(lines[i])]
        if len(h1) != 1:
            rep.add(rel, (h1[0] + 1) if h1 else body_start + 1, "E", "ST004",
                    f"`#` 一级标题必须恰好出现一次，实际 {len(h1)} 次")
        h1_text = ""
        if h1:
            h1_text = re.sub(r"^#+[ \t]+", "", lines[h1[0]]).strip()
            if "title" in fm and h1_text != fm["title"]:
                rep.add(rel, h1[0] + 1, "E", "ST006",
                        f"front matter title（{fm['title']}）与 `#` 标题（{h1_text}）不一致")

        seen = 0
        for i in range(body_start, len(lines)):
            m2 = HEADING_RE.match(lines[i])
            if not m2:
                continue
            lvl = len(m2.group(1))
            if lvl > seen + 1:
                rep.add(rel, i + 1, "E", "ST005", f"标题跳级：H{seen} → H{lvl}")
            seen = max(seen, lvl)

        kind = fm.get("kind")
        if kind == "appendix" and h1_text and "附录" not in h1_text:
            rep.add(rel, 1, "W", "ST007", "kind=appendix 但标题里没有「附录」")
        if kind and kind != "appendix" and "附录" in h1_text:
            rep.add(rel, 1, "W", "ST007", "标题含「附录」但 kind 不是 appendix")

        # ---- 正文禁止项
        prev_nonblank = False
        prev_structural = False
        in_fence = False
        for i in range(body_start, len(lines)):
            ln = lines[i]
            n = i + 1
            if not ln.strip():
                prev_nonblank = False
                prev_structural = False
                continue
            structural = bool(
                ln.startswith(("|", ">", "```", "~~~"))
                or re.match(r"^[-+*][ \t]+\S", ln)
                or re.match(r"^\d+[.)][ \t]+\S", ln)
            )
            if re.match(r"^(```|~~~)", ln):
                in_fence = not in_fence
                prev_nonblank = True
                prev_structural = True
                continue
            if prev_nonblank and not (structural or prev_structural or in_fence):
                rep.add(rel, n, "E", "WS005", "段落内硬换行（一段一行，段间留空行）")
            prev_nonblank = True
            prev_structural = structural
            if in_fence:
                continue

            if ln.startswith("|"):
                rep.add(rel, n, "E", "MD001", "正文禁止表格")
            if re.match(r"^(```|~~~)", ln):
                rep.add(rel, n, "E", "MD002", "正文禁止代码块")
            if "`" in ln:
                rep.add(rel, n, "E", "MD002", "正文禁止行内代码")
            if "](" in ln:
                rep.add(rel, n, "E", "MD003", "正文禁止链接")
            if "![" in ln:
                rep.add(rel, n, "E", "MD004", "正文禁止图片")
            if re.match(r"^\[[^\]\n]+\]:", ln):
                rep.add(rel, n, "E", "MD005", "正文禁止引用式链接定义")
            if re.search(r"\[\^", ln):
                rep.add(rel, n, "E", "MD006", "正文禁止脚注（`[^1]` 会与纯文字的方括号写法冲突）")
            if re.search(r"<[a-zA-Z/!][^>\n]*>", ln):
                rep.add(rel, n, "E", "MD007", "正文禁止原始 HTML")
            if (re.match(r"^-{3,}\s*$", ln) or re.match(r"^\*{3,}\s*$", ln)
                    or re.match(r"^_{3,}\s*$", ln)):
                rep.add(rel, n, "E", "MD008", "场景分隔必须用 `* * *`，禁止 `---`")
            if re.match(r"^[-+*][ \t]+", ln) and ln.strip() != SCENE_BREAK:
                rep.add(rel, n, "E", "MD009", "正文禁止 Markdown 列表（作者手写编号保留原样）")
            if re.match(r"^\d+[.)][ \t]+", ln):
                rep.add(rel, n, "E", "MD009", "正文禁止 Markdown 有序列表")
            if EMPH_CJK_RE.search(ln):
                rep.add(rel, n, "W", "MD010", "行内强调紧贴中文，跨渲染器可能配对失败")

    return build_toc.count_chars(text), chapter_no


# ---------------------------------------------------------------- 作品级检查


def check_work(work_dir: Path, root: Path, rep: Report) -> None:
    """作品目录结构、章节序号连续性、raw 对应。"""
    try:
        rel_dir = work_dir.relative_to(root).as_posix()
    except ValueError:
        rel_dir = work_dir.name

    for entry in sorted(work_dir.iterdir()):
        if entry.name not in WORK_DIR_ALLOWED:
            rep.add(f"{rel_dir}/{entry.name}", 1, "E", "ST008",
                    f"作品目录里只允许 {META_FILE} / {STYLE_FILE} / {CONTENT_DIR}/，"
                    f"多出 `{entry.name}`")

    if not (work_dir / META_FILE).is_file():
        rep.add(f"{rel_dir}/", 1, "E", "ST008", f"作品目录缺少 {META_FILE}")

    content = work_dir / CONTENT_DIR
    chapters: list[tuple[int, Path]] = []
    if not content.is_dir():
        rep.add(f"{rel_dir}/", 1, "W", "ST008",
                f"没有 {CONTENT_DIR}/ 目录（空目录 git 存不下，有章节时自然出现）")
    else:
        for p in sorted(content.iterdir()):
            if p.is_file() and p.suffix == ".md":
                m = CHAPTER_RE.match(p.name)
                if m:
                    chapters.append((int(m.group(1)), p))

    chapters.sort()
    nums = [n for n, _ in chapters]
    seen: set[int] = set()
    for n, p in chapters:
        if n in seen:
            rep.add(f"{rel_dir}/{CONTENT_DIR}/{p.name}", 1, "E", "ST002", f"章节序号 {n:03d} 重复")
        seen.add(n)
    if nums and nums[0] != 0:
        rep.add(f"{rel_dir}/{CONTENT_DIR}/", 1, "E", "ST002", f"章节序号必须从 000 开始，实际起始 {nums[0]:03d}")
    for a, b in zip(nums, nums[1:]):
        if b != a + 1:
            rep.add(f"{rel_dir}/{CONTENT_DIR}/", 1, "E", "ST002",
                    f"章节序号不连续：{a:03d} 之后是 {b:03d}")

    meta_path = work_dir / META_FILE
    if meta_path.is_file():
        meta = build_toc.frontmatter(build_toc.read_text(meta_path))
        title = meta.get("title", "")
        raw_dir = root / RAW_DIR
        if title and raw_dir.is_dir():
            if not (raw_dir / f"{title}.txt").is_file():
                others = [p.name for p in raw_dir.glob("*.txt")]
                rep.add(f"{rel_dir}/{META_FILE}", 1, "W", "ST009",
                        f"raw/ 下没有 `{title}.txt`，无法逐字校验（现有："
                        + ("、".join(others) if others else "无") + "）")


# ---------------------------------------------------------------- 自检

SELFTEST_ALLOW_DIRNAME = "_selftest_allow"

SELFTEST_META = (
    "---\n"
    "title: _selftest\n"
    "status: draft\n"
    "created: 2026-09-25\n"
    "---\n"
    "\n"
    "一段简介。\n"
    "\n"
    "## 目录\n"
    "\n"
    "<!-- toc:begin 由 tools/build_toc.py 生成，请勿手改 -->\n"
    "\n"
    "<!-- toc:end -->\n"
)

SELFTEST_STYLE = (
    "---\n"
    "title: _selftest · 文体约定\n"
    "allow: [heading-not-first, keep-blank-runs, keep-adjacent-lines]\n"
    "---\n"
    "\n"
    "| 符号 | 含义 |\n"
    "|---|---|\n"
    "| `[ ]` | 装置 |\n"
)

SELFTEST_CONTENT = {
    "000-正常.md": (
        "---\n"
        "title: 零 · 编号\n"
        "kind: body\n"
        "---\n"
        "\n"
        "# 零 · 编号\n"
        "\n"
        "档案正文之前必须有编号。\n"
        "\n"
        "[归档员批注：以上说明来源无法确认。]\n"
        "\n"
        "[记录者：■]\n"
    ),
    "002-坏.md": (
        "---\n"
        "title: 不一样的标题\n"
        "kind: body\n"
        "---\n"
        "\n"
        "# 二 · 编号\n"
        "\n"
        "这一行结尾有空格 \n"
        "紧跟着第二行没留空行。\n"
        "这是**重点**内容。\n"
        "\u3000全角空格开头。\n"
        "   半角缩进行。\n"
        "- 一个列表项\n"
        "\n"
        "| a | b |\n"
        "| - | - |\n"
        "\n"
        "链接 [某处](https://example.com) 与脚注 [^1] 与行内代码 `x`。\n"
        "\n"
        "---\n"
        "\n"
        "### 跳级标题\n"
    ),
    "003 空格.md": (
        "---\n"
        "title: 三 · 名字\n"
        "kind: body\n"
        "---\n"
        "\n"
        "# 三 · 名字\n"
        "\n"
        "正文。\n"
    ),
    "004-无头.md": "# 四 · 无头\n\n正文。\n",
    # 装置块在标题之前 —— 《在》 的真实形态，触发 ST003
    "005-装置在前.md": (
        "---\n"
        "title: 五 · 装置在前\n"
        "kind: body\n"
        "---\n"
        "\n"
        "[装置块：标题之前]\n"
        "\n"
        "# 五 · 装置在前\n"
        "\n"
        "正文。\n"
    ),
}

# 必须零告警的样本
SELFTEST_VALID = ("metadata.md", "style.md", "content/000-正常.md")

SELFTEST_EXPECTED = (
    "FM001", "FM003", "FM007", "FM010", "FM011",
    "ST001", "ST002", "ST003", "ST005", "ST006", "ST008", "ST009",
    "WS001", "WS003", "WS004", "WS005",
    "MD001", "MD002", "MD003", "MD006", "MD008", "MD009", "MD010",
)

# 豁免反证样本：独立的作品目录，style.md 声明 allow: [table, list, hr, code]
SELFTEST_ALLOW_META = (
    "---\n"
    "title: _selftest_allow\n"
    "status: draft\n"
    "created: 2026-09-25\n"
    "---\n"
    "\n"
    "豁免测试用。\n"
)

SELFTEST_ALLOW_STYLE = (
    "---\n"
    "title: _selftest_allow · 文体约定\n"
    "allow: [table, list, hr, code]\n"
    "---\n"
    "\n"
    "本篇允许表格与列表。\n"
)

SELFTEST_ALLOW_CHAPTER = (
    "---\n"
    "title: 零 · 违规\n"
    "kind: body\n"
    "---\n"
    "\n"
    "# 零 · 违规\n"
    "\n"
    "| 项 | 值 |\n"
    "| --- | --- |\n"
    "| 温度 | 23.5 |\n"
    "\n"
    "- 振荡\n"
    "- 延迟\n"
    "\n"
    "---\n"
    "\n"
    "用 `代码` 表示系统输出。\n"
    "\n"
    "```\n"
    "ERR-1\n"
    "```\n"
)

# 不声明豁免时必须触发（注意不含 WS005：表格行/列表项/代码块不算段落硬换行）
SELFTEST_EXEMPT_CODES = ("MD001", "MD002", "MD008", "MD009")


def run_self_test(root: Path) -> int:
    def write(p: Path, text: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))

    d = root / WORKS_DIR / SELFTEST_DIRNAME
    allow_dir = root / WORKS_DIR / SELFTEST_ALLOW_DIRNAME
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(allow_dir, ignore_errors=True)

    got: dict[str, list[str]] = {}
    bad_valid: list[str] = []
    ex_findings: list[str] = []
    ex_notes: list[str] = []

    # ---- 主样本（title 必须等于文件夹名 _selftest）
    for name, text in SELFTEST_CONTENT.items():
        write(d / CONTENT_DIR / name, text)
    write(d / META_FILE, SELFTEST_META)
    write(d / STYLE_FILE, SELFTEST_STYLE)
    write(d / "000-散落.md", "# 不该在作品根目录\n")          # ST008
    meta = d / META_FILE
    chapters = build_toc.collect_chapters(d)
    meta.write_bytes(
        build_toc.replace_region(build_toc.read_text(meta),
                                 build_toc.render_toc(chapters, d)).encode("utf-8"))
    clean_meta = build_toc.read_text(meta)
    dirty_meta = clean_meta.replace("| 000 |", "| 999 |", 1)

    # ---- 豁免样本（独立作品目录，避免与主样本的豁免互相干扰）
    write(allow_dir / META_FILE, SELFTEST_ALLOW_META)
    write(allow_dir / STYLE_FILE, SELFTEST_ALLOW_STYLE)
    allow_chapter = allow_dir / CONTENT_DIR / "000-违规.md"
    write(allow_chapter, SELFTEST_ALLOW_CHAPTER)

    try:
        ex = load_exemption(d)
        rels = [META_FILE, STYLE_FILE] + [f"{CONTENT_DIR}/{n}" for n in SELFTEST_CONTENT]
        for rel in rels:
            p = d / rel
            sub = Report()
            check_file(p, rel, role_for(p, d.parent), sub, ex)
            for _r, line, _lv, code, msg in sub.items:
                got.setdefault(code, []).append(f"{rel}:{line} {msg}")
                if rel in SELFTEST_VALID:
                    bad_valid.append(f"{rel}:{line} {code} {msg}")

        wsub = Report()
        check_work(d, root, wsub)                     # ST002 / ST008 / ST009
        for _r, line, _lv, code, msg in wsub.items:
            got.setdefault(code, []).append(f"works/{SELFTEST_DIRNAME}:{line} {msg}")

        # FM007：title 与文件夹名不一致
        write(meta, clean_meta.replace("title: _selftest", "title: 另一个名字", 1))
        sub = Report()
        check_file(meta, META_FILE, "meta", sub, ex)
        for _r, line, _lv, code, msg in sub.items:
            if code == "FM007":
                got.setdefault(code, []).append(f"{META_FILE}:{line} {msg}")

        # FM010：metadata 正文出现 `#`
        write(meta, clean_meta.replace("## 目录", "# 目录", 1))
        sub = Report()
        check_file(meta, META_FILE, "meta", sub, ex)
        for _r, line, _lv, code, msg in sub.items:
            if code == "FM010":
                got.setdefault(code, []).append(f"{META_FILE}:{line} {msg}")

        # FM011：目录索引过期
        write(meta, dirty_meta)
        sub = Report()
        check_file(meta, META_FILE, "meta", sub, ex)
        for _r, line, _lv, code, msg in sub.items:
            if code == "FM011":
                got.setdefault(code, []).append(f"{META_FILE}:{line} {msg}")
        write(meta, clean_meta)

        # ---- 豁免机制：不声明时必须触发，声明后必须静默
        ex2 = load_exemption(allow_dir)
        probe = Report()
        check_file(allow_chapter, "000-违规.md", "chapter", probe, ex2)
        baseline = {code for _r, _l, _lv, code, _m in probe.items}
        missing = sorted(set(SELFTEST_EXEMPT_CODES) - baseline)
        if missing:
            ex_findings.append("反证失败——不声明豁免时这些规则本应触发却未触发，豁免测试无意义："
                               + "、".join(missing))
        sub = Report(suppress=frozenset(ex2.codes))
        check_file(allow_chapter, "000-违规.md", "chapter", sub, ex2)
        for _r, line, _lv, code, msg in sub.items:
            ex_findings.append(f"000-违规.md:{line} {code} {msg}")
        ex_notes = list(ex2.declared)
        if ex2.problems:
            ex_findings.extend(ex2.problems)
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(allow_dir, ignore_errors=True)

    ok = True
    print("自检：规则覆盖（样本在 works/%s/ 与 works/%s/，跑完已删除）"
          % (SELFTEST_DIRNAME, SELFTEST_ALLOW_DIRNAME))
    for code in sorted(SELFTEST_EXPECTED):
        hit = got.get(code)
        if not hit:
            ok = False
        print(f"  {'命中  ' if hit else '未命中'} {code}  {RULES[code]}"
              + (f"   ← {hit[0]}" if hit else ""))
    for code in sorted(set(SELFTEST_EXEMPT_CODES)):
        if code not in got:
            ok = False
            print(f"  未命中 {code}  {RULES[code]}（豁免反证未触发）")
    if bad_valid:
        ok = False
        print("  误报  " + "、".join(SELFTEST_VALID) + " 应为零告警，实际：")
        for f in bad_valid:
            print(f"        {f}")
    else:
        print("  通过  " + "、".join(SELFTEST_VALID) + " 零告警（装置行未被误报）")
    if ex_findings:
        ok = False
        print("  失败  豁免机制：")
        for f in ex_findings:
            print(f"        {f}")
    else:
        print("  通过  豁免机制：不声明时 " + "、".join(SELFTEST_EXEMPT_CODES)
              + " 均触发；声明 allow: [%s] 后全部静默" % "、".join(ex_notes))
    unexpected = sorted(set(got) - set(SELFTEST_EXPECTED))
    if unexpected:
        print("  额外触发（未列入预期，请检查是否误报）：" + "、".join(unexpected))
    print("自检" + ("通过。" if ok else "失败。"))
    return 0 if ok else 1


# ---------------------------------------------------------------- 文件收集


def collect_md(root: Path, targets: list[str]) -> list[Path]:
    if targets:
        out: list[Path] = []
        for t in targets:
            p = (root / t).resolve()
            if p.is_dir():
                out += [q for q in sorted(p.rglob("*.md"))
                        if not (LINT_SKIP_DIRS & set(q.parts))]
            elif p.is_file() and p.suffix == ".md":
                out.append(p)
            else:
                print(f"跳过：找不到 {t}", file=sys.stderr)
        return out
    return [p for p in sorted(root.rglob("*.md"))
            if not (LINT_SKIP_DIRS & set(p.relative_to(root).parts))]


def collect_fixable(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        parts = set(p.relative_to(root).parts)
        if FIX_SKIP_DIRS & parts or RAW_DIR in parts:
            continue
        if p.suffix in FIX_SUFFIXES or p.name in FIX_NAMES:
            out.append(p)
    return out


def fix_eol(files: list[Path], root: Path) -> int:
    changed = 0
    for p in files:
        raw = p.read_bytes()
        new = raw
        if new.startswith(b"\xef\xbb\xbf"):
            new = new[3:]
        new = new.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if new != raw:
            p.write_bytes(new)
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = str(p)
            print(f"  已修：{rel}")
            changed += 1
    return changed


# ---------------------------------------------------------------- 主流程


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="本仓库 Markdown 小说格式校验器")
    ap.add_argument("paths", nargs="*", help="要校验的路径（默认全仓库）")
    ap.add_argument("--strict", action="store_true", help="警告也视为失败")
    ap.add_argument("--stats", action="store_true", help="打印字数统计")
    ap.add_argument("--scope", default="all",
                    choices=["all", "chapter", "meta", "style", "doc", "work", "novel"],
                    help="只跑某档（novel 是 chapter 的别名）")
    ap.add_argument("--list-rules", action="store_true", help="列出全部规则编号")
    ap.add_argument("--features", action="store_true", help="列出 style.md 可声明的豁免项")
    ap.add_argument("--self-test", action="store_true", help="造样本验证每条规则都会触发")
    ap.add_argument("--fix-eol", action="store_true",
                    help="唯一自动修复：CRLF→LF、去 BOM（永不触碰 raw/ 与 .txt）")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent

    if args.list_rules:
        for code in sorted(RULES):
            print(f"{code}  {RULES[code]}")
        return 0

    if args.features:
        print("在 works/<title>/style.md 的 front matter 里声明，例如：")
        print("    allow: [table, list, kind-optional]\n")
        print("豁免项（也可直接写规则码，如 `allow: [MD001]`）：")
        for name in sorted(FEATURES):
            codes, optional = FEATURES[name]
            parts = ["、".join(sorted(codes))] if codes else []
            if optional:
                parts.append("必填 " + "、".join(sorted(optional)))
            print(f"  {name:<21} {FEATURE_DOC[name]:<32} → {'；'.join(parts) or '—'}")
        print("\n不可豁免（阅读器入口契约）：" + "、".join(sorted(EXEMPT_FORBIDDEN)))
        return 0

    if args.self_test:
        return run_self_test(root)

    works = (root / WORKS_DIR).resolve()

    if args.fix_eol:
        targets = collect_fixable(root)
        if args.paths:
            wanted = {(root / t).resolve() for t in args.paths}
            targets = [p for p in targets if p.resolve() in wanted or any(
                p.resolve() == w or w in p.resolve().parents for w in wanted)]
        print(f"normalize EOL/BOM：{len(targets)} 个候选文件")
        print(f"  已修：{fix_eol(targets, root)} 个")

    wanted = SCOPE_ALIASES.get(args.scope, args.scope)
    files = collect_md(root, args.paths)
    rep = Report()
    stats: list[tuple[str, int]] = []
    work_dirs: set[Path] = set()
    meta_paths: list[Path] = []
    ex_cache: dict[Path, Exemption] = {}
    ex_notes: list[tuple[str, list[str]]] = []

    for path in files:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        role = role_for(path, works)
        if wanted != "all" and wanted != "work" and role != wanted:
            continue
        if wanted == "work" and role == "doc":
            continue

        ex = Exemption()
        if role in ("chapter", "meta", "style"):
            work_dir = path.parent if role in ("meta", "style") else path.parent.parent
            work_dirs.add(work_dir)
            if work_dir not in ex_cache:
                ex_cache[work_dir] = load_exemption(work_dir)
                found = ex_cache[work_dir]
                if found.declared or found.problems:
                    try:
                        slug = work_dir.relative_to(root).as_posix()
                    except ValueError:
                        slug = work_dir.name
                    ex_notes.append((slug, found.declared))
                    for problem in found.problems:
                        rep.add(f"{slug}/{STYLE_FILE}", 1, "E", "FM009", problem)
            ex = ex_cache[work_dir]
            if role == "meta":
                meta_paths.append(path)

        rep.suppressed = ex.codes
        count, no = check_file(path, rel, role, rep, ex)
        if args.stats and role == "chapter":
            stats.append((rel, count))
    rep.suppressed = set()

    if wanted in ("all", "work"):
        for work_dir in sorted(work_dirs):
            check_work(work_dir, root, rep)

    current = None
    for rel, line, level, code, msg in sorted(rep.items, key=lambda x: (x[0], x[1])):
        if rel != current:
            current = rel
            print(f"\n{rel}")
        print(f"  {line:>5}  {level}{code}  {msg}")

    if ex_notes:
        print("\n作品豁免声明（works/<title>/style.md 的 `allow:`）")
        for slug, names in sorted(ex_notes):
            print(f"  {slug}: {'、'.join(names) if names else '（无有效项）'}")

    if args.stats:
        print("\n字数（正文，不含 front matter 与标题行）")
        total = 0
        for rel, count in stats:
            total += count
            print(f"  {count:>8}  {rel}")
        if stats:
            print(f"  {total:>8}  合计（{len(stats)} 章）")

    print(f"\n检查 {len(files)} 个文件：{rep.errors} 个错误，{rep.warnings} 个警告")
    if rep.errors or (args.strict and rep.warnings):
        return 1
    print("通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
