import { useQuery } from '@tanstack/react-query'
import { Layout, Menu, Tag, Space, Typography, Tooltip } from 'antd'
import {
  AlertOutlined,
  BarChartOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  LineChartOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons'
import { Link, Outlet, useLocation } from 'react-router-dom'
import api from '@/services/api'
import { fmtMoney } from '@/utils/format'

const { Header, Sider, Content, Footer } = Layout

const MENU = [
  { key: '/market', icon: <DashboardOutlined />, label: '市场总览' },
  { key: '/stocks', icon: <UnorderedListOutlined />, label: '自选/选股' },
  { key: '/signals', icon: <LineChartOutlined />, label: '信号列表' },
  { key: '/backtest', icon: <ExperimentOutlined />, label: '回测研究' },
  { key: '/anomaly', icon: <AlertOutlined />, label: '异动预警' },
  { key: '/admin', icon: <SettingOutlined />, label: '后台管理' },
]

export default function AppLayout() {
  const loc = useLocation()
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })
  const { data: account } = useQuery({ queryKey: ['account'], queryFn: api.account })

  const modeColor = health?.mode === 'live' ? 'red' : health?.mode === 'paper' ? 'orange' : 'blue'
  const modeLabel = health?.mode === 'live' ? '实盘' : health?.mode === 'paper' ? '模拟盘' : '回测'

  const selected = MENU.find((m) => loc.pathname.startsWith(m.key))?.key ?? '/market'

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
          }}
        >
          <Space size={12}>
            <Tag color={modeColor}>{modeLabel}</Tag>
            <Tooltip title={`数据源：${health?.data_source ?? '-'} / 通道：${health?.broker ?? '-'}`}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {health?.data_source ?? '-'} · {health?.broker ?? '-'}
              </Typography.Text>
            </Tooltip>
          </Space>

          <Space size={20}>
            <Typography.Text style={{ fontSize: 13 }}>
              总资产 <span className="mono">{fmtMoney(account?.total_asset)}</span>
            </Typography.Text>
            <Typography.Text style={{ fontSize: 13 }}>
              可用 <span className="mono">{fmtMoney(account?.cash)}</span>
            </Typography.Text>
            <Typography.Text
              style={{ fontSize: 13 }}
              className={account && account.unrealized_pnl >= 0 ? 'up' : 'down'}
            >
              浮动盈亏{' '}
              <span className="mono">
                {fmtMoney(account?.unrealized_pnl)}
              </span>
            </Typography.Text>
          </Space>
        </Header>

        <Content style={{ padding: 16, overflow: 'auto' }}>
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
