"""探针：ETF 份额折算（拆分/合并）对价格序列的污染 —— 大样本扫描。

为什么必须单独探
----------------
``probe_etf_total_return.py`` 跑出 512890 价格年化 2.32%，与"连续 4 年稳定
分红、规模 200 亿"的公开事实矛盾。逐日拆开：

    2021-10-21  净值 单位净值 1.6357 / 累计净值 1.6357
    2021-10-22  单位净值 0.8002 / 累计净值 1.6004  ← **恰好 1/2**，日增长率 −2.16%
    2021-10-25  二级市场价格 1.639 → 0.801，**单日 −51.13%**

证监会基金电子披露网站与证券时报电子报的公告证实：**1:2 基金份额拆分**
（拆分前净值 1.6004 / 份额 73,055,390 → 拆分后 0.8002 / 146,110,780）。
它不是亏损。后果：价格口径 2019-01-18~2026-09-15 年化 2.32%，
真实全收益 ≈ 12.4% —— **差约 10pp/年**，而它是 B 路线（红利低波）的核心标的。

判据演进（前两版都错，记录在此避免重踩）
---------------------------------------
❌ 第一版：比较「价格日收益」与「单位净值日收益」的符号差异。
   错因：**折算日 ETF 停牌**（512890 的 2021-10-22），价格序列根本没有那一行，
   于是价格跳空与净值跳空落在同一个跨日区间里，**同号同幅、互相抵消**。
   结果：漏掉 512890，却把 4 只真折算的原因子算成 1.0000。

❌ 第二版：改对了判据形式（净值侧全收益 vs 价格），但**栽在单位上**。
   东财「日增长率」是**百分数**（``−2.16`` = −2.16%），第二版当小数用，
   再叠加 ``nret > −1.0`` 过滤（把 −2.16 当 −216% 丢掉）→ 7/7 个事件全错：

   | 标的 | 事件日 | 官方日增长率 | 错解 | 真值 |
   |---|---|---|---|---|
   | 512890 | 2021-10-22 | ``−2.16`` | 丢掉后只剩 +0.22 → 因子 2.4964 | **2.006（1:2）** |
   | 159915 | 2024-10-08 | ``17.23`` | 1+17.23 = 18.23 → 因子 15.19 | 无折算 |
   | 510500 | 2015-04-15 | ``2.14`` | 3.14×0.96 = 3.01 → 因子 0.86 | **≈3.55 份额折算** |

   这个错误**不报错、只静默给错因子**，所以生产代码里加了
   ``coerce_return_unit()`` 守卫（见 ``aq/data/etf_actions.py``）。

✅ 第三版（本文件）：判据不变，**但不再自己实现** —— 直接调用生产模块
   ``aq.data.etf_actions.detect_actions``。探针与生产分叉正是前两版翻车
   的根因：同一个判据写两遍，改了这边忘了那边。

    factor = (1 + r_total) / (1 + r_px)，r_total 取**官方日增长率**累乘
    （它是全收益；⚠️ 不能取累计净值比率 —— 那个被累计分红稀释，见
    ``probe_etf_dividend_dilution.py``）

⚠️ 判据边界：**分红也会产生 factor > 1**（分红日价格下跌而净值侧收益含分红），
   量级 = 每份分红 / 价格。红利类 ETF 单次大额分红可达 5%，月频分红仅 0.5%。
   因此阈值取 **10%**，只报 |factor − 1| > 10% 的**大事件** ——
   也正好是唯一会毁掉回测的那一类。

⭐ 但**收益不必依赖本探针是否命中**：全收益一律取
   ``EtfNavStore.total_return``（官方日增长率），它对折算天然免疫。

运行::

    python scripts/probes/probe_etf_split.py

输出写入 ``runtime/etf_split_v3.log``。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

#: 关掉 akshare 内部 tqdm 进度条（否则日志被几万行进度条淹没）
os.environ.setdefault("TQDM_DISABLE", "1")


def _project_root() -> Path:
    """路径解析一律「向上找 config/base.yaml」，不写 parents[N]（换目录会指错层级）。"""
    here = Path(__file__).resolve()
    for p in [here, *here.parents]:
        if (p / "config" / "base.yaml").exists():
            return p
    return here.parents[1]


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import akshare as ak  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from aq.data.etf_actions import (  # noqa: E402
    DEFAULT_FACTOR_THR,
    detect_actions,
    nav_total_return,
    price_jump_candidates,
)

CORE = [
    "510050", "510300", "510500", "512100", "159915", "588000",
    "510880", "512890", "515180", "563020", "515080", "512990",
    "511010", "511260", "511880", "518880", "513100", "513500", "159920",
]

#: 价格单日跳空超过它才进入候选（先廉价筛，再只为候选拉净值）。
#: 直接引用生产常量，避免两处各写一个数。
PX_JUMP = 0.15
FACTOR_THR = DEFAULT_FACTOR_THR
TIMEOUT = 150.0

_CACHE: dict[str, object] = {}


def _run(fn, timeout: float = TIMEOUT):
    box: dict[str, object] = {}

    def work() -> None:
        try:
            box["v"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["e"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return None, "timeout"
    if "e" in box:
        return None, str(box["e"])[:120]
    return box["v"], "ok"


def _cached(key: str, fn):
    if key not in _CACHE:
        _CACHE[key] = _run(fn)
    return _CACHE[key]


def _prefix(code: str) -> str:
    return ("sz" if str(code).startswith(("15", "16")) else "sh") + str(code)


def load_price(code: str) -> pd.DataFrame | None:
    v, st = _cached(f"px:{code}", lambda: ak.stock_zh_a_hist_tx(
        symbol=_prefix(code), start_date="20040101", end_date="20260915"))
    if st != "ok" or v is None or len(v) == 0:  # type: ignore[arg-type]
        return None
    df = v.copy()  # type: ignore[union-attr]
    df["time"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.sort_values("time").reset_index(drop=True)


def load_nav(code: str) -> pd.DataFrame | None:
    """东财净值表 → ``time / unit_nav / cum_nav / nav_ret``。

    ⚠️ ``nav_ret`` 此处**不自己除 100** —— 交给 ``detect_actions`` 内部的
    ``coerce_return_unit`` 判定并报告。自己除 100 就是第二版翻车的写法：
    一旦上游口径变了，探针会再次静默算错。
    """
    v, st = _cached(f"nav:{code}", lambda: ak.fund_etf_fund_info_em(
        fund=code, start_date="20040101", end_date="20260915"))
    if st != "ok" or v is None or len(v) == 0:  # type: ignore[arg-type]
        return None
    df = v.copy()  # type: ignore[union-attr]
    df["time"] = pd.to_datetime(df["净值日期"])
    df["unit_nav"] = pd.to_numeric(df["单位净值"], errors="coerce")
    df["cum_nav"] = pd.to_numeric(df["累计净值"], errors="coerce")
    df["nav_ret"] = pd.to_numeric(df["日增长率"], errors="coerce")
    return df.sort_values("time").reset_index(drop=True)


def analyse(code: str, px: pd.DataFrame | None = None) -> dict:
    """**调用生产代码**检测折算事件 + 交叉校验（不做任何重复实现）。"""
    out: dict = {"code": code, "events": [], "note": "", "candidates": 0}
    px = px if px is not None else load_price(code)
    if px is None or len(px) < 30:
        out["note"] = "价格不可得"
        return out
    p = pd.Series(pd.to_numeric(px["close"], errors="coerce").values,
                  index=px["time"]).dropna().sort_index()
    out["n_price_days"] = int(len(p))
    out["first"], out["last"] = str(p.index[0].date()), str(p.index[-1].date())
    out["px"] = p

    cand = price_jump_candidates(px, PX_JUMP)
    out["candidates"] = int(len(cand))
    if not cand:
        out["note"] = "无价格跳空候选"
        return out

    nav = load_nav(code)
    if nav is None or len(nav) < 30:
        out["note"] = f"有 {len(cand)} 个价格跳空候选，但净值不可得 → **无法判定**"
        out["unresolved"] = [str(t.date()) for t in cand]
        return out
    out["nav"] = nav

    # 生产判据（含单位守卫）
    out["events"] = detect_actions(code, px, nav, jump=PX_JUMP, factor_thr=FACTOR_THR)

    # 收益口径来源（官方日增长率）+ 非除息日不变量
    r_total, src, unit = nav_total_return(nav)
    out["ret_src"] = src
    out["ret_unit"] = unit
    out["ret_total"] = r_total
    if "cum_nav" in nav.columns and "unit_nav" in nav.columns:
        cum = pd.Series(pd.to_numeric(nav["cum_nav"], errors="coerce").values,
                        index=pd.to_datetime(nav["time"])).dropna()
        un = pd.Series(pd.to_numeric(nav["unit_nav"], errors="coerce").values,
                       index=pd.to_datetime(nav["time"])).dropna()
        both = pd.DataFrame({"c": cum.pct_change(), "u": un.pct_change(),
                             "r": r_total}).dropna()
        # 非除息日 = 累计净值与单位净值同步变动的那些天（当日无分红计入）
        nd = both[(both["c"] - both["u"]).abs() < 1e-9]
        if len(nd):
            out["nondiv_dev"] = float((nd["c"] - nd["r"]).abs().max())
            out["nondiv_n"] = int(len(nd))
    # 全期全收益（权威口径 = 官方日增长率累乘）
    if len(r_total):
        out["ret_total_cum"] = float((1.0 + r_total).prod() - 1.0)
    return out


def _ann(total: float, years: float) -> float:
    if years <= 0 or total != total:
        return float("nan")
    return float((1.0 + total) ** (1.0 / years) - 1.0)


def main() -> int:
    t0 = time.time()
    print("ETF 份额折算（拆分/合并）污染检测 —— 第三版：直接调用生产判据")
    print(f"项目根 = {ROOT}")
    print(f"判据 = aq.data.etf_actions.detect_actions"
          f"（factor = (1 + 净值全收益) / (1 + 价格跨日收益)，阈值 {FACTOR_THR:.0%}）")
    print("=" * 104)
    sys.stdout.flush()

    codes = list(CORE)
    v, st = _cached("spot", lambda: ak.fund_etf_spot_em())
    if st == "ok" and v is not None and len(v):  # type: ignore[arg-type]
        spot = v.copy()  # type: ignore[union-attr]
        amt = pd.to_numeric(spot.get("成交额"), errors="coerce")
        spot = spot.assign(_a=amt).sort_values("_a", ascending=False)
        top = [str(c).zfill(6) for c in spot["代码"].head(200)]
        extra = [c for c in top if c not in codes]
        codes += extra
        print(f"样本 = core {len(CORE)} 只 + 成交额前 200 中的其余 {len(extra)} 只 "
              f"= {len(codes)} 只")
    else:
        print(f"样本 = core {len(CORE)} 只（现货清单不可用：{st}）")
    print("=" * 104)
    sys.stdout.flush()

    # --- 阶段 1：只用价格筛候选（廉价）---
    print("[阶段 1] 逐只拉价格，筛出 |单日收益| > %.0f%% 的候选" % (PX_JUMP * 100))
    px_cache: dict[str, pd.DataFrame] = {}
    cand_codes: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(load_price, c): c for c in codes}
        done = 0
        for f in as_completed(futs):
            done += 1
            try:
                df = f.result()
            except Exception:  # noqa: BLE001
                df = None
            if df is None:
                continue
            px_cache[futs[f]] = df
            if price_jump_candidates(df, PX_JUMP):
                cand_codes.append(futs[f])
            if done % 40 == 0:
                print(f"   ... {done}/{len(codes)}（{time.time() - t0:.0f}s，"
                      f"候选 {len(cand_codes)} 只）")
                sys.stdout.flush()
    print(f"  价格可用 {len(px_cache)} 只；跳空候选 **{len(cand_codes)} 只**"
          f"（{time.time() - t0:.0f}s）")
    print(f"  候选 = {' '.join(sorted(cand_codes))}")
    print("  >>> 候选只占样本的 %.1f%% → 生产环境只为候选拉净值即可（成本可忽略）"
          % (100.0 * len(cand_codes) / max(len(codes), 1)))
    sys.stdout.flush()

    # --- 阶段 2：只为候选拉净值并反解（走生产代码）---
    print()
    print("[阶段 2] 为候选拉净值，反解折算因子（aq.data.etf_actions.detect_actions）")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(analyse, c, px_cache[c]): c for c in sorted(cand_codes)}
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"code": futs[f], "events": [],
                                "note": f"FATAL {type(exc).__name__}: {exc}"})
    results.sort(key=lambda r: r["code"])

    hit = [r for r in results if r.get("events")]
    clean = [r for r in results if not r.get("events") and not r.get("unresolved")
             and r.get("candidates")]
    unresolved = [r for r in results if r.get("unresolved")]
    print()
    print("=" * 104)
    print(f"第 1 段 检出份额折算：{len(hit)} 只（候选 {len(results)} 只；"
          f"有跳空但非折算 {len(clean)} 只；净值不可得 {len(unresolved)} 只）")
    print("=" * 104)
    for r in hit:
        print(f"  {r['code']}")
        for e in r["events"]:
            tag = (f"≈ {e['factor_simple']:g}"
                   if e["factor_simple"] == e["factor_simple"] else "**非整数比**")
            print(f"     {e['prev_time'].date()} → {e['time'].date()}"
                  f"（跨 {e['gap_nav_days']} 个净值日）"
                  f"  价格 {e['r_px']:+.2%}  净值全收益 {e['r_total']:+.2%}"
                  f"  反解因子 {e['factor']:.4f}  {tag}"
                  f"  [来源 {e['ret_src']}/{e['ret_unit']}]")
        if r.get("ret_total_cum") == r.get("ret_total_cum"):
            print(f"     全期全收益 = {r['ret_total_cum'] * 100:+.1f}%"
                  f"（官方日增长率累乘；来源 {r.get('ret_src')}/{r.get('ret_unit')}）"
                  f"  非除息日偏离 max = {r.get('nondiv_dev', float('nan')):.2e}"
                  f"（{r.get('nondiv_n', 0)} 天比对）")
    print()
    print(f"  有跳空但反解不构成折算（可能是真行情/大额分红）：{len(clean)} 只")
    if clean:
        print(f"     {' '.join(r['code'] for r in clean)}")
    if unresolved:
        print(f"  ⚠️ {len(unresolved)} 只有价格跳空但净值不可得 → **不能当作无折算**：")
        for r in unresolved:
            print(f"     {r['code']}  候选日 = {r.get('unresolved')}")
    print()
    print(f"  ⚠️ 阈值 {FACTOR_THR:.0%} 专门避开分红（红利类单次大额分红 ≈5%）。")
    print("     因此 |factor−1| 在 (0, 10%] 之间的**小比例折算不会被报出** ——")
    print("     有意的取舍：小折算不会毁掉回测，大折算会。")
    sys.stdout.flush()

    print("=" * 104)
    print("第 2 段 三口径年化：原始价格 / 折算还原 / 权威全收益(官方日增长率)")
    print("=" * 104)
    print(f"  {'代码':<10}{'年数':>6}{'原始价格':>11}{'折算还原':>11}{'全收益':>11}"
          f"{'折算污染':>11}{'分红拖累':>11}")
    print("  " + "-" * 70)
    for r in hit:
        p = r["px"]
        years = (p.index[-1] - p.index[0]).days / 365.25
        a = _ann(float(p.iloc[-1] / p.iloc[0] - 1.0), years)
        f = 1.0
        for e in sorted(r["events"], key=lambda x: x["time"]):
            f *= e["factor"]
        b = _ann(float(p.iloc[-1] * f / p.iloc[0] - 1.0), years)
        c = _ann(r.get("ret_total_cum", float("nan")), years)
        print(f"  {r['code']:<10}{years:>6.1f}{a:>11.2%}{b:>11.2%}{c:>11.2%}"
              f"{(b - a) * 100:>10.2f}pp{(c - b) * 100:>10.2f}pp")
    print()
    print("  ⚠️ 注意「折算污染」与「分红拖累」两列的口径：")
    print("      折算污染 = 折算还原 − 原始价格（不复权会凭空丢掉这么多收益）")
    print("      分红拖累 = 全收益 − 折算还原（价格口径不含分红，会少算这么多）")
    print("      ⚠️ 但 510880/510500 这类标的的**单位净值本身还有未记录跳变**")
    print("      （历史重述或折算），会让折算因子与分红估计都被污染 →")
    print("      这两列只对该标的的折算事件做**量级**提示，不要当精确归因。")
    print()
    print("=" * 104)
    print("第 3 段 结论（必须原样抄进文档 / 代码）")
    print("=" * 104)
    print()
    print("  1. 份额折算在 ETF 上真实存在且会毁掉回测：512890 单日假跌 −51%，")
    print("     使价格年化从 ≈12% 变成 2.32%。")
    print("  2. 判据 = 「净值侧全收益累乘」vs「价格跨日收益」，且**必须走生产代码**；")
    print("     第一版（价格 vs 单位净值）会因停牌而互相抵消，第二版因单位（百分数 vs")
    print("     小数）静默全错。三个版本只在第三版同时正确。")
    print("  3. 生产管线：先用价格筛 |单日跳空| > 15% 的候选，只为候选拉净值反解 →")
    print("     成本从「全池拉净值」降到「几十只」。")
    print("  4. ⭐ **收益不依赖折算检测**：全收益一律取官方日增长率（折算中性、含分红）。")
    print("     折算检测只服务价格路径（成交额/撮合/技术指标）与数据质量告警。")
    print()
    print("完成，用时 %.1fs" % (time.time() - t0))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    os._exit(main())
