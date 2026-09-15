"""组合构建：把信号转成订单。

复用彩票项目"加权复合评分 → 排序取 TopK"的思路。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from aq.core.models import Account, Order, OrderType, Prediction, Side


class Portfolio(ABC):
    """组合构建抽象。"""

    @abstractmethod
    def construct(
        self,
        predictions: list[Prediction],
        account: Account,
        prices: dict[str, float],
        ts: datetime | None = None,
    ) -> list[Order]:
        """给定预测与当前账户，产出目标订单列表。"""


class TopKPortfolio(Portfolio):
    """按评分选出 TopK，等权分配，调仓到目标仓位。

    - 目标持仓 = 评分最高的 K 只，各占 ``weight``；
    - 生成「目标 - 现有」的差额订单（先卖后买）；
    - 卖出资金可用于买入（由风控的 projected 逻辑保证）。
    """

    def __init__(self, top_k: int = 20, weight: float | None = None, min_score: float = 0.55) -> None:
        self.top_k = top_k
        self.weight = weight  # None -> 1/top_k
        self.min_score = min_score

    def construct(
        self,
        predictions: list[Prediction],
        account: Account,
        prices: dict[str, float],
        ts: datetime | None = None,
    ) -> list[Order]:
        ts = ts or datetime.now()

        # 1) 选股：过滤低分，按评分排序取 TopK
        ranked = [p for p in predictions if p.score >= self.min_score]
        ranked.sort(key=lambda p: p.score, reverse=True)
        targets = ranked[: self.top_k]
        target_symbols = {p.symbol for p in targets}

        total_asset = max(account.total_asset, 1e-6)
        w = self.weight if self.weight is not None else (1.0 / self.top_k)

        orders: list[Order] = []

        # 2) 卖出：不在目标池中的持仓
        for sym, pos in account.positions.items():
            if pos.qty > 0 and sym not in target_symbols:
                for oid_qty in _split_sell(pos.available):
                    orders.append(
                        Order(
                            oid="",
                            symbol=sym,
                            side=Side.SELL,
                            qty=oid_qty,
                            type=OrderType.MARKET,
                            strategy_id="topk",
                            account_id=account.account_id,
                            created_at=ts,
                        )
                    )

        # 3) 买入：调仓到目标权重
        for p in targets:
            px = prices.get(p.symbol)
            if not px or px <= 0:
                continue
            target_value = total_asset * w
            target_qty = int(target_value / px // 100) * 100
            pos = account.positions.get(p.symbol)
            cur_qty = pos.qty if pos else 0
            delta = target_qty - cur_qty
            if delta >= 100:
                orders.append(
                    Order(
                        oid="",
                        symbol=p.symbol,
                        side=Side.BUY,
                        qty=delta,
                        type=OrderType.MARKET,
                        strategy_id="topk",
                        account_id=account.account_id,
                        created_at=ts,
                    )
                )
            elif delta <= -100:
                sell_qty = min(abs(delta), (pos.available if pos else 0))
                sell_qty = int(sell_qty // 100) * 100
                if sell_qty >= 100:
                    orders.append(
                        Order(
                            oid="",
                            symbol=p.symbol,
                            side=Side.SELL,
                            qty=sell_qty,
                            type=OrderType.MARKET,
                            strategy_id="topk",
                            account_id=account.account_id,
                            created_at=ts,
                        )
                    )

        return orders


class EqualWeightPortfolio(TopKPortfolio):
    """等权 TopK（TopKPortfolio 的别名，语义更直白）。"""

    def __init__(self, top_k: int = 20, min_score: float = 0.55) -> None:
        super().__init__(top_k=top_k, weight=None, min_score=min_score)


def _split_sell(available: int) -> list[int]:
    """把可卖数量拆成合法的 100 股整数倍（A 股卖出可含零股，这里保守取整）。"""
    qty = int(available // 100) * 100
    return [qty] if qty >= 100 else []
