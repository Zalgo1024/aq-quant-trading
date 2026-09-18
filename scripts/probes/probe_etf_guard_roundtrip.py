"""单位守卫的**写入→读回**往返自检（`probe_etf_unit_guard.py` 的第二层）。

为什么需要它
------------
前一个探针只验证「守卫能否**判定**单位」。但真实管线是
``fetch_nav → save(落盘) → load → nav_total_return``，
判定正确**不等于**落盘后的数值正确 —— 中间还隔着一次
``coerce_return_unit`` 在 ``save`` 里的**再执行**。

本项目在这里连续栽过两次，都是"不报错、只给错数"：

1. 百分数当小数用 → 全部收益偏 100 倍（7/7 折算事件全错）；
2. 守卫的换算方向写反 → 判定对了却**乘** 100 而不除，
   入库后 ``verify`` 的 ``nondiv_dev_max`` 直接爆到 1e6~1e7 量级
   （正常应为 1e-5），并连带把 18/18 只标的误报成"单位净值有未记录跳变"。

判据（三条必须同时成立）
------------------------
- **A 幂等**：同一份数据 save→load→save 两次，``nav_ret`` 逐位不变。
- **B 自洽**：load 后 ``|单位比 − nav_ret|`` 的中位数 ≤ 1e-4（官方只保留 2 位小数）。
- **C 量级**：load 后 ``nav_ret`` 的 99 分位 |值| ≤ 0.30（小数口径的真实上限）。

任一不成立 → 守卫的换算系数有问题，**先修守卫再看业务数字**。

用法
----
    python scripts/probes/probe_etf_guard_roundtrip.py            # 用已落盘的真实数据
    python scripts/probes/probe_etf_guard_roundtrip.py --synth    # 纯合成数据，不依赖 data_cache
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

for _p in Path(__file__).resolve().parents:
    if (_p / "config" / "base.yaml").exists():
        ROOT = _p
        break
else:  # pragma: no cover
    raise SystemExit("找不到仓库根（config/base.yaml）")
sys.path.insert(0, str(ROOT))

from aq.data.etf_actions import (NAV_RESTATE_ABS_RATIO,  # noqa: E402
                                 coerce_return_unit, nav_total_return)
from aq.data.etf_store import EtfNavStore  # noqa: E402

#: 落盘 nav_ret 的 99 分位上限。真实 A 股 ETF 单日全收益极少超 30%。
Q99_MAX = 0.30
#: |单位比 − nav_ret| 的中位上限：官方日增长率只保留 2 位小数百分比。
MED_TOL = 1e-4


def _cases() -> list[tuple[str, str]]:
    return [
        ("510050", "上证50（老，含多次分红）"),
        ("510300", "沪深300"),
        ("512890", "红利低波（1:2 拆分，不分红）"),
        ("510880", "上证红利（高分红）"),
        ("511010", "国债ETF（锚点几乎不动）"),
        ("511880", "货币ETF（日涨幅 ~0.01）"),
        ("518880", "黄金ETF（与权益不同量级）"),
        ("159920", "恒生ETF"),
    ]


def _ret_and_anchor(nav: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """从落盘表取出 ``nav_ret`` 与「单位净值比率」锚，**都挂到日期轴上**。

    ⚠️ 落盘 parquet 的索引是 RangeIndex，``time`` 只是一列。不重建日期索引
    直接 ``reindex`` 到日期轴会得到全 NaN 而**不报错** —— 本探针首版就栽在
    这里，把三条判据全判成 nan（探针的假失败比探针的假通过更浪费，因为
    它会让人去改本来正确的生产代码）。
    """
    idx = pd.to_datetime(nav["time"])
    ret = pd.Series(pd.to_numeric(nav["nav_ret"], errors="coerce").values,
                    index=idx).dropna()
    un = pd.Series(pd.to_numeric(nav["unit_nav"], errors="coerce").values,
                   index=idx).dropna()
    return ret, un.pct_change().dropna()


def synth_roundtrip() -> int:
    """合成数据往返：把百分数喂进 ``save``，看读回后是否是小数。"""
    rng = np.random.RandomState(7)
    tmp = Path(tempfile.mkdtemp()) / "nav"
    ns = EtfNavStore(tmp)
    idx = pd.bdate_range("2019-01-01", periods=1200)

    # (标的, 说明, 日收益均值, 日收益标准差) —— 标准差按真实量级给
    cases = [
        ("510050", "上证50（老，含多次分红）", 0.0002, 0.010),
        ("510300", "沪深300", 0.0002, 0.008),
        ("512890", "红利低波（1:2 拆分，不分红）", 0.0002, 0.008),
        ("510880", "上证红利（高分红）", 0.0002, 0.008),
        ("518880", "黄金ETF", 0.0002, 0.008),
        ("159920", "恒生ETF", 0.0002, 0.010),
        # ⭐ 分辨率陷阱：真实日收益 5e-5，而官方列只有 2 位小数百分比
        #    ⇒ 舍入步长 1e-4 与信号同量级。不幂等的守卫会在这里暴露。
        ("511880", "货币ETF（信号落在舍入精度以下）", 5e-5, 5e-6),
        ("511010", "国债ETF（锚点几乎不动）", 5e-5, 3e-6),
    ]

    bad = 0
    for code, note, mu, sig in cases:
        r_true = rng.normal(mu, sig, len(idx))              # 小数口径的真值
        cum = np.cumprod(1.0 + r_true)
        unit = cum.copy()
        # 官方「日增长率」= 百分数、只保留 2 位小数
        nav_ret_pct = np.round(r_true * 100.0, 2)
        df = pd.DataFrame({"time": idx, "unit_nav": unit, "cum_nav": cum,
                           "nav_ret": nav_ret_pct})
        ns.save(code, df)
        back = ns.load(code)
        got, u_ratio = _ret_and_anchor(back)
        med = float((u_ratio - got.reindex(u_ratio.index).dropna()).abs().median())
        q99 = float(got.abs().quantile(0.99))
        ok = (med <= MED_TOL) and (q99 <= Q99_MAX)
        bad += 0 if ok else 1
        # 年化偏差：量化误差应是**无偏**的，累乘后年化不该被放大
        ann_true = float((1.0 + r_true).prod() ** (250.0 / len(r_true)) - 1.0)
        ann_got = float((1.0 + got).prod() ** (250.0 / len(got)) - 1.0)
        ann_ok = abs(ann_got - ann_true) <= 0.01            # 年化偏差 ≤ 1pp
        if not ann_ok:
            bad += 1
        print(f"  {'OK ' if ok and ann_ok else 'FAIL'} {code} {note:<28}"
              f" 中位|单位比−nav_ret|={med:.2e}  99分位|nav_ret|={q99:.4f}"
              f"  年化 {ann_got:+.2%} vs 真值 {ann_true:+.2%}"
              f"  差 {(ann_got - ann_true) * 100:+.3f}pp")

        # A 幂等：再 save 一次，值必须逐位不变
        ns.save(code, back)
        again = _ret_and_anchor(ns.load(code))[0]
        same = (len(again) == len(got)
                and bool(np.allclose(got.values, again.values, atol=0, rtol=0)))
        if not same:
            bad += 1
            ratio = float((again / got.replace(0, np.nan)).median()) \
                if len(again) == len(got) else float("nan")
            print(f"      FAIL 幂等性被破坏：二次 save 后 nav_ret 变成 {ratio:.4g} 倍"
                  f"（守卫每次经过都改值 → 收益会被静默缩放）")
    return bad


def real_roundtrip() -> int:
    """真实数据只读自检：不写盘，只判「已落盘的 nav_ret 是不是小数口径」。"""
    ns = EtfNavStore()
    bad = 0
    print(f"  {'标的':<20}{'n':>6}{'dev 中位':>12}{'dev 99分位':>12}{'dev max':>11}"
          f"{'分红日':>8}{'重述日':>8}{'ret_src':>10}  判定")
    for code, note in _cases():
        if not ns.has(code):
            print(f"  {code:<20} 未落盘，跳过")
            continue
        n = ns.load(code)
        got, u_ratio = _ret_and_anchor(n)
        med = float((u_ratio - got.reindex(u_ratio.index).dropna()).abs().median())
        q99 = float(got.abs().quantile(0.99))
        v = ns.verify(code)
        src = v.get("ret_src", "?")
        ok = v.get("ok", False)
        bad += 0 if (ok and med <= MED_TOL and q99 <= Q99_MAX) else 1
        print(f"  {code:<20}{len(got):>6}{v.get('dev_med', float('nan')):>12.2e}"
              f"{v.get('dev_p99', float('nan')):>12.2e}"
              f"{v.get('dev_max', float('nan')):>11.2e}"
              f"{v.get('n_div_days', 0):>8}{v.get('n_restate_days', 0):>8}{src:>10}"
              f"  {'OK' if ok else 'FAIL'}  {note}")
    return bad


def negative_control() -> int:
    """**负对照**：判据与守卫必须能抓住"故意做坏"的数据。

    一个永远返回 PASS 的验收等于没有验收。这里做两个**方向相反**的对照：

    - **N1 判据灵敏度**：把真实 ``nav_ret`` 放大 100 倍（模拟单位口径错误），
      判据用的那个统计量（``|nav_ret − 单位比|`` 的中位数）必须爆表。
      **不过则说明判据对口径错误不敏感**，前面的 PASS 就毫无意义。
    - **N2 守卫自愈**：把放大 100 倍的数据喂回守卫，它应当改回正确值
      —— 证明上游单位错误不会静默流到下游。

    ⚠️ 三个坑（本探针前两版都踩了）
    1. **N1 不能走 ``nav_total_return``** —— 它会顺手自愈，把错误抹掉。
       必须手工复现判据，才能测到"判据本身"。
    2. **不能用"只在累计比 == 单位比的日子比较"那个掩码** —— 它是退化样本
       （两个比率都由 4 位小数净值算出，逐位相等基本只在"净值完全没动、
       双双为 0"时成立），对 510300/511010 全部为 0 ⇒ 乘 100 后还是 0 ⇒
       假"不敏感"。
    3. **N1 的阈值必须是"相对放大量 + 越过容差"，不能是绝对量级**：
       货币 ETF 真值只有 5e-5，×100 后中位 9.9e-03 虽然放大了 **322 倍**、
       远超 ``MED_TOL``，却会被硬编码的 1e-2 判成"不敏感"。
       判据：``med_bad > MED_TOL``（即**真的会被容差抓住**）
       且 ``med_bad / med_real >= DECISIVE``（放大是决定性的）。
    """
    ns = EtfNavStore()
    bad = 0
    decisive = 50.0
    print(f"  {'标的':<10}{'真实 dev 中位':>14}{'×100 后中位':>14}{'放大倍数':>11}"
          f"{'N2 守卫纠正后':>15}  判定")
    for code, _note in _cases():
        if not ns.has(code):
            continue
        n = ns.load(code)
        t = pd.to_datetime(n["time"])
        un = pd.Series(pd.to_numeric(n["unit_nav"], errors="coerce").values, index=t)
        rep = pd.Series(pd.to_numeric(n["nav_ret"], errors="coerce").values, index=t)
        d = pd.DataFrame({"u": un.pct_change(), "r": rep}).dropna()
        if len(d) < 30:
            continue
        med_real = float((d["r"] - d["u"]).abs().median())
        med_bad = float((d["r"] * 100.0 - d["u"]).abs().median())
        gain = med_bad / med_real if med_real else float("inf")

        # N2：把 ×100 的数据喂回守卫，看它是否改回
        rv = rep.dropna()
        corrupt = pd.Series(rv.values * 100.0, index=rv.index)
        fixed, tag = coerce_return_unit(corrupt, anchor=un.pct_change())
        ok_n2 = bool(np.allclose(fixed.reindex(rv.index).values, rv.values,
                                 rtol=1e-6, atol=1e-12))

        # N1：做坏之后必须**越过容差**且放大决定性 —— 否则判据抓不住
        ok = (med_bad > MED_TOL) and (gain >= decisive) and ok_n2
        bad += 0 if ok else 1
        print(f"  {code:<10}{med_real:>14.2e}{med_bad:>14.2e}{gain:>11.0e}"
              f"{('✓ 已改回' if ok_n2 else '✗ 未改回'):>15}"
              f"{'  OK' if ok else '  FAIL'}  ({tag})")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synth", action="store_true",
                    help="只跑合成数据往返（不依赖 data_cache）")
    ap.add_argument("--real-only", action="store_true", help="只跑真实数据自检")
    args = ap.parse_args()

    bad = 0
    if not args.real_only:
        print("=" * 118)
        print("第 1 段 合成数据往返：百分数 → save → load，读回必须是小数且幂等")
        print("=" * 118)
        n_bad = synth_roundtrip()
        print(f"  >>> 合成往返失败 {n_bad} 只")
        bad += n_bad
    if not args.synth:
        print()
        print("=" * 118)
        print("第 2 段 真实数据自检（只读）：已落盘的 nav_ret 是否小数口径")
        print("=" * 118)
        n_bad = real_roundtrip()
        print(f"  >>> 真实数据不合格 {n_bad} 只")
        bad += n_bad
        print()
        print("=" * 118)
        print("第 3 段 负对照：判据要抓得住做坏的数据，守卫要能自愈")
        print("=" * 118)
        n_bad = negative_control()
        print(f"  >>> 负对照失败 {n_bad} 只（失败 = 判据不敏感 或 守卫不自愈）")
        bad += n_bad

    print()
    print("=" * 118)
    print(f"总判定：{'PASS' if bad == 0 else f'FAIL（{bad} 项）'}")
    print("  三条判据：A 幂等 / B |单位比−nav_ret| 中位 ≤ 1e-4 / C 99 分位 ≤ 0.30")
    print("  另加负对照：N1 判据对 ×100 必须爆表 / N2 守卫必须把 ×100 改回")
    print("  ⚠️ 若这里 FAIL，先修守卫的换算系数，**不要**去看任何策略数字 ——")
    print("     收益偏 100 倍不会报错，只会让所有下游结论静默失真。")
    print("=" * 118)


if __name__ == "__main__":
    main()
