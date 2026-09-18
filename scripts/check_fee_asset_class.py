"""费率口径不变量检查（入库脚本，不联网）。

背景
----
``aq.core.rules.calc_fees`` 在 2026-09-18 增加了 ``asset_class`` 参数，
目的是让 **ETF 免印花税/免过户费**（原实现对 ETF 卖出也收 0.05% 印花税，
会让「实际成本与预估偏差 < 20%」这条验收判据直接失败）。

这类改动最容易出的问题是**顺手改坏了默认路径**：个股回测的全部历史数字
与缓存都建立在旧费率上，一旦 stock 口径漂了，整个仓库的结论都要重跑。
所以本脚本用**独立复刻**（不 import 任何费率常量）来证明默认路径逐字节不变。

为什么必须用独立复刻
--------------------
项目踩过这个坑：``check_excess_caliber.py`` 首版自己漏减了基准，
"独立实现"本身也要被权威结果反验。所以这里的期望值全部**手写常量**，
不复用 ``aq.core.rules`` 里的任何数字。

用法::

    python scripts/check_fee_asset_class.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq.core.rules import calc_fees, fee_params  # noqa: E402

#: 历史费率（**手写**，不从 rules.py 读 —— 这正是"独立"的含义）
LEGACY = {"commission": 0.00025, "min_commission": 5.0,
          "stamp_tax": 0.0005, "transfer_fee": 0.00001}

#: 检查网格：side × price × qty
PRICES = [0.5, 1.0, 2.5, 3.017, 10.0, 47.83, 100.0, 1234.56]
QTYS = [100, 200, 1000, 10000, 100000]


def legacy_calc_fees(side_is_sell: bool, price: float, qty: int) -> dict[str, float]:
    """旧实现的独立复刻（原 ``rules.py:98`` 函数体，逐字抄写）。"""
    turnover = price * qty
    commission = max(turnover * LEGACY["commission"], LEGACY["min_commission"])
    stamp = turnover * LEGACY["stamp_tax"] if side_is_sell else 0.0
    transfer = turnover * LEGACY["transfer_fee"]
    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp, 2),
        "transfer_fee": round(transfer, 2),
    }


def main() -> int:
    fails: list[str] = []
    n_checked = 0

    print("=" * 78)
    print("A. 默认路径（asset_class 不传 / 传 'stock'）必须与历史实现逐字段相同")
    print("=" * 78)
    for side in (False, True):
        for price in PRICES:
            for qty in QTYS:
                want = legacy_calc_fees(side, price, qty)
                for tag, got in (
                    ("默认", calc_fees(side, price, qty)),
                    ("显式stock", calc_fees(side, price, qty, asset_class="stock")),
                ):
                    n_checked += 1
                    if got != want:
                        fails.append(
                            f"{tag} side={side} px={price} qty={qty}: "
                            f"得到 {got}，历史实现为 {want}"
                        )
    print(f"  对比 {n_checked} 组（side × price × qty）；不一致 {len(fails)} 组")
    print(f"  >>> {'通过：默认路径逐字段不变' if not fails else '失败：默认路径被改动了'}")

    print()
    print("=" * 78)
    print("B. stock 的费率表必须等于历史常量")
    print("=" * 78)
    p = fee_params("stock")
    for k, v in LEGACY.items():
        ok = p[k] == v
        print(f"  {k:<16} 期望 {v:<10} 实得 {p[k]:<10} {'OK' if ok else '不一致'}")
        if not ok:
            fails.append(f"stock.{k}: 期望 {v} 实得 {p[k]}")

    print()
    print("=" * 78)
    print("C. ETF 口径：免印花税、免过户费、佣金与 stock 同")
    print("=" * 78)
    for side in (False, True):
        for price, qty in ((3.017, 2000), (1.0, 6250), (10.0, 625)):
            st = calc_fees(side, price, qty, asset_class="stock")
            et = calc_fees(side, price, qty, asset_class="etf")
            tag = "卖" if side else "买"
            print(f"  {tag} px={price:<7} qty={qty:<6} stock={st}   etf={et}")
            if et["stamp_tax"] != 0.0:
                fails.append(f"etf {tag} stamp_tax 应为 0，实得 {et['stamp_tax']}")
            if et["transfer_fee"] != 0.0:
                fails.append(f"etf {tag} transfer_fee 应为 0，实得 {et['transfer_fee']}")
            if et["commission"] != st["commission"]:
                fails.append(f"etf {tag} commission 应与 stock 相同：{et['commission']} vs {st['commission']}")

    print()
    print("=" * 78)
    print("D. 未登记的资产类别必须**报错**（不静默退化成 stock）")
    print("=" * 78)
    for bad in ("ETFs", "fund", "", "stock "):
        try:
            calc_fees(True, 1.0, 100, asset_class=bad)
            fails.append(f"asset_class={bad!r} 未报错")
            print(f"  {bad!r:<10} 未报错  ← 不合格")
        except KeyError:
            print(f"  {bad!r:<10} KeyError（正确）")

    print()
    print("=" * 78)
    print("E. 一个真实的量级：5 万本金、2 只 ETF、季度调仓（每只约 2.5 万元）")
    print("=" * 78)
    price, qty = 1.0, 25000          # 2.5 万元的单子
    for cls in ("stock", "etf"):
        f = calc_fees(True, price, qty, asset_class=cls)
        tot = sum(f.values())
        print(f"  {cls:<6} 卖出：佣金 {f['commission']:>6.2f}  印花税 {f['stamp_tax']:>6.2f}  "
              f"过户费 {f['transfer_fee']:>5.2f}  合计 {tot:>6.2f} 元"
              f"  （占成交额 {tot / (price * qty):.4%}）")
    print("  >>> ETF 比股票每边省 0.05% 印花税 + 0.001% 过户费 ≈ 0.051%。")
    print("  >>> 季度全换手 = 每年 4 次卖出 → 约 0.204%/年；若按月调仓则 0.612%/年。")
    print("  >>> 对照 docs/小额实盘方案与判据.md §1.2：组合年化量级只有 3~4%，")
    print("  >>> 这不是零头 —— 这正是必须把 ETF 单列一个口径的原因。")

    print()
    print("=" * 78)
    if fails:
        print(f"失败：{len(fails)} 项")
        for f in fails[:20]:
            print(f"  - {f}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
