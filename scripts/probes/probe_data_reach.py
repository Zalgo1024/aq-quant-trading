# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""阶段 0.5 数据可得性预检（只读 + 少量联网抽样，不落盘、不改产物）。

五件事：
  1) 老股票从 1990/2005 起，新浪源实际返回多长
  2) 退市股**名单**接口是否存在
  3) 退市股**历史日线**能否拉到（这是消幸存者偏差的硬约束）
  4) 股息率 / 分红数据的历史深度
  5) 已退市代码在现役股票列表里是否还存在（决定偏差有多大）

只读：不写 data_cache、不覆盖任何产物。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import akshare as ak
import pandas as pd

def _project_root() -> Path:
    """向上找到含 config/base.yaml 的目录作为项目根。

    这样脚本放在 runtime/ 还是 scripts/probes/ 都能跑对。
    """
    here = Path(__file__).resolve()
    for cand in (here.parent, *here.parents):
        if (cand / "config" / "base.yaml").exists():
            return cand
    return here.parents[1]


ROOT = _project_root()


def sec(t: str) -> None:
    print("\n" + "=" * 92)
    print(t)
    print("=" * 92)


def try_call(desc: str, fn, *a, **kw):
    print(f"\n>>> {desc}")
    try:
        out = fn(*a, **kw)
        if out is None or (hasattr(out, "empty") and out.empty):
            print("    [空] 返回空")
            return None
        print(f"    [OK] shape={getattr(out, 'shape', '?')}")
        if hasattr(out, "columns"):
            print(f"    列：{list(out.columns)[:16]}")
        if hasattr(out, "head"):
            print(out.head(3).to_string()[:600])
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"    [失败] {type(exc).__name__}: {str(exc)[:200]}")
        return None


def main() -> int:
    print(f"akshare 版本 {ak.__version__}")

    # ---------- 1) 老股票历史长度 ----------
    sec("1) 老股票历史长度（新浪源 stock_zh_a_daily —— 该接口不传起止日期）")
    for sym in ("sh600000", "sz000001", "sh600651"):
        try:
            df = ak.stock_zh_a_daily(symbol=sym, adjust="hfq")
            if df is not None and not df.empty:
                d = pd.to_datetime(df["date"])
                print(f"    {sym}: {len(df)} 行，{d.min()} ~ {d.max()}")
                n90 = int((d < pd.Timestamp("1995-01-01")).sum())
                n05 = int((d < pd.Timestamp("2005-01-01")).sum())
                print(f"        1995 前 {n90} 行 / 2005 前 {n05} 行")
            else:
                print(f"    {sym}: 空")
        except Exception as exc:  # noqa: BLE001
            print(f"    {sym}: 失败 {type(exc).__name__}: {str(exc)[:120]}")

    # ---------- 2) 退市股名单接口 ----------
    sec("2) 退市股名单接口探测")
    cands = [x for x in dir(ak) if ("delist" in x.lower() or "退市" in x or "risk" in x.lower())]
    print(f"    akshare 中候选函数：{cands}")
    for name in cands:
        fn = getattr(ak, name, None)
        if callable(fn):
            try_call(f"{name}()", fn)

    # ---------- 3) 退市股历史日线 ----------
    sec("3) 已退市代码的历史日线能否拉到")
    # 乐视网 300104（2020 退市）、暴风集团 300431（2020 退市）、ST 锐电 601558
    for sym, note in (("sz300104", "乐视网 2020 退市"),
                      ("sz300431", "暴风集团 2020 退市"),
                      ("sh600069", "*ST 银鸽 2020 退市")):
        try:
            df = ak.stock_zh_a_daily(symbol=sym, adjust="hfq")
            if df is not None and not df.empty:
                print(f"    {sym} ({note}): {len(df)} 行，"
                      f"{df['date'].min()} ~ {df['date'].max()}  ✅ 可拉")
            else:
                print(f"    {sym} ({note}): 空")
        except Exception as exc:  # noqa: BLE001
            print(f"    {sym} ({note}): 失败 {type(exc).__name__}: {str(exc)[:120]}")

    # ---------- 4) 股息率 / 分红 ----------
    sec("4) 股息率 / 分红数据历史深度")
    div_cands = [x for x in dir(ak)
                 if any(k in x.lower() for k in ("fhps", "dividend", "fhratio", "fhpg", "fhsp"))]
    print(f"    akshare 中候选函数：{div_cands}")
    for name in ("stock_fhps_em", "stock_fhps_detail_em", "stock_history_dividend_detail"):
        fn = getattr(ak, name, None)
        if callable(fn):
            try:
                try_call(f"{name}(date='20231231')", fn, date="20231231")
            except TypeError:
                try_call(f"{name}('600000')", fn, "600000")

    # ---------- 5) 退市代码是否还在现役列表 ----------
    sec("5) 现役股票列表里是否还含已退市代码")
    lst_p = ROOT / "data_cache" / "stock_list.parquet"
    if lst_p.exists():
        import pandas as pd
        lst = pd.read_parquet(lst_p)
        print(f"    本地股票列表 {len(lst)} 只，列：{list(lst.columns)[:12]}")
        col = next((c for c in ("symbol", "code", "股票代码") if c in lst.columns), lst.columns[0])
        codes = set(lst[col].astype(str).str[-6:])
        for c in ("300104", "300431", "600069", "000033"):
            print(f"    {c} 在列表中：{c in codes}")
        if "list_date" in lst.columns or "上市日期" in lst.columns:
            lc = "list_date" if "list_date" in lst.columns else "上市日期"
            s = pd.to_datetime(lst[lc], errors="coerce")
            print(f"    上市日期范围：{s.min()} ~ {s.max()}")
            print(f"    2005 前上市且仍在列表：{int((s < '2005-01-01').sum())} 只")
    else:
        print(f"    [缺] {lst_p}")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
