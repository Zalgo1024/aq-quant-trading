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
 9. 防过拟合统计（CSCV / PBO / DSR / N_eff）的标定
10. Walk-forward 定权的前视偏差（公式 / 因果 / 节奏三层）
11. 探针台账与磁盘的一致性 + 漂移检测的负对照

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
    # 冒烟测试只验证"链路跑通"，不是性能测试：主口径改成全市场流动性池后
    # 候选有 3800+ 只，建一次因子面板要好几分钟，会把自检拖垮。
    # 这里显式截断规模（仍走 all 路径，与生产口径一致，只是池子小）。
    # 注：max_symbols 是按代码序截断的，有系统性偏置 —— 生产上保持 0（不截断），
    # 仅在此处为提速而用，不影响"回测引擎能否跑完并真实建仓"这个断言。
    cfg.universe.max_symbols = 300

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
    assert r.status_code == 200, r.text
    ov = r.json()
    # 契约在 2026-09-18「接口真实化」改造后变了：涨跌家数从顶层 up_count
    # 挪进 breadth（up/down/flat）。这里跟着改 —— 否则自检会一直假红，
    # 而"自检红着"比"没自检"更糟：它会让人习惯性忽略失败项。
    assert "breadth" in ov, f"overview 缺 breadth 字段: {sorted(ov)}"
    assert ov["breadth"]["up"] + ov["breadth"]["down"] > 0, (
        "全市场快照为空（涨跌家数都是 0）—— 页面会显示假数据，属于硬规则 ③ 的违反")

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

    # ⚠️ top_k 不能小于 1/single_stock_max：每只目标权重 = 1/top_k，
    # top_k=5 意味着每只 20% > 单票上限 10%，于是**所有买单必然被拒**，
    # 回测跑得完、曲线看着正常，但仓位恒为 0 —— 这条测试曾经就这样
    # "通过"了很久，因为它只检查 run_id 存在。单票上限 10% → 至少 10 只，
    # 取 20 留余量。
    # max_symbols：全市场池 3800+ 只会把自检拖到几分钟，这里只验证链路。
    # 断言重点是"真的建仓了"（见下方 avg_exposure），池子大小不影响该断言。
    r = client.post("/api/backtest", json={"start": "2024-01-01", "end": "2024-09-30",
                                           "top_k": 20, "max_symbols": 300})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"], "no run_id"
    assert "metrics" in body

    # 断言它**真的买进去了**：只验证流程跑通会放过"什么都没做"的回测
    diag = body.get("diagnostics") or {}
    expo = diag.get("avg_exposure")
    assert expo is not None, "接口未返回 diagnostics，无法判断回测是否真的执行"
    assert expo > 0.5, (
        f"回测跑完但平均仓位只有 {expo:.1%}，订单多半被风控全拒 "
        f"（拒单原因：{diag.get('reject_reasons')}）—— 这条测试等于没验证")

    r = client.get(f"/api/backtest/{body['run_id']}")
    assert r.status_code == 200

    # signals 在改造后是「对象」而非裸数组，条目在 signals["signals"] 里
    print(f"        → 信号 {len(signals.get('signals', []))} 条 "
          f"(样本 {signals.get('sample_size')} 只) | 回测 {body['run_id']} 通过")


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
# 9. 防过拟合统计（CSCV / PBO / DSR）的**标定**
# ==========================================================================


def t_cscv() -> None:
    """用已知答案的模拟数据标定统计模块。

    为什么这一项不能省
    ------------------
    PBO 算错是**静默的**：公式里相对秩 ρ 的方向搞反之后，程序照跑、
    数字照出，只是"真有优势的配置被判成 PBO=1.0、纯噪声反倒只有 0.88"。
    开发这一层时就真踩了这个坑，所以必须把"零假设下 PBO 应 ≈0.5、
    有真信号时应显著低于 0.5"钉成断言，否则下次重构还会再犯。
    """
    import numpy as np
    import pandas as pd
    from statistics import NormalDist

    from aq.backtest import cscv as C

    T, N, S = 1000, 4, 10
    rng = np.random.default_rng(99)

    def avg_pbo(nrep: int, drift: float = 0.0) -> float:
        out = []
        for _ in range(nrep):
            X = pd.DataFrame(rng.normal(0, 0.01, (T, N)))
            if drift:
                X[0] = X[0] + drift
            out.append(C.probability_of_backtest_overfitting(
                X, n_subperiods=S)["pbo"])
        return float(np.mean(out))

    # (a) 零假设：PBO 是对**数据生成过程**的期望 0.5，单次数据集会有很大
    #     离散，所以必须在多个随机样本上平均后断言
    p0 = avg_pbo(30)
    assert 0.35 <= p0 <= 0.65, f"零假设下 PBO 应≈0.5，实际 {p0:.3f}"

    # (b) 有真信号：一个配置持续跑赢 → 挑参应当可复现 → PBO 显著低
    p1 = avg_pbo(30, drift=0.0012)
    assert p1 < 0.25, f"有真信号时 PBO 应显著低于 0.5，实际 {p1:.3f}"

    # (c) DSR：N=1（不做多重检验校正）时必须退化成 Φ(t)
    r = rng.normal(0.0009, 0.01, T)
    d1 = C.deflated_sharpe(r, n_trials=1)
    want = NormalDist().cdf(d1["t_stat"])
    assert abs(d1["deflated_sharpe"] - want) < 1e-6, (
        f"N=1 时 DSR 应等于 Φ(t)：{d1['deflated_sharpe']} vs {want}")
    assert d1["deflated_sharpe"] > 0.95, "t=4.7 的强信号在 N=1 下必须显著"

    # (d) 多重检验门槛 SR0 随试错次数单调上升
    prev = -1.0
    for n in (2, 5, 20, 100, 500):
        cur = C.deflated_sharpe(r, n_trials=n)["sr_threshold"]
        assert cur > prev, f"SR0 应随 N 单调增：N={n} 时 {cur} <= {prev}"
        prev = cur

    # (e) 有效独立试验数：完全相关 → 1；独立 → ≈N
    X = pd.DataFrame(rng.normal(0, 0.01, (T, 4)))
    X[1] = X[0]
    X[2] = X[0]
    X[3] = X[0]
    ne = C.effective_trials(X)["n_eff"]
    assert abs(ne - 1.0) < 0.05, f"完全相关时 N_eff 应≈1，实际 {ne}"
    ne2 = C.effective_trials(pd.DataFrame(rng.normal(0, 0.01, (T, 4))))["n_eff"]
    assert 3.0 <= ne2 <= 5.0, f"独立时 N_eff 应≈4，实际 {ne2}"


# ==========================================================================
# 10. Walk-forward 定权的**前视偏差**检验
# ==========================================================================


def t_wf_lookahead() -> None:
    """用合成 IC 时序验证 walk-forward 切片不含前视。

    为什么这一项不能省
    ------------------
    前视是**静默**类错误：切片位置错了程序照样跑完、照样出夏普，只是这个
    夏普不是样本外的。而 7.13 / 7.14 的全部结论都建立在"喂给 CSCV 的日收益
    是样本外的"这个前提上 —— 这个前提只由 walkforward.py 里几行切片保证，
    所以必须把断言钉住。

    检验体在 ``scripts/wf_lookahead_check.py``（可单独运行，含
    "扰动未来数据"这一因果层黄金标准），这里只做断言汇总，避免
    同一套逻辑写两份、然后悄悄分叉。
    """
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "scripts" / "wf_lookahead_check.py"
    assert p.exists(), f"缺少前视检验脚本 {p}"
    spec = importlib.util.spec_from_file_location("_wf_lookahead_check", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    assert mod.check_lag_boundary(), "公式层失败：refit 用到了 d-(horizon+lag) 之后的 IC"
    assert mod.check_causal_perturbation(), "因果层失败：扰动未来数据改变了历史权重"
    assert mod.check_refit_cadence(), "节奏层失败：refit 时点未单调推进或间隔不足"


# ==========================================================================
# 11. 探针台账：登记 ↔ 磁盘的一致性，**含负对照**
# ==========================================================================


def t_probe_registry() -> None:
    """探针台账必须与磁盘一致，且**漂移检测必须真的会报**。

    为什么这一项要带负对照
    ----------------------
    一个"永远返回无漂移"的检查器会安静地通过所有测试 —— 它什么都不检查，
    却给出最大的安慰。所以这里往台账里插一条**虚构的脚本名**，
    断言它必须出现在 ``missing`` 里；不出现就说明漂移比对根本没在工作。

    负对照只改**临时目录里的 README 副本**（并把模块级常量临时指过去），
    不触碰仓库里的任何真实文件。
    """
    import tempfile
    from pathlib import Path

    from aq.api import probes as P

    d = P.probe_registry()
    assert d["available"], f"台账不可用：{d.get('reason')}"

    # (a) 磁盘上都得有：清单有的、磁盘没有 = 文档在引用不存在的证据
    assert not d["missing"], f"清单有但磁盘无：{d['missing']}"
    # (b) 每条的规模都读得出来（说明按行位置解析没错位、文件真的可读）
    bad = [e["script"] for e in d["entries"] if not e["exists"] or e["n_lines"] <= 0]
    assert not bad, f"这些探针读不出内容：{bad}"
    # (c) 支撑最重结论的那几个必须在册 —— 台账漏掉它们等于漏掉项目的核心证据
    names = {e["script"] for e in d["entries"]}
    for must in (
        "probe_survivorship_corrected.py",
        "probe_divyield_factor_alpha.py",
        "probe_etf_rotation.py",
        "probe_limit_field_bias.py",
    ):
        assert must in names, f"关键探针未登记：{must}"
    # (d) 大多数条目要能点回文档。不要求 100%：有的探针本就不支撑文档结论
    #     （如 dump_etf_event.py 是调试用），要求全解析等于逼人去编引用。
    assert d["n_resolved_doc"] >= d["n_registered"] - 2, (
        f"可解析文档只有 {d['n_resolved_doc']}/{d['n_registered']}，"
        "文档解析多半退化了")
    # (e) 三份结构化小节都要解析出来（否则页面会静默少掉整块内容）
    assert len(d["conventions"]) >= 3, f"「三条约定」解析出 {len(d['conventions'])} 条"
    assert len(d["pitfalls"]) >= 3, f"「通用陷阱」解析出 {len(d['pitfalls'])} 条"
    assert d["prereq"].strip(), "「前置条件」命令块为空"

    # --- 负对照：插一条虚构脚本名，漂移必须被抓到 ---
    text = P.README.read_text(encoding="utf-8")
    bogus = "probe_negative_control_does_not_exist.py"
    assert "| `segment_consistency.py`" in text, "README 表头结构变了，负对照无法注入"
    text2 = text.replace(
        "| `segment_consistency.py`",
        f"| `{bogus}` | 负对照，不应存在 | — | — |\n| `segment_consistency.py`",
        1,
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "README.md"
        tmp.write_text(text2, encoding="utf-8")
        old = P.README
        P.README = tmp
        try:
            d2 = P.probe_registry()
        finally:
            P.README = old
    assert bogus in d2["missing"], (
        f"漂移检测没工作：虚构的 {bogus} 没被抓进 missing（missing={d2['missing']}）")
    assert bogus not in d2["unregistered"], "虚构条目不该出现在 unregistered（方向反了）"
    assert len(d2["entries"]) == len(d["entries"]) + 1, "注入的条目没被解析进 entries"

    print(f"        → 台账 {d['n_registered']} 条（磁盘 {d['n_on_disk']} 个 .py）"
          f"｜可点回文档 {d['n_resolved_doc']} 条｜漂移 {len(d['missing'])}/{len(d['unregistered'])}"
          f"｜负对照已捕获")


def t_archive_caliber() -> None:
    """研究产物归档：口径判定必须抓得住「不可引用」，且与 README 人工表零漂移。

    这一项为什么存在：归档页要回答「我以前跑出来的数字还能不能引用」。
    判定可能的两个错向，**危险的是把不可引用判成可引用** ——
    读者会去引用一个口径已失效的数字（本项目的 t 口径 bug 就是这么传出去的）。
    所以负对照准备的不是一个坏输入，而是**五类**：
    缺字段（旧 raw）、字段只在一半配置里（口径不一致）、没有 configs（判定不了）、
    json 读不出来、以及权重基准目录已消失（不可复现）。五类都必须落到"不可引用"。

    只有一类输入该被判可引用（口径齐备 + 权重基准还在），用来验"它没把什么都判死"——
    一个永远返回 False 的判定器和永远返回 True 的一样没用。
    """
    import json as _json
    import tempfile
    from pathlib import Path

    from aq.api import archive as A

    with tempfile.TemporaryDirectory() as td:
        rt = Path(td)
        (rt / "cscv").mkdir()
        (rt / "factor_research").mkdir()

        def wcfg(name: str, configs: list, **extra) -> None:
            doc = {
                "universe": "liquid",
                "period": {"start": "2019-01-01", "end": "2026-08-31"},
                "configs": configs,
                "verdict": {"pbo": 0.2, "t_stat": 0.9, "dsr": 0.3, "significant": False},
                "pbo_main": {"pbo": 0.2, "haircut": 0.5},
                "dsr_main": {"deflated_sharpe": 0.3},
            }
            doc.update(extra)
            (rt / "cscv" / name).write_text(_json.dumps(doc), encoding="utf-8")

        GOOD = {"label": "a", "score_neutralize": True}
        CAL = "raw_minus_benchmark_no_rf"

        wcfg("cscv_old_raw.json", [{"label": "a"}, {"label": "b"}])
        wcfg("cscv_fixed.json", [GOOD], excess_caliber=CAL)
        wcfg("cscv_partial.json", [GOOD, {"label": "b"}], excess_caliber=CAL)
        wcfg("cscv_no_configs.json", [], excess_caliber=CAL)
        (rt / "cscv" / "cscv_broken.json").write_text("{ not json", encoding="utf-8")
        wcfg(
            "cscv_orphan_basis.json",
            [GOOD],
            excess_caliber=CAL,
            weight_basis="runtime/factor_research/does_not_exist",
        )

        for name, meta in (
            ("neu_run", {"neutralized": True, "universe": "liquid", "n_factors": 31}),
            ("raw_run", {"neutralized": False, "universe": "liquid", "n_factors": 27}),
        ):
            d0 = rt / "factor_research" / name
            d0.mkdir()
            (d0 / "meta.json").write_text(_json.dumps(meta), encoding="utf-8")
        (rt / "factor_research" / "no_meta_run").mkdir()

        d = A.archive_index(root=rt)
        assert d["available"], "临时目录里明明有产物，available 不该是 False"
        got = {e["name"]: e for e in d["cscv"]}

        # 五类坏输入 → 一律不可引用，且必须给出原因
        bad_ones = (
            "cscv_old_raw",
            "cscv_partial",
            "cscv_no_configs",
            "cscv_broken",
            "cscv_orphan_basis",
        )
        for name in bad_ones:
            assert got[name]["quotable"] is False, f"{name} 应判不可引用，却判成可引用"
            assert got[name]["blockers"], f"{name} 判了不可引用却没给原因（读者无法行动）"
        # 唯一的好输入必须活着 —— 否则判定器就是"一律判死"
        assert got["cscv_fixed"]["quotable"] is True, "口径齐备的产物被判成不可引用"
        assert not got["cscv_fixed"]["blockers"]

        # 中间态要各自报出来，不能一律归成 raw
        assert got["cscv_old_raw"]["caliber"]["neutralize"] == "missing"
        assert got["cscv_partial"]["caliber"]["neutralize"] == "partial"
        assert got["cscv_no_configs"]["caliber"]["neutralize"] == "unknown", (
            "没有 configs 时应如实说「判定不了」，而不是默认通过")
        assert got["cscv_broken"]["readable"] is False
        # 两个独立口径维度要分开报：缺 excess_caliber 与缺 score_neutralize 是两回事
        assert any("excess_caliber" in b for b in got["cscv_old_raw"]["blockers"])
        assert any("权重基准" in b for b in got["cscv_orphan_basis"]["blockers"])
        # DSR 必须真读到值（键名是 deflated_sharpe；取错键会静默变成 None）
        assert got["cscv_fixed"]["dsr_main"] is not None, "DSR 读成 None —— 多半是键名取错了"

        runs = {e["name"]: e for e in d["factor_runs"]}
        assert runs["neu_run"]["caliber"]["state"] == "neutralized"
        assert runs["raw_run"]["caliber"]["state"] == "raw"
        assert runs["no_meta_run"]["caliber"]["state"] == "unknown", "缺 meta 不能默认通过"

        # available 的语义：空目录 / 不存在的目录都要 False + 原因，不许返回空列表冒充正常
        empty = Path(td) / "empty_rt"
        empty.mkdir()
        d2 = A.archive_index(root=empty)
        assert d2["available"] is False and d2["unavailable_reason"].strip(), (
            "空 runtime 目录应 available=False 并说明原因")
        d3 = A.archive_index(root=Path(td) / "nope")
        assert d3["available"] is False and d3["unavailable_reason"].strip(), (
            "不存在的 runtime 目录应 available=False 并说明原因")

    # --- 真仓库：机械判定必须与 README 那张人工表逐行一致 ---
    real = A.archive_index()
    table = A.readme_quotable_table()
    assert table, "README「历史结果口径清点」表解析出 0 行 —— 表结构变了或解析退化"
    mism = [x for x in real["readme_drift"] if x["kind"] == "verdict_mismatch"]
    assert not mism, f"归档口径判定与 README 人工表不一致：{mism}"
    gone = [x for x in real["readme_drift"] if x["kind"] == "missing_on_disk"]
    assert not gone, f"README 表里列了、磁盘上却没有的产物：{gone}"

    print(
        f"        → 归档 {real['summary']['n_cscv']} 个 CSCV + {real['summary']['n_factor_runs']} 轮因子研究"
        f"｜可引用 {real['summary']['n_quotable']}｜README 表 {len(table)} 行·判定零漂移"
        f"｜负对照 5 类坏输入全部判为不可引用"
    )


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
    check("9. 防过拟合统计标定（PBO 零假设≈0.5 / DSR / N_eff）", t_cscv)
    check("10. Walk-forward 定权的前视偏差（公式/因果/节奏三层）", t_wf_lookahead)
    check("11. 探针台账一致性与漂移检测（含负对照）", t_probe_registry)
    check("12. 研究产物归档的口径判定（含 5 类负对照 + README 表零漂移）", t_archive_caliber)

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
