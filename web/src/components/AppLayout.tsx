// 全局框架：三组导航 + 顶部研究状态条 + 常驻风险提示。
//
// 导航为什么从"7 项平铺"改成"三组"：
//   旧版把「因子打分」「异动预警」和「研究进展」并排放一级菜单，
//   但前者展示的产物已经被项目自己证伪了 —— 信息架构本身在暗示"这些都还能用"。
//   新版按**研究流程**分组：结论（我在哪）/ 证据（数字从哪来）/ 实验（动手的地方），
//   已证伪的产物降级进「实验」，后台管理沉成侧栏底部的一个图标。
//
// 「实验」组内排序：**研究产物在前，工具在最后**。
//   实验室（结论↔脚本）、归档（跑过什么、哪些数字还引用得起）看的是"已经做过的"；
//   因子打分 / 回测是在做；异动扫描只是一个统计工具（本项目从未检验过它有无预测力），
//   所以它排末位，且页内自带「工具 · 非研究结论」标注。
import { Button, Layout, Menu, Tooltip } from 'antd'
import {
  AlertOutlined,
  AppstoreOutlined,
  BarChartOutlined,
  BookOutlined,
  DatabaseOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  FundOutlined,
  InboxOutlined,
  LineChartOutlined,
  SettingOutlined,
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
      // 盘后看板紧挨「市场与池」：两者是**同一份全池快照**的两种读法
      // （那边负责分析与下钻，这边负责一屏扫完）。它借了盯盘软件的密度，
      // 但页头硬标「日频截面 · 非实时」—— 密度可以借，"数据在动"的暗示不能借。
      { key: '/board', label: '盘后看板', icon: <AppstoreOutlined /> },
      { key: '/stocks', label: '全市场行情', icon: <UnorderedListOutlined /> },
    ],
  },
  {
    group: '实验',
    items: [
      // 实验室排在实验区第一位：动手之前先看「已经做过什么、怎么做死的」，
      // 比直接看当日打分榜有用得多。
      { key: '/lab', label: '实验室', icon: <ExperimentOutlined /> },
      // 归档紧随其后：实验室管"结论是哪个脚本跑出来的"，归档管
      // "哪一次运行的结果现在还引用得起"（口径判定 + README 表交叉核对）。
      { key: '/archive', label: '研究归档', icon: <InboxOutlined /> },
      { key: '/signals', label: '因子打分', icon: <LineChartOutlined /> },
      { key: '/backtest', label: '回测研究', icon: <BarChartOutlined /> },
      // 异动扫描沉到末位：它是工具，不是研究结论（页内自带"工具 · 非研究结论"标注）。
      { key: '/anomaly', label: '异动扫描', icon: <AlertOutlined /> },
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
          // ⚠️ 下面四条不是装饰，缺了它们「沉到侧栏底部」就是一句假话：
          //    Layout 是 flex 行容器，Sider 默认会被拉伸到**整个文档高度**
          //    （实测：内容 2134px 时 Sider 也是 2134px），于是运维入口的
          //    y 坐标 = 2102 —— 得滚到页尾才看得见，那不是"角落"，是"藏起来"。
          //    sticky + 100vh + align-self:flex-start 让它钉在视口左缘，
          //    导航在内部滚动，运维入口永远贴在视口左下角。
          position: 'sticky',
          top: 0,
          height: '100vh',
          alignSelf: 'flex-start',
          overflow: 'hidden',
        }}
      >
        {/* 侧栏做成纵向 flex：导航在上，运维入口（后台）沉底。
            「后台」不属于研究流程，但又不能完全藏起来 ——
            沉到最底部是"在但不用看"的正确位置。

            它原来挂在顶部状态条的右上角，和主题开关、版本号挤在一起；
            而状态条要回答的是「走到哪一步 / 数据多新 / 有没有故障」，
            运维入口混在里面会稀释那三个问题。 */}
        <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
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
              flex: 'none',
            }}
          >
            <FundOutlined style={{ marginRight: 8 }} />
            AI 量化研究台
          </div>
          <Menu
            theme={dark ? 'dark' : 'light'}
            mode="inline"
            selectedKeys={selected ? [selected] : []}
            items={menuItems}
            style={{ background: 'transparent', borderInlineEnd: 'none', flex: '1 1 auto', overflowY: 'auto' }}
          />
          {/* 运维入口收成一个**贴角的图标**。
              原来是一行 12px 灰字「后台与实盘」——灰字也仍然是一条"可以点进去看看"
              的邀请，而它不属于研究流程。收成图标后视觉权重≈0，
              但 Tooltip 与 aria-label 保证它**仍可被发现**：
              藏的是权重，不是可发现性。 */}
          <div
            style={{
              flex: 'none',
              padding: '4px 14px 8px',
              borderTop: '0.5px solid var(--aq-border)',
              display: 'flex',
              justifyContent: 'flex-end',
            }}
          >
            <Tooltip title="后台与实盘（运维入口，不属于研究流程）" placement="right">
              <Link to="/admin" aria-label="后台与实盘">
                <Button type="text" size="small" className="dim" icon={<SettingOutlined />} />
              </Link>
            </Tooltip>
          </div>
        </div>
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
