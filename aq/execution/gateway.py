"""交易网关抽象 —— 模拟/实盘的分水岭。

上层（策略 / 组合 / 风控）只依赖 ``TradingGateway`` 接口，
永远不知道背后是模拟撮合还是真实券商通道。

切换实盘：实现一个新的 ``TradingGateway`` 子类并在 ``create_gateway`` 注册。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from aq.core.models import Account, Fill, Order, Position

if TYPE_CHECKING:
    from aq.config.settings import Settings


class GatewayError(RuntimeError):
    """网关层统一异常。"""


class TradingGateway(ABC):
    """交易网关接口契约。

    实现类必须保证：
    - 线程安全（行情线程与策略线程可能并发调用）；
    - 所有下单失败均以 ``Fill(status=REJECTED/UNFILLED, reason=...)`` 返回，**不抛异常**；
    - ``get_account()`` 返回的是**快照副本**，调用方修改不应影响内部状态。
    """

    #: 网关标识，与配置 ``execution.broker`` 对应
    name: str = "abstract"

    # ------------------------------------------------------------------ 连接
    @abstractmethod
    def connect(self, cfg: "Settings") -> None:
        """建立连接/初始化账户。"""

    def disconnect(self) -> None:
        """断开连接（默认空实现）。"""

    @property
    def is_connected(self) -> bool:
        return getattr(self, "_connected", False)

    # ------------------------------------------------------------ 账户查询
    @abstractmethod
    def get_account(self) -> Account:
        """账户快照（现金/冻结/持仓/浮盈）。"""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """持仓列表。"""

    def get_position(self, symbol: str) -> Position | None:
        for p in self.get_positions():
            if p.symbol == symbol:
                return p
        return None

    # ------------------------------------------------------------ 交易指令
    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """提交订单，返回带 oid/status 的订单对象。"""

    @abstractmethod
    def cancel_order(self, oid: str) -> bool:
        """撤单。成功返回 True。"""

    @abstractmethod
    def get_order_status(self, oid: str) -> Fill | None:
        """查询订单成交情况。"""

    # -------------------------------------------------------------- 扩展点
    def on_fill(self, cb) -> None:  # type: ignore[no-untyped-def]
        """注册成交回报回调（实盘通道通常异步推送）。"""
        self._fill_cbs = getattr(self, "_fill_cbs", [])
        self._fill_cbs.append(cb)

    def _emit_fill(self, fill: Fill) -> None:
        for cb in getattr(self, "_fill_cbs", []):
            cb(fill)


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------


def create_gateway(broker: str, cfg: "Settings") -> TradingGateway:
    """按配置创建网关实例。

    - ``sim``    -> SimGateway（现在可用）
    - ``qmt``    -> QMTAdapter（预留壳，未接入）
    - ``ptrade`` -> PTradeAdapter（预留壳）
    - ``jq``     -> JoinQuantAdapter（预留壳）
    """
    broker = (broker or "sim").lower()

    if broker == "sim":
        from aq.execution.sim_gateway import SimGateway

        gw: TradingGateway = SimGateway()
        gw.connect(cfg)
        return gw

    # 实盘通道按需 import，避免未安装依赖时影响主流程
    if broker == "qmt":
        from aq.execution.adapters.qmt import QMTAdapter

        gw = QMTAdapter()
        gw.connect(cfg)
        return gw

    if broker == "ptrade":
        from aq.execution.adapters.ptrade import PTradeAdapter

        gw = PTradeAdapter()
        gw.connect(cfg)
        return gw

    if broker == "jq":
        from aq.execution.adapters.joinquant import JoinQuantAdapter

        gw = JoinQuantAdapter()
        gw.connect(cfg)
        return gw

    raise GatewayError(f"未知的 broker: {broker!r}（可选：sim/qmt/ptrade/jq）")
