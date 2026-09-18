# ---------------------------------------------------------------------------
# 研究探针（只读）：不改任何产物，只探测外部数据源的可得性。
# 2026-09-18 新建（纳入版本控制），目的是让 docs/etf 路线文档里的每个数字可被外部复现。
# 前置：需要 akshare（见 README「快速开始」）；不需要先有 data_cache/。
# 注意：本探针**不写任何文件**，输出靠重定向（runtime/etf_reach.log）。
# ---------------------------------------------------------------------------
"""ETF 数据来源可得性探测（阶段 1a-1，只读）。

为什么需要它
------------
转向 ETF 路线后，第一件事不是写策略，而是搞清楚**数据能不能拿到**。
个股侧的教训很直接：东财 K 线接口（``push2his``）在本机代理下被拦，
退市股日线只有腾讯源能拿 —— 这些都不是"应该能拿到"，是实测出来的。

本探针固化的四段输出
--------------------
1. **数据源矩阵** —— 逐个接口探可用性、行数、覆盖区间；
2. **ETF 池口径** —— 现役清单并集、名录是否含已终止产品、代码段规则的反例；
3. **分红/复权口径** —— 单位净值 vs 累计净值、腾讯源是否支持 ETF 复权；
4. **偏差方向声明** —— 我们漏掉了哪一类标的、会让回测往哪个方向偏。

三条必须记住的结论（2026-09-18 实测）
------------------------------------
- 腾讯 ``stock_zh_a_hist_tx`` 与新浪 ``fund_etf_hist_sina`` 都能回溯到
  **2005-02-23**（510050 上市首日，5243 / 5245 行）→ 起点需求不需要付费源。
- ``fund_name_em`` **不含已终止基金**：分级基金 ``150`` 段曾有 100+ 只，
  2020 年底按监管要求全部终止/转型，如今名录里只剩 6+2=8 只。
- **已退市 ETF 的行情两条源都拿不到**（腾讯 IndexError、新浪空），
  与个股退市股（腾讯源有数据）**相反** → ETF 侧只能给偏差上界，不能给点估计。

两处容易被写错、本探针会当场纠正的地方
--------------------------------------
- 东财 ``push2his`` **不是永久封锁，是间歇性可用**（同一天先 ProxyError
  后 OK 都出现过）→ 不能当主源，但可以作为第三源偶尔交叉校验。
- ``fund_etf_dividend_sina`` **是逐只接口**（默认 ``symbol='sh510050'``），
  返回 18 行不是"全市场覆盖不足"，而是 510050 这一只的分红史。
  → ETF 分红可以逐只拿，全收益序列可以自己构造（见
  ``probe_etf_total_return.py``，实测分红拖累 1.7~3.2pp/年）。

用法::

    python scripts/probes/probe_etf_reach.py                # 全部四段
    python scripts/probes/probe_etf_reach.py --section 1    # 只跑数据源矩阵
    python scripts/probes/probe_etf_reach.py --section 2 3  # 跑池口径与复权口径
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path


# --- 项目根：向上找 config/base.yaml（**禁止** parents[N]，换目录会指错层级）---
def _find_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "config" / "base.yaml").exists():
            return p
    raise RuntimeError(f"未找到 config/base.yaml（从 {start} 向上找）")


ROOT = _find_root(Path(__file__).resolve().parent)

#: ETF 代码段（**仅用于统计分布，不能用作 ETF 识别规则** —— 见第 2 段的反例）
ETF_CODE_SEGMENTS = (
    "510", "511", "512", "513", "515", "516", "517", "518", "520",
    "560", "561", "562", "563", "588", "589", "159",
)

#: 已不在现役清单、但出现在东财名录里的代码（2026-09-18 实测差集，用于第 2 段）
GHOST_CODES = ["159002", "159004", "159006", "510080", "510081", "560002", "560003"]

#: 已确认退市的个股（对照组：证明腾讯源对个股退市股是有效的）
DELISTED_STOCKS = ["sz300104", "sh600069"]

# --- 缓存：同一次运行里每个接口只拉一次（name_em / spot 各要 20+ 秒）---
_CACHE: dict[str, tuple[str, object, float]] = {}

#: 单次接口超时（秒），由 ``--timeout`` 覆盖。按需 lazy 到线程里跑，
#: 因为部分 akshare 接口在代理不通时会长时间挂住。
TIMEOUT = 70.0


def _probe(label: str, fn, timeout: float | None = None) -> tuple[str, object, float]:
    """在线程里跑 ``fn``，超时则放弃（前台跑长任务会被 SIGTERM，超时保护必要）。

    返回 ``(status, value, elapsed)``，status ∈ ok / error / timeout。
    """
    timeout = TIMEOUT if timeout is None else timeout
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["v"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["e"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_worker)
    t.daemon = True
    t0 = time.time()
    t.start()
    t.join(timeout)
    dt = time.time() - t0
    if t.is_alive():
        return ("timeout", None, dt)
    if "e" in box:
        return ("error", box["e"], dt)
    return ("ok", box["v"], dt)


def _cached(key: str, fn, timeout: float | None = None) -> tuple[str, object, float]:
    if key not in _CACHE:
        _CACHE[key] = _probe(key, fn, timeout)
    return _CACHE[key]


def _rows(v) -> int:
    try:
        return len(v)
    except Exception:  # noqa: BLE001
        return -1


def _date_range(df) -> str:
    """从 DataFrame 里找一个日期列，返回 ``min ~ max``。找不到返回 ``-``。"""
    if df is None or _rows(df) <= 0:
        return "-"
    import pandas as pd

    for col in df.columns:
        cl = str(col).strip().lower()
        if cl in ("date", "time", "日期", "净值日期", "trade_date"):
            s = pd.to_datetime(df[col], errors="coerce").dropna()
            if len(s):
                return f"{s.min().date()} ~ {s.max().date()}"
    return "-"


def _fmt(st: str, v, dt: float, expect: str = "") -> str:
    if st == "ok":
        head = f"[OK ] {_rows(v):>6} 行  {_date_range(v):<25}"
    elif st == "error":
        head = f"[ERR] {str(v)[:70]:<99}"
    else:
        head = f"[TO ] 超时 {dt:.0f}s"
    return head + (f"  期望 {expect}" if expect else "")


# ---------------------------------------------------------------------------
# 第 1 段：数据源矩阵
# ---------------------------------------------------------------------------

def section_sources() -> None:
    import akshare as ak

    print("=" * 110)
    print("第 1 段 数据源矩阵（akshare %s）" % ak.__version__)
    print("=" * 110)
    print("说明：行数会随交易日漂移，±0.5% 属正常；「期望」是 2026-09-18 的实测值。")
    print("-" * 110)

    cases = [
        ("现役ETF清单-东财", lambda: ak.fund_etf_spot_em(), "1621"),
        ("现役ETF清单-新浪", lambda: ak.fund_etf_category_sina(symbol="ETF基金"), "1674"),
        ("全量基金名录-东财", lambda: ak.fund_name_em(), "27869"),
        ("ETF日线-东财(510300)", lambda: ak.fund_etf_hist_em(
            symbol="510300", period="daily", start_date="20190101",
            end_date="20260915", adjust="qfq"),
         "1870 或 ProxyError（代理间歇放行，别当主源）"),
        ("ETF日线-腾讯(510050)", lambda: ak.stock_zh_a_hist_tx(
            symbol="sh510050", start_date="20040101", end_date="20260915"),
         "5243 行 2005-02-23~"),
        ("ETF日线-新浪(510050)", lambda: ak.fund_etf_hist_sina(symbol="sh510050"),
         "5245 行 2005-02-23~"),
        ("ETF净值-东财(510300)", lambda: ak.fund_etf_fund_info_em(
            fund="510300", start_date="20040101", end_date="20260915"), "1874"),
        ("上交所ETF规模", lambda: ak.fund_etf_scale_sse(), "593（统计日期滞后）"),
        ("深交所ETF规模", lambda: ak.fund_etf_scale_szse(), "akshare bug"),
        ("ETF分红-新浪(510050)", lambda: ak.fund_etf_dividend_sina(), "18 = 单只的分红"),
        ("个股退市日线-腾讯(对照)", lambda: ak.stock_zh_a_hist_tx(
            symbol="sz300104", start_date="19900101", end_date="20991215"), "有数据"),
    ]
    for name, fn, expect in cases:
        st, v, dt = _cached(name, fn)
        print(f"{name:<24}{_fmt(st, v, dt, expect)}")

    print()
    print("已退市 ETF 的日线（8 只已不在现役清单的代码，逐只探两个源）")
    ghosts = ["159002", "159004", "159006", "159047", "510080", "510081", "511080", "512470"]
    for code in ghosts:
        pfx = "sz" if code.startswith(("15", "16")) else "sh"
        st_t, v_t, dt_t = _cached(f"tx-{code}", lambda c=code, p=pfx: ak.stock_zh_a_hist_tx(
            symbol=p + c, start_date="20040101", end_date="20260915"))
        st_s, v_s, dt_s = _cached(f"sina-{code}", lambda c=code, p=pfx: ak.fund_etf_hist_sina(
            symbol=p + c))
        print(f"  {code}  腾讯 {_fmt(st_t, v_t, dt_t):<46}新浪 {_fmt(st_s, v_s, dt_s)}")


# ---------------------------------------------------------------------------
# 第 2 段：ETF 池口径
# ---------------------------------------------------------------------------

def section_pool() -> None:
    import akshare as ak
    import pandas as pd

    print()
    print("=" * 110)
    print("第 2 段 ETF 池口径：现役清单 / 名录是否含已终止 / 代码段规则反例")
    print("=" * 110)

    st_spot, spot, _ = _cached("现役ETF清单-东财", lambda: ak.fund_etf_spot_em())
    st_sina, sina_l, _ = _cached("现役ETF清单-新浪",
                                 lambda: ak.fund_etf_category_sina(symbol="ETF基金"))
    st_name, name_all, _ = _cached("全量基金名录-东财", lambda: ak.fund_name_em())

    live: set[str] = set()
    if st_spot == "ok":
        live |= set(spot["代码"].astype(str).str.zfill(6))
    if st_sina == "ok":
        live |= set(sina_l["代码"].astype(str).str.replace("sh", "").str.replace("sz", "")
                    .str.zfill(6))
    print(f"现役清单并集 = {len(live)} 只")
    if st_spot == "ok":
        print(f"  东财 spot 独有 = {len(set(spot['代码'].astype(str).str.zfill(6)) - live)}")
    if st_sina == "ok":
        s_only = set(sina_l["代码"].astype(str).str.replace("sh", "").str.replace("sz", "")
                     .str.zfill(6)) - set(spot["代码"].astype(str).str.zfill(6)) \
            if st_spot == "ok" else set()
        print(f"  新浪 list 独有 = {len(s_only)}（东财 spot 未收录）")

    if st_name != "ok":
        print("[跳过] fund_name_em 不可用，无法做名录分析")
        return

    codes = name_all["基金代码"].astype(str).str.zfill(6)
    print()
    print("名录里的代码段分布（证明「名录 = 当前存续」）")
    for seg in ("150", "151", "159", "16", "161", "510", "511", "512", "513",
                "515", "516", "517", "518", "520", "560", "561", "562", "563",
                "588", "589"):
        print(f"  seg {seg} = {int(codes.str.startswith(seg).sum())}")
    print("  >>> 铁证：分级基金 150 段曾有 100+ 只（2020 年底全部终止/转型），")
    print("  >>>       如今名录里只剩 150=6 + 151=2 = 8 只 → 名录**不含已终止产品**。")

    seg_mask = codes.str.startswith(ETF_CODE_SEGMENTS)
    in_seg = set(codes[seg_mask])
    print()
    print(f"名录里「代码落在 ETF 段」的 = {len(in_seg)} 只")
    print(f"名录有 / 现役清单无 = {len(in_seg - live)} 只")
    print(f"  >>> 但这**不等于**「已清盘清单」：见下面的反例。")

    print()
    print("代码段规则的反例（这些代码落在 ETF 段，但**不是 ETF**）")
    print(f"  {'代码':<8}{'净值':<14}{'区间':<28}{'腾讯日线':<12}{'新浪日线'}")
    for code in GHOST_CODES:
        pfx = "sz" if code.startswith(("15", "16")) else "sh"
        st_n, nav, _ = _cached(f"nav-{code}", lambda c=code: ak.fund_etf_fund_info_em(
            fund=c, start_date="19900101", end_date="20260915"))
        st_t, v_t, _ = _cached(f"tx-{code}", lambda c=code, p=pfx: ak.stock_zh_a_hist_tx(
            symbol=p + c, start_date="20040101", end_date="20260915"))
        st_s, v_s, _ = _cached(f"sina-{code}", lambda c=code, p=pfx: ak.fund_etf_hist_sina(
            symbol=p + c))
        nav_txt = str(_rows(nav)) if st_n == "ok" else "-"
        print(f"  {code:<8}{nav_txt:<14}{_date_range(nav):<28}"
              f"{('有' if st_t == 'ok' else '无'):<12}"
              f"{('有' if st_s == 'ok' else '无')}")
    print("  >>> 510080/510081 成立于 2004 年（那时全市场只有 510050 一只 ETF），")
    print("  >>> 净值连续到 2026 却没有任何场内行情 → 它们是**普通开放式基金**。")
    print("  >>> 结论：ETF 识别必须依赖来源清单，禁止写「代码段 = ETF」的规则。")


# ---------------------------------------------------------------------------
# 第 3 段：分红 / 复权口径
# ---------------------------------------------------------------------------

def section_caliber() -> None:
    import akshare as ak

    print()
    print("=" * 110)
    print("第 3 段 分红 / 复权口径")
    print("=" * 110)

    st, nav, _ = _cached("nav-510300", lambda: ak.fund_etf_fund_info_em(
        fund="510300", start_date="20040101", end_date="20260915"))
    if st == "ok" and _rows(nav) > 0:
        last = nav.iloc[-1]
        try:
            unit = float(last["单位净值"])
            accum = float(last["累计净值"])
            print(f"510300 最新：单位净值 {unit:.4f}  累计净值 {accum:.4f}  "
                  f"比值 {unit / accum:.2f}x")
            print("  >>> 比值远大于 1 且不是分红造成的（分红只会让累计 > 单位）")
            print("  >>> → 东财「累计净值」含**份额折算**，**不能直接当复权因子**。")
        except Exception as exc:  # noqa: BLE001
            print(f"  [警告] 净值列解析失败：{exc}")

    print()
    print("腾讯源是否支持 ETF 复权（计划里标注的「待验证」项）")
    for adj in ("", "qfq", "hfq"):
        label = f"tx-510050-adj={adj or 'none'}"
        st_t, v_t, dt = _cached(label, lambda a=adj: ak.stock_zh_a_hist_tx(
            symbol="sh510050", start_date="20040101", end_date="20260915", adjust=a))
        if st_t == "ok" and _rows(v_t) > 0:
            try:
                c_first = float(v_t["close"].iloc[0])
                c_last = float(v_t["close"].iloc[-1])
                print(f"  adjust={adj or '(默认)':<8} {_rows(v_t):>5} 行  "
                      f"close {c_first:.3f} -> {c_last:.3f}")
            except Exception:  # noqa: BLE001
                print(f"  adjust={adj or '(默认)':<8} {_rows(v_t):>5} 行（列名异常）")
        else:
            print(f"  adjust={adj or '(默认)':<8} {st_t} {str(v_t)[:60]}")
    print("  >>> 判据要**看比例**（末价/首价），不能只看数值：")
    print("  >>>   三档比例若互不相同且不自洽 = 该源的 ETF 复权不可信。")
    print("  >>>   2026-09-18 实测 510050：默认 0.876->2.958（3.38x）、")
    print("  >>>   qfq 0.079->2.958（37.4x）、hfq 1.037->4.445（4.29x）→")
    print("  >>>   **互相矛盾，腾讯 adjust 对 ETF 不可用**。")
    print("  >>>   → 只能用「原始价格做撮合与信号」+「分红单独处理」。")

    st_d, div, _ = _cached("etf_div_510050", lambda: ak.fund_etf_dividend_sina())
    if st_d == "ok" and _rows(div) > 0:
        print()
        print(f"新浪 ETF 分红表（默认 symbol='sh510050'）：{_rows(div)} 行，"
              f"列 = {list(div.columns)}")
        print("  >>> ⚠️ 自纠一处：这个接口**是逐只的**（默认参数就是 510050），")
        print("  >>>   不是「全市场覆盖不足」。传 symbol='sh510300' 另得一张表。")
        print("  >>>   → ETF 分红**可以**逐只拿，全收益序列可以自己构造。")
        print("  >>>   实测分红拖累 1.7~3.2pp/年（见 probe_etf_total_return.py），")
        print("  >>>   量级等于整个组合的预期收益 → 口径选择是决定性的，不是细节。")


# ---------------------------------------------------------------------------
# 第 4 段：偏差方向声明
# ---------------------------------------------------------------------------

def section_declaration() -> None:
    print()
    print("=" * 110)
    print("第 4 段 偏差方向声明（这一段是结论，必须原样抄进文档）")
    print("=" * 110)
    print("""
拿到什么：
  - 现役 ETF 清单（东财 1621 / 新浪 1674）、上市日（日线首日，无偏）、
    日线（腾讯/新浪，2005-02-23 起）、净值（东财，含累计净值）。

拿不到什么：
  - **已退市/已清盘 ETF 的清单与行情**。腾讯源 IndexError、新浪源空。
    东财名录也不含已终止产品（分级基金 150 段仅剩 8 只可证）。

偏差方向（必须写进产物与文档）：
  - ETF 清盘通常发生在**规模萎缩、主题过气之后** → 清盘前往往跑输同类。
  - 只用「今天活着的 ETF」回测 → **系统性剔除掉跑得差的标的**
    → 策略收益被**系统性高估**，且高估方向单一（只向上偏，不会向下偏）。
  - 这一点与个股侧相反：个股退市股腾讯源**能**拿到（乐视网等），
    所以个股侧的幸存者偏差已被量化（见 docs/终局诊断与重启方案.md），
    而 ETF 侧**只能给上界区间，不能给点估计**。

由此得到两条硬性设计约束：
  1. 轮动策略的零假设必须是「**等权持有同一个可见池**」，
     而不是「买入持有某一只 ETF」—— 这样漏掉的标的对策略与零对照
     影响方向相同、可部分抵消，**相对差仍有意义**。
  2. 产物必须带 `universe_caliber="live_only_biased"` 自描述标记，
     **绝对收益数字一律不可引用**。
""".rstrip())


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 数据来源可得性探测（只读）")
    ap.add_argument("--section", nargs="*", type=int, default=[1, 2, 3, 4],
                    help="要跑的段号（1 数据源矩阵 / 2 池口径 / 3 复权口径 / 4 偏差声明）")
    ap.add_argument("--timeout", type=float, default=70.0, help="单次接口超时（秒）")
    args = ap.parse_args()

    global TIMEOUT
    TIMEOUT = args.timeout

    try:
        import akshare  # noqa: F401
    except ImportError:
        print("[错误] 未安装 akshare，请先：pip install akshare")
        return 1

    print(f"项目根 = {ROOT}")
    t0 = time.time()
    if 1 in args.section:
        section_sources()
    if 2 in args.section:
        section_pool()
    if 3 in args.section:
        section_caliber()
    if 4 in args.section:
        section_declaration()
    print()
    print(f"完成，用时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
