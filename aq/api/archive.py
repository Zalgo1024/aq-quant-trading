"""研究产物归档装配（回答「我以前跑过什么、**哪些数字还能引用**」）。

设计原则
--------
**判定规则不写在这里，它写在 ``README.md`` 第 5 节，本模块只做机械执行。**

README「历史结果口径清点（引用前必看）」给的关键是两条**可机械执行**的判别规则：

1. 产物 json 的 ``configs[].score_neutralize`` 字段**不存在** ⇒ raw 口径，不可引用。
   "没有该字段"本身即结论，**不等于**"默认中性化"。
2. json 里**没有** ``excess_caliber`` 字段 ⇒ 该产物的超额类数字是「双扣 rf」修正**之前**的，不可引用。

把这两条做成代码，是为了让那张人工表**不会腐烂**：人工表会在某次重跑后失真，
而失真时既不报错也不缺数据 —— 本项目已被这类静默失真咬过两次
（``etf_list.name`` 整体错位、t 口径多除 √年数）。所以本模块还做第三件事：
**把 README 第 5 节的 ✅/❌ 与自己的计算逐行比对，不一致就报 ``readme_drift``。**

⛔ 本页数据全部来自 ``runtime/``，而 ``runtime/`` 被 gitignore
⇒ **干净克隆 / 换机器下本页必然为空**。因此拿不到时返回 ``available=false`` 并说明原因，
绝不返回空列表冒充"跑过的实验一个都没有"。

⚠️ 另一条硬规则：这里的**收益类数字（PBO / t / DSR）只在 ``quotable=true`` 时可对外引用**；
``quotable=false`` 的产物仍然会显示这些数字，因为**它们本身就是"不能引用的示范"**，
但要与"可引用"在视觉上分开（前端用填充强度，不用红绿 —— 红绿是涨跌专用）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from aq.config.settings import PROJECT_ROOT

RUNTIME_DIR = PROJECT_ROOT / "runtime"
CSCV_DIR_NAME = "cscv"
FACTOR_RUNS_DIR_NAME = "factor_research"
README = PROJECT_ROOT / "README.md"


# --------------------------------------------------------------------- 工具
def _rel(path: Path) -> str:
    """仓库相对路径；不在仓库内时退回文件名。

    自检会用**临时目录**造一批假产物跑负对照（含"把不可引用判成可引用"的坏输入），
    那些文件在仓库外，直接 ``relative_to`` 会抛 ValueError 把自检本身弄失败。
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


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _num(x) -> float | None:
    """只接受真数字；字符串 / None 一律返回 None（不猜、不转义）。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def _stem_key(name: str) -> str:
    """产物名的归一化键：``cscv_liquid_neu_main`` ↔ 目录 ``liquid_neu_main``。

    这两者**必须**能对上，否则"旁边有没有收益缓存"会一律算成没有（假阴性）。
    """
    return name[len("cscv_"):] if name.startswith("cscv_") else name


# --------------------------------------------------------------------- 口径判定
def classify_cscv(doc: dict) -> dict:
    """按 README 第 5 节的两条规则判定一个 CSCV 产物 json 的口径。

    返回 ``{neutralize, n_with_field, n_configs, excess_caliber, quotable, blockers}``。

    ⚠️ 三个"不干净"的中间态必须各自报出来，不能一律归成 raw：
    - 字段全体缺失 → raw 口径（旧），不可引用；
    - 字段**部分**存在 → 口径不一致（比全缺更可疑：一次运行里混了两种口径），不可引用；
    - 没有 ``configs`` → 判定不了，如实说"判定不了"，**不默认通过**。
    """
    raw_configs = doc.get("configs")
    configs = raw_configs if isinstance(raw_configs, list) else []
    n_cfg = len(configs)
    n_with = sum(1 for c in configs if isinstance(c, dict) and "score_neutralize" in c)

    if n_cfg == 0:
        neutralize = "unknown"
    elif n_with == n_cfg:
        neutralize = "present"
    elif n_with == 0:
        neutralize = "missing"
    else:
        neutralize = "partial"

    value = None
    if n_cfg and isinstance(configs[0], dict):
        v = configs[0].get("score_neutralize")
        value = bool(v) if isinstance(v, bool) else None

    excess = doc.get("excess_caliber")
    excess = str(excess) if excess else ""

    blockers: list[str] = []
    if neutralize == "unknown":
        blockers.append("产物里没有 configs，无法判定打分口径 —— 不默认通过")
    elif neutralize == "missing":
        blockers.append(
            "configs[].score_neutralize 字段不存在 ⇒ raw 口径（旧）；"
            "「没有该字段」本身即结论，不是「默认中性化」"
        )
    elif neutralize == "partial":
        blockers.append(
            f"只有 {n_with}/{n_cfg} 个配置带 score_neutralize ⇒ 同一次运行里混了口径，不可用"
        )
    if not excess:
        blockers.append("没有 excess_caliber ⇒ 超额类数字是「双扣 rf」修正之前的，不可引用")

    return {
        "neutralize": neutralize,
        "neutralize_value": value,
        "n_with_field": n_with,
        "n_configs": n_cfg,
        "excess_caliber": excess,
        "quotable": not blockers,
        "blockers": blockers,
    }


def classify_factor_run(meta: dict) -> dict:
    """因子研究产物的口径判定（对应 README §4.1：打分口径必须与 IC 估计口径一致）。

    ``neutralized=false`` 的轮次本身不是错误，但它的结论**已被同等中性化口径的运行取代**，
    只在做 A/B 对照（比如第 4 节「原始 vs 中性化」）时才有引用价值 —— 因此单列一档，
    而不是和中性化轮次混在一起排序。
    """
    v = meta.get("neutralized")
    if v is True:
        state, note = "neutralized", ""
    elif v is False:
        state, note = "raw", "raw 口径：IC 在未中性化的面板上估；结论已被中性化口径的运行取代（仅 A/B 对照可用）"
    else:
        state, note = "unknown", "meta.json 里没有 neutralized 字段 ⇒ 口径未知，不默认通过"
    return {"state": state, "note": note}


# --------------------------------------------------------------------- 交叉核对
_ROW_RE = re.compile(r"^\|\s*`?(cscv_[A-Za-z0-9_]+)`?\s*\|")
_MARK_RE = re.compile(r"^[✅❌]")


def readme_quotable_table() -> dict[str, bool]:
    """解析 README「历史结果口径清点」表 → ``{产物名: 是否可引用}``。

    这是**人工维护的另一份声明**。本模块算出来的判定要与它逐行一致；
    不一致就报漂移 —— 与探针台账同一套思路：人写的表会腐烂，且腐烂时不报错。

    解析不出来就返回空 dict（调用方据此报 ``readme_available=false``），
    **不返回一份空表冒充"README 说全都能引用"**。
    """
    if not README.exists():
        return {}
    try:
        text = README.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, bool] = {}
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        last = cells[-1]
        mark = last[:1]
        if not _MARK_RE.match(mark):
            # 有的行把表情写在单元格开头的空白之后
            m2 = _MARK_RE.search(last)
            if not m2:
                continue
            mark = m2.group(0)
        out[m.group(1)] = mark == "✅"
    return out


# --------------------------------------------------------------------- 采集
def _cscv_entries(root: Path) -> list[dict]:
    d = root / CSCV_DIR_NAME
    if not d.is_dir():
        return []
    runs_dir = d
    out: list[dict] = []
    for p in sorted(d.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # ⚠️ 这一支必须与正常支**字段形状一致**（quotable / blockers / caliber 都在），
            # 否则前端读到一条坏产物就崩在 undefined 上 —— 契约不能按分支变。
            blk = [f"产物 json 解析失败：{type(exc).__name__} —— 数字不可复核"]
            out.append({
                "name": p.stem,
                "path": _rel(p),
                "mtime": _mt(p),
                "bytes": _size(p),
                "readable": False,
                "parse_error": f"{type(exc).__name__}: {exc}",
                "weight_basis": "",
                "weight_basis_exists": False,
                "has_returns": (runs_dir / f"{p.stem}_returns.csv").exists(),
                "n_rets_cache": 0,
                "caliber": {"neutralize": "unknown", "quotable": False, "blockers": blk},
                "quotable": False,
                "blockers": blk,
            })
            continue
        if not isinstance(doc, dict):
            continue

        verdict = doc.get("verdict") if isinstance(doc.get("verdict"), dict) else {}
        pbo_main = doc.get("pbo_main") if isinstance(doc.get("pbo_main"), dict) else {}
        pbo_exc = doc.get("pbo_excess") if isinstance(doc.get("pbo_excess"), dict) else {}
        dsr_main = doc.get("dsr_main") if isinstance(doc.get("dsr_main"), dict) else {}
        dsr_exc = doc.get("dsr_excess") if isinstance(doc.get("dsr_excess"), dict) else {}
        period = doc.get("period") if isinstance(doc.get("period"), dict) else {}

        # 权重基准（IC 权重来源目录）是否还在磁盘上 —— 不在就**不可复现**
        wb = str(doc.get("weight_basis") or "")
        wb_rel = wb.replace("\\", "/").lstrip("./")
        wb_exists = bool(wb_rel) and (PROJECT_ROOT / wb_rel).is_dir()

        rets = runs_dir / f"{p.stem}_returns.csv"
        # 每份产物旁边还有一个**去掉 `cscv_` 前缀**的同名目录（里面是 rets/ 逐配置收益缓存）——
        # 它是 `--only-stats` 能"只重算统计量、不重跑回测"的前提，
        # 所以"有没有这个缓存"决定的是**能不能低成本复核**，与"能不能引用"是两件事。
        # ⚠️ 目录名是 `liquid_neu_main` 而 json 名是 `cscv_liquid_neu_main`：
        # 直接拿 stem 去找目录会得到 n_cache=0（假阴性），实踩过。
        cache_dir = runs_dir / _stem_key(p.stem)
        n_cache = (
            sum(1 for _ in cache_dir.rglob("*") if _.is_file()) if cache_dir.is_dir() else 0
        )
        caliber = classify_cscv(doc)
        blockers = list(caliber["blockers"])
        if wb and not wb_exists:
            blockers.append(f"权重基准 {wb} 已不在磁盘上 ⇒ 这次运行无法原样复现")
        quotable = caliber["quotable"] and not (wb and not wb_exists)

        out.append({
            "name": p.stem,
            "path": _rel(p),
            "mtime": _mt(p),
            "bytes": _size(p),
            "readable": True,
            "generated_at": str(doc.get("generated_at") or ""),
            "universe": str(doc.get("universe") or ""),
            "universe_note": str(doc.get("universe_note") or ""),
            "benchmark": str(doc.get("benchmark") or ""),
            "period": {"start": str(period.get("start") or ""), "end": str(period.get("end") or "")},
            "weight_basis": wb,
            "weight_basis_exists": wb_exists,
            "has_returns": rets.exists(),
            "returns_path": _rel(rets) if rets.exists() else "",
            "n_rets_cache": n_cache,
            "pbo_main": _num(pbo_main.get("pbo")),
            "pbo_excess": _num(pbo_exc.get("pbo")),
            "best_t": _num(verdict.get("t_stat")),
            # ⚠️ DSR 的键叫 ``deflated_sharpe``，不叫 ``dsr`` —— 直接取 "dsr" 会静默拿到 None，
            # 页面上表现为"这批产物都没算 DSR"，而不是报错（实踩：第一版就这么写的）。
            "dsr_main": _num(dsr_main.get("deflated_sharpe")),
            "dsr_excess": _num(dsr_exc.get("deflated_sharpe")),
            "haircut": _num(pbo_main.get("haircut")),
            "verdict_significant": verdict.get("significant"),
            "verdict_note": [str(x) for x in (verdict.get("note") or [])][:6],
            "caliber": {**caliber, "quotable": quotable, "blockers": blockers},
            "quotable": quotable,
            "blockers": blockers,
        })
    return out


def _factor_runs(root: Path) -> list[dict]:
    d = root / FACTOR_RUNS_DIR_NAME
    if not d.is_dir():
        return []
    out: list[dict] = []
    for sub in sorted(p for p in d.iterdir() if p.is_dir()):
        meta_p = sub / "meta.json"
        meta: dict = {}
        if meta_p.exists():
            try:
                loaded = json.loads(meta_p.read_text(encoding="utf-8"))
                meta = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                meta = {}
        files = sorted(p.name for p in sub.iterdir() if p.is_file())
        cal = classify_factor_run(meta)
        out.append({
            "name": sub.name,
            "path": _rel(sub),
            "mtime": _mt(sub),
            "has_meta": meta_p.exists(),
            "has_report": (sub / "report.md").exists(),
            "has_summary": (sub / "summary.csv").exists(),
            "n_files": len(files),
            "extra_files": [f for f in files if f.endswith(".broken") or not f.endswith((".csv", ".json", ".md", ".parquet", ".log"))],
            "generated_at": str(meta.get("generated_at") or ""),
            "universe": str(meta.get("universe") or ""),
            "start": str(meta.get("start") or ""),
            "end": str(meta.get("end") or ""),
            "n_factors": int(meta["n_factors"]) if isinstance(meta.get("n_factors"), int) else None,
            "n_rows": int(meta["n_rows"]) if isinstance(meta.get("n_rows"), int) else None,
            "n_symbols": int(meta["n_symbols"]) if isinstance(meta.get("n_symbols"), int) else None,
            "n_days": int(meta["n_days"]) if isinstance(meta.get("n_days"), int) else None,
            "n_significant": int(meta["n_significant"]) if isinstance(meta.get("n_significant"), int) else None,
            "n_strong": int(meta["n_strong"]) if isinstance(meta.get("n_strong"), int) else None,
            "caliber": cal,
        })
    return out


# --------------------------------------------------------------------- 主入口
def archive_index(root: Path | None = None) -> dict:
    """研究产物归档总表。``root`` 可指定（自检用临时目录跑负对照）。"""
    rt = Path(root) if root is not None else RUNTIME_DIR

    cscv = _cscv_entries(rt) if rt.is_dir() else []
    runs = _factor_runs(rt) if rt.is_dir() else []

    reasons: list[str] = []
    if not rt.is_dir():
        reasons.append(
            f"{_rel(rt)}/ 不存在。该目录被 gitignore ⇒ 干净克隆或换机器时**本页必然为空**；"
            "归档是本机运行历史，不是仓库内容。"
        )
    elif not cscv and not runs:
        reasons.append(
            f"{_rel(rt)}/ 下既没有 {CSCV_DIR_NAME}/*.json 也没有 {FACTOR_RUNS_DIR_NAME}/*/ "
            "⇒ 这台机器上还没有可归档的研究产物。"
        )

    table = readme_quotable_table()
    names = {e["name"] for e in cscv}
    drift: list[dict] = []
    if table:
        for e in cscv:
            if e["name"] in table and table[e["name"]] != e["quotable"]:
                drift.append({
                    "kind": "verdict_mismatch",
                    "name": e["name"],
                    "readme": table[e["name"]],
                    "computed": e["quotable"],
                    "blockers": e["blockers"],
                })
        for name in sorted(set(table) - names):
            drift.append({
                "kind": "missing_on_disk",
                "name": name,
                "readme": table[name],
                "computed": None,
                "blockers": ["README 表里列了这个产物，但磁盘上没有对应 json ⇒ 读者无法复核"],
            })

    # 第三类偏差：**磁盘有、README 表没列**（与探针台账的 unregistered 同源）——
    # 产物存在但它的口径状态没人写下来，读者无从判断能不能引用。属提示级，不是错误。
    unlisted = sorted(names - set(table)) if table else []

    # 第四类：只剩收益缓存、结果 json 已丢。收益还在 ⇒ 可重算；但没有当时的口径标记，
    # 所以**即使重算也拿不回"当时那版结论"**。
    cache_only: list[dict] = []
    cdir = rt / CSCV_DIR_NAME
    if cdir.is_dir():
        keys = {_stem_key(n) for n in names}
        for sub in sorted(p for p in cdir.iterdir() if p.is_dir()):
            if _stem_key(sub.name) not in keys:
                cache_only.append({
                    "name": sub.name,
                    "path": _rel(sub),
                    "mtime": _mt(sub),
                    "n_files": sum(1 for f in sub.rglob("*") if f.is_file()),
                })

    quotable = [e for e in cscv if e.get("quotable")]
    stale = [e for e in cscv if not e.get("quotable")]
    mts = [e["mtime"] for e in cscv + runs if e.get("mtime")]

    return {
        "available": not reasons,
        "unavailable_reason": " ".join(reasons),
        "root": _rel(rt),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "readme_available": bool(table),
        "readme_n_rows": len(table),
        "readme_drift": drift,
        "readme_unlisted": unlisted,
        "cache_only_dirs": cache_only,
        "summary": {
            "n_cscv": len(cscv),
            "n_quotable": len(quotable),
            "n_stale": len(stale),
            "n_factor_runs": len(runs),
            "n_unlisted": len(unlisted),
            "n_cache_only": len(cache_only),
            "n_missing_weight_basis": sum(
                1 for e in cscv if e.get("weight_basis") and not e.get("weight_basis_exists")
            ),
            "n_without_returns": sum(
                1 for e in cscv if e.get("readable") and not e.get("has_returns")
            ),
            "latest_mtime": max(mts) if mts else "",
        },
        "cscv": cscv,
        "factor_runs": runs,
        "rules": [
            "可引用 = configs[].score_neutralize 字段存在 + 有 excess_caliber + 权重基准目录仍在磁盘上。",
            "configs[].score_neutralize **不存在** ⇒ raw 口径（旧）——「没有该字段」本身即结论，不等于「默认中性化」。",
            "没有 excess_caliber ⇒ 超额类数字是「双扣 rf」修正之前的。",
            "无 *_returns.csv ⇒ 无法用 --only-stats 重算，只能重跑；n_rets_cache>0 表示旁边还留着逐配置收益缓存。",
            "以上判定与 README「历史结果口径清点」表逐行比对：判定不一致见 readme_drift，"
            "磁盘有而表未列见 readme_unlisted。",
        ],
        "caveat": (
            "归档 = 本机 runtime/ 下的运行历史，不入库；"
            "quotable=false 的产物仍会显示数字，因为它们是「不能引用的示范」，但要与可引用项分开读。"
        ),
    }
