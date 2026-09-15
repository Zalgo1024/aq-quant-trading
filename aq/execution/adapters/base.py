"""实盘适配器基类。

提供"未接入"时的统一行为：所有交易指令抛 ``NotImplementedAdapter``，
避免误以为壳子能直接下单。

实盘实操提醒
------------
- **QMT（迅投）**：本地客户端 + xtquant Python 库，需客户端登录后本地起服务；
- **PTrade（恒生）**：券商托管，策略运行在券商环境；
- **聚宽/米筐/掘金**：云端策略 + 实盘通道，通过其 API 下单。

无论哪种，都需要实现：
    1. ``connect``   —— 登录、查询账户、订阅回报；
    2. ``submit_order`` / ``cancel_order`` —— 映射到通道的下单撤单接口；
    3. ``get_order_status`` —— 查询或由回报回调维护本地订单簿；
    4. 断线重连与状态对账（live 环境必做）。
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from aq.core.models import Account, Fill, Order, Position
from aq.execution.gateway import GatewayError, TradingGateway

if TYPE_CHECKING:
    from aq.config.settings import Settings


class NotImplementedAdapter(GatewayError):
    """适配器尚未实现。"""


class LiveAdapterBase(TradingGateway):
    """实盘适配器基类：统一未实现行为。"""

    name = "live-base"

    def __init__(self) -> None:
        self._connected = False
        self._cfg: Settings | None = None

    def connect(self, cfg: "Settings") -> None:
        self._cfg = cfg
        raise NotImplementedAdapter(
            f"{self.name} 适配器尚未接入。请参考 aq/execution/adapters/ 下的说明完成实现；"
            "当前请使用 mode=paper + broker=sim 进行模拟验证。"
        )

    @abstractmethod
    def _connect_impl(self, cfg: "Settings") -> None:
        """子类实现具体的登录逻辑。"""

    def get_account(self) -> Account:
        raise NotImplementedAdapter(f"{self.name}.get_account 未实现")

    def get_positions(self) -> list[Position]:
        raise NotImplementedAdapter(f"{self.name}.get_positions 未实现")

    def submit_order(self, order: Order) -> Order:
        raise NotImplementedAdapter(f"{self.name}.submit_order 未实现")

    def cancel_order(self, oid: str) -> bool:
        raise NotImplementedAdapter(f"{self.name}.cancel_order 未实现")

    def get_order_status(self, oid: str) -> Fill | None:
        raise NotImplementedAdapter(f"{self.name}.get_order_status 未实现")
