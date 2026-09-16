import axios from 'axios'
import type {
  AccountInfo,
  Anomaly,
  BacktestResponse,
  FactorCorr,
  FactorIcSeries,
  FactorMeta,
  HealthInfo,
  MarketOverview,
  Prediction,
  Signal,
  StockDetail,
  StockInfo,
} from '@/types'

const http = axios.create({
  baseURL: '/api',
  timeout: 120_000, // 回测可能较慢
})

export const api = {
  health: () => http.get<HealthInfo>('/health').then((r) => r.data),
  config: () => http.get<Record<string, unknown>>('/config').then((r) => r.data),

  marketOverview: () => http.get<MarketOverview>('/market/overview').then((r) => r.data),
  stocks: () => http.get<StockInfo[]>('/stocks').then((r) => r.data),
  stockDetail: (code: string, limit = 250) =>
    http.get<StockDetail>(`/stock/${code}/detail`, { params: { limit } }).then((r) => r.data),
  stockPrediction: (code: string) =>
    http.get<Prediction>(`/stock/${code}/prediction`).then((r) => r.data),

  signals: (top = 20) => http.get<Signal[]>('/signals', { params: { top } }).then((r) => r.data),
  factorAnalysis: () =>
    http
      .get<{ factors: FactorMeta[]; meta: Record<string, unknown>; note: string; generated_at?: string }>(
        '/factor/analysis',
      )
      .then((r) => r.data),
  factorIcTs: (factors = '') =>
    http.get<FactorIcSeries>('/factor/ic_ts', { params: { factors } }).then((r) => r.data),
  factorCorr: () => http.get<FactorCorr>('/factor/corr').then((r) => r.data),
  anomaly: () => http.get<Anomaly[]>('/anomaly').then((r) => r.data),
  account: () => http.get<AccountInfo>('/account').then((r) => r.data),

  runBacktest: (payload: {
    start?: string
    end?: string
    top_k?: number
    initial_cash?: number
    mode?: string
  }) => http.post<BacktestResponse>('/backtest', payload).then((r) => r.data),
}

export default api
