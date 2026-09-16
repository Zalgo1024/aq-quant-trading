"""回测引擎 —— 与模拟盘/实盘共用同一事件循环与撮合内核。

事件循环（每个交易日）：

    1. 开盘前：解开 T+1 冻结、按前收盘 mark-to-market、检查止损；
    2. 用截至昨日的 bar 计算因子 → 打分 → 组合构建 → 生成订单；
    3. **用今日 bar 撮合昨日生成的订单**（避免未来函数：下单次日成交）；
    4. 收盘：按今日收盘价 mark-to-market，记录权益快照。

关键点：订单在 t 日生成，用 t+1 日的 bar 撮合 —— 这是杜绝"未来函数"的最小代价方案。
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pandas as pd

from aq.backtest.metrics import compute_metrics
from aq.core.models import (
    Account,
    BacktestResult,
    Bar,
    EquityPoint,
    Order,
    OrderType,
    Side,
)
from aq.data.index_store import IndexBarStore, IndexDataMissing
from aq.data.provider import DataProvider, get_provider
from aq.data.universe import UniverseSelector
from aq.execution.sim_gateway import SimGateway
from aq.factors.library import FactorLibrary
from aq.factors.scoring import build_scorer
from aq.portfolio.construct import TopKPortfolio
from aq.portfolio.risk import RiskEngine, RiskLimit


class BacktestEngine:
    def __init__(self, cfg) -> None:  # type: ignore[no-untyped-def]
        self.cfg = cfg
        self.provider: DataProvider = get_provider(cfg)
        self.gateway = SimGateway()
        self.gateway.connect(cfg)
        self.gateway.set_slippage(cfg.backtest.cost.slippage)

        limit = RiskLimit.from_config(cfg)
        # 行业映射（用于行业上限）
        stocks = self.provider.get_stock_list()
        limit.industry_map = {s["symbol"]: (s.get("industry") or "unknown") for s in stocks}
        limit.blacklist = {s["symbol"] for s in stocks if s.get("is_st")}
        self.risk = RiskEngine(limit)

        self.library = FactorLibrary()
        # P2：权重来源由 model.weight_source 决定（prior / ic）
        self.scorer = build_scorer(cfg, self.library)
        self.portfolio = TopKPortfolio(
            top_k=cfg.model.top_k,
            min_score=cfg.model.score_threshold,
        )

        self.symbols: list[str] = []
        self._bars: dict[str, dict[str, Bar]] = {}   # symbol -> date -> Bar
        self._last_predictions = []
        self._rejected_count = 0
        # P2：预计算的因子矩阵 {date: DataFrame(index=symbol, columns=因子)}
        # 用它替代"每天对每只股票重算一遍因子"——后者在因子数从 10 增到 27 后
        # 复杂度爆炸（回测从秒级掉到十几分钟）。
        self._panel: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------ 准备
    def _prepare(self) -> list[date]:
        """按 universe 配置选出股票池，并预加载行情。

        注意：原先这里取的是 ``stocks[: top_k * 3]`` —— 即「按代码排序的前 60 只」，
        既不是沪深 300 也没排除 ST/次新股，组合毫无代表性。现改为走
        ``UniverseSelector``，配置里的 ``universe.*`` 才真正生效。
        """
        stocks = self.provider.get_stock_list()

        selector = UniverseSelector(self.cfg)
        try:
            self.symbols = selector.select(asof=self.cfg.backtest.start)
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 股票池筛选失败（{exc}），退化为前 N 只")
            self.symbols = [s["symbol"] for s in stocks][: self.cfg.model.top_k * 3]

        if not self.symbols:
            # 兜底：本地有行情的全部
            self.symbols = [s["symbol"] for s in stocks][: self.cfg.model.top_k * 3]

        # 行业 / 黑名单映射（风控用）
        meta = {s["symbol"]: s for s in stocks}
        self.risk.limit.industry_map = {
            s: (meta.get(s, {}).get("industry") or "unknown") for s in self.symbols
        }
        self.risk.limit.blacklist = {
            s for s in self.symbols if meta.get(s, {}).get("is_st")
        }

        # 流通股本：换手率因子需要（缺股本时该因子返回 None，不影响其它因子）
        self.library.float_shares = {
            s: (meta.get(s, {}).get("float_share") or meta.get(s, {}).get("total_share") or 0.0)
            for s in self.symbols
        }

        print(f"股票池：{len(self.symbols)} 只（universe.index="
              f"{getattr(self.cfg.universe, 'index', 'all')}）")

        all_dates: set[date] = set()
        for sym in self.symbols:
            bars = self.provider.get_daily(sym, self.cfg.backtest.start, self.cfg.backtest.end)
            self._bars[sym] = {b.time.strftime("%Y-%m-%d"): b for b in bars}
            all_dates.update(b.time.date() for b in bars)

        self._build_factor_panel()
        return sorted(all_dates)

    # ------------------------------------------------------------- 因子面板
    def _build_factor_panel(self) -> None:
        """一次性把回测区间内所有股票的因子算好，按日期切片缓存。

        走 :mod:`aq.factors.panel` 的向量化内核，与因子研究用的是同一套算法，
        因此"研究里有效的因子"和"回测里用的因子"不可能对不上。
        失败时静默降级为逐日标量计算（慢但不会阻塞流程）。
        """
        from aq.factors.panel import FactorPanelBuilder, PanelConfig

        dates = sorted({d for m in self._bars.values() for d in m})
        if not dates:
            return
        try:
            pcfg = PanelConfig(
                start=dates[0],
                end=dates[-1],
                universe="all",
                factors=list(self.library.names),
                symbols=list(self.symbols),
            )
            panel = FactorPanelBuilder(pcfg).build()
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 因子面板构建失败（{type(exc).__name__} {exc}），"
                  f"退化为逐日标量计算（较慢）")
            return

        fac_cols = [c for c in self.library.names if c in panel.columns]
        panel = panel[["date", "symbol"] + fac_cols]
        self._panel = {d: g.set_index("symbol")[fac_cols]
                       for d, g in panel.groupby("date", sort=False)}
        print(f"因子面板：{len(panel):,} 行 / {len(self._panel)} 个交易日 / "
              f"{len(fac_cols)} 个因子")

    def _factors_at(self, ds: str) -> dict[str, dict[str, float | None]]:
        """取 ds 日（含）为止的因子横截面。"""
        if self._panel:
            sub = self._panel.get(ds)
            if sub is not None and not sub.empty:
                return {
                    sym: {c: (None if pd.isna(v) else float(v)) for c, v in row.items()}
                    for sym, row in sub.iterrows()
                }
            return {}

        # 降级路径：逐只股票用标量因子库现算
        factor_map: dict[str, dict[str, float | None]] = {}
        for sym in self.symbols:
            series = self._bars.get(sym, {})
            bars = [b for k, b in sorted(series.items()) if k <= ds]
            if len(bars) < 60:
                continue
            factor_map[sym] = self.library.compute(sym, bars)
        return factor_map

    # ------------------------------------------------------------------ 主循环
    def run(self) -> BacktestResult:
        dates = self._prepare()
        if not dates:
            raise RuntimeError("没有可用于回测的数据，请检查 data 配置或 provider")

        self.gateway.reset(
            cash=self.cfg.backtest.initial_cash,
            account_id=self.cfg.execution.account_id,
        )
        self._rejected_count = 0

        equity_curve: list[EquityPoint] = []
        pending_orders: list[Order] = []
        all_fills = []

        for i, d in enumerate(dates):
            ds = d.isoformat()

            # ---------- 1) 开盘前 ----------
            self.gateway.on_new_day(ds)

            # ---------- 2) 撮合昨日订单（用今日 bar）----------
            if pending_orders:
                day_bars = {s: self._bars[s][ds] for s in self.symbols if ds in self._bars[s]}
                for o in pending_orders:
                    bar = day_bars.get(o.symbol)
                    if bar is None:
                        continue
                    # execute 接收订单号（oid），由网关从内部订单簿取回 Order
                    fill = self.gateway.execute(o.oid, bar, risk_engine=self.risk)
                    if fill.filled_qty > 0:
                        all_fills.append(fill)
                pending_orders = []

            # ---------- 3) 收盘后：mark-to-market ----------
            day_bars = {s: self._bars[s][ds] for s in self.symbols if ds in self._bars[s]}
            self.gateway.mark_to_market(day_bars)

            # ---------- 4) 基于"截至今日"的数据生成明日订单 ----------
            if i + 1 < len(dates):
                tomorrow_orders: list[Order] = []

                # 止损检查（生成卖出单）
                for sym, reason in self.risk.check_stop_loss(self.gateway.account):
                    pos = self.gateway.account.positions.get(sym)
                    if pos and pos.available >= 100:
                        tomorrow_orders.append(
                            Order(
                                oid="",
                                symbol=sym,
                                side=Side.SELL,
                                qty=int(pos.available // 100) * 100,
                                type=OrderType.MARKET,
                                strategy_id="stop_loss",
                                account_id=self.gateway.account.account_id,
                            )
                        )

                # 因子打分 → 组合 → 订单
                preds = self._score_at(ds)
                self._last_predictions = preds
                prices = {s: b.close for s, b in day_bars.items()}
                orders = self.portfolio.construct(preds, self.gateway.account, prices)

                # 组合级风控
                approved, rejected = self.risk.portfolio_check(
                    orders, self.gateway.account, day_bars
                )
                self._rejected_count += len(rejected)
                tomorrow_orders.extend(approved)

                # 提交：**必须收集 submit_order 的返回值**，
                # 因为 oid 是在提交时才生成的；直接 append 原对象会拿到空 oid，
                # 导致次日撮合时报"未知订单"。
                for o in tomorrow_orders:
                    submitted = self.gateway.submit_order(o)
                    pending_orders.append(submitted)

            # ---------- 5) 记录权益 ----------
            acct = self.gateway.account
            equity_curve.append(
                EquityPoint(
                    time=datetime.combine(d, datetime.min.time()),
                    equity=round(acct.total_asset, 2),
                )
            )

        # ------------------------------------------------------------------ 收尾
        self.gateway.persist()

        # 基准对齐：把沪深300（或配置指定指数）的日收益按**同一交易日序列**
        # 对齐到策略权益曲线上，供 alpha/beta/信息比率计算使用。
        benchmark_rets, bench_note = self._benchmark_returns(equity_curve)
        if bench_note:
            print(f"[基准] {bench_note}")

        metrics = compute_metrics(
            equity_curve,
            initial_cash=self.cfg.backtest.initial_cash,
            fills=all_fills,
            benchmark_rets=benchmark_rets,
        )

        return BacktestResult(
            run_id=uuid4().hex[:12],
            start=dates[0],
            end=dates[-1],
            equity=equity_curve,
            drawdown=_drawdown_series(equity_curve),
            metrics=metrics,
            trades=all_fills,
            config=self.cfg.model_dump(mode="json"),
            created_at=datetime.now(),
        )

    # ------------------------------------------------------------------ 基准
    def _benchmark_returns(
        self, equity: list[EquityPoint]
    ) -> tuple[list[float] | None, str]:
        """取基准指数的日收益序列，按权益曲线的交易日逐日对齐。

        返回 ``(rets, note)``；``rets`` 为 ``None`` 表示无基准（指标不计算）。
        **对齐失败时返回 None 而非近似值** —— 错位的基准序列会算出
        看起来正常的假 alpha，比没有更危险。
        """
        code = getattr(self.cfg.backtest, "benchmark", "") or ""
        if not code:
            return None, "未配置 benchmark，alpha/beta/信息比率不计算"

        try:
            store = IndexBarStore()
            if not store.has(code):
                return None, (
                    f"基准 {code} 行情缺失（data_cache/index_bars/），"
                    f"alpha/beta/信息比率不计算；"
                    f"运行 python scripts/fetch_index_bars.py 可补齐"
                )
            idx = store.load(
                code,
                start=min(p.time for p in equity).date(),
                end=max(p.time for p in equity).date(),
            )
        except (IndexDataMissing, KeyError, ValueError) as exc:
            return None, f"基准 {code} 不可用（{exc}），alpha/beta/信息比率不计算"

        # 权益曲线的每一天 → 该日基准收盘点位
        by_date = {t.date(): float(c) for t, c in zip(idx["time"], idx["close"])}
        dates = [p.time.date() for p in equity]

        # 从第二天开始算收益（与 metrics 里 rets 的口径一致：n 个权益点 → n-1 个收益）
        rets: list[float] = []
        missing = 0
        for i in range(1, len(dates)):
            prev_c, cur_c = by_date.get(dates[i - 1]), by_date.get(dates[i])
            if prev_c is None or cur_c is None or prev_c <= 0:
                missing += 1
                continue
            rets.append(cur_c / prev_c - 1)

        if missing:
            return None, (
                f"基准 {code} 有 {missing} 个交易日缺数据，无法逐日对齐，"
                f"alpha/beta/信息比率不计算"
            )
        return rets, f"{code} {len(idx)} 行已对齐（{len(rets)} 个交易日）"

    @property
    def rejected_count(self) -> int:
        """风控拒单数（诊断用）。"""
        return self._rejected_count

    # ------------------------------------------------------------------ 打分
    def _score_at(self, ds: str):  # type: ignore[no-untyped-def]
        """用截至 ds（含）的因子横截面打分。"""
        return self.scorer.score_cross_section(self._factors_at(ds))

    @property
    def last_predictions(self):  # type: ignore[no-untyped-def]
        return self._last_predictions


# --------------------------------------------------------------------------
# 便捷函数
# --------------------------------------------------------------------------


def run_backtest(cfg) -> BacktestResult:  # type: ignore[no-untyped-def]
    return BacktestEngine(cfg).run()


def _drawdown_series(equity: list[EquityPoint]) -> list[EquityPoint]:
    out: list[EquityPoint] = []
    peak = equity[0].equity if equity else 0.0
    for p in equity:
        peak = max(peak, p.equity)
        dd = (p.equity - peak) / peak if peak else 0.0
        out.append(EquityPoint(time=p.time, equity=round(dd, 6)))
    return out
