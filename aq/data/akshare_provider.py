"""akshare 数据源实现（P1 生产路径）。

数据源选择（2026-09 实测）
--------------------------
主源为**新浪** ``stock_zh_a_daily``，实测下来最稳：

- 覆盖沪深主板 / 创业板 / 科创板 / 北交所，数据可到当日；
- ``volume`` 单位是**股**（东财 ``stock_zh_a_hist`` 是**手**，差 100 倍，曾踩坑）；
- 一次返回全量历史，请求数少、不易触发限流。

备用源：腾讯 ``stock_zh_a_hist_tx``（volume 单位也是股）。
东财 ``stock_zh_a_hist``（volume 单位是手）保留但在部分网络环境下
（如本机 127.0.0.1:17823 代理）会被 push2his.eastmoney.com 拒绝。

复权与真实价（本项目最关键的一条约定）
--------------------------------------
``Bar.open/high/low/close`` 存**后复权价**：除权日收益率连续，算因子/信号不会
出现假跳空。但回测撮合与资金结算**必须用真实价**——后复权价可能是真实价的
十几倍（浦发银行 2026 年后复权 ~160 元、真实价 ~9.2 元），用它算
「100 万本金能买多少手」会错得离谱，且最低 5 元佣金这类约束也会失真。

因此本 provider 同时拉取**后复权**与**不复权**两份序列，按日期对齐后算出

    adj_factor = close_hfq / close_raw      （只在除权日跳变的分段常数）

回填到 ``Bar.adj_factor``；回测侧用 ``bar.open_raw`` / ``bar.close_raw``
（= 后复权价 / adj_factor）拿到真实价做撮合与资金计算。

⚠️ 数据版权归各数据源所有，请遵守 akshare 与上游接口的使用条款，
   不要高频爬取，建议收盘后批量更新。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from aq.core.models import Bar
from aq.core.rules import limit_prices
from aq.data.adj import smooth_adj_factor
from aq.data.provider import DataProvider

# 各源 volume 的单位：True 表示「手」（需 ×100 转成股），False 表示「股」
_VOLUME_IN_LOTS = {"eastmoney": True, "sina": False, "tx": False}


class AkshareProvider(DataProvider):
    name = "akshare"

    def __init__(self, cfg=None, source: str = "sina") -> None:  # type: ignore[no-untyped-def]
        self.cfg = cfg
        self.source = source  # sina | tx | eastmoney
        cache_dir = getattr(getattr(cfg, "data", None), "cache_dir", "data_cache")
        self.cache_dir = Path(cache_dir)
        if not self.cache_dir.is_absolute():
            from aq.config.settings import PROJECT_ROOT

            self.cache_dir = PROJECT_ROOT / self.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._list_cache: list[dict] | None = None
        self._is_st_map: dict[str, bool] = {}

    # ------------------------------------------------------------------ 股票列表
    def get_stock_list(self) -> list[dict]:
        if self._list_cache is not None:
            return self._list_cache

        import akshare as ak

        df = ak.stock_info_a_code_name()
        out = []
        for _, r in df.iterrows():
            code = str(r["code"]).zfill(6)
            name = str(r["name"])
            out.append(
                {
                    "symbol": code,
                    "name": name,
                    "industry": "",
                    "board": _board_of(code),
                    "list_date": "",
                    "is_st": _is_st_name(name),
                }
            )
        self._list_cache = out
        self._is_st_map = {s["symbol"]: bool(s.get("is_st")) for s in out}
        return out

    def set_st_map(self, mapping: dict[str, bool]) -> None:
        """外部注入 ST 标记（批量拉取脚本已知名称时可用，省一次列表请求）。"""
        self._is_st_map.update(mapping)

    # ------------------------------------------------------------------ 日线
    def get_daily(self, symbol: str, start, end, adjust="post") -> list[Bar]:
        s = _fmt(start)
        e = _fmt(end)

        df_hfq = self._fetch(symbol, s, e, "hfq")
        if df_hfq is None or df_hfq.empty:
            return []

        # 不复权序列：用于还原真实价。失败则降级为 VWAP 近似。
        df_raw = None
        try:
            df_raw = self._fetch(symbol, s, e, "")
        except Exception:  # noqa: BLE001
            df_raw = None

        is_st = self._is_st_map.get(symbol, False)
        return self._to_bars(
            symbol,
            df_hfq,
            is_st=is_st,
            df_raw=df_raw,
            volume_in_lots=_VOLUME_IN_LOTS.get(self.source, False),
        )

    def _fetch(self, symbol: str, start: str, end: str, adj: str) -> pd.DataFrame:
        """拉取单只股票日线，按日期区间裁剪后返回。"""
        import akshare as ak

        if self.source == "sina":
            df = ak.stock_zh_a_daily(symbol=_prefixed(symbol), adjust=adj)
            if df is None or df.empty:
                return pd.DataFrame()
            df["date"] = pd.to_datetime(df["date"]).dt.date
            mask = (df["date"] >= _to_date(start)) & (df["date"] <= _to_date(end))
            return df.loc[mask].reset_index(drop=True)

        if self.source == "tx":
            df = ak.stock_zh_a_hist_tx(
                symbol=_prefixed(symbol),
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
                adjust=adj,
            )
            if df is None or df.empty:
                return pd.DataFrame()
            df["date"] = pd.to_datetime(df["date"]).dt.date
            return df.reset_index(drop=True)

        # eastmoney
        adj_map = {"hfq": "hfq", "qfq": "qfq", "": ""}
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust=adj_map.get(adj, "hfq"),
        )
        if df is None or df.empty:
            return pd.DataFrame()
        cn = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
              "最低": "low", "成交量": "volume", "成交额": "amount"}
        df = df.rename(columns=cn)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------ 转换
    @staticmethod
    def _to_bars(
        symbol: str,
        df: pd.DataFrame,
        is_st: bool = False,
        df_raw: pd.DataFrame | None = None,
        volume_in_lots: bool = False,
        fallback_vwap: bool = True,
    ) -> list[Bar]:
        # 各源列名统一成英文
        df = df.rename(
            columns={
                "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
                "收盘": "close", "成交量": "volume", "成交额": "amount",
            }
        )

        # 不复权收盘价（真实价），按日期建索引
        raw_close_by_date: dict[date, float] = {}
        if df_raw is not None and not df_raw.empty:
            dr = df_raw.rename(columns={"日期": "date", "收盘": "close"})
            if "date" in dr.columns and "close" in dr.columns:
                for _, rr in dr.iterrows():
                    c = _f(rr.get("close"))
                    if c and c > 0:
                        raw_close_by_date[_as_date(rr["date"])] = c

        bars: list[Bar] = []
        pre_close: float | None = None
        last_factor: float | None = None
        raw_factors: list[float] = []

        for _, r in df.iterrows():
            close = _f(r.get("close"))
            if close is None or close <= 0:
                continue

            volume = _f(r.get("volume")) or 0.0
            amount = _f(r.get("amount")) or 0.0
            if volume_in_lots:  # 东财的成交量是「手」
                volume = volume * 100.0

            d = _as_date(r["date"])

            # ---- 复权因子：优先用「后复权收盘 / 不复权收盘」（精确、分段常数）
            factor = None
            rc = raw_close_by_date.get(d)
            if rc and rc > 0:
                f = close / rc
                if 0.2 <= f <= 20000:
                    factor = f
            if factor is None and fallback_vwap and volume > 0 and amount > 0:
                # 降级：用当日成交均价 VWAP 近似真实价（有噪声，仅兜底）
                vwap = amount / volume
                if vwap > 0:
                    f = close / vwap
                    if 0.2 <= f <= 20000:
                        factor = f
            if factor is None:
                factor = last_factor if last_factor else 1.0
            # 复权因子应当是分段常数：未拿到新值时沿用上一个（除权才变）
            last_factor = factor
            raw_factors.append(factor)

            # ---- 涨跌停：必须在「真实价」尺度上四舍五入到分再换算回复权尺度。
            # 交易所是按真实价（如 11.42 元）算出涨停价 12.56 元再取整到分的；
            # 若直接在后复权价（11.42 × 135.66 = 1549.24）上乘 1.1 再取整，
            # 会与真实涨停价差出几毛钱，足以把「涨停」误判成「没涨停」。
            up = down = None
            if pre_close:
                pre_close_raw = pre_close / factor if factor else pre_close
                up_raw, down_raw = limit_prices(pre_close_raw, symbol, is_st=is_st)
                up = up_raw * (factor or 1.0)
                down = down_raw * (factor or 1.0)

            bars.append(
                Bar(
                    symbol=symbol,
                    time=datetime.combine(d, datetime.min.time()),
                    freq="1d",
                    open=_f(r.get("open")) or close,
                    high=_f(r.get("high")) or close,
                    low=_f(r.get("low")) or close,
                    close=close,
                    volume=volume,
                    amount=amount,
                    pre_close=pre_close,
                    limit_up=up,
                    limit_down=down,
                    is_trading=volume > 0,
                    adj_factor=round(factor, 6),
                )
            )
            pre_close = close

        # ---- 复权因子分段常数化 ----
        # 新浪的后复权价与真实价都只保留 2 位小数，对低价股来说"分"就是
        # 0.3% 的量化误差，会让 factor 每天抖动（实测 170 只股票跳变上百次）。
        # 这里做变点检测 + 段内取中位数，还原成真正的分段常数。
        if raw_factors:
            smoothed = smooth_adj_factor(raw_factors)
            for b, f_old, f_new in zip(bars, raw_factors, smoothed):
                if f_old and f_new and abs(f_new - f_old) > 1e-9:
                    # 涨跌停价在真实价尺度上本就是对的，只需按新因子换算回复权尺度
                    if b.limit_up is not None:
                        b.limit_up = b.limit_up / f_old * f_new
                    if b.limit_down is not None:
                        b.limit_down = b.limit_down / f_old * f_new
                b.adj_factor = round(f_new, 6)
        return bars


# ---------------------------------------------------------------------- 工具


def _board_of(code: str) -> str:
    if code.startswith("688"):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith(("8", "4")):
        return "BSE"
    return "MAIN"


def _is_st_name(name: str) -> bool:
    """ST / *ST / 退市整理期标识。"""
    n = name.upper().replace(" ", "")
    return n.startswith("ST") or n.startswith("*ST") or "退" in n


def _prefixed(code: str) -> str:
    """600000 -> sh600000；000001 -> sz000001；北交所 -> bj。

    注意 **920 代码段**：北交所 2024 年起启用 920xxx 新代码（原 8xxxxx / 4xxxxx），
    而 9 开头的沪市代码只有 B 股 900xxx。若按老规则把 920 归到 sh，
    新浪源会查不到，343 只北交所股票会全部拉取失败。
    """
    code = str(code).strip()
    if code.startswith("920"):
        return "bj" + code
    if code.startswith(("60", "68", "51", "11", "9")):
        return "sh" + code
    if code.startswith(("8", "4")):
        return "bj" + code
    return "sz" + code


def _f(v) -> float | None:  # type: ignore[no-untyped-def]
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_date(v) -> date:  # type: ignore[no-untyped-def]
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return pd.to_datetime(v).date()


def _to_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _fmt(v) -> str:  # type: ignore[no-untyped-def]
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]
