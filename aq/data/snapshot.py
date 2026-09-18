"""全市场行情快照 + 指数概览（供 API / 前端使用）。

为什么必须单独一层
------------------
前端「市场总览」原先直接调 ``provider.get_stock_list()[:30]``，并把它返回的
``bars.close`` 当成「现价」展示。两个后果都很严重：

1. **样本不是市场**：``[:30]`` 是按代码序的前 30 只（清一色 000xxx 平安银行、
   万科A …），据此算出的「涨跌家数」与真实全市场无关。
2. **现价是后复权价**：``data_cache/bars/`` 的 ``close`` 是**后复权价**。
   平安银行的 ``close`` ≈ 1781，真实价 ≈ 11.8（= close / adj_factor）。
   直接展示会出现「平安银行 1781 元」这种一眼假的数字。

本模块一次性扫描全池，产出**最新交易日截面**并落盘缓存：
首次约几十秒，之后按 (文件数, 最新 mtime) 签名校验复用，毫秒级返回。

口径不变量（重要，改代码前先读）
--------------------------------
* ``bars/`` 的 ``open/high/low/close`` **全部是后复权价**；
  对外暴露的「现价」一律是 ``close / adj_factor``（见 :meth:`real_price`）。
* ``pct_chg`` 一律用 ``close / pre_close - 1``。分子分母同为后复权，
  **除权日不失真**，且这是唯一「不需要修 adj_factor 也对」的涨跌幅算法
  —— 不要改成 ``close / prev_close`` 再自行复权。
* 停牌股（最后一根 K 线早于市场最新交易日）**不进涨跌家数**，单独计数。
  否则一堆上月停牌的股票会被算成「平盘」，把平盘家数灌水。
* ⚠️ 「是否 ST」用的是 **stock_list.parquet 的当前名称快照**，不是逐日状态。
  这是既有已知偏差（见 ``docs/终端诊断`` 与 `UniverseSelector` 文档），
  故本模块产出的池标记为 ``liquid_snapshot``，**不能当作历史无偏口径**。
* ⚠️ 涨跌停家数是**近似值**（按收益幅度 |pct| ≥ 9.8% 统计），
  **故意不用** bars 的 ``limit_up/limit_down`` 字段 —— 该字段已被证明
  由「当前名称快照的 ST」推算，口径错误（错配率 10.59%，创业板 31.5%，
  见 ``scripts/probes/probe_limit_field_bias.py``）。宁可用近似口径并在
  前端标注「近似」，也不要引用一个已知错的字段。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from aq.config.settings import PROJECT_ROOT

#: 流动性池门槛（与 config/base.yaml 的 universe 节保持一致）
LIQUID_MIN_AMOUNT = 2.0e7       # 近 20 日日均成交额下限（元）
LIQUID_MIN_LIST_DAYS = 180      # 上市满 180 自然日
LOOKBACK_AMOUNT = 20            # 成交额均值回看窗口（交易日）
LOOKBACK_Z = 60                 # 异动 z-score 的滚动窗口（交易日）
#: 每只股票实际读取的尾部行数 = 上面两个窗口的较大者 + 1（最后一行是"当日"）
LOOKBACK_ROWS = max(LOOKBACK_AMOUNT, LOOKBACK_Z) + 1

#: 异动阈值（|z| ≥ 该值才输出）
ANOMALY_Z = 2.5

#: 近似涨跌停判定阈值（|pct_chg| ≥ 该值）。仅用于「家数」这种粗粒度展示。
LIMIT_APPROX = 0.098

#: 前端指数卡片的展示顺序与期望清单。
#: 刻意包含两个**本地尚未落盘**的宽基（上证指数 / 创业板指）—— 前端会显式
#: 显示「未落盘」，而不是伪造一个 0.00%（旧实现正是硬编码 0.0）。
INDEX_DISPLAY: list[tuple[str, str]] = [
    ("000001.SH", "上证指数"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("000016.SH", "上证50"),
    ("399006.SZ", "创业板指"),
]


def _default_bars_dir() -> Path:
    return PROJECT_ROOT / "data_cache" / "bars"


def _default_out_dir() -> Path:
    return PROJECT_ROOT / "runtime" / "web"


def _signature(bars_dir: Path, meta_path: Path) -> dict:
    """行情目录签名：文件数 + 最新 mtime（用于判断缓存是否过期）。

    为什么不用「最新交易日」判断：那需要逐个打开文件读日期（全池 ~12s），
    而 mtime 一次 stat 即可。任何增量拉取都会刷新 mtime，故足够可靠。
    """
    n = 0
    newest = 0.0
    if bars_dir.exists():
        with os.scandir(bars_dir) as it:
            for e in it:
                if not e.name.endswith(".parquet"):
                    continue
                n += 1
                m = e.stat().st_mtime
                if m > newest:
                    newest = m
    if meta_path.exists():
        newest = max(newest, meta_path.stat().st_mtime)
    return {"n_bars_files": n, "newest_mtime": round(newest, 3)}


def _read_tail(path: Path, n: int) -> tuple[pd.DataFrame, str] | None:
    """只读需要的那几列，返回 ``(尾部 n 行, 该股最早日期)``。

    列缺失时返回 None（跳过该股）。顺手带回首行的日期 —— ``time`` 列本来就
    整列读进来了，取首个元素是零成本，能省掉将来"再扫一遍全池求数据起点"。
    """
    import pyarrow.parquet as pq

    need = ["time", "close", "pre_close", "amount", "adj_factor"]
    try:
        tbl = pq.read_table(path, columns=need)
    except Exception:  # noqa: BLE001 — 少数字段不全的旧文件退化为全列读取
        try:
            tbl = pq.read_table(path)
            cols = [c for c in need if c in tbl.column_names]
            if "time" not in cols or "close" not in cols:
                return None
            tbl = tbl.select(cols)
        except Exception:  # noqa: BLE001
            return None
    df = tbl.to_pandas()
    if df.empty:
        return None
    t = pd.to_datetime(df["time"])
    first = t.min().date().isoformat()
    return df.tail(max(n, 2)), first


class MarketSnapshot:
    """全池最新交易日截面（带落盘缓存）。"""

    def __init__(
        self,
        bars_dir: str | Path | None = None,
        out_dir: str | Path | None = None,
    ) -> None:
        self.bars_dir = Path(bars_dir) if bars_dir else _default_bars_dir()
        self.out_dir = Path(out_dir) if out_dir else _default_out_dir()
        self.snap_path = self.out_dir / "market_snapshot.parquet"
        self.meta_path = self.out_dir / "market_snapshot.meta.json"
        self.stock_list_path = self.bars_dir.parent / "stock_list.parquet"

    # ---------------------------------------------------------------- 构建
    def is_fresh(self) -> bool:
        if not (self.snap_path.exists() and self.meta_path.exists()):
            return False
        try:
            old = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        sig = _signature(self.bars_dir, self.stock_list_path)
        if old.get("signature") != sig:
            return False
        # 快照本身必须比所有源文件新
        return self.snap_path.stat().st_mtime >= sig["newest_mtime"] - 1e-6

    def build(self, verbose: bool = True) -> pd.DataFrame:
        """扫描全池，产出截面并落盘。返回 DataFrame。"""
        rows: list[dict] = []
        files = sorted(self.bars_dir.glob("*.parquet"))
        for i, p in enumerate(files):
            got = _read_tail(p, LOOKBACK_ROWS)
            if got is None:
                continue
            tail, first_date = got
            d = tail.copy()
            d["time"] = pd.to_datetime(d["time"])
            d = d.sort_values("time")
            last = d.iloc[-1]
            adj = float(last.get("adj_factor") or 1.0) or 1.0
            close = float(last["close"])
            pre = float(last.get("pre_close") or 0.0)

            # ---- 异动 z 值：当日收益 -> 相对前 LOOKBACK_Z 日分布的标准化偏离 ----
            # 用 close 的比值（后复权同口径），不是 pre_close —— 后者只在"当日"有值。
            z = 0.0
            if len(d) > LOOKBACK_Z:
                c = d["close"].astype(float)
                rets = c.pct_change().dropna()
                win = rets.iloc[-(LOOKBACK_Z + 1):-1]     # 前 60 日，不含当日
                if len(win) >= 20:
                    mu = float(win.mean())
                    sd = float(win.std(ddof=1))
                    if sd > 1e-12:
                        z = (float(rets.iloc[-1]) - mu) / sd

            rows.append(
                {
                    "symbol": p.stem,
                    "last_date": last["time"].date().isoformat(),
                    "first_date": first_date,
                    "close_raw": close / adj,
                    "pct_chg": (close / pre - 1.0) if pre else 0.0,
                    "amount": float(last.get("amount") or 0.0),
                    "amount_ma20": float(d["amount"].tail(LOOKBACK_AMOUNT).mean())
                    if "amount" in d else 0.0,
                    "adj_factor": adj,
                    "z_score": round(z, 3),
                    "n_rows_seen": len(d),
                }
            )
            if verbose and (i + 1) % 1000 == 0:
                print(f"[快照] 已扫描 {i + 1}/{len(files)} ...", flush=True)

        if not rows:
            raise RuntimeError(f"未在 {self.bars_dir} 找到任何可用行情文件")

        snap = pd.DataFrame(rows)
        asof = snap["last_date"].max()

        # 合并元数据（名称/板块/行业/上市日/ST 快照）
        if self.stock_list_path.exists():
            meta = pd.read_parquet(self.stock_list_path)
            keep = [c for c in ("symbol", "name", "board", "industry", "industry_cs",
                                "list_date", "is_st") if c in meta.columns]
            meta = meta[keep].copy()
            meta["symbol"] = meta["symbol"].astype(str).str.zfill(6)
            snap = snap.merge(meta, on="symbol", how="left")
        else:
            for c in ("name", "board", "industry", "industry_cs", "list_date"):
                snap[c] = ""
            snap["is_st"] = False

        snap["name"] = snap["name"].fillna(snap["symbol"])
        snap["board"] = snap["board"].fillna("")

        # 是否在最新交易日有成交（停牌股不进涨跌家数）
        snap["is_active"] = snap["last_date"] == asof
        # 近似涨跌停（见模块 docstring：故意不用 limit_up/limit_down 字段）
        snap["at_limit_up"] = snap["pct_chg"] >= LIMIT_APPROX
        snap["at_limit_down"] = snap["pct_chg"] <= -LIMIT_APPROX

        # 主口径流动性池（snapshot 口径，含已知 ST 快照偏差）
        ld = pd.to_datetime(snap.get("list_date"), errors="coerce")
        asof_ts = pd.Timestamp(asof)
        listed_days = (asof_ts - ld).dt.days
        name = snap["name"].astype(str)
        snap["liquid_snapshot"] = (
            snap["is_active"]
            & (~snap["is_st"].fillna(False).astype(bool))
            & (~name.str.contains("ST|退", regex=True, na=False))
            & (listed_days.fillna(9999) >= LIQUID_MIN_LIST_DAYS)
            & (snap["amount_ma20"] >= LIQUID_MIN_AMOUNT)
        )

        meta_out = {
            "asof": asof,
            "bars_first_date": str(snap["first_date"].min()),
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "n_symbols": int(len(snap)),
            "n_active": int(snap["is_active"].sum()),
            "n_suspended": int((~snap["is_active"]).sum()),
            "n_liquid_snapshot": int(snap["liquid_snapshot"].sum()),
            "amount_lookback": LOOKBACK_AMOUNT,
            "liquid_min_amount": LIQUID_MIN_AMOUNT,
            "liquid_min_list_days": LIQUID_MIN_LIST_DAYS,
            "limit_approx_threshold": LIMIT_APPROX,
            "caliber_note": (
                "价格口径：现价=后复权价/复权因子；涨跌=close/pre_close-1（除权日不失真）；"
                "ST 过滤为当前名称快照口径（已知偏差）；涨跌停家数为幅度近似口径。"
            ),
            "signature": _signature(self.bars_dir, self.stock_list_path),
        }

        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.snap_path.with_suffix(".parquet.tmp")
        snap.to_parquet(tmp, index=False)
        os.replace(tmp, self.snap_path)
        self.meta_path.write_text(
            json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if verbose:
            print(f"[快照] 完成：{len(snap)} 只，asof={asof}，"
                  f"活跃 {meta_out['n_active']}，流动性池 {meta_out['n_liquid_snapshot']}",
                  flush=True)
        return snap

    # ---------------------------------------------------------------- 读取
    def load(self, rebuild: bool = False) -> tuple[pd.DataFrame, dict]:
        """返回 ``(截面, meta)``；缓存过期时自动重建。"""
        if rebuild or not self.is_fresh():
            snap = self.build()
            return snap, json.loads(self.meta_path.read_text(encoding="utf-8"))
        snap = pd.read_parquet(self.snap_path)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        return snap, meta

    # ---------------------------------------------------------------- 聚合
    def overview(self, top: int = 15) -> dict:
        """市场总览：涨跌家数、分布、三榜、指数。"""
        snap, meta = self.load()
        act = snap[snap["is_active"]]

        up = int((act["pct_chg"] > 0).sum())
        down = int((act["pct_chg"] < 0).sum())
        flat = int((act["pct_chg"] == 0).sum())

        # 涨跌分布（按 1% 一档，两端合并）。返回 lo/hi（**百分数**，None=开区间）
        # 供前端「点柱子 -> 下钻到该区间的股票」使用，避免前端去解析中文标签。
        edges = [-10, -9, -7, -5, -3, -1, 0, 1, 3, 5, 7, 9, 10]
        labels = ["<-9%", "-9~-7", "-7~-5", "-5~-3", "-3~-1", "-1~0",
                  "0~1", "1~3", "3~5", "5~7", "7~9", ">9%"]
        pct = act["pct_chg"] * 100.0
        dist = []
        for i, lab in enumerate(labels):
            lo, hi = edges[i], edges[i + 1]
            if i == 0:
                m = pct < hi
            elif i == len(labels) - 1:
                m = pct >= lo
            else:
                m = (pct >= lo) & (pct < hi)
            dist.append({
                "label": lab,
                "count": int(m.sum()),
                "lo": None if i == 0 else float(lo),
                "hi": None if i == len(labels) - 1 else float(hi),
            })

        def _rows(df: pd.DataFrame, cols: list[str]) -> list[dict]:
            return [
                {
                    "symbol": str(r["symbol"]),
                    "name": str(r["name"]),
                    "board": str(r["board"]),
                    "close": round(float(r["close_raw"]), 3),
                    "pct_chg": round(float(r["pct_chg"]), 4),
                    "amount": round(float(r["amount"]), 2),
                }
                for _, r in df.iterrows()
            ]

        cols = ["symbol", "name", "board", "close_raw", "pct_chg", "amount"]
        gainers = act.nlargest(top, "pct_chg")[cols]
        losers = act.nsmallest(top, "pct_chg")[cols]
        actives = act.nlargest(top, "amount")[cols]

        pct_series = act["pct_chg"]
        return {
            "asof": meta["asof"],
            "built_at": meta["built_at"],
            "universe": {
                "n_symbols": meta["n_symbols"],
                "n_active": meta["n_active"],
                "n_suspended": meta["n_suspended"],
                "n_liquid_snapshot": meta["n_liquid_snapshot"],
            },
            "breadth": {
                "up": up,
                "down": down,
                "flat": flat,
                "limit_up_approx": int(act["at_limit_up"].sum()),
                "limit_down_approx": int(act["at_limit_down"].sum()),
                "median_pct": round(float(pct_series.median()), 4),
                "mean_pct": round(float(pct_series.mean()), 4),
            },
            "distribution": dist,
            "top_gainers": _rows(gainers, cols),
            "top_losers": _rows(losers, cols),
            "top_amount": _rows(actives, cols),
            "indices": index_overview(),
            "caliber_note": meta["caliber_note"],
        }

    def anomalies(self, top: int = 30, min_abs_z: float = ANOMALY_Z) -> list[dict]:
        """全市场价格异动（z-score 口径，覆盖全池而非前 30 只）。

        z 值 = (当日收益 − 前 60 日均值) / 前 60 日标准差。停牌股不参与。
        这是**纯量价统计**，不含任何预测含义 —— 前端需明确标注。
        """
        snap, _ = self.load()
        act = snap[snap["is_active"]]
        hit = act[act["z_score"].abs() >= min_abs_z].copy()
        hit["abs_z"] = hit["z_score"].abs()
        hit = hit.nlargest(top, "abs_z")

        out: list[dict] = []
        for _, r in hit.iterrows():
            az = abs(float(r["z_score"]))
            sev = "severe" if az >= 4 else ("warn" if az >= 3 else "info")
            out.append(
                {
                    "time": str(r["last_date"]),
                    "symbol": str(r["symbol"]),
                    "name": str(r["name"]),
                    "board": str(r["board"]),
                    "type": "价格异动",
                    "z_score": float(r["z_score"]),
                    "pct_chg": round(float(r["pct_chg"]), 4),
                    "close": round(float(r["close_raw"]), 3),
                    "amount": round(float(r["amount"]), 2),
                    "severity": sev,
                    "detail": (f"{r['name']} 当日 {float(r['pct_chg']):+.2%}，"
                               f"偏离 60 日分布 {float(r['z_score']):+.2f}σ"),
                }
            )
        return out

    def quotes(
        self,
        keyword: str = "",
        board: str = "",
        only_liquid: bool = False,
        min_pct: float | None = None,
        max_pct: float | None = None,
        sort: str = "amount",
        order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """全市场行情表（分页 + 关键词 + 板块 + 涨跌幅区间 + 流动性池过滤）。

        这是替换旧实现 ``get_stock_list()[:30]`` 的入口：前端搜索框输入代码/名称，
        或按板块筛选，全部落在真实全池（默认 5548 只活跃）上。

        ``min_pct`` / ``max_pct`` 传**小数**（如 0.05 = +5%），左闭右开，
        用于「点涨跌分布柱子 -> 下钻到具体股票」的联动。

        ``sort`` 支持 ``amount`` / ``pct_chg`` / ``symbol`` / ``close``。
        """
        snap, meta = self.load()
        df = snap[snap["is_active"]].copy()
        if only_liquid:
            df = df[df["liquid_snapshot"]]
        if board:
            df = df[df["board"].astype(str).str.upper() == board.upper()]
        if min_pct is not None:
            df = df[df["pct_chg"] >= min_pct]
        if max_pct is not None:
            df = df[df["pct_chg"] < max_pct]
        kw = keyword.strip()
        if kw:
            mask = (df["symbol"].astype(str).str.contains(kw, regex=False)
                    | df["name"].astype(str).str.contains(kw, regex=False))
            df = df[mask]

        key = sort if sort in ("amount", "pct_chg", "symbol", "close_raw") else "amount"
        df = df.sort_values(key, ascending=(order == "asc"))
        total = int(len(df))
        page = df.iloc[offset: offset + limit]

        items = [
            {
                "symbol": str(r["symbol"]),
                "name": str(r["name"]),
                "board": str(r["board"]),
                "industry": str(r["industry"]) if pd.notna(r.get("industry")) else "",
                "close": round(float(r["close_raw"]), 3),
                "pct_chg": round(float(r["pct_chg"]), 4),
                "amount": round(float(r["amount"]), 2),
                "amount_ma20": round(float(r["amount_ma20"]), 2),
                "z_score": float(r["z_score"]),
                "in_liquid": bool(r["liquid_snapshot"]),
            }
            for _, r in page.iterrows()
        ]
        return {
            "asof": meta["asof"],
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
        }


def real_price(close_adj: float, adj_factor: float) -> float:
    """后复权价 -> 真实价。**前端展示价格前必须过这一步。**"""
    af = adj_factor or 1.0
    return close_adj / af


def index_overview(spark_len: int = 60) -> list[dict]:
    """指数概览（点位 / 当日涨跌 / 近 N 日 sparkline）。

    未落盘的指数返回 ``available: False``，**不伪造 0.00%**。
    """
    from aq.data.index_store import IndexBarStore

    store = IndexBarStore()
    out: list[dict] = []
    for code, name in INDEX_DISPLAY:
        item: dict = {"symbol": code, "name": name, "available": False}
        try:
            df = store.load(code)
        except Exception:  # noqa: BLE001 — IndexDataMissing 等
            out.append(item)
            continue
        if len(df) < 2:
            out.append(item)
            continue
        close = df["close"].astype(float)
        last, prev = float(close.iloc[-1]), float(close.iloc[-2])
        tail = close.tail(spark_len)
        base = float(tail.iloc[0]) or 1.0
        item.update(
            {
                "available": True,
                "close": round(last, 2),
                "pct_chg": round(last / prev - 1.0, 4) if prev else 0.0,
                "asof": pd.to_datetime(df["time"].iloc[-1]).date().isoformat(),
                # 给原始点位（不是归一化值）：前端既能画走势，也能显示真实量级。
                # 需要归一化时在前端做（除以 closes[0]），比在后端丢掉信息好。
                "closes": [round(float(v), 2) for v in tail],
                "spark_dates": [
                    pd.to_datetime(t).date().isoformat() for t in df["time"].tail(spark_len)
                ],
                "range_60d": round(last / base - 1.0, 4),
            }
        )
        out.append(item)
    return out
