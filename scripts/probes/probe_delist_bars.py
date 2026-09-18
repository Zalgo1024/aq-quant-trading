# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只读 data_cache/ 与 runtime/cscv/ 的既有结果。
# 2026-09-18 从 runtime/ 纳入版本控制，目的是让文档里的数字可被外部复现。
# 前置：需要先有 data_cache/（见 README「快速开始」第 5 步拉取全市场日线）。
# ---------------------------------------------------------------------------
"""退市股历史日线的备用源探测（只读，不落盘）。

上一轮预检：新浪 `stock_zh_a_daily` 对已退市代码全部报
`JSONDecodeError: No value to decode`（接口返回空）。
但新浪是主源、腾讯/东财是备用源 —— 备用源走的是**不同上游**，值得一试。

被测代码（已确认退市）：
  sz300104 乐视网（2020 退市）
  sz300431 暴风集团（2020 退市）
  sh600069 *ST 银鸽（2020 退市）
  sz000033 新都退（2019 退市）
  sh600002 齐鲁退市（2006 退市）
  sz000003 PT金田A（2002 退市）
"""
from __future__ import annotations

import akshare as ak

CASES = [
    ("sz300104", "乐视网 2020退"),
    ("sz300431", "暴风集团 2020退"),
    ("sh600069", "*ST银鸽 2020退"),
    ("sz000033", "新都退 2019退"),
    ("sh600002", "齐鲁退市 2006退"),
    ("sz000003", "PT金田A 2002退"),
]


def try_tx(sym: str) -> str | None:
    try:
        df = ak.stock_zh_a_hist_tx(symbol=sym, start_date="19900101",
                                   end_date="20991231", adjust="hfq")
        if df is None or df.empty:
            return "空"
        dcol = next((c for c in df.columns if "date" in c.lower() or "日期" in c), df.columns[0])
        return f"{len(df)} 行  {df[dcol].min()} ~ {df[dcol].max()}"
    except Exception as exc:  # noqa: BLE001
        return f"失败 {type(exc).__name__}: {str(exc)[:80]}"


def try_em(sym: str) -> str | None:
    code = sym[2:]
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date="19900101", end_date="20991231", adjust="hfq")
        if df is None or df.empty:
            return "空"
        dcol = next((c for c in df.columns if "date" in c.lower() or "日期" in c), df.columns[0])
        return f"{len(df)} 行  {df[dcol].min()} ~ {df[dcol].max()}"
    except Exception as exc:  # noqa: BLE001
        return f"失败 {type(exc).__name__}: {str(exc)[:80]}"


def main() -> int:
    print(f"{'代码':<10}{'说明':<18}{'腾讯 tx':<34}{'东财 em'}")
    print("-" * 100)
    for sym, note in CASES:
        print(f"{sym:<10}{note:<18}{str(try_tx(sym)):<34}{str(try_em(sym))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
