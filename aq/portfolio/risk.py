"""风控引擎 —— **模拟盘与实盘共用同一份规则**。

设计原则：所有"交易该不该发出去"的判断集中在此，
策略层不做风控，网关层不重复实现风控。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aq.core.models import Account, Bar, Order, Position, Side
from aq.core.rules import DEFAULT_RISK, board_of, limit_prices


@dataclass
class RiskLimit:
    single_stock_max: float = DEFAULT_RISK["single_stock_max"]
    industry_max: float = DEFAULT_RISK["industry_max"]
    total_position_max: float = DEFAULT_RISK["total_position_max"]
    stop_loss: float = DEFAULT_RISK["stop_loss"]
    max_drawdown: float = DEFAULT_RISK["max_drawdown"]
    liquidity_min_turnover: float = DEFAULT_RISK["liquidity_min_turnover"]
    # 黑名单：ST / 退市 / 停牌
    blacklist: set[str] = field(default_factory=set)
    # 行业映射（symbol -> 行业名）
    industry_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg) -> "RiskLimit":  # type: ignore[no-untyped-def]
        r = cfg.risk
        return cls(
            single_stock_max=r.single_stock_max,
            industry_max=r.industry_max,
            total_position_max=r.total_position_max,
            stop_loss=r.stop_loss,
            max_drawdown=r.max_drawdown,
            liquidity_min_turnover=r.liquidity_min_turnover,
        )


class RiskEngine:
    """事前风控：订单级 + 组合级。"""

    def __init__(self, limit: RiskLimit | None = None) -> None:
        self.limit = limit or RiskLimit()

    # ---------------------------------------------------------------- 订单级
    def pre_trade_check(
        self,
        order: Order,
        account: Account,
        bar: Bar | None = None,
    ) -> tuple[bool, str]:
        """单笔订单校验。返回 (是否通过, 原因)。"""
        lim = self.limit

        # 1) 黑名单（ST / 退市 / 停牌）
        if order.symbol in lim.blacklist:
            return False, f"{order.symbol} 在黑名单中（ST/退市标的）"

        # 2) 停牌
        if bar is not None and not bar.is_trading:
            return False, "标的停牌"

        # 3) 涨跌停方向性校验（快速剔除必不成交单）
        if bar is not None and bar.limit_up is not None and bar.limit_down is not None:
            if order.side == Side.BUY and bar.close >= bar.limit_up - 1e-6:
                return False, "涨停板买入风险，禁止追板"
            if order.side == Side.SELL and bar.close <= bar.limit_down + 1e-6:
                return False, "跌停板无法卖出"

        # 4) 流动性门槛
        if bar is not None and bar.amount and bar.amount < lim.liquidity_min_turnover:
            return False, f"流动性不足（成交额 {bar.amount / 1e8:.2f} 亿 < 门槛 {lim.liquidity_min_turnover / 1e8:.2f} 亿）"

        # 5) 卖出：不可裸卖空 + T+1
        if order.side == Side.SELL:
            pos = account.positions.get(order.symbol)
            avail = pos.available if pos else 0
            if avail <= 0:
                return False, "可用持仓为 0（T+1 限制或未持有）"
            if order.qty > avail:
                return False, f"卖出 {order.qty} 股超出可用 {avail} 股"

        # 6) 买入：单票上限
        if order.side == Side.BUY:
            total = max(account.total_asset, 1e-6)
            pos = account.positions.get(order.symbol)
            cur_value = pos.market_value if pos else 0.0
            px = order.limit_price or (bar.close_raw if bar else (pos.last_price if pos else 0.0))
            add_value = px * order.qty
            if (cur_value + add_value) / total > lim.single_stock_max + 1e-6:
                return (
                    False,
                    f"超出单票上限 {lim.single_stock_max:.0%}"
                    f"（现 {cur_value:.0f} + 拟买 {add_value:.0f} / 总 {total:.0f}）",
                )

            # 7) 现金校验
            if add_value > account.cash:
                return False, f"资金不足（需 {add_value:.0f}，可用 {account.cash:.0f}）"

        return True, ""

    # ---------------------------------------------------------------- 组合级
    def portfolio_check(
        self,
        orders: list[Order],
        account: Account,
        bars: dict[str, Bar] | None = None,
    ) -> tuple[list[Order], list[tuple[Order, str]]]:
        """组合级校验：总仓位、行业上限。

        Returns
        -------
        (approved, rejected)
            approved 为通过的订单；rejected 为 (订单, 原因) 列表。
        """
        lim = self.limit
        approved: list[Order] = []
        rejected: list[tuple[Order, str]] = []

        total = max(account.total_asset, 1e-6)
        # 现有市值（按行业汇总）
        industry_value: dict[str, float] = {}
        for p in account.positions.values():
            ind = lim.industry_map.get(p.symbol, "unknown")
            industry_value[ind] = industry_value.get(ind, 0.0) + p.market_value

        current_pos_value = sum(p.market_value for p in account.positions.values())
        projected = current_pos_value

        # 先卖后买，卖出释放的仓位可用于买入
        # 注意：不能用 sorted(key=...) 直接排 Order —— pydantic 模型不可哈希，
        # 元素比较会触发 __eq__ 导致 TypeError。改用索引稳定排序。
        ordered = sorted(enumerate(orders), key=lambda t: (0 if t[1].side == Side.SELL else 1, t[0]))
        for _, o in ordered:
            bar = bars.get(o.symbol) if bars else None
            ok, reason = self.pre_trade_check(o, account, bar)
            if not ok:
                rejected.append((o, reason))
                continue

            px = o.limit_price or (bar.close_raw if bar else 0.0)
            value = px * o.qty

            if o.side == Side.BUY:
                # 总仓位上限
                if (projected + value) / total > lim.total_position_max + 1e-6:
                    rejected.append((o, f"超出总仓位上限 {lim.total_position_max:.0%}"))
                    continue
                # 行业上限
                ind = lim.industry_map.get(o.symbol, "unknown")
                if (industry_value.get(ind, 0.0) + value) / total > lim.industry_max + 1e-6:
                    rejected.append((o, f"行业【{ind}】超出上限 {lim.industry_max:.0%}"))
                    continue
                industry_value[ind] = industry_value.get(ind, 0.0) + value
                projected += value
            else:
                projected -= value
                ind = lim.industry_map.get(o.symbol, "unknown")
                industry_value[ind] = max(0.0, industry_value.get(ind, 0.0) - value)

            approved.append(o)

        return approved, rejected

    # ---------------------------------------------------------------- 止损
    def check_stop_loss(self, account: Account) -> list[tuple[str, str]]:
        """返回触发止损的 (symbol, 原因) 列表。"""
        out: list[tuple[str, str]] = []
        for sym, p in account.positions.items():
            if p.qty <= 0 or p.avg_cost <= 0:
                continue
            loss = (p.last_price - p.avg_cost) / p.avg_cost
            if loss <= -self.limit.stop_loss:
                out.append((sym, f"触发止损（浮亏 {loss:.2%} <= -{self.limit.stop_loss:.0%}）"))
        return out


def default_risk_engine(cfg=None) -> RiskEngine:  # type: ignore[no-untyped-def]
    if cfg is None:
        return RiskEngine()
    return RiskEngine(RiskLimit.from_config(cfg))
