// 与后端 aq/api/app.py 1:1 对齐的类型定义。
//
// 约定：后端「拿不到数据」时返回 available:false / 空数组 + 原因，
// 而不是 0 或假值。前端据此显示"未落盘"，不显示 0.00%。

export type Side = 'BUY' | 'SELL'
export type Direction = 'BUY' | 'SELL' | 'HOLD'
export type Severity = 'info' | 'warn' | 'severe'

export interface FactorContrib {
  name: string
  weight: number
  value: number
  contrib: number
}

export interface Prediction {
  symbol: string
  name?: string
  time?: string
  score: number
  confidence: number
  direction: Direction
  factor_contrib: FactorContrib[]
  model_version: string
  explain: string
  in_sample?: boolean
  sample_size?: number
  sample_caliber?: string
  weight_source?: string
  caveat?: string
}

export interface Signal {
  id: string
  symbol: string
  name?: string
  side: Side
  strength: number
  confidence: number
  trigger_factor: string
  ts: string
  source: string
}

export interface Quote {
  symbol: string
  name: string
  board: string
  industry?: string
  close: number
  pct_chg: number
  amount: number
  amount_ma20: number
  z_score: number
  in_liquid: boolean
}

/** /api/market/overview —— 全部来自本地全池快照 */
export interface Breadth {
  up: number
  down: number
  flat: number
  limit_up_approx: number
  limit_down_approx: number
  median_pct: number
  mean_pct: number
}

export interface DistBucket {
  label: string
  count: number
  /** 百分数区间（None = 开区间），供点柱子下钻筛选用 */
  lo: number | null
  hi: number | null
}

export interface UniverseInfo {
  n_symbols: number
  n_active: number
  n_suspended: number
  n_liquid_snapshot: number
}

export interface IndexQuote {
  symbol: string
  name: string
  available: boolean
  close?: number
  pct_chg?: number
  asof?: string
  /** 近 N 日**原始点位**（非归一化），前端自行决定是否归一 */
  closes?: number[]
  spark_dates?: string[]
  range_60d?: number
}

export interface MarketOverview {
  asof: string
  built_at: string
  universe: UniverseInfo
  breadth: Breadth
  distribution: DistBucket[]
  top_gainers: Quote[]
  top_losers: Quote[]
  top_amount: Quote[]
  indices: IndexQuote[]
  caliber_note: string
}

export interface StockInfo {
  symbol: string
  name: string
  industry?: string
  board?: string
  list_date?: string
  is_st?: boolean
}

export interface KBar {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export interface StockDetail {
  symbol: string
  name: string
  bars: KBar[]
  factors: Record<string, number | null>
  price_caliber: string
  adj_factor_last: number
  last_close_adj: number
  last_close_raw: number
  first_date: string
  last_date: string
}

export interface Anomaly {
  time: string
  symbol: string
  name?: string
  board?: string
  type: string
  z_score: number
  pct_chg?: number
  close?: number
  amount?: number
  severity: Severity
  detail: string
}

export interface AnomalyResponse {
  asof: string
  n_scanned: number
  min_abs_z: number
  items: Anomaly[]
  caveat: string
}

export interface Position {
  symbol: string
  qty: number
  available: number
  avg_cost: number
  last_price: number
  realized_pnl: number
}

export interface AccountInfo {
  account_id: string
  cash: number
  market_value: number
  total_asset: number
  realized_pnl: number
  unrealized_pnl: number
  positions: Position[]
  is_funded?: boolean
}

export interface BacktestMetrics {
  total_return: number
  annual_return: number
  sharpe: number
  sortino: number
  max_drawdown: number
  calmar: number
  win_rate: number
  profit_loss_ratio: number
  turnover: number
  information_ratio: number
  alpha: number
  beta: number
  cscv?: number | null
  deflated_sharpe?: number | null
}

export interface EquityPoint {
  time: string
  equity: number
  benchmark?: number | null
}

export interface BacktestResponse {
  run_id: string
  start: string
  end: string
  metrics: BacktestMetrics
  equity: EquityPoint[]
  drawdown: EquityPoint[]
  trade_count: number
  elapsed_seconds?: number
  max_symbols?: number
  diagnostics?: Record<string, unknown>
  caveat?: string
}

export interface FactorMeta {
  name: string
  group: string
  desc: string
  direction: number
  ic: number | null
  rank_ic: number | null
  rank_icir: number | null
  icir: number | null
  rank_ic_t: number | null
  rank_ic_p: number | null
  q_ls: number | null
  q_mono: number | null
  turnover: number | null
  significant: boolean
}

/** 逐日 IC 时序（/api/factor/ic_ts） */
export interface FactorIcSeries {
  dates: string[]
  series: Record<string, (number | null)[]>
  note?: string
}

/** 因子相关性矩阵（/api/factor/corr） */
export interface FactorCorr {
  labels: string[]
  matrix: (number | null)[][]
  note?: string
}

export interface HealthInfo {
  status: string
  version: string
  mode: string
  broker: string
  data_source: string
  time: string
  data_asof: string
  n_active: number
  n_liquid: number
  snapshot_built_at: string
  snapshot_error: string
  /** 代码身份块：boot = 进程启动那一刻的磁盘快照，disk = 磁盘现值。
   *  backend_stale=true 说明端口上跑的还是旧代码（启动器会自动重启）。 */
  code?: {
    boot?: Record<string, unknown>
    disk?: Record<string, unknown>
    backend_stale?: boolean
  }
}

export interface QuotesResponse {
  asof: string
  total: number
  offset: number
  limit: number
  items: Quote[]
}

export interface SignalsResponse {
  asof: string
  generated_at: string
  universe_total: number
  sample_size: number
  sample_caliber: string
  weight_source: string
  n_active_factors: number
  caveat: string
  signals: Signal[]
  buy_count: number
  sell_count: number
  hold_count: number
}

/** 项目状态 / 研究进展（/api/project/status） */
export type ItemStatus = 'done' | 'in_progress' | 'blocked' | 'todo'

export interface StageItem {
  name: string
  status: ItemStatus
  detail: string
  severity?: string
  source?: string
  quotable?: boolean
}

export interface Stage {
  id: string
  name: string
  goal: string
  source?: string
  items: StageItem[]
  progress: number
  n_items: number
  n_done: number
  /** 人工维护的路线结论（在 docs/研究进展.json 里写，接口原样透传）。
   *  如 "已否决" / "完成" / "进行中"；缺省时前端退化为按 progress 推断。 */
  outcome?: string
  /** 结论的一句话理由，用作悬停说明 */
  outcome_reason?: string
}

export interface Verdict {
  claim: string
  verdict: string
  evidence: string
  source?: string
  quotable?: boolean
}

export interface Gap {
  name: string
  detail: string
  severity: string
}

export interface DataAssets {
  bars: {
    n_files: number
    first_date: string
    last_date: string
    n_active: number
    n_suspended: number
  }
  delisted_bars: { n_files: number; n_listed: number }
  st_flags: { n_files: number; n_needed: number; ratio: number }
  valuation: { n_files: number }
  etf: { n_bars: number; n_nav: number }
  index_bars: {
    available: string[]
    missing: string[]
    n_available: number
    n_expected: number
  }
  calendar: { n_trade_days: number }
  liquid_snapshot: number
  caliber_note: string
}

export interface ProjectStatus {
  updated_at: string
  generated_at: string
  doc_error: string
  stages: Stage[]
  verdicts: Verdict[]
  gaps: Gap[]
  next_steps: string[]
  data_assets: DataAssets
  repo: { commit?: string; date?: string; subject?: string; branch?: string }
}

/** 一条探针 = 一次「可复现的证据」。登记信息来自 scripts/probes/README.md，
 *  文件状态由后端实时扫盘 —— 两者是分开的来源，所以能互相校对。 */
export interface ProbeEntry {
  script: string
  /** 这个脚本回答什么问题（README 手写） */
  question: string
  /** 依赖哪些本地数据（README 手写） */
  deps: string
  /** README 里「支撑的文档结论」原文 */
  doc_ref: string
  /** 解析出的文档相对路径；解析不出为空串（不猜） */
  doc_path: string
  /** 章节号，如 "1.1" */
  doc_section: string
  /** 文档是否真的存在；null = 该条本来就没写文档 */
  doc_exists: boolean | null
  /** README 用 ⭐ 标了「重点 / 必跑」 */
  starred: boolean
  exists: boolean
  size_bytes: number
  mtime: string
  n_lines: number
  /** 模块 docstring 首行（脚本自己写的"在测什么"） */
  docstring: string
}

export interface ProbeRegistry {
  available: boolean
  /** available=false 时的原因（缺文件 / 表格解析失败） */
  reason?: string
  source?: string
  readme_mtime?: string
  n_registered?: number
  n_on_disk?: number
  n_resolved_doc?: number
  /** 清单里有、磁盘上找不到 —— 文档在引用不存在的证据 */
  missing?: string[]
  /** 磁盘上有、清单里没登记 —— 有证据但读者看不到 */
  unregistered?: string[]
  /** 引用了解析不到的文档路径 */
  broken_doc?: string[]
  latest_script_mtime?: string
  entries: ProbeEntry[]
  conventions?: string[]
  prereq?: string
  pitfalls?: string[]
  caveat?: string
}

/** 一个 CSCV 产物的打分口径判定（/api/archive）。
 *  规则写在 README 第 5 节，后端只做机械执行 —— 前端也**不要**在这里重算规则。 */
export interface ArchiveCaliber {
  /** present = 全配置都带 score_neutralize；missing = 全缺（raw 旧口径）；
   *  partial = 一次运行里混了口径；unknown = 没有 configs，判定不了 */
  neutralize: 'present' | 'missing' | 'partial' | 'unknown'
  neutralize_value?: boolean | null
  n_with_field?: number
  n_configs?: number
  /** 有值说明是「双扣 rf」修正之后的超额口径 */
  excess_caliber?: string
  quotable?: boolean
  blockers?: string[]
}

/** 一枚 CSCV 产物（runtime/cscv/*.json）。字段在坏产物分支会缺，故几乎全可选。 */
export interface ArchiveCscv {
  name: string
  path: string
  mtime: string
  bytes: number
  readable: boolean
  parse_error?: string
  generated_at?: string
  universe?: string
  universe_note?: string
  benchmark?: string
  period?: { start: string; end: string }
  weight_basis?: string
  weight_basis_exists?: boolean
  /** 是否有 *_returns.csv（能用 --only-stats 低成本复核的前提） */
  has_returns?: boolean
  returns_path?: string
  /** 旁边那个同名目录里的逐配置收益缓存文件数（>0 表示可低成本重算） */
  n_rets_cache?: number
  pbo_main?: number | null
  pbo_excess?: number | null
  best_t?: number | null
  dsr_main?: number | null
  dsr_excess?: number | null
  haircut?: number | null
  verdict_significant?: boolean | null
  verdict_note?: string[]
  caliber: ArchiveCaliber
  quotable: boolean
  blockers: string[]
}

/** 一轮因子研究产物（runtime/factor_research/<run>/）。 */
export interface ArchiveFactorRun {
  name: string
  path: string
  mtime: string
  has_meta: boolean
  has_report: boolean
  has_summary: boolean
  n_files: number
  extra_files: string[]
  generated_at?: string
  universe?: string
  start?: string
  end?: string
  n_factors?: number | null
  n_rows?: number | null
  n_symbols?: number | null
  n_days?: number | null
  n_significant?: number | null
  n_strong?: number | null
  caliber: { state: 'neutralized' | 'raw' | 'unknown'; note: string }
}

/** README 人工表与本模块计算不一致的一处（kind: verdict_mismatch / missing_on_disk） */
export interface ArchiveDrift {
  kind: string
  name: string
  readme: boolean | null
  computed: boolean | null
  blockers: string[]
}

/** 只剩收益缓存、结果 json 已丢的目录 */
export interface ArchiveCacheOnly {
  name: string
  path: string
  mtime: string
  n_files: number
}

/** /api/archive —— 研究产物归档总表。
 *  数据全来自 runtime/（gitignore）⇒ 干净克隆下 available=false 属正常。 */
export interface ArchiveIndex {
  available: boolean
  unavailable_reason: string
  root: string
  generated_at: string
  readme_available: boolean
  readme_n_rows: number
  readme_drift: ArchiveDrift[]
  readme_unlisted: string[]
  cache_only_dirs: ArchiveCacheOnly[]
  summary: {
    n_cscv: number
    n_quotable: number
    n_stale: number
    n_factor_runs: number
    n_unlisted: number
    n_cache_only: number
    n_missing_weight_basis: number
    n_without_returns: number
    latest_mtime: string
  }
  cscv: ArchiveCscv[]
  factor_runs: ArchiveFactorRun[]
  rules: string[]
  caveat: string
}

