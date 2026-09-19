// 全局框架：三组导航 + 顶部研究状态条 + 常驻风险提示。
//
// 导航为什么从"7 项平铺"改成"三组"：
//   旧版把「因子打分」「异动预警」和「研究进展」并排放一级菜单，
//   但前者展示的产物已经被项目自己证伪了 —— 信息架构本身在暗示"这些都还能用"。
//   新版按**研究流程**分组：结论（我在哪）/ 证据（数字从哪来）/ 实验（动手的地方），
//   已证伪的产物降级进「实验」，后台管理移出主导航（收到状态条右侧）。
import { Layout, Menu } from 'antd'
import {
  AlertOutlined,
  BarChartOutlined,
  BookOutlined,
  DatabaseOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  LineChartOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { useMemo } from 'react'
import StatusBar from './StatusBar'
import { useTheme } from '@/theme/store'

const { Header, Sider, Content, Footer } = Layout

const NAV = [
  {
    group: '研究主线',
    items: [
      { key: '/', label: '研究进展', icon: <FileSearchOutlined /> },
      { key: '/verdicts', label: '结论台账', icon: <BookOutlined /> },
    ],
  },
  {
    group: '证据',
    items: [
      { key: '/status', label: '数据资产', icon: <DatabaseOutlined /> },
      { key: '/market', label: '市场与池', icon: <DashboardOutlined /> },
      { key: '/stocks', label: '全市场行情', icon: <UnorderedListOutlined /> },
    ],
  },
  {
    group: '实验',
    items: [
      { key: '/signals', label: '因子打分', icon: <LineChartOutlined /> },
      { key: '/anomaly', label: '异动预警', icon: <AlertOutlined /> },
      { key: '/backtest', label: '回测研究', icon: <ExperimentOutlined /> },
    ],
  },
]

export default function AppLayout() {
  const loc = useLocation()
  const mode = useTheme((s) => s.mode)
  const dark = mode === 'dark'

  const selected = useMemo(() => {
    const p = loc.pathname
    if (p === '/') return '/'
    if (p.startsWith('/stock/')) return '/stocks' // 个股详情从行情表下钻，不占导航
    const keys = NAV.flatMap((g) => g.items.map((i) => i.key)).filter((k) => k !== '/')
    // 取最长匹配，避免短前缀抢走高亮
    return keys.filter((k) => p.startsWith(k)).sort((a, b) => b.length - a.length)[0] ?? ''
  }, [loc.pathname])

  const menuItems = NAV.map((g) => ({
    type: 'group' as const,
    label: g.group,
    children: g.items.map((i) => ({
      key: i.key,
      icon: i.icon,
      label: <Link to={i.key}>{i.label}</Link>,
    })),
  }))

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        width={196}
        theme={dark ? 'dark' : 'light'}
        breakpoint="lg"
        collapsedWidth={64}
        style={{
          background: 'var(--aq-surface)',
          borderRight: '0.5px solid var(--aq-border)',
        }}
      >
        <div
          style={{
            height: 56,
            display: 'flex',
            alignItems: 'center',
            paddingLeft: 20,
            color: 'var(--aq-text)',
            fontWeight: 500,
            fontSize: 14,
            letterSpacing: 0.5,
          }}
        >
          <BarChartOutlined style={{ marginRight: 8 }} />
          AI 量化研究台
        </div>
        <Menu
          theme={dark ? 'dark' : 'light'}
          mode="inline"
          selectedKeys={selected ? [selected] : []}
          items={menuItems}
          style={{ background: 'transparent', borderInlineEnd: 'none' }}
        />
      </Sider>

      <Layout>
        <Header
          style={{
            display: 'flex',
            alignItems: 'center',
            padding: '0 16px',
            background: 'var(--aq-surface)',
            borderBottom: '0.5px solid var(--aq-border)',
            height: 56,
            lineHeight: '56px',
          }}
        >
          <StatusBar />
        </Header>

        <Content style={{ padding: 16, overflow: 'auto' }}>
          <Outlet />
        </Content>

        <Footer
          style={{
            padding: '10px 16px',
            background: 'var(--aq-surface)',
            borderTop: '0.5px solid var(--aq-border)',
          }}
        >
          <div className="risk-banner">
            风险提示：本系统仅用于技术研究与学习，<b>不构成任何投资建议</b>，不承诺任何收益。
            历史回测结果不代表未来表现。程序化交易请遵守法律法规并按要求报备。请理性投资。
          </div>
        </Footer>
      </Layout>
    </Layout>
  )
}
