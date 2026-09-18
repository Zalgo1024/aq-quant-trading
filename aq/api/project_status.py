"""项目状态装配（回答「这个项目现在怎么样了」）。

设计原则
--------
**能算的一律实时算，算不出的才让人维护。**

- 「数据资产」部分（文件数、覆盖只数、日期范围、覆盖率）全部由本模块扫盘得到，
  不写死任何数字 —— 写死的数字会随数据更新而变成谎言。
- 「研究结论 / 已知缺口 / 下一步」部分来自 ``docs/研究进展.json``，
  每条都带 ``source`` 指向 ``docs/`` 下的真实文档，且可标 ``quotable=false``
  表示「依赖有偏口径，绝对数值不可对外引用」。

后者之所以不写成代码常量：那些是**研究判断**，需要能被人 diff、评审、追责；
塞进 .py 里会变成没人敢改的魔法字符串。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from aq.config.settings import PROJECT_ROOT

STATUS_DOC = PROJECT_ROOT / "docs" / "研究进展.json"
CACHE_DIR = PROJECT_ROOT / "data_cache"


# --------------------------------------------------------------------- 工具
def _count_parquet(d: Path) -> int:
    if not d.exists():
        return 0
    return sum(1 for e in os.scandir(d) if e.name.endswith(".parquet"))


def _count_any(d: Path) -> int:
    if not d.exists():
        return 0
    return sum(1 for _ in os.scandir(d))


def _git_head() -> dict:
    """当前提交与分支（拿不到就返回空，**不编造**）。"""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--pretty=%h|%cd|%s", "--date=short"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return {}
        h, cd, subj = out.stdout.strip().split("|", 2)
        br = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        return {"commit": h, "date": cd, "subject": subj,
                "branch": br.stdout.strip() if br.returncode == 0 else ""}
    except Exception:  # noqa: BLE001
        return {}


def _delisted_expected() -> int:
    """退市股清单条数（分母用清单，不用已抓到的行情文件数）。

    ⚠️ 这两个数**不相等**：清单 333 只，但 ``delisted_bars/`` 只有 306 个文件
    （真缺口 2 只 + 无法获取的僵尸股）。ST 覆盖率的正确分母是"所有曾有行情的
    股票"，即 现役 + 清单，而不是 现役 + 已抓到行情的退市股 ——
    用后者会把分母做小、覆盖率虚高。
    """
    p = CACHE_DIR / "delisted_list.parquet"
    if p.exists():
        try:
            import pandas as pd

            return int(len(pd.read_parquet(p)))
        except Exception:  # noqa: BLE001
            pass
    return _count_parquet(CACHE_DIR / "delisted_bars")


# --------------------------------------------------------------------- 资产
def data_assets() -> dict:
    """扫盘得到的数据资产统计（全部实时计算）。"""
    from aq.data.snapshot import MarketSnapshot, index_overview

    snap = MarketSnapshot()
    try:
        _, meta = snap.load()
    except Exception as exc:  # noqa: BLE001
        meta = {"error": f"{type(exc).__name__}: {exc}"}

    n_bars = _count_parquet(CACHE_DIR / "bars")
    n_delisted = _count_parquet(CACHE_DIR / "delisted_bars")
    n_delisted_exp = _delisted_expected()
    n_st = _count_parquet(CACHE_DIR / "st_flags")
    n_val = _count_parquet(CACHE_DIR / "valuation")
    n_etf_bars = _count_parquet(CACHE_DIR / "etf_bars")

    # ST 逐日状态的需求总量 = 现役有行情的 + 退市清单（退市股在退市前也有 ST 期间）
    need_st = n_bars + max(n_delisted_exp, n_delisted)
    idx = [i for i in index_overview() if i.get("available")]
    idx_all = index_overview()

    return {
        "bars": {
            "n_files": n_bars,
            "first_date": meta.get("bars_first_date", ""),
            "last_date": meta.get("asof", ""),
            "n_active": meta.get("n_active", 0),
            "n_suspended": meta.get("n_suspended", 0),
        },
        "delisted_bars": {"n_files": n_delisted, "n_listed": n_delisted_exp},
        "st_flags": {
            "n_files": n_st,
            "n_needed": need_st,
            "ratio": round(n_st / need_st, 4) if need_st else 0.0,
        },
        "valuation": {"n_files": n_val},
        "etf": {
            "n_bars": n_etf_bars,
            "n_nav": _count_any(CACHE_DIR / "etf_nav"),
        },
        "index_bars": {
            "available": [i["symbol"] for i in idx],
            "missing": [i["symbol"] for i in idx_all if not i.get("available")],
            "n_available": len(idx),
            "n_expected": len(idx_all),
        },
        "calendar": {"n_trade_days": _read_calendar_len()},
        "liquid_snapshot": meta.get("n_liquid_snapshot", 0),
        "caliber_note": meta.get("caliber_note", ""),
    }


def _read_calendar_len() -> int:
    p = CACHE_DIR / "calendar.parquet"
    if not p.exists():
        return 0
    try:
        import pandas as pd

        return int(len(pd.read_parquet(p)))
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------- 主入口
def project_status() -> dict:
    """完整状态：人工维护的研究结论 + 实时计算的数据资产。"""
    curated: dict = {}
    doc_err = ""
    if STATUS_DOC.exists():
        try:
            curated = json.loads(STATUS_DOC.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            doc_err = f"读取 {STATUS_DOC.name} 失败：{type(exc).__name__} {exc}"
    else:
        doc_err = f"缺少 {STATUS_DOC.name}"

    stages = curated.get("stages", [])
    # 阶段进度由条目状态推出（done=1 / blocked|in_progress=0.5 / todo=0），
    # 不用手填百分比 —— 手填的百分比一定会在某次改动后失真。
    _w = {"done": 1.0, "blocked": 0.5, "in_progress": 0.5, "todo": 0.0}
    for st in stages:
        items = st.get("items", [])
        n = len(items) or 1
        st["progress"] = round(sum(_w.get(i.get("status", "todo"), 0.0) for i in items) / n, 4)
        st["n_items"] = len(items)
        st["n_done"] = sum(1 for i in items if i.get("status") == "done")

    return {
        "updated_at": curated.get("updated_at", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "doc_error": doc_err,
        "stages": stages,
        "verdicts": curated.get("verdicts", []),
        "gaps": curated.get("gaps", []),
        "next_steps": curated.get("next_steps", []),
        "data_assets": data_assets(),
        "repo": _git_head(),
    }
