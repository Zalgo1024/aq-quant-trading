"""QMT（迅投 xtquant）实盘适配器 —— 预留壳。

接入步骤（将来）：
1. 安装迅投 MiniQMT 客户端并登录；
2. ``pip install xtquant``（随客户端提供，通常需手动拷贝）；
3. 实现 ``_connect_impl``：``XtQuantTrader(path, session_id)`` + ``start()`` + ``subscribe``；
4. ``submit_order`` 映射为 ``order_stock(...)``；
5. ``get_account``/``get_positions`` 使用 ``query_stock_asset`` / ``query_stock_positions``；
6. 处理 ``on_stock_order`` / ``on_stock_trade`` 回报回调。

⚠️ 实盘前请完成程序化交易报备，并先以极小资金试跑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aq.execution.adapters.base import LiveAdapterBase

if TYPE_CHECKING:
    from aq.config.settings import Settings


class QMTAdapter(LiveAdapterBase):
    name = "qmt"

    def _connect_impl(self, cfg: "Settings") -> None:  # pragma: no cover - 预留
        raise NotImplementedError("QMT 适配器待接入")
