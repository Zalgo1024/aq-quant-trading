"""探针台账装配（回答「这些结论是哪个脚本跑出来的、怎么复现」）。

设计原则
--------
**登记信息读 README，存在性与新鲜度实时扫盘，两者交叉校对。**

为什么不直接扫目录：``scripts/probes/`` 下的文件名只能告诉你"有这么个脚本"，
而研究者真正要问的是"它回答了什么问题、依赖什么数据、支撑哪份文档的哪个结论"
—— 这三项磁盘算不出来，只能由人写在 ``scripts/probes/README.md`` 里。

为什么还要扫盘：**防 README 腐烂**。人工维护的登记表会过期，且过期时
既不报错也不缺数据。两种腐烂都致命，方向相反：

- **清单有、磁盘无** → 文档在引用一个不存在的证据（最严重：读者无法复核）；
- **磁盘有、清单无** → 证据存在但没人看得到（等于没做）。

所以本模块把两种偏差都显式报出来（``missing`` / ``unregistered``），
而不是假装台账是完整的。

⚠️ 与 ``project_status.py`` 同样的约定：算得出来的一律实时算，算不出的才读人工文件；
读不到就 ``available=false`` 并说明原因，**绝不返回空列表冒充"正常但为空"**。
"""

from __future__ import annotations

import ast
import re
from datetime import datetime
from pathlib import Path

from aq.config.settings import PROJECT_ROOT

PROBES_DIR = PROJECT_ROOT / "scripts" / "probes"
README = PROBES_DIR / "README.md"
DOCS_DIR = PROJECT_ROOT / "docs"


# --------------------------------------------------------------------- 工具
def _rel(path: Path) -> str:
    """仓库相对路径；不在仓库内时退回文件名。

    为什么不能直接 ``relative_to(PROJECT_ROOT)``：自检会用**临时目录里的
    README 副本**跑负对照（往表里插一条虚构脚本名，断言漂移被抓到），
    那个副本在仓库外，直接 relative_to 会抛 ValueError 把自检本身弄失败。
    """
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def _mt(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _first_doc_line(path: Path) -> str:
    """模块 docstring 的首行（探针自己写的"这个脚本在测什么"）。

    用 ``ast`` 而不是正则：探针里到处是引号，正则会抓错。解析失败返回空串
    （**不猜**，猜出来的说明文字会和代码实际做的事不一致）。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ""
    doc = ast.get_docstring(tree) or ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _split_row(line: str) -> list[str]:
    """按未转义的 ``|`` 切单元格（README 表格里 ``\\|`` 是正文的竖线）。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


def _plain(cell: str) -> str:
    """去掉行内 markdown 标记（反引号 / 粗体 / 链接语法），保留可见文字。"""
    s = cell.replace("`", "")
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return s.strip()


_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
#: 形如「终局诊断 §1.1」「ETF 数据层 §4/§6」——文档简称 + 章节号
_DOCREF = re.compile(r"^(?P<name>[^§]+?)\s*§.*$")


def _resolve_doc(cell: str) -> tuple[str, str]:
    """把「支撑的文档结论」单元格解析成 ``(文档相对路径, 章节号)``。

    两种写法都要认：
    1. 明确的相对链接 ``[ETF 数据层 §4/§6](../docs/xxx.md)`` → 直接取 href；
    2. 裸简称 ``终局诊断 §1.1`` → 拿简称去匹配 ``docs/`` 下的文件名**前缀**
       （去掉空格后比较），**必须唯一命中**才认，否则返回空串。
       不唯一就不猜 —— 点错文档比不点更糟。
    """
    m = _LINK.search(cell)
    if m:
        href = m.group(2).strip()
        target = (PROBES_DIR / href).resolve()
        try:
            rel = target.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            rel = href
        sec = ""
        m2 = re.search(r"§\s*([0-9][0-9./]*)", _plain(m.group(1)))
        if m2:
            sec = m2.group(1)
        return (rel, sec)

    plain = _plain(cell)
    m3 = _DOCREF.match(plain)
    if not m3:
        return ("", "")
    name = re.sub(r"\s+", "", m3.group("name"))
    sec_m = re.search(r"§\s*([0-9][0-9./]*)", plain)
    sec = sec_m.group(1) if sec_m else ""
    if not name:
        return ("", sec)

    cands: list[str] = []
    for d in (DOCS_DIR, PROJECT_ROOT):
        for p in sorted(d.glob("*.md")):
            stem = re.sub(r"\s+", "", p.stem)
            if stem == name or stem.startswith(name):
                rel = p.relative_to(PROJECT_ROOT).as_posix()
                if rel not in cands:
                    cands.append(rel)
    return (cands[0] if len(cands) == 1 else "", sec)


# --------------------------------------------------------------------- 解析
def _section_lines(text: str, heading: str) -> list[str]:
    """取 ``## <heading>`` 到下一个 ``##`` 之间的正文行。"""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for ln in lines:
        if ln.startswith("## "):
            inside = ln[3:].strip() == heading
            continue
        if inside:
            out.append(ln)
    return out


def _parse_table(text: str) -> list[dict]:
    """解析「## 清单」下的 Markdown 表格。"""
    rows = [ln for ln in _section_lines(text, "清单") if ln.lstrip().startswith("|")]
    if len(rows) < 3:  # 表头 + 分隔线 + 至少一行
        return []
    out: list[dict] = []
    for ln in rows[2:]:
        cells = _split_row(ln)
        if len(cells) < 4:
            continue
        # ⭐ 是「重点/必跑」标记，README 里有时写在脚本列、有时写在问题列 → 整行找
        starred = "⭐" in ln
        script = _plain(cells[0]).replace("⭐", "").strip()
        if not script or script.lower() in {"脚本", "---"}:
            continue
        doc_rel, sec = _resolve_doc(cells[3])
        doc_exists = (PROJECT_ROOT / doc_rel).is_file() if doc_rel else None
        out.append(
            {
                "script": script,
                "question": _plain(cells[1]).replace("⭐", "").strip(),
                "deps": _plain(cells[2]),
                "doc_ref": _plain(cells[3]),
                "doc_path": doc_rel,
                "doc_section": sec,
                "doc_exists": doc_exists,
                "starred": starred,
            }
        )
    return out


def _parse_fenced(text: str, heading: str, lang: str = "bash") -> str:
    """取某个小节里第一段围栏代码块的内容。"""
    body = _section_lines(text, heading)
    buf: list[str] = []
    inside = False
    for ln in body:
        if ln.strip().startswith("```"):
            if not inside:
                inside = True
                continue
            break
        if inside:
            buf.append(ln)
    return "\n".join(buf).strip()


def _parse_bullets(text: str, heading: str) -> list[str]:
    """取某个小节里的一级列表项（``- `` 或 ``1. `` 都认；含缩进续行，合并成一行）。

    ⚠️ 第一版只认 ``- ``，于是「## 三条约定」解析出 **0 条** ——
    该小节用的是有序列表 ``1. `` / ``2. `` / ``3. ``。
    报 0 之前先问这个 0 是怎么来的：这里的 0 不是"文档没写"，是**解析器不认**。
    """
    body = _section_lines(text, heading)
    items: list[str] = []
    for ln in body:
        if not ln.strip():
            continue
        m = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", ln)
        if m:
            items.append(m.group(1).strip())
        elif ln.startswith(("  ", "\t")) and items:
            items[-1] += " " + ln.strip()
    return [_plain(i) for i in items]


# --------------------------------------------------------------------- 主入口
def probe_registry() -> dict:
    """探针台账：登记信息（README）+ 文件实况（扫盘）+ 两者偏差。"""
    if not README.is_file():
        return {
            "available": False,
            "reason": f"缺少 {_rel(README)}",
            "entries": [],
        }

    text = README.read_text(encoding="utf-8")
    entries = _parse_table(text)
    if not entries:
        return {
            "available": False,
            "reason": (
                "README 存在但「## 清单」表格解析出 0 行 —— "
                "表格格式变了或标题被改名，此时**不能**把空表当成正常结果返回。"
            ),
            "entries": [],
        }

    on_disk = sorted(p.name for p in PROBES_DIR.glob("*.py") if p.name != "__init__.py")
    disk_set = set(on_disk)

    for e in entries:
        f = PROBES_DIR / e["script"]
        e["exists"] = f.is_file()
        if e["exists"]:
            st = f.stat()
            e["size_bytes"] = st.st_size
            e["mtime"] = _mt(f)
            try:
                e["n_lines"] = len(f.read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeDecodeError):
                e["n_lines"] = 0
            e["docstring"] = _first_doc_line(f)
        else:
            e["size_bytes"] = 0
            e["mtime"] = ""
            e["n_lines"] = 0
            e["docstring"] = ""

    registered = {e["script"] for e in entries}
    missing = sorted(registered - disk_set)        # 清单有、磁盘没有
    unregistered = sorted(disk_set - registered)   # 磁盘有、清单没登记
    broken_doc = [e["script"] for e in entries if e.get("doc_exists") is False]

    # 新鲜度：所有已存在脚本里最新的那个（一眼看出台账是不是长期没人动）
    mtimes = [e["mtime"] for e in entries if e["mtime"]]
    return {
        "available": True,
        "source": _rel(README),
        "readme_mtime": _mt(README),
        "n_registered": len(entries),
        "n_on_disk": len(on_disk),
        "n_resolved_doc": sum(1 for e in entries if e["doc_path"]),
        "missing": missing,
        "unregistered": unregistered,
        "broken_doc": broken_doc,
        "latest_script_mtime": max(mtimes) if mtimes else "",
        "entries": entries,
        "conventions": _parse_bullets(text, "三条约定"),
        "prereq": _parse_fenced(text, "前置条件"),
        "pitfalls": _parse_bullets(text, "四个通用陷阱（都在这堆脚本里踩过，所以写下来）"),
        "caveat": (
            "登记信息来自 scripts/probes/README.md（人工维护），文件状态实时扫盘。"
            "探针依赖 data_cache/（未入库），干净克隆下**跑不出数字** —— "
            "缺数据时应报错或跳过，而不是给出一个看起来正常但其实是空集的数字。"
        ),
    }
