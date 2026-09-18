# 研究探针（`scripts/probes/`）

这里放的是**一次性研究探针**：用来回答"某个结论到底站不站得住"的只读脚本。
它们不属于交易管线（不被 `aq/` 或 `scripts/` 主流程引用），但**支撑着文档里的关键数字**，
所以从 `runtime/` 挪进来纳入版本控制 —— 否则文档引用的路径在仓库里查无此文件，
外部读者只能选择相信或不信。

## 三条约定

1. **只读**。不写 `data_cache/`、不改 `runtime/cscv/` 里任何既有产物、不产生新缓存。
   唯一的副作用是打印到 stdout。
2. **不预置结论**。每个脚本的 docstring 都写清"为什么这么测"和"哪种结果算证伪"。
3. **可失踪**。它们依赖 `data_cache/`（`.gitignore` 内，需自行拉取）。
   缺数据时应当报错或跳过，而不是给一个看起来正常但其实是空集的数字。

## 前置条件

```bash
# 必须有本地数据（约 35 分钟，见 README「快速开始」第 5 步）
python scripts/fetch_daily.py
python scripts/fetch_valuation.py
python scripts/fetch_meta.py
python scripts/fetch_index_bars.py

# 部分探针还需要 CSCV 产物（即 runtime/cscv/<池子>_<tag>/rets/）
python scripts/cscv_test.py --universe liquid --variant full_neu_v2 \
  --factor-set all --score-neutralize --tag neu_main
```

`probe_survivorship.py` 与 `probe_style_exposure.py` 是本目录里最重的两个
（前者要把 5562 只日线读进内存，约 2~5 分钟）。

## 清单

| 脚本 | 回答什么问题 | 依赖 | 支撑的文档结论 |
|---|---|---|---|
| `segment_consistency.py` | 按年/半年/季度切开后，超额收益的**符号**是不是一致？ | `runtime/cscv/<tag>/rets/` | 终局诊断 §1.1 |
| `probe_style_exposure.py` | 所谓 alpha 是不是只是**市场 + 规模**暴露？（Newey-West 双因子回归） | 上面 + `data_cache/index_bars/` | 终局诊断 §1.2 |
| `probe_etf_replication.py` | 把回归系数翻译成静态 ETF 组合，**跑不跑得赢策略本身**？ | 同上 | 终局诊断 §1.3 |
| `probe_survivorship.py` | 幸存者偏差有多大？（跌得多的后续表现 / 便宜=捡刀子 / 删失规模 / 组合拖累） | `data_cache/bars`+`valuation`+`stock_list`，联网取退市名单 | 终局诊断 §1.4 |
| `probe_factor_dimensionality.py` | 31 个因子实际有几个**独立方向**？（PR、主成分、MP 噪声上界） | `runtime/factor_research/` | 终局诊断 §4「多智能体不做」 |
| `probe_risk_budget.py` | 给定「组合最大回撤 ≤ X%」，**权益仓位上限**是多少？（附历史水下区间清单） | `data_cache/index_bars/` | 小额实盘方案 §1 |
| `probe_data_reach.py` | 1990/2005 的数据到底拉不拉得到？（5 项预检） | 联网 | 终局诊断 §3 |
| `probe_delist_bars.py` | 退市股的**历史日线**有没有可用源？ | 联网 | 终局诊断 §3 |
| `probe_excess_rf.py` | 超额口径有没有把无风险利率扣两次？ | 联网 + `runtime/cscv/*.json` | README §4.2 |
| `verify_neu_equivalence.py` | 逐日中性化 ≡ 整表中性化？（应当是逐格完全相同） | `data_cache/` | README §4.1 |
| `ab_verdict.py` | B1 两臂并排：只认 CSCV 之后的 PBO / DSR / 超额 t | `runtime/cscv/*.json` ×2 | 收益来源候选 §2.5 |

## 两个通用陷阱（都在这堆脚本里踩过，所以写下来）

- **任何"从大池子里挑少数"的指标，必须先建随机基准。**
  本项目栽过两次：一次是行业集中度的 TVD（随机抽 20 只的基准就有 0.545），
  一次是因子池维数（必须配 Marchenko-Pastur 上界 λ⁺ 与逐序号 95% 分位）。
- **"独立实现"本身也要被权威结果反验。**
  写一个独立脚本去核对权威产物时，先让它复现出**已知正确的那个数**，
  否则你只是用第二份错误去"验证"第一份错误。
