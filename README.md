# A 股 AI 量化交易系统（aq_project）

> 仅面向 **A 股股票**；**模拟优先（paper）**，实盘接口预留；本地单机部署。
> 本仓库为可运行骨架：数据层 / 因子层 / 模型层 / 组合风控 / 执行层（SimGateway）/ 回测 / API / 前端。

## 快速开始

```powershell
# 1) 创建虚拟环境（推荐）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) 安装依赖
pip install -r requirements.txt

# 3) 冒烟自测（无需联网、无外部数据）
python -m aq.smoke_test

# 4) 启动 API
python -m uvicorn aq.api.app:app --reload --port 8000
# 打开 http://127.0.0.1:8000/docs

# 5) 拉取真实全市场日线（约 35 分钟；P1）
pip install akshare
python scripts/fetch_daily.py --workers 4        # 行情：后复权 + 真实价
python scripts/fetch_meta.py                     # 元数据：日历/上市日期/股本/行业/指数成分
python scripts/check_data_quality.py             # 数据质检

# 6) 切到真实数据源（config/base.yaml: data.source = local）
python aq/smoke_test.py                          # 应 8/8 通过

# 之后每天增量更新（建议 19:00 后）
python scripts/update_daily.py --overlap 10

# 7) 因子研究（P2）
python scripts/factor_research.py                       # 全市场动态池，2019 起
python scripts/factor_research.py --neutralize          # 行业+市值中性化后再检验
# 产出：runtime/factor_research/<时间戳>/{summary.csv, ic_ts.parquet, corr.csv, report.md}
```

## 因子层（P2）

**27 个纯量价因子**，算法只在 `aq/factors/vlib.py` 实现一次，研究与实盘共用，
杜绝"回测里有效、上线后算出另一个值"。

| 组 | 因子 |
|---|---|
| 动量 | `mom_20` `mom_60` `mom_120` `mom_60_vol_adj` |
| 反转 | `rev_5` |
| 风险 | `vol_20` `vol_60` `dvol_20` `mdd_20` `skew_20` `hl_range_20` |
| 趋势 | `ma_bias_20` `ma_bias_60` `ma_cross_5_20` `rsi_14` `stoch_pos_20` |
| 量能 | `vol_ratio` `turnover_chg` `amihud_20` `amount_log_20` `up_down_vol_20` `vol_price_corr_20` `turn_rate_20` |
| 结构 | `days_since_high_60` `limit_up_cnt_20` `gap` `intraday_ret` |

工具链：

| 模块 | 作用 |
|---|---|
| `aq/factors/vlib.py` | 向量化因子内核（**唯一权威实现**）+ 先验方向 |
| `aq/factors/panel.py` | 因子面板：全市场流式构建 + 前瞻收益对齐 + 可交易标记 |
| `aq/factors/neutralize.py` | 行业 + 市值中性化（FWL 定理全表向量化） |
| `aq/factors/ic.py` | 自研 IC 检验：IC / RankIC / ICIR / **Newey-West t** / 分位收益 / 衰减 / 换手 |
| `scripts/factor_research.py` | 一键：建面板 → 中性化 → 逐因子检验 → 去冗余 → 出报告 |

两个容易踩的坑，代码里已处理：

1. **一字板不可交易**：次日停牌 / 一字涨停（买不进）/ 一字跌停（卖不出）都打上
   `t1_tradable=False`，IC 检验自动剔除，否则会算出"买一字板"的假 alpha。
2. **重叠持有期的高估**：持有 5 天时，相邻两天的 IC 样本高度重叠，IC 序列自相关，
   普通 t 检验会系统性高估显著性。因此 t 值默认做 **Newey-West 修正**。

打分权重支持两种来源（`config/*.yaml` 的 `model.weight_source`）：
`prior`（内置先验）或 `ic`（读最近一次因子研究的 RankICIR 自动定权，
符号直接取实测 IC 方向）。找不到 IC 结果时静默降级为先验，不会让线上流程崩掉。

## 数据资产（`data_cache/`，P1 产出）

| 文件 | 内容 |
|---|---|
| `bars/{symbol}.parquet` | 日线 5218+ 只 × 914 万根 K 线，2018-01-02 ~ 2026-09-15 |
| `stock_list.parquet` | 5562 只：中文名 / 板块 / 上市日期 / 总股本 / 流通股本 / 行业 / ST |
| `industry.parquet` | 证监会细分行业 84 类，覆盖 99.9% |
| `index_constituents.parquet` | 沪深300(300) / 中证500(500) / 上证50(50) / 中证1000(1000) |
| `calendar.parquet` | 交易日历 8797 天（1990-12-19 ~ 2026-12-31） |

## 目录

```
aq_project/
├─ aq/
│  ├─ core/         # 领域模型：Order/Fill/Account/Position/Bar/Signal + 枚举
│  ├─ config/       # 配置加载（yaml + 环境变量覆盖）
│  ├─ data/         # 数据层：Provider 抽象 + akshare/tushare/mock 实现 + 存储
│  ├─ factors/      # 因子计算 / 中性化 / 验证
│  ├─ models/       # 截面/时序/情绪模型 + 训练与推理
│  ├─ portfolio/    # 组合构建 + 风控引擎
│  ├─ execution/    # MarketFeed / TradingGateway 抽象 + SimGateway + 实盘 Adapter 壳
│  ├─ backtest/     # 回测引擎（复用 SimGateway 撮合）
│  ├─ api/          # FastAPI 服务
│  └─ smoke_test.py # 自包含端到端冒烟测试
├─ config/          # base.yaml / paper.yaml / backtest.yaml / live.example.yaml
├─ web/             # React + Vite + ECharts 前端（红涨绿跌、¥）
├─ docs/            # 技术设计文档、开发计划
└─ sql/             # PostgreSQL/TimescaleDB schema
```

## 设计要点

- **一套代码三种模式**：回测 / 模拟 / 实盘共用同一事件循环与撮合内核，只换 `MarketFeed` 与 `TradingGateway`。
- **风控下沉**：单票/行业/仓位/ST/涨跌停等规则集中在校验层，模拟与实盘共用一份。
- **A 股真实规则**：T+1、涨跌停封板不成交、印花税（仅卖出）、佣金最低 5 元、滑点、100 股/手、不可裸卖空。
- **可解释信号**：`signal.json` 携带评分与因子贡献明细，前端可展示"为什么推荐"。

## ⚠️ 后复权价 vs 真实价（务必先看）

这是本项目最容易踩的坑，直接决定回测结果可不可信：

| 用途 | 用什么 | 说明 |
|---|---|---|
| 因子 / 收益率 / 信号 | **后复权价** `bar.close` | 除权日收益率连续 |
| 撮合 / 资金 / 股数 / 手续费 | **真实价** `bar.open_raw` `bar.close_raw` | 后复权价可能是真实价的十几倍（浦发银行 2026 年：后复权 159.66 元 vs 真实 9.18 元）。用后复权价算资金，能买多少手会全错 |
| 涨跌停判断 | 真实价算完再换算回复权尺度 | 交易所按真实价四舍五入到分；直接在后复权价上乘 1.1 会差约 0.6 元，足以把涨停误判成没涨停 |

`Bar.adj_factor = 后复权价 / 真实价`，是**只在除权日跳变的分段常数**（实测浦发 8 年跳变 9 次、
茅台 13 次，均落在除权日）。数据层同时拉取后复权与不复权两份序列合成它。

**注意**：新浪的后复权价与真实价都只保留 2 位小数，低价股的"分"就是 0.2~0.4% 的量化噪声，
会让 `adj_factor` 每天抖动（170 只股票曾跳变上百次）。因此 `aq/data/adj.py` 会做
**变点检测 + 段内取中位数**，把它还原成真正的分段常数（全市场跳变 83,612 → 20,523）。
已在拉取链路默认启用，历史数据可用 `scripts/fix_adj_factor.py` 就地修复。

## 免责声明

本项目仅用于技术研究与学习，**不构成任何投资建议**，不承诺任何收益。历史回测结果不代表未来表现。
程序化交易请遵守当地法律法规并按要求报备。请理性投资。
