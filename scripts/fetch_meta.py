"""P1：基础元数据拉取 —— 交易日历 / 上市日期 / 股本 / 行业分类 / 指数成分。

``scripts/fetch_daily.py`` 只解决了「行情」，但要跑得像样还需要四块元数据：

1. **交易日历**：调仓日、N 日平移、停市判断（``aq/data/calendar.py`` 消费）
2. **上市日期**：次新股过滤（``universe.min_list_days``）目前因缺该字段而失效
3. **总股本 / 流通股本**：市值类因子与流动性风控需要
4. **行业分类**：``risk.industry_max``（单一行业 30% 上限）目前 industry 全为空，
   该风控形同虚设
5. **指数成分**：沪深 300 / 中证 500 / 上证 50，用于基准对齐与股票池筛选

数据源（均已实测可用，东财源在本机代理下被拒，故全部避开）：
- 交易日历：新浪 ``tool_trade_date_hist_sina``
- 深市：深交所 ``stock_info_sz_name_code``（含上市日期/股本/证监会行业）
- 沪市主板 / 科创板：上交所 ``stock_info_sh_name_code``
- 北交所：``stock_info_bj_name_code``（含股本/行业）
- 行业（全市场统一口径）：新浪 ``stock_sector_spot`` + ``stock_sector_detail``
- 指数成分：中证指数 ``index_stock_cons``

用法::

    python scripts/fetch_meta.py                    # 全量
    python scripts/fetch_meta.py --only calendar    # 只拉日历
    python scripts/fetch_meta.py --only stocks index
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from aq.config.settings import PROJECT_ROOT  # noqa: E402

CACHE = PROJECT_ROOT / "data_cache"
BARS = CACHE / "bars"

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _atomic_parquet(df: pd.DataFrame, path: Path) -> None:
    """原子写：先写 .tmp，再替换，避免半截文件被其他进程读到。

    注：不用 os.replace 以外的删除动作；tmp 清理走 best-effort。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    try:
        os.replace(tmp, path)
    except OSError:
        # 目标被占用（如 IDE 文件监听持句柄）时退化为原地覆盖
        import shutil
        shutil.copyfile(tmp, path)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _num(v) -> float:
    """把 '19,405,918,198' 这类带千分位的字符串转成 float。"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("%", "").strip()
    if not s or s in ("-", "--", "nan", "None"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _board_of(code: str) -> str:
    if code.startswith("688") or code.startswith("689"):
        return "STAR"
    if code.startswith(("300", "301")):
        return "CHINEXT"
    if code.startswith(("8", "4", "920")):
        return "BSE"
    return "MAIN"


# ==================================================================== 日历
def fetch_calendar() -> pd.DataFrame | None:
    import akshare as ak

    _log("拉取交易日历 ...")
    try:
        df = ak.tool_trade_date_hist_sina()
    except Exception as exc:  # noqa: BLE001
        _log(f"  [失败] 交易日历：{exc}")
        return None
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df = df.drop_duplicates(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    out = CACHE / "calendar.parquet"
    _atomic_parquet(df, out)
    _log(f"  交易日历 {len(df)} 天（{df['trade_date'].iloc[0]} ~ {df['trade_date'].iloc[-1]}）→ {out.name}")
    return df


# ==================================================================== 股票元数据
def _fetch_sz() -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_info_sz_name_code(symbol="A股列表")
    out = pd.DataFrame({
        "symbol": df["A股代码"].astype(str).str.zfill(6),
        "name": df["A股简称"].astype(str).str.replace(r"\s+", "", regex=True),
        "list_date": pd.to_datetime(df["A股上市日期"], errors="coerce").dt.date,
        "total_share": df["A股总股本"].map(_num),
        "float_share": df["A股流通股本"].map(_num),
        "industry_cs": df["所属行业"].astype(str).str.strip(),
    })
    _log(f"  深交所 {len(out)} 只")
    return out


def _fetch_sh(symbol: str, label: str) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_info_sh_name_code(symbol=symbol)
    out = pd.DataFrame({
        "symbol": df["证券代码"].astype(str).str.zfill(6),
        "name": df["证券简称"].astype(str).str.strip(),
        "list_date": pd.to_datetime(df["上市日期"], errors="coerce").dt.date,
    })
    out["total_share"] = 0.0
    out["float_share"] = 0.0
    out["industry_cs"] = ""
    _log(f"  上交所{label} {len(out)} 只")
    return out


def _fetch_bj() -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_info_bj_name_code()
    out = pd.DataFrame({
        "symbol": df["证券代码"].astype(str).str.zfill(6),
        "name": df["证券简称"].astype(str).str.strip(),
        "list_date": pd.to_datetime(df["上市日期"], errors="coerce").dt.date,
        "total_share": df["总股本"].map(_num),
        "float_share": df["流通股本"].map(_num),
        "industry_cs": df["所属行业"].astype(str).str.strip(),
    })
    _log(f"  北交所 {len(out)} 只")
    return out


def fetch_stocks() -> pd.DataFrame | None:
    _log("拉取全市场股票元数据（上市日期 / 股本 / 证监会行业）...")
    frames = []
    for fn, label in ((_fetch_sz, "深交所"), (lambda: _fetch_sh("主板A股", "主板"),
                                              "上交所主板"),
                      (lambda: _fetch_sh("科创板", "科创板"), "上交所科创板"),
                      (_fetch_bj, "北交所")):
        try:
            frames.append(fn())
        except Exception as exc:  # noqa: BLE001
            _log(f"  [失败] {label}：{type(exc).__name__} {exc}")
    if not frames:
        return None

    meta = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["symbol"], keep="first")

    # 与已有列表合并（保留已落盘标记、ST 标记等）
    base_path = CACHE / "stock_list.parquet"
    if base_path.exists():
        try:
            base = pd.read_parquet(base_path)
        except Exception:  # noqa: BLE001
            base = pd.DataFrame(columns=["symbol"])
    else:
        # 还没跑过 fetch_daily：退化成扫描 bars 目录
        base = pd.DataFrame({"symbol": sorted(p.stem for p in BARS.glob("*.parquet"))})

    for col in ("name", "list_date", "total_share", "float_share", "industry_cs"):
        if col not in base.columns:
            base[col] = "" if col in ("name", "industry_cs") else (None if col == "list_date" else 0.0)

    merged = base.merge(meta, on="symbol", how="left", suffixes=("", "_new"))

    def _is_blank(s: pd.Series) -> pd.Series:
        """空值判定：NaN 之外还要把 '' / '0' / '0.0' / 'NaT' 都算作空。

        踩过的坑：base 里原有的列可能是空字符串（''.notna() == True），
        只用 notna() 判断会导致新拉到的上市日期/股本永远填不进去。
        """
        return s.isna() | s.astype(str).isin(["", "0", "0.0", "nan", "None", "NaT", "NaN"])

    for col in ("name", "list_date", "total_share", "float_share", "industry_cs"):
        new = f"{col}_new"
        if new in merged.columns:
            merged[col] = merged[col].where(~_is_blank(merged[col]), merged[new])
            merged = merged.drop(columns=[new])

    # 上市日期统一成 YYYY-MM-DD 字符串（parquet 里存 date 对象易踩类型坑）
    merged["list_date"] = pd.to_datetime(merged["list_date"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")

    merged["board"] = merged["symbol"].map(_board_of)
    if "is_st" in merged.columns:
        merged["is_st"] = merged["is_st"].fillna(False).astype(bool)
    else:
        merged["is_st"] = merged.get("name", pd.Series(dtype=str)).astype(str).str.contains(
            "ST|退", na=False)

    # 已落盘标记：回测只应对**确实有行情**的股票建模。
    #
    # ⚠️ 只查 `exists()` 是不够的：抓取失败会留下 0 行的 parquet，
    # 文件在、数据无。历史上这个字段因此虚高/虚低过（北交所修复后
    # 数据已补齐，但旧清单没更新，显示 344 只"无数据"而实际都有）。
    # 这里改为**同时校验行数**，让标记与真实可用数据一致。
    def _has_bars(sym: str) -> bool:
        p = BARS / f"{sym}.parquet"
        if not p.exists():
            return False
        try:
            import pyarrow.parquet as pq

            return int(pq.ParquetFile(p).metadata.num_rows) > 0
        except Exception:  # noqa: BLE001
            return False

    merged["has_data"] = merged["symbol"].map(_has_bars)

    _atomic_parquet(merged, base_path)
    _log(f"  合并后 {len(merged)} 只 → {base_path.name} "
         f"（有上市日期 {merged['list_date'].notna().sum()}，"
         f"有行业 {int((merged['industry_cs'].astype(str).str.len() > 0).sum())}，"
         f"已落盘 {int(merged['has_data'].sum())}）")
    return merged


# ==================================================================== 行业分类
def _fetch_sw(level: int, workers: int) -> pd.DataFrame | None:
    """申万行业成分。level=1 → 31 个一级行业；level=3 → 335 个三级行业。

    实测 ``ak.sw_index_third_cons`` 对一级行业代码同样有效（返回列会变成
    「申万1级」），因此无需再走「三级→二级→一级」的层级回溯，
    31 次请求即可拿到全市场的一级行业映射。
    """
    import akshare as ak

    if level == 1:
        info = ak.sw_index_first_info()
    else:
        info = ak.sw_index_third_info()
    pairs = list(zip(info["行业代码"].astype(str), info["行业名称"].astype(str)))
    _log(f"  申万{level}级行业 {len(pairs)} 个，并发抓取成分 ...")

    def work(item):  # type: ignore[no-untyped-def]
        code, name = item
        for attempt in range(1, 4):
            try:
                df = ak.sw_index_third_cons(symbol=code)
                if df is None or df.empty:
                    return pd.DataFrame()
                col = f"申万{level}级"
                sym = df["股票代码"].astype(str).str.replace(r"\.(SH|SZ|BJ)$", "", regex=True)
                return pd.DataFrame({
                    "symbol": sym.str.zfill(6),
                    f"industry_sw{level}": str(name),
                    f"industry_sw{level}_code": code,
                })
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    return pd.DataFrame()
                time.sleep(0.8 * attempt)
        return pd.DataFrame()

    if workers <= 1:
        results = [work(p) for p in pairs]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(work, pairs))

    frames = [r for r in results if not r.empty]
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["symbol"], keep="first")


def fetch_industry(workers: int = 4, with_sw: bool = False) -> pd.DataFrame | None:
    """行业分类：证监会细分（主）→ 申万一级 → 新浪 49 类 → 深交所门类 → unknown。

    数据源现状（本机实测）：
    - 东财行业源被本机代理拦截，不可用；
    - 新浪「行业」口径 = **证监会细分行业**（84 类官方标准），覆盖 5621/5562 只，
      口径统一、粒度适中，故作为主口径；
    - 申万一级最权威（31 类），但申万官网约半数行业返回空页，只能覆盖 ~2400 只，
      默认不拉（``--sw1`` 开启），拉到则作为次级口径写入 ``industry_sw1``；
    - 新浪 49 类与深交所/北交所列表自带门类作为最后兜底。
    """
    import akshare as ak

    _log("拉取行业分类 ...")
    frames: list[pd.DataFrame] = []

    # 1) 申万一级（可选：源不稳定，默认关闭）
    if with_sw:
        try:
            sw1 = _fetch_sw(1, workers)
            if sw1 is not None:
                frames.append(sw1)
                _log(f"  申万一级 {len(sw1)} 只")
        except Exception as exc:  # noqa: BLE001
            _log(f"  [失败] 申万一级：{type(exc).__name__} {exc}")

    # 3) 新浪板块（兜底：两个口径覆盖的股票集合不同，互补）
    #    「行业」其实是证监会细分行业（84 类，官方标准，覆盖率 99.9%），
    #    「新浪行业」是新浪自己的 49 类板块。前者口径统一且几乎全覆盖，故优先。
    for indicator, col in (("行业", "industry_csrc"), ("新浪行业", "industry_sina")):
        try:
            got = _fetch_sina_sector(indicator, col, workers)
            if got is not None:
                frames.append(got)
                _log(f"  新浪「{indicator}」{len(got)} 只（兜底）")
        except Exception as exc:  # noqa: BLE001
            _log(f"  [失败] 新浪「{indicator}」：{type(exc).__name__} {exc}")

    if not frames:
        return None

    # 合并（按优先级依次 left join，已有值不覆盖）
    ind = frames[0]
    for f in frames[1:]:
        ind = ind.merge(f, on="symbol", how="outer")

    out = CACHE / "industry.parquet"
    _atomic_parquet(ind, out)
    _log(f"  行业表 {len(ind)} 只 → {out.name}")

    # 回写 stock_list：申万一级 > 新浪 > 证监会门类 > unknown
    base_path = CACHE / "stock_list.parquet"
    if base_path.exists():
        base = pd.read_parquet(base_path)
        # 清掉本环节自己产出的列（industry_cs 由 fetch_stocks 提供，保留作兜底）
        for c in ("industry_csrc", "industry_sina", "industry_sina84",
                  "industry_sw1", "industry_sw1_code"):
            if c in base.columns:
                base = base.drop(columns=[c])
        base = base.merge(ind, on="symbol", how="left")

        def _s(col: str) -> pd.Series:
            return (base[col].astype(str).str.strip() if col in base.columns
                    else pd.Series([""] * len(base)))

        ind_final = _s("industry_csrc")
        for fallback in ("industry_sw1", "industry_sina", "industry_cs"):
            ind_final = ind_final.where(ind_final.str.len() > 0, _s(fallback))
        base["industry"] = ind_final.replace({"": "unknown", "nan": "unknown", "None": "unknown"})

        _atomic_parquet(base, base_path)
        known = int((base["industry"] != "unknown").sum())
        _log(f"  回写 stock_list：有行业 {known}/{len(base)}（{known / max(len(base), 1) * 100:.1f}%）"
             f" | 申万一级覆盖 {int((_s('industry_sw1').str.len() > 0).sum())}")
        vc = base.loc[base["industry"] != "unknown", "industry"].value_counts()
        _log(f"  行业数 {len(vc)}，Top10：" + "、".join(f"{k}({v})" for k, v in vc.head(10).items()))
    return ind


def _fetch_sina_sector(indicator: str, col: str, workers: int) -> pd.DataFrame | None:
    """新浪板块成分。``indicator`` 可取 '新浪行业'（49 类）/ '行业'（84 类）。

    两个口径覆盖的股票集合并不相同 —— 实测 49 类偏重深市，84 类能补上
    不少沪市股票，因此两个都拉，互为补充。
    """
    import akshare as ak

    sectors = ak.stock_sector_spot(indicator=indicator)
    pairs = list(zip(sectors["label"].astype(str), sectors["板块"].astype(str)))
    _log(f"  新浪「{indicator}」{len(pairs)} 个板块 ...")

    def work(item):  # type: ignore[no-untyped-def]
        label, name = item
        for attempt in range(1, 4):
            try:
                df = ak.stock_sector_detail(sector=label)
                if df is None or df.empty:
                    return pd.DataFrame()
                return pd.DataFrame({
                    "symbol": df["code"].astype(str).str.zfill(6),
                    col: str(name),
                })
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    return pd.DataFrame()
                time.sleep(1.0 * attempt)
        return pd.DataFrame()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(work, pairs))
    got = [r for r in res if not r.empty]
    if not got:
        return None
    return pd.concat(got, ignore_index=True).drop_duplicates(subset=["symbol"], keep="first")


# ==================================================================== 指数成分
INDEXES = {
    "000300": "沪深300",
    "000905": "中证500",
    "000016": "上证50",
    "000852": "中证1000",
}


def fetch_index(workers: int = 4) -> pd.DataFrame | None:
    """抓指数成分（含纳入日期，供 ``asof`` 时点池使用）。

    数据源选择（**已实测比较，勿随意改回**）
    ----------------------------------------
    两个候选源返回的**完整度差别巨大**：::

        指数        应有    官方中证源     index_stock_cons
        000300     300       300            288   ← 缺 12
        000905     500       500            429   ← 缺 71
        000016      50        50             50
        000852    1000      1000            772   ← 缺 228

    ``ak.index_stock_cons``（中证指数官网 样本列表）会返回**行数正确但
    内容重复**的表：例如 000852 返回 1000 行，却只有 772 个唯一代码，
    多出的 228 行是逐字段完全相同的副本。这会静默**丢掉 23% 的成分**。

    ``ak.index_stock_cons_csindex``（官方成分文件 ``{code}cons.xls``）
    返回的数量**与指数编制规则完全一致**，故作为主源。

    ⚠️ 两者**都是"当前成分快照"**，都不含历史剔除记录，因此都**无法**
    消除幸存者偏差。``in_date`` 只能用来消除前视偏差。
    """
    import akshare as ak

    _log(f"拉取指数成分（{', '.join(INDEXES.values())}）...")

    #: 指数编制规则里的成分数量，用于校验完整性
    expected_n = {"000300": 300, "000905": 500, "000016": 50, "000852": 1000}

    def work(item):  # type: ignore[no-untyped-def]
        """合并两个源，取长补短：

        - **官方成分文件**（``index_stock_cons_csindex``）给出**完整的成分
          全集**（300/500/50/1000，与编制规则一致），但没有"纳入日期"。
        - **``index_stock_cons``** 有 ``纳入日期``，但会**重复丢码**
          （实测 000852 返回 1000 行却只有 772 个唯一代码）。

        实测两者关系是 **官方源 ⊇ stock_cons**（交集 288，反向差集 0），
        因此以官方源为全集、用 stock_cons 补 ``in_date``，是无损的。
        """
        code, name = item
        want = expected_n.get(code, 0)

        # 源 1：官方成分文件（全集）
        official = None
        for attempt in range(1, 4):
            try:
                df = ak.index_stock_cons_csindex(symbol=code)
                official = pd.DataFrame({
                    "symbol": df["成分券代码"].astype(str).str.zfill(6),
                    "name": df["成分券名称"].astype(str).str.strip(),
                }).drop_duplicates(subset=["symbol"], keep="first")
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    break
                time.sleep(1.0 * attempt)

        # 源 2：含纳入日期（可能缺码）
        dated = None
        for attempt in range(1, 4):
            try:
                d = ak.index_stock_cons(symbol=code)
                dated = pd.DataFrame({
                    "symbol": d["品种代码"].astype(str).str.zfill(6),
                    "in_date": pd.to_datetime(d["纳入日期"], errors="coerce").dt.date,
                }).drop_duplicates(subset=["symbol"], keep="first")
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    break
                time.sleep(1.0 * attempt)

        if official is None and dated is None:
            _log(f"  [失败] {name}({code}) 两个源都不可用")
            return pd.DataFrame()

        if official is None:
            _log(f"  [警告] {name}({code}) 官方源不可用，退回 index_stock_cons"
                 f"（成分可能不全）")
            out = dated.copy()
            out["name"] = ""
        elif dated is None:
            _log(f"  [警告] {name}({code}) 无 in_date（index_stock_cons 不可用），"
                 f"前视偏差过滤将保守保留全部成分")
            out = official.copy()
            out["in_date"] = pd.NaT
        else:
            out = official.merge(dated, on="symbol", how="left")

        out.insert(0, "index_name", name)
        out.insert(0, "index_code", code)

        n = len(out)
        n_dated = int(out["in_date"].notna().sum()) if "in_date" in out.columns else 0
        flag = "" if not want or n == want else f"  ⚠️ 期望 {want} 只"
        _log(f"  {name}({code}) {n} 只（有纳入日期 {n_dated}）{flag}")
        return out

    items = list(INDEXES.items())
    results = [work(i) for i in items] if workers <= 1 else list(
        ThreadPoolExecutor(max_workers=min(workers, len(items))).map(work, items))

    frames = [r for r in results if not r.empty]
    if not frames:
        _log("  [失败] 指数成分全部拉取失败")
        return None
    cons = pd.concat(frames, ignore_index=True)

    # 去重：即便主源已保证唯一，仍兜一层（拼接/重跑可能引入重复）
    before = len(cons)
    cons = cons.drop_duplicates(subset=["index_code", "symbol"], keep="first")
    if before != len(cons):
        _log(f"  去重：{before} → {len(cons)} 条（移除 {before - len(cons)} 条重复）")

    out = CACHE / "index_constituents.parquet"
    _atomic_parquet(cons, out)
    for code, name in INDEXES.items():
        n = int((cons["index_code"] == code).sum())
        _log(f"  {name}({code}) {n} 只")
    _log(f"  指数成分合计 {len(cons)} 条 → {out.name}")
    return cons


# ==================================================================== main
def main() -> int:
    ap = argparse.ArgumentParser(description="拉取交易日历 / 股票元数据 / 行业分类 / 指数成分")
    ap.add_argument("--only", nargs="*", default=None,
                    choices=["calendar", "stocks", "industry", "index"],
                    help="只执行指定环节，默认全部")
    ap.add_argument("--workers", type=int, default=4, help="并发线程数")
    ap.add_argument("--sw1", action="store_true",
                    help="额外拉取申万一级行业（源不稳定，约半数行业会返回空页）")
    args = ap.parse_args()

    todo = set(args.only or ["calendar", "stocks", "industry", "index"])
    t0 = time.time()
    _log("=" * 64)
    _log(f"环节：{', '.join(sorted(todo))} | 并发 {args.workers}")
    _log("=" * 64)

    try:
        import akshare  # noqa: F401
    except ImportError:
        print("[错误] 未安装 akshare，请先：pip install akshare")
        return 1

    ok = True
    if "calendar" in todo:
        ok &= fetch_calendar() is not None
    if "stocks" in todo:
        ok &= fetch_stocks() is not None
    if "industry" in todo:
        ok &= fetch_industry(args.workers, with_sw=args.sw1) is not None
    if "index" in todo:
        ok &= fetch_index(args.workers) is not None

    _log("-" * 64)
    _log(f"完成，耗时 {time.time() - t0:.1f}s | 数据目录 {CACHE}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
