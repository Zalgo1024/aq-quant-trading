import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import AppLayout from '@/components/AppLayout'
import MarketOverviewPage from '@/pages/MarketOverview'
import StockListPage from '@/pages/StockList'
import StockDetailPage from '@/pages/StockDetail'
import SignalsPage from '@/pages/Signals'
import BacktestPage from '@/pages/Backtest'
import AnomalyPage from '@/pages/Anomaly'
import AdminPage from '@/pages/Admin'
import '@/styles/global.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 30_000 },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.darkAlgorithm, // 交易员习惯：深色
        token: {
          colorPrimary: '#1668dc',
          colorError: '#f5222d',   // 涨（红）
          colorSuccess: '#52c41a', // 跌（绿）
          borderRadius: 6,
        },
      }}
    >
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<AppLayout />}>
              <Route index element={<Navigate to="/market" replace />} />
              <Route path="market" element={<MarketOverviewPage />} />
              <Route path="stocks" element={<StockListPage />} />
              <Route path="stock/:code" element={<StockDetailPage />} />
              <Route path="signals" element={<SignalsPage />} />
              <Route path="backtest" element={<BacktestPage />} />
              <Route path="anomaly" element={<AnomalyPage />} />
              <Route path="admin" element={<AdminPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </ConfigProvider>
  </React.StrictMode>,
)
