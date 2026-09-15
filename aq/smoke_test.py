"""自包含端到端冒烟测试。

一条命令跑通全链路，**无需联网、无需外部数据**：

    python -m aq.smoke_test
    python aq/smoke_test.py      # 两种写法都可以

之所以要保证 `python aq/smoke_test.py` 也能跑：双击 bat / IDE 直接右键运行时
经常是「脚本路径」形式，此时 `sys.path[0]` 会变成 `aq/` 目录本身，导致
`import aq.xxx` 直接 ModuleNotFoundError。下面做了自动路径兜底。

覆盖：
 1. 配置加载（base + paper 叠加）
 2. 数据层（mock provider）
 3. 因子计算 + 横截面打分
 4. 风控引擎（T+1 / 涨跌停 / 单票上限 / 资金不足）
 5. SimGateway 撮合（涨停买不进 / 跌停卖不出 / 费用计算 / T+1 冻结）
 6. 回测引擎全流程 + 绩效指标
 7. API 关键端点（health / market / signals / factor / anomaly / account / backtest）
 8. 实盘适配器壳的正确报错

环境说明：本机若注入了 safe-delete shim，脚本只在**自身子进程**层面处理，
不修改机器上的任何全局配置。
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

# --- 路径兜底：保证 `python aq/smoke_test.py` 也能 import aq.* ------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS = "  [PASS]"
FAIL = "  [FAIL]"
_results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> bool:  # type: ignore[no-untyped-def]
    try:
        fn()
        print(f"{PASS} {name}")
        _results.append((name, True, ""))
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {name}: {exc}")
        if os.environ.get("AQ_SMOKE_VERBOSE") == "1":
            traceback.print_exc()
        _results.append((name, False, str(exc)))
        return False


# ==========================================================================
# 1. 配置
# ==========================================================================


def t_config() -> None:
    from aq.config.settings import load_settings

    s = load_settings(mode="paper")
    assert s.mode.value == "paper", f"mode={s.mode}"
    assert s.backtest.initial_cash == 1_000_000.0
    assert s.mode.value == "paper"

    # 环境变量覆盖
    os.environ["AQ_MODEL__TOP_K"] = "7"
    import importlib

    from aq.config import settings as st

    importlib.reload(st)
    s2 = st.load_settings(mode="paper")
    assert s2.model.top_k == 7, f"env override failed: {s2.model.top_k}"
    del os.environ["AQ_MODEL__TOP_K"]
    importlib.reload(st)


# ==========================================================================
# 2. 数据层
# ==========================================================================


def t_data() -> None:
    from aq.config.settings import load_settings
    from aq.data.provider import get_provider

    cfg = load_settings(mode="paper")
    p = get_provider(cfg)
    stocks = p.get_stock_list()
    assert len(stocks) >= 5, f"stock list too short: {len(stocks)}"

    bars = p.get_daily("600000", "2024-01-01", "2024-06-30")
    assert len(bars) > 50, f"bars too few: {len(bars)}"
    assert bars[0].time < bars[-1].time, "bars not sorted"
    assert bars[-1].limit_up is not None, "limit_up missing"


# ==========================================================================
# 3. 因子 + 打分
# ==========================================================================


def t_factors() -> None:
    from aq.config.settings import load_settings
    from aq.data.provider import get_provider
    from aq.factors.library import FactorLibrary
    from aq.factors.scoring import FactorScorer

    cfg = load_settings(mode="paper")
    p = get_provider(cfg)
    lib = FactorLibrary()
    assert len(lib.names) >= 8, f"too few factors: {lib.names}"

    factor_map = {}
    for s in p.get_stock_list()[:8]:
        bars = p.get_daily(s["symbol"], cfg.backtest.start, cfg.backtest.end)
        factor_map[s["symbol"]] = lib.compute(s["symbol"], bars)

    scorer = FactorScorer(lib)
    preds = scorer.score_cross_section(factor_map)
    assert len(preds) >= 5, f"predictions too few: {len(preds)}"
    # 评分应降序
    scores = [x.score for x in preds]
    assert scores == sorted(scores, reverse=True), "predictions not sorted desc"
    # 因子贡献明细应存在，且 Σcontrib ≈ score（前端瀑布图对账依赖此等式）
    top = preds[0]
    assert top.factor_contrib, "factor_contrib empty"
    total_contrib = sum(c.contrib for c in top.factor_contrib)
    assert abs(total_contrib - top.score) < 1e-4, (
        f"contrib mismatch: sum={total_contrib:.8f} score={top.score:.8f}"
    )
    # 归一化得分应在 [0,1]
    for c in top.factor_contrib:
        assert 0.0 <= c.value <= 1.0, f"factor {c.name} value out of range: {c.value}"


# ==========================================================================
# 4. 风控引擎
# ==========================================================================


def t_risk() -> None:
    from aq.core.models import Account, Bar, Order, OrderType, Position, Side
    from aq.portfolio.risk import RiskEngine, RiskLimit

    r = RiskEngine(RiskLimit(single_stock_max=0.10, liquidity_min_turnover=0.0))
    acct = Account(cash=100_000.0)

    bar = Bar(
        symbol="600000", time=datetime.now(), open=10.0, high=10.5, low=9.8,
        close=10.0, volume=1e7, amount=1e8, pre_close=10.0, limit_up=11.0, limit_down=9.0,
    )

    # 单票上限：拟买 20% -> 应拒绝
    o = Order(oid="t1", symbol="600000", side=Side.BUY, qty=2000, type=OrderType.LIMIT, limit_price=10.0)
    ok, reason = r.pre_trade_check(o, acct, bar)
    assert not ok and "单票" in reason, f"should reject: {ok}, {reason}"

    # 正常买入 5% -> 通过
    o2 = Order(oid="t2", symbol="600000", side=Side.BUY, qty=500, type=OrderType.LIMIT, limit_price=10.0)
    ok2, reason2 = r.pre_trade_check(o2, acct, bar)
    assert ok2, f"should pass: {reason2}"

    # T+1：可用为 0 时卖出应拒绝
    acct.positions["600000"] = Position(symbol="600000", qty=1000, available=0, avg_cost=10.0)
    o3 = Order(oid="t3", symbol="600000", side=Side.SELL, qty=1000)
    ok3, reason3 = r.pre_trade_check(o3, acct, bar)
    assert not ok3 and "T+1" in reason3, f"T+1 not enforced: {ok3}, {reason3}"

    # 涨停禁买
    up_bar = bar.model_copy(update={"close": 11.0})
    o4 = Order(oid="t4", symbol="600000", side=Side.BUY, qty=100)
    ok4, reason4 = r.pre_trade_check(o4, acct, up_bar)
    assert not ok4 and "涨停" in reason4, f"limit-up buy not blocked: {reason4}"

    # 止损
    acct.positions["600519"] = Position(
        symbol="600519", qty=100, available=100, avg_cost=100.0, last_price=85.0
    )
    stops = r.check_stop_loss(acct)
    assert any(s[0] == "600519" for s in stops), f"stop loss not triggered: {stops}"


# ==========================================================================
# 5. SimGateway 撮合
# ==========================================================================


def t_sim_gateway() -> None:
    from aq.config.settings import load_settings
    from aq.core.models import Bar, Order, OrderStatus, OrderType, Side
    from aq.execution.sim_gateway import SimGateway

    cfg = load_settings(mode="paper")
    gw = SimGateway()
    cfg.execution.persist_path = ""   # 测试不落盘
    gw.connect(cfg)
    gw.reset(cash=1_000_000.0)
    gw.set_slippage(0.001)

    def mk_bar(close: float, open_: float, up: float, down: float, trading: bool = True) -> Bar:
        return Bar(
            symbol="600000", time=datetime(2024, 1, 2), open=open_, high=max(open_, close),
            low=min(open_, close), close=close, volume=1e7, amount=1e8,
            pre_close=close, limit_up=up, limit_down=down, is_trading=trading,
        )

    # --- 正常买入 ---
    o = gw.submit_order(Order(oid="", symbol="600000", side=Side.BUY, qty=1000, type=OrderType.MARKET))
    fill = gw.execute(o.oid, mk_bar(10.0, 10.0, 11.0, 9.0))
    assert fill.status == OrderStatus.FILLED, f"buy failed: {fill.status} {fill.reason}"
    assert fill.avg_price == 10.01, f"slippage wrong: {fill.avg_price}"   # 10.0 * 1.001
    assert fill.commission >= 5.0, f"commission too low: {fill.commission}"
    assert fill.stamp_tax == 0.0, "buy should have no stamp tax"
    acct = gw.get_account()
    assert "600000" in acct.positions
    assert acct.positions["600000"].qty == 1000

    # --- T+1：当日买入不可卖 ---
    o_sell = gw.submit_order(Order(oid="", symbol="600000", side=Side.SELL, qty=1000))
    fill2 = gw.execute(o_sell.oid, mk_bar(10.5, 10.5, 11.5, 9.5))
    assert fill2.status == OrderStatus.REJECTED, f"T+1 not enforced: {fill2.status}"

    # --- 次日解冻后可卖，卖出收印花税 ---
    gw.on_new_day("2024-01-03")
    assert gw.get_account().positions["600000"].available == 1000
    o_sell2 = gw.submit_order(Order(oid="", symbol="600000", side=Side.SELL, qty=1000))
    fill3 = gw.execute(o_sell2.oid, mk_bar(11.0, 11.0, 12.0, 10.0))
    assert fill3.status == OrderStatus.FILLED, f"sell failed: {fill3.reason}"
    assert fill3.stamp_tax > 0, "sell should have stamp tax"

    # --- 涨停买不进 ---
    o_buy_up = gw.submit_order(Order(oid="", symbol="600519", side=Side.BUY, qty=100))
    fill4 = gw.execute(o_buy_up.oid, mk_bar(11.0, 11.0, 11.0, 9.0))
    assert fill4.status == OrderStatus.UNFILLED, f"limit-up buy should be unfilled: {fill4.status}"

    # --- 跌停卖不出 ---
    gw.account.positions["600519"] = gw.account.positions.get("600519") or __import__(
        "aq.core.models", fromlist=["Position"]
    ).Position(symbol="600519", qty=100, available=100, avg_cost=10.0)
    o_sell_dn = gw.submit_order(Order(oid="", symbol="600519", side=Side.SELL, qty=100))
    fill5 = gw.execute(o_sell_dn.oid, mk_bar(9.0, 9.0, 11.0, 9.0))
    assert fill5.status == OrderStatus.UNFILLED, f"limit-down sell should be unfilled: {fill5.status}"

    # --- 停牌 ---
    o_susp = gw.submit_order(Order(oid="", symbol="600000", side=Side.BUY, qty=100))
    fill6 = gw.execute(o_susp.oid, mk_bar(10.0, 10.0, 11.0, 9.0, trading=False))
    assert fill6.status == OrderStatus.UNFILLED, f"suspension should block: {fill6.status}"

    # --- 资金不足 ---
    o_big = gw.submit_order(Order(oid="", symbol="600000", side=Side.BUY, qty=10_000_000))
    fill7 = gw.execute(o_big.oid, mk_bar(10.0, 10.0, 11.0, 9.0))
    assert fill7.filled_qty < 10_000_000, "should be shrunk or rejected"


# ==========================================================================
# 6. 回测
# ==========================================================================


def t_backtest() -> None:
    from aq.backtest.engine import BacktestEngine
    from aq.config.settings import load_settings

    cfg = load_settings(mode="backtest")
    cfg.backtest.start = "2023-01-01"
    cfg.backtest.end = "2024-06-30"
    cfg.execution.persist_path = ""

    result = BacktestEngine(cfg).run()
    assert result.run_id, "no run_id"
    assert len(result.equity) > 100, f"equity too short: {len(result.equity)}"
    assert result.metrics.total_return is not None
    assert result.metrics.max_drawdown <= 0, f"max_drawdown should be <=0: {result.metrics.max_drawdown}"
    assert len(result.drawdown) == len(result.equity)

    m = result.metrics
    print(
        f"        → 区间 {result.start} ~ {result.end} | "
        f"总收益 {m.total_return:.2%} | 年化 {m.annual_return:.2%} | "
        f"夏普 {m.sharpe} | 最大回撤 {m.max_drawdown:.2%} | 成交 {len(result.trades)} 笔"
    )


# ==========================================================================
# 7. API
# ==========================================================================


def t_api() -> None:
    from fastapi.testclient import TestClient

    from aq.api.app import app

    client = TestClient(app)

    r = client.get("/api/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"

    r = client.get("/api/config")
    assert r.status_code == 200

    r = client.get("/api/stocks")
    assert r.status_code == 200 and len(r.json()) > 0

    r = client.get("/api/market/overview")
    assert r.status_code == 200 and "up_count" in r.json()

    r = client.get("/api/signals")
    assert r.status_code == 200
    signals = r.json()

    r = client.get("/api/factor/analysis")
    assert r.status_code == 200 and len(r.json()["factors"]) > 0

    r = client.get("/api/anomaly")
    assert r.status_code == 200

    r = client.get("/api/account")
    assert r.status_code == 200 and "total_asset" in r.json()

    code = client.get("/api/stocks").json()[0]["symbol"]
    r = client.get(f"/api/stock/{code}/detail")
    assert r.status_code == 200 and len(r.json()["bars"]) > 0

    r = client.get(f"/api/stock/{code}/prediction")
    assert r.status_code == 200, r.text

    r = client.post("/api/backtest", json={"start": "2024-01-01", "end": "2024-09-30", "top_k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"], "no run_id"
    assert "metrics" in body

    r = client.get(f"/api/backtest/{body['run_id']}")
    assert r.status_code == 200

    print(f"        → 信号 {len(signals)} 条 | 回测 {body['run_id']} 通过")


# ==========================================================================
# 8. 实盘壳
# ==========================================================================


def t_live_shell() -> None:
    from aq.config.settings import load_settings
    from aq.execution.gateway import create_gateway
    from aq.execution.adapters.base import NotImplementedAdapter

    cfg = load_settings(mode="paper")
    for broker in ("qmt", "ptrade", "jq"):
        try:
            create_gateway(broker, cfg)
        except NotImplementedAdapter:
            continue
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{broker} 应抛 NotImplementedAdapter，实际: {type(exc).__name__}: {exc}") from exc
        else:
            raise AssertionError(f"{broker} 未实现却创建成功")


# ==========================================================================
# main
# ==========================================================================


def main() -> int:
    print("=" * 70)
    print(" A 股 AI 量化交易系统 —— 冒烟测试")
    print("=" * 70)

    check("1. 配置加载（yaml + 环境变量覆盖）", t_config)
    check("2. 数据层（provider + 行情）", t_data)
    check("3. 因子计算 + 横截面打分 + 贡献明细", t_factors)
    check("4. 风控引擎（单票上限/T+1/涨停/止损）", t_risk)
    check("5. SimGateway 撮合（滑点/费用/T+1/涨跌停/停牌）", t_sim_gateway)
    check("6. 回测引擎全流程 + 绩效指标", t_backtest)
    check("7. API 端点（health/market/signals/backtest…）", t_api)
    check("8. 实盘适配器壳正确报错", t_live_shell)

    print("-" * 70)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f" 结果: {passed}/{total} 通过")

    if passed < total:
        print("\n 失败项：")
        for name, ok, err in _results:
            if not ok:
                print(f"   - {name}: {err}")
        return 1

    print(" 全部通过。可启动 API：python -m uvicorn aq.api.app:app --port 8000")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
