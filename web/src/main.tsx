import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import AppLayout from '@/components/AppLayout'
import HomePage from '@/pages/Home'
import VerdictsPage from '@/pages/Verdicts'
import ProjectStatusPage from '@/pages/ProjectStatus'
import MarketOverviewPage from '@/pages/MarketOverview'
import StockListPage from '@/pages/StockList'
import StockDetailPage from '@/pages/StockDetail'
import SignalsPage from '@/pages/Signals'
import BacktestPage from '@/pages/Backtest'
import AnomalyPage from '@/pages/Anomaly'
import AdminPage from '@/pages/Admin'
import { antdConfig } from '@/theme/tokens'
import { useTheme } from '@/theme/store'
import '@/styles/global.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 30_000 },
  },
})

function Root() {
  // 主题从 store 读 → ConfigProvider 重建 → antd 全量换色。
  // CSS 变量一侧由 store 在 setMode 时写 <html data-theme>，两边同源。
  const mode = useTheme((s) => s.mode)
  return (
    <ConfigProvider locale={zhCN} theme={antdConfig(mode)}>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<AppLayout />}>
              {/* 首页 = 研究进展。理由见 pages/Home.tsx 顶部注释 */}
              <Route index element={<HomePage />} />
              <Route path="verdicts" element={<VerdictsPage />} />
              <Route path="status" element={<ProjectStatusPage />} />
              <Route path="market" element={<MarketOverviewPage />} />
              <Route path="stocks" element={<StockListPage />} />
              <Route path="stock/:code" element={<StockDetailPage />} />
              <Route path="signals" element={<SignalsPage />} />
              <Route path="backtest" element={<BacktestPage />} />
              <Route path="anomaly" element={<AnomalyPage />} />
              <Route path="admin" element={<AdminPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
)
