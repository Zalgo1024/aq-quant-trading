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
