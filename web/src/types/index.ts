// 与后端 aq/core/models.py 1:1 对齐的类型定义

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
  time?: string
  score: number
  confidence: number
  direction: Direction
  factor_contrib: FactorContrib[]
  model_version: string
  explain: string
}

export interface Signal {
  id: string
  symbol: string
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
  close: number
  pct_chg: number
  amount: number
}

export interface IndexQuote {
  symbol: string
  name: string
  pct_chg: number
}

export interface MarketOverview {
  up_count: number
  down_count: number
  flat_count: number
  quotes: Quote[]
  indices: IndexQuote[]
  north_bound: number
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
}

export interface Anomaly {
  time: string
  symbol: string
  type: string
  z_score: number
  severity: Severity
  detail: string
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
}

export interface FactorMeta {
  name: string
  direction: number
  ic: number | null
  rank_ic: number | null
  icir: number | null
}

export interface HealthInfo {
  status: string
  version: string
  mode: string
  broker: string
  data_source: string
  time: string
}
