"""PTrade（恒生）实盘适配器 —— 预留壳。

PTrade 通常由券商托管，策略运行在券商提供的环境内。
接入时把本适配器的方法映射到 PTrade 提供的
``order`` / ``order_target`` / ``get_positions`` 等 API 即可。

⚠️ 需券商开通权限并完成报备。当前请使用模拟盘。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aq.execution.adapters.base import LiveAdapterBase

if TYPE_CHECKING:
    from aq.config.settings import Settings


class PTradeAdapter(LiveAdapterBase):
    name = "ptrade"

    def _connect_impl(self, cfg: "Settings") -> None:  # pragma: no cover - 预留
        raise NotImplementedError("PTrade 适配器待接入")
