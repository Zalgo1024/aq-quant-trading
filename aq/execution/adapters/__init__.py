"""实盘通道适配器（预留壳）。

当前均为**未实现**状态；接入时只需补全对应方法，策略/组合/风控代码零改动。

接入前请确认：
1. 已阅读并同意对应通道的使用条款与监管报备要求；
2. 凭据通过环境变量注入（``AQ_EXECUTION__*``），不要提交到版本库；
3. 先在模拟盘充分验证策略。
"""

from aq.execution.adapters.base import LiveAdapterBase, NotImplementedAdapter  # noqa: F401
