"""探针：ETF 分红数据源矩阵（找出新浪源的覆盖缺口）。

为什么单独立一个探针
--------------------
``probe_etf_total_return.py`` 跑出一个**当场就该怀疑**的结果：

    红利低波ETF(512890)  新浪分红记录 **1 条**，累计分红 0.000 元/份
    创业板ETF(159915)    新浪分红记录 **0 条**
    科创50ETF(588000)    新浪分红记录 **0 条**
    黄金ETF(518880)      新浪分红记录 **0 条**

其中 512890（华泰柏瑞红利低波）自 2024 年起**按月分红**，报 0 条
几乎必然是**数据缺失**而非事实。这与本项目的既有教训完全一致：

    「某因子/某字段报 0，先证明它是真 0，再拿它做结论。」

如果分红缺失发生在**红利类 ETF** 上，后果是致命的 ——
红利 ETF 的收益几乎全在分红里（510880 实测分红拖累 4.91pp/年），
漏掉分红 = 把 B 路线（红利低波）的超额算成负数。所以本探针的目的
不是"再确认一次新浪能用"，而是**给每只标的分红数据做真 0 / 假 0 的判别**。

判别手段（不依赖外部资料，纯数据自证）
--------------------------------------
1. **东财基金分红** ``fund_fh_em``：独立于新浪的第二来源。
2. **拆分表** ``fund_cf_em``：若某日发生份额折算，价格会跳空但**不是分红**。
   没有这一步，折算会被误判成"漏掉的分红"。
3. **净值缺口反推**：``累计净值 − 单位净值`` 的**跳升**只能来自分红
   （累计净值不含再投资、含分红）。逐日取差分，跳升日 = 疑似分红日。
   这是**与两个来源都独立**的第三个证据，用来打破"两源都错"的僵局。
   ⚠️ 份额折算会同时改变两者，所以必须配合 (2) 使用。

运行::

    python scripts/probes/probe_etf_dividend_source.py

输出写入 ``runtime/etf_dividend_source.log``（由调用方重定向）。
"""

from __future__ import annotations

import os
import sys
import threading
import time

import akshare as ak
import pandas as pd

#: 重点核查对象：新浪报 0 或报"0.000 元/份"的标的，外加红利类对照。
#: ``expect`` 只是"该不该有分红"的先验，不作为判据 —— 判据是三个数据源互相印证。
FOCUS = [
    # symbol, 代码, 简称, 先验是否有分红
    ("sh512890", "512890", "红利低波ETF", "应有（月频分红）"),
    ("sh510880", "510880", "上证红利ETF", "应有（对照组，新浪有 19 条）"),
    ("sz159915", "159915", "创业板ETF", "少见（宽基）"),
    ("sh588000", "588000", "科创50ETF", "少见（宽基）"),
    ("sh518880", "518880", "黄金ETF", "一般无（商品）"),
    ("sh513100", "513100", "纳指ETF", "少见（跨境）"),
    ("sh515180", "515180", "红利ETF易方达", "应有"),
    ("sh563020", "563020", "低波红利ETF", "应有"),
]

#: 新浪分红表里这几列是事实标准（已在 probe_etf_reach 实测确认）。
_SINA_DATE_COL = "日期"
_SINA_CUM_COL = "累计分红"

_TIMEOUT = 90.0
_CACHE: dict[str, tuple[str, object, float]] = {}


def _probe(label: str, fn, timeout: float = _TIMEOUT) -> tuple[str, object, float]:
    """在线程里跑 ``fn``；超时返回 timeout 而不是挂死（腾讯/东财都会偶发挂住）。"""
    box: dict[str, object] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - 探针要记录失败类型
            box["error"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=work, daemon=True)
    t0 = time.time()
    th.start()
    th.join(timeout)
    dt = time.time() - t0
    if th.is_alive():
        return "timeout", None, dt
    if "error" in box:
        return "error", box["error"], dt
    return "ok", box["value"], dt


def _cached(key: str, fn, timeout: float = _TIMEOUT) -> tuple[str, object, float]:
    if key not in _CACHE:
        _CACHE[key] = _probe(key, fn, timeout)
    return _CACHE[key]


def _rows(v: object) -> int:
    try:
        return len(v)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return -1


def _show(label: str, status: str, value: object, elapsed: float) -> None:
    if status != "ok":
        print(f"  [{label}] {status.upper()} ({elapsed:.1f}s) {str(value)[:160]}")
    else:
        print(f"  [{label}] OK ({elapsed:.1f}s) rows={_rows(value)}")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 第 1 段：候选来源矩阵
# ---------------------------------------------------------------------------

def section_1_sources() -> None:
    print("=" * 100)
    print("第 1 段 候选分红来源矩阵（找替新浪的备源）")
    print("=" * 100)

    cands = [
        ("东财-基金分红(fund_fh_em)", lambda: ak.fund_fh_em()),
        ("东财-基金拆分(fund_cf_em)", lambda: ak.fund_cf_em()),
        ("新浪-ETF分红(逐只 sh510880)", lambda: ak.fund_etf_dividend_sina(symbol="sh510880")),
        ("东财-ETF净值(510880)", lambda: ak.fund_etf_fund_info_em(
            fund="510880", start_date="20040101", end_date="20260915")),
    ]
    for label, fn in cands:
        st, v, dt = _cached(label, fn)
        _show(label, st, v, dt)
        if st == "ok" and _rows(v) > 0:
            df = v  # type: ignore[assignment]
            print(f"      列 = {list(df.columns)}")  # type: ignore[union-attr]
            print(df.head(4).to_string()[:900])  # type: ignore[union-attr]
        sys.stdout.flush()
    print()


# ---------------------------------------------------------------------------
# 第 2 段：逐只三源对照
# ---------------------------------------------------------------------------

def section_2_crosscheck() -> None:
    print("=" * 100)
    print("第 2 段 逐只三源对照：新浪 / 东财分红表 / 净值缺口反推")
    print("=" * 100)

    st_fh, fh, _ = _cached("东财-基金分红(fund_fh_em)", lambda: ak.fund_fh_em())
    fh_ok = st_fh == "ok" and fh is not None and len(fh) > 0  # type: ignore[arg-type]
    if fh_ok:
        cols = list(fh.columns)  # type: ignore[union-attr]
        print(f"  东财分红表列 = {cols}")
        code_col = next((c for c in cols if "代码" in c), None)
        if code_col:
            fh = fh.copy()  # type: ignore[union-attr]
            fh["_c"] = fh[code_col].astype(str).str.zfill(6)
        print()
    else:
        print(f"  ⚠️ 东财分红表不可用（{st_fh}）→ 本段退化为「新浪 vs 净值缺口」两源对照")
        print()

    hdr = f"  {'symbol':<10}{'简称':<16}{'新浪条数':>9}{'新浪累计':>10}{'东财条数':>9}{'东财累计':>10}{'净值缺口跳升':>13}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    mismatch: list[str] = []
    for sym, code, name, _prior in FOCUS:
        # --- 新浪 ---
        st_s, sdf, _ = _cached(f"新浪-{code}", (lambda s=sym: ak.fund_etf_dividend_sina(symbol=s)))
        s_n, s_cum = 0, 0.0
        if st_s == "ok" and sdf is not None and len(sdf) > 0:  # type: ignore[arg-type]
            s_n = len(sdf)  # type: ignore[arg-type]
            if _SINA_CUM_COL in sdf.columns:  # type: ignore[union-attr]
                s_cum = float(pd.to_numeric(sdf[_SINA_CUM_COL], errors="coerce").fillna(0).iloc[-1])  # type: ignore[union-attr]

        # --- 东财分红表 ---
        e_n, e_cum = 0, 0.0
        if fh_ok and "_c" in fh.columns:  # type: ignore[union-attr]
            one = fh[fh["_c"] == code]  # type: ignore[union-attr]
            e_n = len(one)
            amt_col = next((c for c in one.columns if "每份" in c or "分红" in c and "比" not in c), None)
            if e_n and amt_col:
                e_cum = float(pd.to_numeric(one[amt_col], errors="coerce").fillna(0).sum())

        # --- 净值缺口反推 ---
        jumps = 0
        st_n, nav, _ = _cached(f"净值-{code}", (lambda c=code: ak.fund_etf_fund_info_em(
            fund=c, start_date="20040101", end_date="20260915")))
        if st_n == "ok" and nav is not None and len(nav) > 0:  # type: ignore[arg-type]
            nav = nav.copy()  # type: ignore[union-attr]
            u_col = next((c for c in nav.columns if c in ("单位净值",)), None)
            a_col = next((c for c in nav.columns if "累计净值" in c), None)
            if u_col and a_col:
                gap = (pd.to_numeric(nav[a_col], errors="coerce")
                       - pd.to_numeric(nav[u_col], errors="coerce"))
                gap = gap.dropna()
                if len(gap) > 1:
                    # 缺口**跳升**才对应分红；缓慢漂移多是数据噪声。
                    d = gap.diff()
                    thr = max(0.002, float(d.std() or 0.0) * 3.0)
                    jumps = int((d > thr).sum())

        flag = ""
        if s_n == 0 and (jumps > 0 or e_n > 0):
            flag = "  ⚠️ 新浪漏（假 0）"
            mismatch.append(f"{code} {name}")
        elif s_n <= 1 and jumps >= 3:
            flag = "  ⚠️ 新浪缺多半"
            mismatch.append(f"{code} {name}")
        print(f"  {sym:<10}{name:<16}{s_n:>9}{s_cum:>10.3f}{e_n:>9}{e_cum:>10.3f}{jumps:>13}{flag}")
        sys.stdout.flush()

    print()
    if mismatch:
        print(f"  >>> **新浪源不可用的标的 = {len(mismatch)} 只**：" + "；".join(mismatch))
        print("  >>> 结论：ETF 全收益序列**不能只依赖新浪分红表**，必须多源合并。")
    else:
        print("  >>> 三源一致，新浪源在本批标的上无缺口。")
    print()


# ---------------------------------------------------------------------------
# 第 3 段：新浪漏报的严重性（用净值缺口估漏掉的金额）
# ---------------------------------------------------------------------------

def section_3_impact() -> None:
    print("=" * 100)
    print("第 3 段 漏报的代价：把缺口当分红补回去，年化差多少")
    print("=" * 100)
    print("  做法：对每只标的算三条年化 ——")
    print("        A. 价格口径（漏分红）")
    print("        B. 价格 + 新浪分红")
    print("        C. 价格 + 净值缺口反推的分红（独立于来源表）")
    print()

    hdr = f"  {'symbol':<10}{'简称':<16}{'区间':<24}{'年数':>6}{'A 价格':>9}{'B +新浪':>10}{'C +缺口':>10}{'B-A':>8}{'C-A':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for sym, code, name, _prior in FOCUS:
        st_p, px, _ = _cached(f"行情-{code}", (lambda s=sym: ak.stock_zh_a_hist_tx(
            symbol=s, start_date="20040101", end_date="20260915")))
        if st_p != "ok" or px is None or len(px) == 0:  # type: ignore[arg-type]
            print(f"  {sym:<10}{name:<16}行情不可用（{st_p}）")
            continue
        px = px.copy()  # type: ignore[union-attr]
        px["date"] = pd.to_datetime(px["date"])
        px = px.sort_values("date").reset_index(drop=True)
        p = pd.Series(pd.to_numeric(px["close"], errors="coerce").values,
                      index=px["date"]).dropna()
        if len(p) < 60:
            continue

        def ann(r: pd.Series) -> float:
            r = r.dropna()
            if len(r) < 30:
                return float("nan")
            return float((1.0 + r).prod() ** (252.0 / len(r)) - 1.0)

        a = ann(p.pct_change())

        # B：新浪累计分红
        st_s, sdf, _ = _cached(f"新浪-{code}", (lambda s=sym: ak.fund_etf_dividend_sina(symbol=s)))
        b = float("nan")
        if st_s == "ok" and sdf is not None and len(sdf) > 1:  # type: ignore[arg-type]
            d = pd.Series(pd.to_numeric(sdf[_SINA_CUM_COL], errors="coerce").values,  # type: ignore[union-attr]
                          index=pd.to_datetime(sdf[_SINA_DATE_COL]))  # type: ignore[union-attr]
            d = d[~d.index.duplicated(keep="last")].reindex(p.index, method="ffill").fillna(0.0)
            b = ann(((p + d.diff().fillna(0.0)) / p.shift(1) - 1.0))

        # C：净值缺口反推
        c = float("nan")
        st_n, nav, _ = _cached(f"净值-{code}", (lambda cc=code: ak.fund_etf_fund_info_em(
            fund=cc, start_date="20040101", end_date="20260915")))
        if st_n == "ok" and nav is not None and len(nav) > 1:  # type: ignore[arg-type]
            nav = nav.copy()  # type: ignore[union-attr]
            u_col = next((x for x in nav.columns if x == "单位净值"), None)
            a_col = next((x for x in nav.columns if "累计净值" in x), None)
            if u_col and a_col:
                nav["_d"] = pd.to_datetime(nav["净值日期"] if "净值日期" in nav.columns
                                           else nav.iloc[:, 0])
                gap = (pd.to_numeric(nav[a_col], errors="coerce")
                       - pd.to_numeric(nav[u_col], errors="coerce")).values
                g = pd.Series(gap, index=nav["_d"]).dropna()
                g = g[~g.index.duplicated(keep="last")].sort_index()
                gg = g.reindex(p.index, method="ffill").fillna(0.0)
                c = ann((p + gg.diff().fillna(0.0)) / p.shift(1) - 1.0)

        years = (p.index[-1] - p.index[0]).days / 365.25
        rng = f"{p.index[0].date()} ~ {p.index[-1].date()}"
        print(f"  {sym:<10}{name:<16}{rng:<24}{years:>6.1f}"
              f"{a:>9.2%}{b:>10.2%}{c:>10.2%}"
              f"{(b - a) * 100:>7.2f}pp{(c - a) * 100:>7.2f}pp")
        sys.stdout.flush()

    print()
    print("  >>> 读法：B−A 是新浪认账的拖累，C−A 是净值缺口认账的拖累。")
    print("  >>> 若 C−A 显著大于 B−A → 新浪漏报，且漏的量级足以改写结论。")
    print("  >>> 若 C 明显异常（如超过 10%）→ 更可能是份额折算，需查 fund_cf_em。")
    print()


def main() -> int:
    print("ETF 分红数据源探针")
    print(f"akshare = {ak.__version__}")
    print("判据：某只标的的新浪分红条数为 0 或累计为 0.000 时，")
    print("      必须由「东财分红表」或「净值缺口跳升」**至少一方**证实为假 0。")
    print()
    sys.stdout.flush()

    section_1_sources()
    section_2_crosscheck()
    section_3_impact()

    print("=" * 100)
    print("第 4 段 结论（必须原样抄进文档 / 代码）")
    print("=" * 100)
    print()
    print("  本探针只负责摆证据；结论行由运行结果填写，见 runtime/etf_dividend_source.log。")
    print()
    print("完成。")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    os._exit(main())
