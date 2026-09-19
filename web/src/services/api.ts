import axios from 'axios'
import type {
  AccountInfo,
  AnomalyResponse,
  ArchiveIndex,
  BacktestResponse,
  FactorCorr,
  FactorIcSeries,
  FactorMeta,
  HealthInfo,
  IndexQuote,
  MarketOverview,
  Prediction,
  ProbeRegistry,
  ProjectStatus,
  QuotesResponse,
  SignalsResponse,
  StockDetail,
  StockInfo,
} from '@/types'

const http = axios.create({
  baseURL: '/api',
  timeout: 180_000, // 首次打分要算 400 只因子（~17s），回测更慢
})

export const api = {
  health: () => http.get<HealthInfo>('/health').then((r) => r.data),
  config: () => http.get<Record<string, unknown>>('/config').then((r) => r.data),

  // ---- 市场 ----
  marketOverview: (top = 15) =>
    http.get<MarketOverview>('/market/overview', { params: { top } }).then((r) => r.data),
  marketIndices: () =>
    http
      .get<{ indices: IndexQuote[]; n_available: number; note: string }>('/market/indices')
      .then((r) => r.data),
  rebuildSnapshot: () =>
    http
      .post<{ ok: boolean; seconds: number; n_symbols: number; asof: string }>(
        '/market/snapshot/rebuild',
      )
      .then((r) => r.data),

  /** 全市场行情表（分页/搜索/板块/池过滤） */
  quotes: (p: {
    keyword?: string
    board?: string
    only_liquid?: boolean
    sort?: string
    order?: string
    limit?: number
    offset?: number
  }) => http.get<QuotesResponse>('/quotes', { params: p }).then((r) => r.data),

  // ---- 个股 ----
  stocks: () => http.get<StockInfo[]>('/stocks').then((r) => r.data),
  stockDetail: (code: string, limit = 250) =>
    http.get<StockDetail>(`/stock/${code}/detail`, { params: { limit } }).then((r) => r.data),
  stockPrediction: (code: string) =>
    http.get<Prediction>(`/stock/${code}/prediction`).then((r) => r.data),

  // ---- 打分 / 因子 ----
  signals: (top = 20) =>
    http.get<SignalsResponse>('/signals', { params: { top } }).then((r) => r.data),
  factorAnalysis: () =>
    http
      .get<{
        factors: FactorMeta[]
        meta: Record<string, unknown>
        note: string
        run_id?: string
        generated_at?: string
      }>('/factor/analysis')
      .then((r) => r.data),
  factorIcTs: (factors = '') =>
    http.get<FactorIcSeries>('/factor/ic_ts', { params: { factors } }).then((r) => r.data),
  factorCorr: () => http.get<FactorCorr>('/factor/corr').then((r) => r.data),

  anomaly: (top = 30, minAbsZ = 2.5) =>
    http
      .get<AnomalyResponse>('/anomaly', { params: { top, min_abs_z: minAbsZ } })
      .then((r) => r.data),

  account: () => http.get<AccountInfo>('/account').then((r) => r.data),

  // ---- 项目状态 ----
  projectStatus: () => http.get<ProjectStatus>('/project/status').then((r) => r.data),

  /** 探针台账：结论 ↔ 探针脚本 ↔ 文档（登记信息读 README，文件状态实时扫盘） */
  probes: () => http.get<ProbeRegistry>('/probes').then((r) => r.data),

  /** 研究产物归档：跑过什么、**哪些数字还能引用**（口径判定 + README 表交叉核对） */
  archive: () => http.get<ArchiveIndex>('/archive').then((r) => r.data),

  runBacktest: (payload: {
    start?: string
    end?: string
    top_k?: number
    initial_cash?: number
    mode?: string
    max_symbols?: number
  }) => http.post<BacktestResponse>('/backtest', payload).then((r) => r.data),
}

export default api
