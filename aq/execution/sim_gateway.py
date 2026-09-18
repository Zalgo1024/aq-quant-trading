"""SimGateway —— 本地模拟撮合网关（A 股真实规则）。

回测与模拟盘**共用此撮合内核**，避免"回测一套、实盘一套"的经典陷阱。

A 股规则建模：
1. **T+1**：当日买入的股票次日才可卖出；
2. **涨跌停封板**：开盘即涨停 → 买单不可成交；开盘即跌停 → 卖单不可成交；
3. **费用**：佣金（万 2.5，最低 5 元）+ 印花税（仅卖出，千 0.5）+ 过户费；
4. **滑点**：买入成交价上浮、卖出下浮；
5. **最小单位**：买入须为 100 股整数倍（校验在 Order 中完成）；
6. **不可裸卖空**：卖出数量不得超过可用持仓。

撮合约定
--------
- 成交价基于**下一根 bar 的开盘价 ± 滑点**，杜绝未来函数；
- 账户状态可持久化为 JSON，重启不丢仓位。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aq.core.models import (
    Account,
    Bar,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from aq.core.rules import calc_fees
from aq.execution.gateway import GatewayError, TradingGateway

if TYPE_CHECKING:
    from aq.config.settings import Settings


class SimGateway(TradingGateway):
    """虚拟账户 + A 股撮合。"""

    name = "sim"

    def __init__(self) -> None:
        self._connected = False
        self.account = Account()
        self._orders: dict[str, Order] = {}
        self._fills: dict[str, Fill] = {}
        self._persist_path: Path | None = None
        self._t1_locked: dict[str, int] = {}   # symbol -> 当日买入未解冻数量
        self._last_trade_date: str = ""
        #: symbol -> 资产类别（"stock" / "etf"），决定费率口径。
        #: **默认空 = 全部按 stock**，保证既有个股回测逐字不变。
        #: ⚠️ 不要用「代码段」自动推断 ETF：510080/560002 这类代码落在 ETF 段
        #: 却是普通开放式基金（见 scripts/probes/probe_etf_reach.py 第 2 段）。
        #: ETF 路线必须显式注入（成员关系来自 data_cache/etf_list.parquet）。
        self._asset_class: dict[str, str] = {}

    # ------------------------------------------------------------ 资产类别
    def set_asset_class_map(self, mapping: dict[str, str]) -> None:
        """注入 symbol → 资产类别映射（ETF 路线用）。默认不注入 = 全是 stock。"""
        self._asset_class = {str(k): str(v) for k, v in mapping.items()}

    def _class_of(self, symbol: str) -> str:
        return self._asset_class.get(str(symbol), "stock")

    # ------------------------------------------------------------ 连接/持久化
    def connect(self, cfg: "Settings") -> None:
        self._persist_path = None
        if cfg.execution.persist_path:
            self._persist_path = Path(cfg.execution.persist_path)
            if not self._persist_path.is_absolute():
                from aq.config.settings import PROJECT_ROOT

                self._persist_path = PROJECT_ROOT / self._persist_path
            self._load_state()

        if not self.account.positions and self.account.cash == 1_000_000.0:
            # 首次初始化：使用配置初始资金
            self.account = Account(
                account_id=cfg.execution.account_id,
                cash=cfg.backtest.initial_cash,
            )
        self._connected = True

    def reset(self, cash: float = 1_000_000.0, account_id: str = "sim") -> None:
        """重置账户（回测每个 run 开头调用）。"""
        self.account = Account(account_id=account_id, cash=cash)
        self._orders.clear()
        self._fills.clear()
        self._t1_locked.clear()
        self._last_trade_date = ""

    def _load_state(self) -> None:
        if self._persist_path and self._persist_path.exists():
            try:
                raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
                self.account = Account(**raw)
                self._t1_locked = raw.get("_t1_locked", {})
                self._last_trade_date = raw.get("_last_trade_date", "")
            except (json.JSONDecodeError, ValueError, TypeError):
                # 状态文件损坏时从零开始，不让模拟盘整个挂掉
                self.account = Account()
                self._t1_locked = {}

    def persist(self) -> None:
        """落盘账户状态。"""
        if not self._persist_path:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.account.model_dump(mode="json")
        payload["_t1_locked"] = self._t1_locked
        payload["_last_trade_date"] = self._last_trade_date
        self._persist_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------ 账户查询
    def get_account(self) -> Account:
        # 返回深拷贝快照，避免调用方误改内部状态
        self._refresh_market_values()
        return self.account.model_copy(deep=True)

    def get_positions(self) -> list[Position]:
        self._refresh_market_values()
        return [p.model_copy(deep=True) for p in self.account.positions.values()]

    # ------------------------------------------------------------ 交易日切换
    def on_new_day(self, trade_date: str) -> None:
        """每个交易日开始时调用：解开昨日买入的 T+1 冻结。"""
        if trade_date != self._last_trade_date:
            for sym, pos in self.account.positions.items():
                pos.available = pos.qty
            self._t1_locked.clear()
            self._last_trade_date = trade_date

    # ------------------------------------------------------------ 下单
    def submit_order(self, order: Order) -> Order:
        if not self._connected:
            raise GatewayError("SimGateway 未连接，请先 connect()")

        order.oid = order.oid or uuid.uuid4().hex[:12]
        order.created_at = order.created_at or datetime.now()
        order.status = OrderStatus.SUBMITTED
        self._orders[order.oid] = order
        self._fills[order.oid] = Fill(
            oid=order.oid,
            symbol=order.symbol,
            side=order.side,
            status=OrderStatus.SUBMITTED,
        )
        return order

    def execute(self, oid: str, next_bar: Bar, risk_engine=None) -> Fill:
        """对已提交订单进行撮合。

        由回测引擎/模拟盘循环在**取得下一根 bar 后**调用。

        Parameters
        ----------
        oid:
            订单号。
        next_bar:
            下一根（成交时点）bar，用于确定成交价与涨跌停状态。
        risk_engine:
            可选的事前风控引擎，若提供则先校验。
        """
        order = self._orders.get(oid)
        if order is None:
            raise GatewayError(f"未知订单 {oid}")

        # --- 1) 事前风控（与实盘共用同一份规则）---
        if risk_engine is not None:
            ok, reason = risk_engine.pre_trade_check(order, self.account, next_bar)
            if not ok:
                return self._reject(order, reason)

        # --- 2) 停牌不可成交 ---
        if not next_bar.is_trading:
            return self._unfilled(order, "停牌")

        # --- 3) 涨跌停封板：买不进涨停、卖不出跌停 ---
        # 注意：撮合一律用**真实价**（bar.open_raw / limit_up_raw）。
        # bar.open/close 是后复权价，可能是真实价的十几倍，直接拿来算资金会全错。
        ref_open = next_bar.open_raw
        lu = next_bar.limit_up_raw
        ld = next_bar.limit_down_raw
        if order.side == Side.BUY and lu and ref_open >= lu - 1e-6:
            return self._unfilled(order, "涨停封板，买单无法成交")
        if order.side == Side.SELL and ld and ref_open <= ld + 1e-6:
            return self._unfilled(order, "跌停封板，卖单无法成交")

        # --- 4) 限价单价格校验 ---
        if order.type == OrderType.LIMIT and order.limit_price is not None:
            if order.side == Side.BUY and order.limit_price < ref_open:
                return self._unfilled(order, "限价低于开盘价，未触及")
            if order.side == Side.SELL and order.limit_price > ref_open:
                return self._unfilled(order, "限价高于开盘价，未触及")

        # --- 5) 成交价（下一根 bar 开盘价 ± 滑点）---
        slip_rate = getattr(self, "_slippage", 0.001)
        sign = 1 if order.side == Side.BUY else -1
        fill_px = round(ref_open * (1 + sign * slip_rate), 2)

        # --- 6) 资金/持仓校验 ---
        qty = order.qty
        asset_class = self._class_of(order.symbol)
        if order.side == Side.BUY:
            fees = calc_fees(False, fill_px, qty, asset_class=asset_class)
            need = round(fill_px * qty + sum(fees.values()), 2)
            if need > self.account.cash:
                # 资金不足时按可买数量缩量（向下取整到 100 股）
                affordable = int((self.account.cash / (fill_px * (1 + 0.0005))) // 100) * 100
                if affordable < 100:
                    return self._reject(order, f"资金不足（需 {need:.2f}，可用 {self.account.cash:.2f}）")
                qty = affordable
        else:
            pos = self.account.positions.get(order.symbol)
            avail = pos.available if pos else 0
            if avail <= 0:
                return self._reject(order, "可用持仓为 0（T+1 或未持有）")
            if qty > avail:
                qty = avail  # 缩量到可用数量

        # --- 7) 更新账户与持仓 ---
        fees = calc_fees(order.side == Side.SELL, fill_px, qty, asset_class=asset_class)
        turnover = round(fill_px * qty, 2)
        pos = self.account.positions.get(order.symbol)
        if pos is None:
            pos = Position(symbol=order.symbol)
            self.account.positions[order.symbol] = pos

        if order.side == Side.BUY:
            self.account.cash = round(self.account.cash - turnover - sum(fees.values()), 2)
            new_qty = pos.qty + qty
            pos.avg_cost = round((pos.avg_cost * pos.qty + turnover) / new_qty, 4) if new_qty else 0.0
            pos.qty = new_qty
            pos.last_price = fill_px
            # T+1：当日买入不计入可卖
            self._t1_locked[order.symbol] = self._t1_locked.get(order.symbol, 0) + qty
            pos.available = max(0, pos.qty - self._t1_locked.get(order.symbol, 0))
        else:
            self.account.cash = round(self.account.cash + turnover - sum(fees.values()), 2)
            realized = round((fill_px - pos.avg_cost) * qty, 2)
            pos.realized_pnl = round(pos.realized_pnl + realized, 2)
            self.account.realized_pnl = round(self.account.realized_pnl + realized, 2)
            pos.qty -= qty
            pos.available = max(0, pos.available - qty)
            pos.last_price = fill_px
            if pos.qty <= 0:
                pos.qty = 0
                pos.available = 0
                pos.avg_cost = 0.0

        self.account.updated_at = datetime.now()

        fill = Fill(
            oid=order.oid,
            symbol=order.symbol,
            side=order.side,
            filled_qty=qty,
            avg_price=fill_px,
            commission=fees["commission"],
            stamp_tax=fees["stamp_tax"],
            transfer_fee=fees["transfer_fee"],
            slippage_cost=round(abs(fill_px - ref_open) * qty, 2),
            status=OrderStatus.FILLED if qty == order.qty else OrderStatus.PARTIAL,
            ts=next_bar.time,
        )
        order.status = fill.status
        self._fills[order.oid] = fill
        self._emit_fill(fill)
        return fill

    def cancel_order(self, oid: str) -> bool:
        order = self._orders.get(oid)
        if order is None or order.status not in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            return False
        order.status = OrderStatus.CANCELLED
        return True

    def get_order_status(self, oid: str) -> Fill | None:
        return self._fills.get(oid)

    def all_fills(self) -> list[Fill]:
        return [f for f in self._fills.values() if f.filled_qty > 0]

    # ------------------------------------------------------------ 内部工具
    def _refresh_market_values(self) -> None:
        """用最近一次已知价格刷新市值（回测中由 mark_to_market 更新）。"""
        for p in self.account.positions.values():
            p.last_price = p.last_price or p.avg_cost

    def mark_to_market(self, bars: dict[str, Bar]) -> None:
        """按当日收盘价重估持仓。

        用**真实价** close_raw：账户的现金、成本、市值都在真实价尺度上，
        若混入后复权价会把市值放大十几倍，浮盈浮亏全错。
        """
        for sym, bar in bars.items():
            if sym in self.account.positions:
                self.account.positions[sym].last_price = bar.close_raw

    def _reject(self, order: Order, reason: str) -> Fill:
        order.status = OrderStatus.REJECTED
        order.reason = reason
        fill = Fill(
            oid=order.oid,
            symbol=order.symbol,
            side=order.side,
            status=OrderStatus.REJECTED,
            reason=reason,
        )
        self._fills[order.oid] = fill
        return fill

    def _unfilled(self, order: Order, reason: str) -> Fill:
        order.status = OrderStatus.UNFILLED
        order.reason = reason
        fill = Fill(
            oid=order.oid,
            symbol=order.symbol,
            side=order.side,
            status=OrderStatus.UNFILLED,
            reason=reason,
        )
        self._fills[order.oid] = fill
        return fill

    def set_slippage(self, rate: float) -> None:
        self._slippage = rate
