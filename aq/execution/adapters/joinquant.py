"""聚宽 / 米筐 / 掘金等云端通道适配器 —— 预留壳。

这些平台通常提供 REST/WebSocket 下单接口。
接入时把 ``submit_order`` 映射到其 ``place_order`` 即可。

⚠️ 注意各平台的服务条款与合规要求。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aq.execution.adapters.base import LiveAdapterBase

if TYPE_CHECKING:
    from aq.config.settings import Settings


class JoinQuantAdapter(LiveAdapterBase):
    name = "jq"

    def _connect_impl(self, cfg: "Settings") -> None:  # pragma: no cover - 预留
        raise NotImplementedError("聚宽/米筐/掘金适配器待接入")
