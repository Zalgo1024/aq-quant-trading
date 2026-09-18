import { useQuery } from '@tanstack/react-query'
import { Layout, Menu, Tag, Space, Tooltip, Typography, Alert } from 'antd'
import {
  AlertOutlined,
  BarChartOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  LineChartOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons'
import { Link, Outlet, useLocation } from 'react-router-dom'
import api from '@/services/api'

const { Header, Sider, Content, Footer } = Layout

const MENU = [
  { key: '/market', icon: <DashboardOutlined />, label: '市场总览' },
  { key: '/status', icon: <FileSearchOutlined />, label: '研究进展' },
  { key: '/stocks', icon: <UnorderedListOutlined />, label: '全市场行情' },
  { key: '/signals', icon: <LineChartOutlined />, label: '因子打分' },
  { key: '/anomaly', icon: <AlertOutlined />, label: '异动预警' },
  { key: '/backtest', icon: <ExperimentOutlined />, label: '回测研究' },
  { key: '/admin', icon: <SettingOutlined />, label: '后台管理' },
]

export default function AppLayout() {
  const loc = useLocation()
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })

  const modeColor = health?.mode === 'live' ? 'red' : health?.mode === 'paper' ? 'orange' : 'blue'
  const modeLabel = health?.mode === 'live' ? '实盘' : health?.mode === 'paper' ? '模拟盘' : '回测'

  const selected = MENU.find((m) => loc.pathname.startsWith(m.key))?.key ?? '/market'
  const snapOk = health && !health.snapshot_error

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider width={200} theme="dark" breakpoint="lg" collapsedWidth={64}>
        <div
          style={{
            height: 56,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#fff',
            fontWeight: 600,
            fontSize: 15,
            letterSpacing: 1,
          }}
        >
          <BarChartOutlined style={{ marginRight: 8 }} />
          AI 量化
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selected]}
          items={MENU.map((m) => ({
            ...m,
            label: <Link to={m.key}>{m.label}</Link>,
          }))}
        />
      </Sider>

      <Layout>
        <Header
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '0 20px',
            background: '#1f1f1f',
            height: 56,
            lineHeight: '56px',
          }}
        >
          {/* 左侧：模式 + 数据源。以前这里显示"总资产 ¥0.00"——账户根本没投钱，
              那个 ¥0.00 会被误读成"系统坏了"或"钱没了"。改成显示数据口径状态。 */}
          <Space size={12} wrap>
            <Tag color={modeColor}>{modeLabel}</Tag>
            <Tooltip title={`数据源：${health?.data_source ?? '-'} / 交易通道：${health?.broker ?? '-'}`}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {health?.data_source ?? '-'} · {health?.broker ?? '-'}
              </Typography.Text>
            </Tooltip>
            {snapOk ? (
              <Typography.Text style={{ fontSize: 12 }}>
                数据截至{' '}
                <span className="mono">{health?.data_asof || '—'}</span>
                {' · '}
                全市场 <span className="mono">{health?.n_active ?? 0}</span> 只
                {' · '}
                流动性池 <span className="mono">{health?.n_liquid ?? 0}</span> 只
              </Typography.Text>
            ) : (
              <Typography.Text type="warning" style={{ fontSize: 12 }}>
                行情快照未就绪
              </Typography.Text>
            )}
          </Space>

          <Space size={16}>
            <Link to="/status">
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                研究进展 ›
              </Typography.Text>
            </Link>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              v{health?.version ?? '-'}
            </Typography.Text>
          </Space>
        </Header>

        <Content style={{ padding: 16, overflow: 'auto' }}>
          {health?.snapshot_error && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="行情快照不可用，页面数值可能为空"
              description={
                <span style={{ fontSize: 12 }}>
                  {health.snapshot_error} —— 可在「市场总览」页点「重建快照」，或执行
                  <span className="mono"> python -c "from aq.data.snapshot import MarketSnapshot as M; M().build()"</span>
                </span>
              }
            />
          )}
          <Outlet />
        </Content>

        <Footer style={{ padding: '10px 20px', background: '#1f1f1f' }}>
          <div className="risk-banner">
            风险提示：本系统仅用于技术研究与学习，<b>不构成任何投资建议</b>，不承诺任何收益。
            历史回测结果不代表未来表现。程序化交易请遵守法律法规并按要求报备。请理性投资。
          </div>
        </Footer>
      </Layout>
    </Layout>
  )
}
