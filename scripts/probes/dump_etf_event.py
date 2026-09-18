"""把 ETF 折算事件附近的「价格 / 官方净值 / 官方日增长率」三源原始行并排打出来。

目的：判定「官方日增长率」是否可作为权威全收益序列，以及折算因子到底该取多少。
只打印窄窗口，输出必须小。

用法：
    python scripts/probes/dump_etf_event.py
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

# 路径解析：向上找 config/base.yaml（不要写 parents[N]，换目录会指错层级）
_here = Path(__file__).resolve()
_root = next(p for p in _here.parents if (p / "config" / "base.yaml").exists())
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

warnings.filterwarnings("ignore")

import akshare as ak  # noqa: E402
import pandas as pd  # noqa: E402

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 50)

EVENTS = [
    ("512890", "2021-10-15", "2021-11-02"),
    ("159915", "2024-09-20", "2024-10-15"),
    ("588000", "2024-09-20", "2024-10-15"),
    ("510500", "2015-04-01", "2015-04-22"),
    ("512100", "2022-08-25", "2022-09-15"),
    ("513100", "2022-01-05", "2022-01-25"),
    ("513500", "2022-03-20", "2022-04-08"),
]


def _pfx(code: str) -> str:
    return "sz" if code.startswith(("15", "16")) else "sh"


def dump_nav(code: str, d0: str, d1: str) -> pd.DataFrame:
    n = ak.fund_etf_fund_info_em(fund=code, start_date="20000101", end_date="20260915")
    n["净值日期"] = pd.to_datetime(n["净值日期"])
    w = n[(n["净值日期"] >= d0) & (n["净值日期"] <= d1)].copy()
    return w.sort_values("净值日期").reset_index(drop=True)


def dump_px(code: str, d0: str, d1: str) -> pd.DataFrame:
    p = ak.stock_zh_a_hist_tx(
        symbol=_pfx(code) + code, start_date="20040101", end_date="20260915"
    )
    p["date"] = pd.to_datetime(p["date"])
    p["close"] = pd.to_numeric(p["close"], errors="coerce")
    w = p[(p["date"] >= d0) & (p["date"] <= d1)].copy()
    return w.sort_values("date").reset_index(drop=True)


def _num(x):
    """把 '+1.23%' / '1.23' / '' 统一成 float 或 nan（保留是否为百分号的信息）。"""
    if x is None:
        return float("nan")
    s = str(x).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "nan", "None"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def show(code: str, d0: str, d1: str) -> None:
    print("=" * 118)
    print(f"{code}  窗口 {d0} ~ {d1}")
    try:
        n = dump_nav(code, d0, d1)
        if n.empty:
            print("  净值：窗口内 0 行")
        else:
            raw = n["日增长率"].astype(str).tolist()
            print(f"  净值 {len(n)} 行  |  日增长率原始样本 = {raw[:3]}")
            nb = n.copy()
            nb["日增%(数值)"] = nb["日增长率"].map(_num)
            nb["单位净值"] = nb["单位净值"].map(_num)
            nb["累计净值"] = nb["累计净值"].map(_num)
            nb["gap=累计-单位"] = nb["累计净值"] - nb["单位净值"]
            nb["单位比"] = nb["单位净值"] / nb["单位净值"].shift(1)
            nb["gap变动"] = nb["gap=累计-单位"].diff()
            print(
                nb[
                    [
                        "净值日期",
                        "单位净值",
                        "累计净值",
                        "gap=累计-单位",
                        "单位比",
                        "gap变动",
                        "日增%(数值)",
                    ]
                ].to_string(index=False)
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  净值 ERR {type(exc).__name__}: {exc}")

    try:
        p = dump_px(code, d0, d1)
        if p.empty:
            print("  价格：窗口内 0 行")
        else:
            pb = p[["date", "close"]].copy()
            pb["价格比"] = pb["close"] / pb["close"].shift(1)
            print(f"  价格 {len(p)} 行")
            print(pb.to_string(index=False))
    except Exception as exc:  # noqa: BLE001
        print(f"  价格 ERR {type(exc).__name__}: {exc}")


def test_em_adjust() -> None:
    """测东财 ETF 行情接口在三种复权口径下的可用性 —— 若能拿到 hfq，全收益问题直接解决。"""
    print("=" * 118)
    print("东财 fund_etf_hist_em 复权口径可用性测试")
    for code in ["512890", "510300", "159915"]:
        for adj in ["", "qfq", "hfq"]:
            try:
                d = ak.fund_etf_hist_em(
                    symbol=code, period="daily",
                    start_date="20040101", end_date="20260915", adjust=adj,
                )
                d["日期"] = pd.to_datetime(d["日期"])
                d = d.sort_values("日期")
                first = d.iloc[0]
                # 512890 拆分日 2021-10-25 附近的价格比 —— hfq 应无 −50% 跳空
                w = d[(d["日期"] >= "2021-10-20") & (d["日期"] <= "2021-10-28")]
                ratios = (w["收盘"] / w["收盘"].shift(1)).round(4).tolist()
                print(
                    f"  {code} adjust={adj!r:<5} n={len(d):<5} "
                    f"首={first['日期'].date()} {first['收盘']:<8} "
                    f"末={d.iloc[-1]['日期'].date()} {d.iloc[-1]['收盘']:<8} "
                    f"| 拆分窗口价格比={ratios}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  {code} adjust={adj!r:<5} ERR {type(exc).__name__}: {str(exc)[:80]}")


if __name__ == "__main__":
    for code, d0, d1 in EVENTS:
        show(code, d0, d1)
    test_em_adjust()
