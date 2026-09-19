"""FastAPI 应用。

接口契约（与前端 ``web/src/types/`` 对齐）：

    GET  /api/health                 系统状态（数据截至日 / 池规模 / 代码版本身份）
    GET  /api/config
    GET  /api/market/overview        全市场总览（真实涨跌家数/分布/三榜/指数）
    GET  /api/market/indices         指数概览（含 60 日 sparkline，缺数据显式标 False）
    POST /api/market/snapshot/rebuild  强制重建行情快照（约 20~30s）
    GET  /api/quotes                 全市场行情表（分页/搜索/板块/池过滤）
    GET  /api/stocks                 全市场元数据（5562 条，重；一般用 /api/quotes）
    GET  /api/stock/{code}/detail    个股日线（**前复权**，价格可读）
    GET  /api/stock/{code}/prediction 单只打分（对样本截面做横截面 z 标准化）
    GET  /api/signals                因子打分榜（研究产物，**未被证实有预测力**）
    GET  /api/factor/analysis|ic_ts|corr
    GET  /api/anomaly                全市场价格异动（z-score）
    GET  /api/account
    POST /api/backtest  /  GET /api/backtest/{run_id}
    GET  /api/project/status         研究进展 / 数据资产 / 已封存结论
    GET  /api/probes                 探针台账（结论 ↔ 探针脚本 ↔ 文档，含台账漂移）
    GET  /api/archive                研究产物归档（哪些跑过的结果还能引用，含 README 表漂移）

「数据真实性」三条硬规则（改动本文件时请遵守）
------------------------------------------------
1. 任何对外数字都必须来自本地落盘数据（``data_cache/``）或真实计算结果；
   **不允许硬编码占位值**（旧实现曾把三个指数涨跌幅写死 0.0，前端永远显示 0.00%）。
2. 价格必须区分口径：``data_cache/bars/`` 的 OHLC 是**后复权价**，
   展示前必须换算（现价 = close / adj_factor；K 线用前复权）。
3. 拿不到数据时**显式报缺**（``available: false`` / 空列表 + 原因），
   不要返回 0 / 假值让前端看起来"有数据"。
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aq import __version__
from aq.backtest.engine import BacktestEngine
from aq.config.settings import PROJECT_ROOT, get_settings
from aq.core.models import BacktestResult, Prediction
from aq.data.provider import get_provider
from aq.data.snapshot import MarketSnapshot, index_overview
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

#: 打分样本上限。全池 5065 只逐个算因子要几分钟，前端点一下就会超时；
#: 故按**成交额**取前 N 只（不是按代码序 —— 按代码序会引入系统性偏置）。
#: 实测单只因子计算约 42ms，N=400 首次约 17s，之后走缓存。
SIGNAL_SAMPLE_MAX = 400
#: 打分结果缓存存活秒数（样本内因子值基本不变，只是重算太贵）
SIGNAL_TTL = 900

#: 打分结果必须随附的声明。放在这里是为了让"分数"和"分数的局限"
#: 永远一起出现 —— 分开写迟早会有一处忘记带上。
CAVEAT_SCORE = (
    "多因子加权在本项目研究中已被证伪：IC 加权相对等权无统计优势"
    "（PBO 0.170 / best_t 0.993），且收益几乎全部来自小市值暴露"
    "（beta_size t=13，alpha t=0.55）。本分数为研究中间产物，不构成选股建议。"
)


def _cfg():  # type: ignore[no-untyped-def]
    if "cfg" not in _CACHE:
        _CACHE["cfg"] = get_settings()
    return _CACHE["cfg"]


def _provider():  # type: ignore[no-untyped-def]
    if "provider" not in _CACHE:
        _CACHE["provider"] = get_provider(_cfg())
    return _CACHE["provider"]


def _snapshot() -> MarketSnapshot:
    if "snapshot" not in _CACHE:
        _CACHE["snapshot"] = MarketSnapshot()
    return _CACHE["snapshot"]


def _mt(path: Path) -> str:
    """文件修改时间（秒级 ISO 字符串）；不存在返回空串（不抛异常）。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _disk_fingerprint() -> dict:
    """磁盘**当前**状态：后端源码最新修改时间 + 前端构建产物。

    ⚠️ 这个值每次调用都重新读盘，所以它单独**不能**用来判断"进程跑的是不是旧代码"——
      必须与进程启动时的快照比对（见 ``_BOOT_FINGERPRINT``）。
      前端产物同理：``mount_frontend`` 是按请求从磁盘读文件的，重建前端**不需要重启**
      （index.html 会指向新的哈希文件）；只有后端源码改动才需要重启。
    """
    dist = PROJECT_ROOT / "web" / "dist" / "index.html"
    asset: str | None = None
    if dist.exists():
        text = dist.read_text(encoding="utf-8", errors="ignore")
        # 从 <script type="module" ... src="/assets/index-<hash>.js"> 取出带哈希的产物名
        for token in text.split('"'):
            if "/assets/" in token and token.endswith(".js"):
                asset = token.rsplit("/", 1)[-1]
                break
    # 后端指纹取整个 aq 包 python 文件的最新 mtime —— 只盯 app.py 会漏掉改 snapshot.py 的情况
    newest = 0.0
    for py in (PROJECT_ROOT / "aq").rglob("*.py"):
        try:
            newest = max(newest, py.stat().st_mtime)
        except OSError:
            pass
    try:
        if Path(__file__).stat().st_mtime > newest:
            newest = Path(__file__).stat().st_mtime
    except OSError:
        pass
    return {
        "backend_mtime": datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else "",
        "dist_asset": asset,
        "dist_index_mtime": _mt(dist),
        "dist_present": dist.exists(),
    }


#: 进程启动瞬间的磁盘快照。放模块级只算一次，用来回答
#: 「我这个进程加载的是哪个版本的代码」。端口上被**改代码之前**启动的旧进程占着，
#: 是"改动没生效"这类误判最常见的来源（本机实踩过），启动脚本据此决定是否重启。
_BOOT_FINGERPRINT: dict[str, Any] = dict(_disk_fingerprint())
_BOOT_FINGERPRINT["boot_time"] = datetime.now().isoformat(timespec="seconds")


def _code_identity() -> dict:
    """给启动脚本用的身份块：进程启动时的快照 + 磁盘现值 + 是否已落后。"""
    disk = _disk_fingerprint()
    stale = bool(_BOOT_FINGERPRINT.get("backend_mtime")) and (
        _BOOT_FINGERPRINT.get("backend_mtime") != disk.get("backend_mtime")
    )
    return {"boot": dict(_BOOT_FINGERPRINT), "disk": disk, "backend_stale": stale}


# ---------------------------------------------------------------- 基础
@app.get("/api/health")
def health() -> dict:
    cfg = _cfg()
    snap_meta: dict = {}
    try:
        _, snap_meta = _snapshot().load()
    except Exception as exc:  # noqa: BLE001 — 快照不可用不应让健康检查挂掉
        snap_meta = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok",
        "version": __version__,
        "mode": cfg.mode.value,
        "broker": cfg.execution.broker,
        "data_source": cfg.data.source,
        "time": datetime.now().isoformat(),
        # ↓ 前端头部显示"数据截至"用，替代原先空账户的 ¥0.00
        "data_asof": snap_meta.get("asof", ""),
        "n_active": snap_meta.get("n_active", 0),
        "n_liquid": snap_meta.get("n_liquid_snapshot", 0),
        "snapshot_built_at": snap_meta.get("built_at", ""),
        "snapshot_error": snap_meta.get("error", ""),
        # ↓ 供启动脚本判断"端口上跑的是不是最新代码"
        "code": _code_identity(),
    }


@app.get("/api/config")
def config() -> dict:
    return _cfg().model_dump(mode="json")


# ---------------------------------------------------------------- 市场
@app.get("/api/market/overview")
def market_overview(top: int = Query(15, ge=5, le=50)) -> dict:
    """市场总览：涨跌家数、涨跌分布、涨幅/跌幅/成交额三榜、指数概览。

    全部取自本地全池快照（默认 5548 只活跃股），**不再取前 30 只**。
    """
    return _snapshot().overview(top=top)


@app.get("/api/market/indices")
def market_indices() -> dict:
    """指数概览。未落盘的指数返回 ``available: false``（不伪造 0.00%）。"""
    idx = index_overview()
    return {
        "indices": idx,
        "n_available": sum(1 for i in idx if i.get("available")),
        "note": ("指数行情来自 data_cache/index_bars/（与个股物理隔离，避免代码撞车）。"
                 "缺失项运行 python scripts/fetch_index_bars.py 补齐。"),
    }


@app.post("/api/market/snapshot/rebuild")
def rebuild_snapshot() -> dict:
    """强制重建行情快照（全池扫描，约 20~30 秒）。"""
    t0 = time.time()
    try:
        snap = _snapshot().build(verbose=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"重建失败：{type(exc).__name__} {exc}") from exc
    _, meta = _snapshot().load()
    return {"ok": True, "seconds": round(time.time() - t0, 1),
            "n_symbols": int(len(snap)), "asof": meta.get("asof")}


@app.get("/api/quotes")
def quotes(
    keyword: str = Query("", description="代码或名称关键词"),
    board: str = Query("", description="MAIN/CHINEXT/STAR/BSE，空=全部"),
    only_liquid: bool = Query(False, description="只看流动性池"),
    min_pct: float | None = Query(None, description="涨跌幅下限（小数，0.05=+5%）"),
    max_pct: float | None = Query(None, description="涨跌幅上限（小数，左闭右开）"),
    sort: str = Query("amount", description="amount|pct_chg|symbol|close"),
    order: str = Query("desc"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """全市场行情表。替换旧实现「取元数据前 30 只」。"""
    return _snapshot().quotes(
        keyword=keyword, board=board, only_liquid=only_liquid,
        min_pct=min_pct, max_pct=max_pct, sort=sort, order=order,
        limit=limit, offset=offset,
    )


# ---------------------------------------------------------------- 个股
@app.get("/api/stocks")
def stocks() -> list[dict]:
    """全市场元数据（5562 条）。前端列表请改用 /api/quotes（带行情且可分页）。"""
    return _provider().get_stock_list()


def _name_of(code: str) -> str:
    """取股票名称。以前这里读 ``_CACHE["names"]``，但那个缓存只有调用过
    打分接口后才有值 —— 直接打开个股页会显示代码当名称。现在统一走快照。"""
    try:
        snap, _ = _snapshot().load()
        hit = snap.loc[snap["symbol"].astype(str) == str(code), "name"]
        if len(hit):
            return str(hit.iloc[0])
    except Exception:  # noqa: BLE001
        pass
    return str(code)


@app.get("/api/stock/{code}/detail")
def stock_detail(code: str, limit: int = Query(250, ge=30, le=2000)) -> dict:
    """个股日线。

    ⚠️ 价格口径：``bars/`` 存的是**后复权价**。这里统一除以**最后一日的
    复权因子**（即"前复权到最新日"），于是：
      * 最右端 == 真实成交价（平安银行 11.82 而不是 1781）；
      * 历史段连续，除权跳空被正确消除，区间收益可直接由首尾相除得到。
    响应里显式带 ``price_caliber`` 说明，前端在图上标注。
    """
    cfg = _cfg()
    provider = _provider()
    bars = provider.get_daily(code, cfg.backtest.start, cfg.backtest.end)
    if not bars:
        raise HTTPException(404, f"未找到 {code} 的行情数据")

    scale = float(getattr(bars[-1], "adj_factor", 1.0) or 1.0)
    lib = FactorLibrary()
    factors = lib.compute(code, bars)

    out_bars = [
        {
            "time": b.time.isoformat(),
            "open": round(b.open / scale, 4),
            "high": round(b.high / scale, 4),
            "low": round(b.low / scale, 4),
            "close": round(b.close / scale, 4),
            "volume": b.volume,
            "amount": b.amount,
        }
        for b in bars[-limit:]
    ]
    last = bars[-1]
    return {
        "symbol": code,
        "name": _name_of(code),
        "bars": out_bars,
        "factors": factors,
        "price_caliber": "前复权（基准=最后交易日），最右端即真实成交价",
        "adj_factor_last": scale,
        "last_close_adj": round(last.close, 4),
        "last_close_raw": round(last.close / scale, 4),
        "first_date": bars[0].time.date().isoformat(),
        "last_date": last.time.date().isoformat(),
    }


# ---------------------------------------------------------------- 打分 / 信号
def _score_all() -> dict:
    """对**流动性池按成交额前 N** 只打分（带 TTL 缓存）。

    历史遗留：这里以前是 ``get_stock_list()[:30]``，即"按代码序前 30 只"。
    那既不是市场也不是任何有意义的样本，且只有 30 只时横截面 z 分数噪声极大。

    ⚠️ 研究结论提示（务必随结果一起展示，不要单独把分数当推荐）：
    本项目的多因子加权在研究中**已被证伪** —— IC 加权相对等权无统计优势
    （PBO 0.170 / best_t 0.993），且收益几乎全部来自小市值暴露
    （beta_size t=13，alpha t=0.55）。所以这里的分数是**研究中间产物**。

    返回值里带 ``raw_factor_map``（原始因子值），供单只打分扩截面复用 ——
    **不能**用 ``Prediction.factor_contrib[].value`` 代替它：那是压缩到 [0,1]
    的定向得分，拿它当原始值做横截面标准化会得到完全错误的分位。
    """
    key = "scores"
    now = time.time()
    if key in _CACHE and now - _CACHE.get("scores_at", 0) < SIGNAL_TTL:
        return _CACHE[key]

    cfg = _cfg()
    provider = _provider()
    snap, meta = _snapshot().load()
    lib = FactorLibrary()
    scorer = build_scorer(cfg, lib)
    pool = snap[snap["liquid_snapshot"]].nlargest(SIGNAL_SAMPLE_MAX, "amount_ma20")
    symbols = pool["symbol"].astype(str).tolist()

    factor_map: dict[str, dict[str, float | None]] = {}
    for s in symbols:
        bars = provider.get_daily(s, cfg.backtest.start, cfg.backtest.end)
        if len(bars) < 60:
            continue
        factor_map[s] = lib.compute(s, bars)

    preds = scorer.score_cross_section(factor_map)
    payload = {
        "asof": meta.get("asof", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_total": meta.get("n_liquid_snapshot", 0),
        "sample_size": len(factor_map),
        "sample_caliber": f"流动性池按 20 日均成交额取前 {SIGNAL_SAMPLE_MAX} 只",
        "weight_source": getattr(scorer, "ic_source", "prior"),
        "n_active_factors": len(scorer.active_factors()),
        "caveat": CAVEAT_SCORE,
        "predictions": preds,
        "raw_factor_map": factor_map,
        "scorer": scorer,
    }
    _CACHE[key] = payload
    _CACHE["scores_at"] = now
    return payload


@app.get("/api/stock/{code}/prediction")
def stock_prediction(code: str) -> dict:
    """单只打分（对**同一横截面口径**的样本做 z 标准化）。

    与旧实现的区别：以前只能对"样本内 30 只"返回结果，其余一律 404。
    现在对**任意**有行情的股票都能算：把该股原始因子值并入样本原始因子面板，
    重新跑一次 ``score_cross_section``，再取该股。N=400 的截面多 1 只，
    z 分数变化可忽略，而**口径完全一致**（这正是旧写法缺失的）。
    """
    cfg = _cfg()
    provider = _provider()
    scored = _score_all()

    for p in scored["predictions"]:
        if p.symbol == code:
            return {**p.model_dump(mode="json"), "in_sample": True,
                    "name": _name_of(code),
                    "sample_size": scored["sample_size"],
                    "sample_caliber": scored["sample_caliber"],
                    "caveat": scored["caveat"], "weight_source": scored["weight_source"]}

    bars = provider.get_daily(code, cfg.backtest.start, cfg.backtest.end)
    if len(bars) < 60:
        raise HTTPException(404, f"{code} 行情不足 60 根，无法计算因子")

    lib = FactorLibrary()
    fvals = lib.compute(code, bars)
    merged = dict(scored["raw_factor_map"])
    merged[code] = fvals
    preds = scored["scorer"].score_cross_section(merged)
    for p in preds:
        if p.symbol == code:
            return {**p.model_dump(mode="json"), "in_sample": False,
                    "name": _name_of(code),
                    "sample_size": len(merged),
                    "sample_caliber": scored["sample_caliber"] + "（并入该股后重算截面）",
                    "caveat": scored["caveat"], "weight_source": scored["weight_source"]}
    raise HTTPException(404, f"{code} 打分失败（因子全缺失）")


@app.get("/api/signals")
def signals(top: int = 20, min_confidence: float = 0.0) -> dict:
    """因子打分榜（研究产物）。"""
    scored = _score_all()
    preds: list[Prediction] = scored["predictions"]
    out = []
    for p in preds:
        if p.direction == "HOLD" or p.confidence < min_confidence:
            continue
        top_factor = (max(p.factor_contrib, key=lambda c: c.contrib).name
                      if p.factor_contrib else "")
        out.append(
            {
                "id": f"{p.symbol}-{p.model_version}",
                "symbol": p.symbol,
                "name": _name_of(p.symbol),
                "side": p.direction,
                "strength": p.score,
                "confidence": p.confidence,
                "trigger_factor": top_factor,
                "ts": scored["generated_at"],
                "source": "factor-score",
                "in_liquid_pool": True,
            }
        )
        if len(out) >= top:
            break
    return {
        "asof": scored["asof"],
        "generated_at": scored["generated_at"],
        "universe_total": scored["universe_total"],
        "sample_size": scored["sample_size"],
        "sample_caliber": scored["sample_caliber"],
        "weight_source": scored["weight_source"],
        "n_active_factors": scored["n_active_factors"],
        "caveat": scored["caveat"],
        "signals": out,
        "buy_count": sum(1 for p in preds if p.direction == "BUY"),
        "sell_count": sum(1 for p in preds if p.direction == "SELL"),
        "hold_count": sum(1 for p in preds if p.direction == "HOLD"),
    }


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
            "run_id": d.name,
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
def anomaly(top: int = Query(30, ge=1, le=200),
            min_abs_z: float = Query(2.5, ge=0.5, le=10.0)) -> dict:
    """全市场价格异动（滚动 60 日 z-score，覆盖全池）。

    旧实现只扫前 30 只股票；现在覆盖全部活跃股（5548 只），且 z 值在快照
    构建时一并算好（不额外读盘）。**这是纯量价统计，不含预测含义。**
    """
    sn = _snapshot()
    items = sn.anomalies(top=top, min_abs_z=min_abs_z)
    _, meta = sn.load()
    return {
        "asof": meta.get("asof", ""),
        "n_scanned": meta.get("n_active", 0),
        "min_abs_z": min_abs_z,
        "items": items,
        "caveat": ("滚动 60 日 z-score 量价统计，衡量「当日收益偏离自身近期分布的程度」，"
                   "与基本面无关，也不构成买卖建议。"),
    }


# ---------------------------------------------------------------- 账户
@app.get("/api/account")
def account() -> dict:
    from aq.core.models import RunMode
    from aq.execution.gateway import create_gateway

    cfg = _cfg()
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
        # 账户尚未投入资金的真实状态（前端据此显示"未初始化"而不是 ¥0.00 的假象）
        "is_funded": bool(acct.cash or acct.market_value or acct.positions),
    }


# ---------------------------------------------------------------- 回测
class BacktestRequest(BaseModel):
    start: str | None = None
    end: str | None = None
    top_k: int | None = None
    initial_cash: float | None = None
    mode: str | None = None
    # 股票池规模上限。None = 沿用配置（生产默认 0，即不截断）。
    # 存在的理由：全市场流动性池有 5000+ 只候选，建一次因子面板要几分钟，
    # 前端点一次"跑回测"就卡死。给调用方一个显式截断的口子，
    # 用于快速预览 / 自检。（⚠️ 截断按代码序，有系统性偏置，不能用于结论）
    max_symbols: int | None = None


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
    if req.max_symbols:
        overrides["universe"] = {"max_symbols": req.max_symbols}

    cfg = load_settings(mode=req.mode or "backtest", **overrides)
    t0 = time.time()
    result = BacktestEngine(cfg).run()
    elapsed = time.time() - t0
    _RESULTS[result.run_id] = result
    return {
        "run_id": result.run_id,
        "start": str(result.start),
        "end": str(result.end),
        "metrics": result.metrics.model_dump(mode="json"),
        "equity": [p.model_dump(mode="json") for p in result.equity],
        "drawdown": [p.model_dump(mode="json") for p in result.drawdown],
        "trade_count": len(result.trades),
        "elapsed_seconds": round(elapsed, 1),
        "max_symbols": getattr(getattr(cfg, "universe", None), "max_symbols", 0),
        # 过程诊断（平均仓位/持仓数/拒单原因）。**必须随接口返回**：
        # 只看 metrics 无法判断回测是否真的按预期执行 —— 曾经有一条
        # 冒烟测试"通过"了很久，实际是 top_k=5 与单票上限 10% 冲突，
        # 890 笔订单全被拒、仓位恒为 0，而收益曲线看起来完全正常。
        "diagnostics": result.diagnostics,
        "caveat": ("历史回测不代表未来表现。本项目多因子路线在研究中已被证伪"
                   "（见研究进展页），此处结果仅用于验证回测引擎本身。"),
    }


@app.get("/api/backtest/{run_id}")
def get_backtest(run_id: str) -> dict:
    r = _RESULTS.get(run_id)
    if r is None:
        raise HTTPException(404, f"未找到回测记录 {run_id}")
    return r.model_dump(mode="json")


# ---------------------------------------------------------------- 项目状态
@app.get("/api/project/status")
def project_status_api() -> dict:
    """研究进展 / 数据资产 / 已封存结论 / 已知缺口。"""
    from aq.api.project_status import project_status

    try:
        return project_status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"装配项目状态失败：{type(exc).__name__} {exc}") from exc


# ---------------------------------------------------------------- 探针台账
@app.get("/api/probes")
def probes_api() -> dict:
    """可复现证据台账：每条结论由哪个探针产生、怎么跑、出自哪份文档。

    登记信息读 ``scripts/probes/README.md``（人工维护），
    文件的存在性 / 行数 / mtime 实时扫盘，并把两者的偏差报出来 ——
    台账是不是过期，只有交叉校对才看得出来。
    """
    from aq.api.probes import probe_registry

    try:
        return probe_registry()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"装配探针台账失败：{type(exc).__name__} {exc}") from exc


# ---------------------------------------------------------------- 研究产物归档
@app.get("/api/archive")
def archive_api() -> dict:
    """跑过的实验产物，以及**哪些数字还能引用**。

    判定规则不是在这里写的，它写在 ``README.md`` 第 5 节「历史结果口径清点」：
    ``configs[].score_neutralize`` 字段不存在 ⇒ raw 口径（旧）；没有 ``excess_caliber``
    ⇒ 超额类数字是「双扣 rf」修正之前的。本接口把这两条机械执行，并把结果与
    README 那张人工表逐行比对，不一致就报 ``readme_drift``。

    ⚠️ 数据来自 ``runtime/``，该目录被 gitignore ⇒ 干净克隆下必然为空，
    此时返回 ``available=false`` 并说明原因，**不返回空列表冒充"没有产物"**。
    """
    from aq.api.archive import archive_index

    try:
        return archive_index()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"装配研究产物归档失败：{type(exc).__name__} {exc}") from exc


# ---------------------------------------------------------------- 静态前端
def mount_frontend() -> None:
    """若前端已构建（web/dist），由 API 直接提供整个界面。

    为什么不用 ``app.mount("/", StaticFiles(html=True))`` 了事：
    前端路由是 BrowserRouter（真实路径 ``/status``、``/stock/600519``），
    StaticFiles 只对**目录**回退到 index.html，对这类路径会直接 404 ——
    表现就是「在页面里点进研究进展正常，但一刷新或收藏该链接就白屏 404」。
    所以改成：/assets 走静态文件，其余路径有文件就给文件（favicon 等），
    没有就交回 index.html 让前端路由处理（SPA fallback）。

    ⚠️ 本函数必须在**所有 /api 路由注册之后**调用，否则兜底路由会把
    未知的 /api/* 请求也变成 index.html。这里对 api/ 前缀显式回 404。
    """
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = PROJECT_ROOT / "web" / "dist"
    if not dist.exists():
        return

    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):  # type: ignore[no-untyped-def]
        if full_path.startswith("api/"):
            raise HTTPException(404, f"未知接口 /{full_path}")
        cand = dist / full_path
        if full_path and cand.is_file():
            return FileResponse(cand)
        return FileResponse(dist / "index.html")


mount_frontend()
