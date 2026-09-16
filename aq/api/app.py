"""FastAPI 应用。

接口契约（与前端 types/ 1:1 对齐）：

    GET  /api/health
    GET  /api/config
    GET  /api/market/overview
    GET  /api/stocks
    GET  /api/stock/{code}/detail
    GET  /api/stock/{code}/prediction
    GET  /api/signals
    GET  /api/factor/analysis
    GET  /api/anomaly
    GET  /api/account
    POST /api/backtest
    GET  /api/backtest/{run_id}
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aq import __version__
from aq.backtest.engine import BacktestEngine
from aq.config.settings import PROJECT_ROOT, get_settings
from aq.core.models import Anomaly, BacktestResult, Prediction
from aq.data.provider import get_provider
from aq.factors.library import FactorLibrary
from aq.factors.scoring import build_scorer

app = FastAPI(
    title="A 股 AI 量化交易系统 API",
    version=__version__,
    description="仅供技术研究与学习，不构成任何投资建议。",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 本地开发；生产请收敛为前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- 进程内缓存
_CACHE: dict[str, Any] = {}
_RESULTS: dict[str, BacktestResult] = {}


def _cfg():  # type: ignore[no-untyped-def]
    if "cfg" not in _CACHE:
        _CACHE["cfg"] = get_settings()
    return _CACHE["cfg"]


def _provider():  # type: ignore[no-untyped-def]
    if "provider" not in _CACHE:
        _CACHE["provider"] = get_provider(_cfg())
    return _CACHE["provider"]


# ---------------------------------------------------------------- 基础
@app.get("/api/health")
def health() -> dict:
    cfg = _cfg()
    return {
        "status": "ok",
        "version": __version__,
        "mode": cfg.mode.value,
        "broker": cfg.execution.broker,
        "data_source": cfg.data.source,
        "time": datetime.now().isoformat(),
    }


@app.get("/api/config")
def config() -> dict:
    return _cfg().model_dump(mode="json")


# ---------------------------------------------------------------- 市场
@app.get("/api/market/overview")
def market_overview() -> dict:
    """市场总览：涨跌家数、指数、Top 榜。"""
    cfg = _cfg()
    provider = _provider()
    stocks = provider.get_stock_list()[:30]

    quotes = []
    up = down = 0
    for s in stocks:
        bars = provider.get_daily(s["symbol"], cfg.backtest.start, cfg.backtest.end)
        if not bars:
            continue
        last = bars[-1]
        pct = (last.close / last.pre_close - 1) if last.pre_close else 0.0
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        quotes.append(
            {
                "symbol": s["symbol"],
                "name": s["name"],
                "close": last.close,
                "pct_chg": round(pct, 4),
                "amount": last.amount,
            }
        )

    return {
        "up_count": up,
        "down_count": down,
        "flat_count": max(0, len(quotes) - up - down),
        "quotes": quotes,
        "indices": [
            {"symbol": "000300", "name": "沪深300", "pct_chg": 0.0},
            {"symbol": "000001", "name": "上证指数", "pct_chg": 0.0},
            {"symbol": "399006", "name": "创业板指", "pct_chg": 0.0},
        ],
        "north_bound": 0.0,
    }


# ---------------------------------------------------------------- 个股
@app.get("/api/stocks")
def stocks() -> list[dict]:
    return _provider().get_stock_list()


def _score_all() -> list[Prediction]:
    """对全池打分（带进程内缓存）。"""
    key = "predictions"
    if key in _CACHE:
        return _CACHE[key]

    cfg = _cfg()
    provider = _provider()
    lib = FactorLibrary()
    scorer = build_scorer(cfg, lib)

    factor_map: dict[str, dict[str, float | None]] = {}
    names: dict[str, str] = {}
    for s in provider.get_stock_list()[:30]:
        bars = provider.get_daily(s["symbol"], cfg.backtest.start, cfg.backtest.end)
        if len(bars) < 60:
            continue
        factor_map[s["symbol"]] = lib.compute(s["symbol"], bars)
        names[s["symbol"]] = s["name"]

    preds = scorer.score_cross_section(factor_map)
    for p in preds:
        p.explain = names.get(p.symbol, p.symbol) + " | " + p.explain
    _CACHE[key] = preds
    _CACHE["names"] = names
    return preds


@app.get("/api/stock/{code}/detail")
def stock_detail(code: str, limit: int = Query(250, ge=30, le=2000)) -> dict:
    cfg = _cfg()
    provider = _provider()
    bars = provider.get_daily(code, cfg.backtest.start, cfg.backtest.end)
    if not bars:
        raise HTTPException(404, f"未找到 {code} 的行情数据")

    names = _CACHE.get("names", {})
    lib = FactorLibrary()
    factors = lib.compute(code, bars)

    return {
        "symbol": code,
        "name": names.get(code, code),
        "bars": [
            {
                "time": b.time.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "amount": b.amount,
            }
            for b in bars[-limit:]
        ],
        "factors": factors,
    }


@app.get("/api/stock/{code}/prediction")
def stock_prediction(code: str) -> dict:
    preds = _score_all()
    for p in preds:
        if p.symbol == code:
            return p.model_dump(mode="json")
    raise HTTPException(404, f"未找到 {code} 的预测结果")


# ---------------------------------------------------------------- 信号
@app.get("/api/signals")
def signals(min_confidence: float = 0.0, top: int = 20) -> list[dict]:
    """交易信号列表。

    注意：``min_confidence`` 默认 0.0（不过滤）。
    在股票池较小（如 mock 的 12 只）时，横截面一致性指标天然偏低，
    过高阈值会把所有信号滤掉。真实全市场（3000+ 只）可用 0.5 左右。
    """
    preds = _score_all()[: top * 2]
    out = []
    for p in preds:
        if p.direction == "HOLD" or p.confidence < min_confidence:
            continue
        top_factor = max(p.factor_contrib, key=lambda c: c.contrib).name if p.factor_contrib else ""
        out.append(
            {
                "id": f"{p.symbol}-{p.model_version}",
                "symbol": p.symbol,
                "side": p.direction,
                "strength": p.score,
                "confidence": p.confidence,
                "trigger_factor": top_factor,
                "ts": datetime.now().isoformat(),
                "source": "model",
            }
        )
        if len(out) >= top:
            break
    return out


FACTOR_DIR = PROJECT_ROOT / "runtime" / "factor_research"


def _latest_factor_dir() -> "Path | None":
    """最新的因子研究结果目录。"""
    if not FACTOR_DIR.exists():
        return None
    dirs = [d for d in FACTOR_DIR.iterdir() if d.is_dir() and (d / "summary.csv").exists()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


@app.get("/api/factor/analysis")
def factor_analysis() -> dict:
    """因子概览：优先返回真实 IC 检验结果，无结果时退化为因子清单。"""
    lib = FactorLibrary()
    base = {
        n: {
            "name": n,
            "group": lib.group(n),
            "desc": lib.desc(n),
            "direction": lib.direction(n),
            "ic": None,
            "rank_ic": None,
            "rank_icir": None,
            "icir": None,
            "rank_ic_t": None,
            "rank_ic_p": None,
            "q_ls": None,
            "q_mono": None,
            "turnover": None,
            "significant": False,
        }
        for n in lib.names
    }

    d = _latest_factor_dir()
    if d is None:
        return {
            "factors": list(base.values()),
            "meta": {},
            "note": "尚无因子检验结果。运行 `python scripts/factor_research.py` 生成。",
        }

    try:
        import pandas as pd

        summary = pd.read_csv(d / "summary.csv")
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
        name_col = "因子" if "因子" in summary.columns else "factor"
        for _, row in summary.iterrows():
            n = str(row[name_col]).replace("_neu", "")
            if n not in base:
                continue
            f = base[n]
            f["ic"] = _f(row.get("ic"))
            f["rank_ic"] = _f(row.get("rank_ic"))
            f["rank_icir"] = _f(row.get("rank_icir"))
            f["icir"] = _f(row.get("icir"))
            f["rank_ic_t"] = _f(row.get("rank_ic_t"))
            f["rank_ic_p"] = _f(row.get("rank_ic_p"))
            f["q_ls"] = _f(row.get("q_ls"))
            f["q_mono"] = _f(row.get("q_mono"))
            f["turnover"] = _f(row.get("turnover"))
            f["direction"] = int(row.get("direction", f["direction"]) or f["direction"])
            p = row.get("rank_ic_p")
            ric = row.get("rank_ic")
            f["significant"] = bool(
                p is not None and ric is not None
                and float(p) < 0.05 and abs(float(ric)) > 0.02
            )
        return {
            "factors": list(base.values()),
            "meta": meta,
            "generated_at": datetime.fromtimestamp(d.stat().st_mtime).isoformat(timespec="seconds"),
            "note": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"factors": list(base.values()), "meta": {},
                "note": f"读取因子检验结果失败：{type(exc).__name__} {exc}"}


@app.get("/api/factor/ic_ts")
def factor_ic_ts(factors: str = Query("", description="逗号分隔，空=全部")) -> dict:
    """逐日 IC 时序（前端画 IC 曲线 / 衰减用）。"""
    d = _latest_factor_dir()
    if d is None or not (d / "ic_ts.parquet").exists():
        return {"dates": [], "series": {}, "note": "暂无数据"}
    import pandas as pd

    df = pd.read_parquet(d / "ic_ts.parquet")
    want = [f.strip() for f in factors.split(",") if f.strip()]
    series: dict[str, list[float | None]] = {}
    for c in df.columns:
        if not c.endswith("__rankic"):
            continue
        nm = c.replace("__rankic", "")
        if want and nm not in want:
            continue
        series[nm] = [None if pd.isna(v) else round(float(v), 6) for v in df[c]]
    return {"dates": [str(x) for x in df.index], "series": series}


@app.get("/api/factor/corr")
def factor_corr() -> dict:
    """因子相关性矩阵（前端热力图）。"""
    d = _latest_factor_dir()
    if d is None or not (d / "corr.csv").exists():
        return {"labels": [], "matrix": [], "note": "暂无数据"}
    import pandas as pd

    df = pd.read_csv(d / "corr.csv", index_col=0)
    return {
        "labels": [str(c) for c in df.columns],
        "matrix": [[None if pd.isna(v) else round(float(v), 4) for v in row]
                   for row in df.values],
    }


def _f(v) -> "float | None":
    """转 float；NaN / None / 不可转换 -> None。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, 6)


# ---------------------------------------------------------------- 异常
@app.get("/api/anomaly")
def anomaly() -> list[Anomaly]:
    """量价异动检测（复用彩票项目的 z-score 思路）。"""
    cfg = _cfg()
    provider = _provider()
    out: list[Anomaly] = []
    for s in provider.get_stock_list()[:30]:
        bars = provider.get_daily(s["symbol"], cfg.backtest.start, cfg.backtest.end)
        if len(bars) < 30:
            continue
        rets = [bars[i].close / bars[i - 1].close - 1 for i in range(1, len(bars))]
        window = rets[-60:-1]
        if len(window) < 20:
            continue
        mu = sum(window) / len(window)
        sd = (sum((r - mu) ** 2 for r in window) / len(window)) ** 0.5 or 1e-6
        z = (rets[-1] - mu) / sd
        if abs(z) >= 2.0:
            severity = "severe" if abs(z) >= 3 else "warn"
            out.append(
                Anomaly(
                    time=bars[-1].time,
                    symbol=s["symbol"],
                    type="价格异动",
                    z_score=round(z, 2),
                    severity=severity,  # type: ignore[arg-type]
                    detail=f"{s['name']} 当日收益 {rets[-1]:.2%}，z={z:.2f}",
                )
            )
    out.sort(key=lambda a: abs(a.z_score), reverse=True)
    return out[:20]


# ---------------------------------------------------------------- 账户
@app.get("/api/account")
def account() -> dict:
    from aq.execution.gateway import create_gateway

    cfg = _cfg()
    from aq.core.models import RunMode

    if cfg.mode == RunMode.LIVE:
        # 实盘模式下才创建实盘网关；当前会抛 NotImplementedAdapter
        gw = create_gateway(cfg.execution.broker, cfg)
    else:
        gw = create_gateway("sim", cfg)
    acct = gw.get_account()
    return {
        "account_id": acct.account_id,
        "cash": acct.cash,
        "market_value": acct.market_value,
        "total_asset": acct.total_asset,
        "realized_pnl": acct.realized_pnl,
        "unrealized_pnl": acct.unrealized_pnl,
        "positions": [p.model_dump(mode="json") for p in acct.positions.values()],
    }


# ---------------------------------------------------------------- 回测
class BacktestRequest(BaseModel):
    start: str | None = None
    end: str | None = None
    top_k: int | None = None
    initial_cash: float | None = None
    mode: str | None = None


@app.post("/api/backtest")
def run_backtest_api(req: BacktestRequest) -> dict:
    from aq.config.settings import load_settings

    overrides: dict[str, Any] = {}
    if req.top_k:
        overrides["model"] = {"top_k": req.top_k}
    if req.initial_cash:
        overrides["backtest"] = {"initial_cash": req.initial_cash}
    if req.start and req.end:
        overrides.setdefault("backtest", {})
        overrides["backtest"]["start"] = req.start
        overrides["backtest"]["end"] = req.end

    cfg = load_settings(mode=req.mode or "backtest", **overrides)
    result = BacktestEngine(cfg).run()
    _RESULTS[result.run_id] = result
    return {
        "run_id": result.run_id,
        "start": str(result.start),
        "end": str(result.end),
        "metrics": result.metrics.model_dump(mode="json"),
        "equity": [p.model_dump(mode="json") for p in result.equity],
        "drawdown": [p.model_dump(mode="json") for p in result.drawdown],
        "trade_count": len(result.trades),
        # 过程诊断（平均仓位/持仓数/拒单原因）。**必须随接口返回**：
        # 只看 metrics 无法判断回测是否真的按预期执行 —— 曾经有一条
        # 冒烟测试"通过"了很久，实际是 top_k=5 与单票上限 10% 冲突，
        # 890 笔订单全被拒、仓位恒为 0，而收益曲线看起来完全正常。
        "diagnostics": result.diagnostics,
    }


@app.get("/api/backtest/{run_id}")
def get_backtest(run_id: str) -> dict:
    r = _RESULTS.get(run_id)
    if r is None:
        raise HTTPException(404, f"未找到回测记录 {run_id}")
    return r.model_dump(mode="json")


# ---------------------------------------------------------------- 静态前端
def mount_frontend() -> None:
    """若前端已构建（web/dist），挂载为静态站点。"""
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    from aq.config.settings import PROJECT_ROOT

    dist = PROJECT_ROOT / "web" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")


mount_frontend()
