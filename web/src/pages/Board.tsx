// 盘后看板（/board）—— 借盯盘软件的**视觉密度**，但只读收盘后的日频截面。
//
// 为什么单独开一页，而不是把「市场与池」改密一点：
//   盯盘软件那种密度（指数条 / 温度带 / 热力图 / 多股同列）是有诱惑力的，
//   但它默认携带一整套**暗示**：数据在动、有信号、可以下单。而本项目
//   ——没有 tick / level-5 / 逐笔数据源（只有日频收盘截面）；
//   ——A→B→C 三条收益来源候选全部证伪（见 README 第 7 节）。
//   把密度借过来、把暗示留下，等于用界面复述一个已经被自己否掉的命题。
//
//   所以这一页的判据是「冻结时钟测试」：
//     **同一个交易日内 09:00 打开和 23:00 打开，这页必须长得一模一样。**
//   凡是通不过这条测试的组件（分时图、盘口、秒级刷新、条件单推送）
//   一律不做 —— 不是"以后补"，是**这个数据层支撑不起**。
//
//   页头那行「日频截面 · asof … · 非实时」因此不是免责声明，是**页面规格**：
//   它定义这页是什么，也定义它不承诺什么。
//
// 与 /market 的关系：**同源**（同一份全池快照、同一个 top），只是读法不同。
//   这里刻意共用 react-query 的 ['market'] key ⇒ 两页之间来回切不会重复打后端。
//   /market 负责"分析 + 下钻"，本页负责"一屏扫完今天发生了什么"。
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Col, Empty, Row, Spin, Tooltip } from 'antd'
import ReactECharts from 'echarts-for-react'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import type { IndexQuote, Quote } from '@/types'
import { fmtAmount, fmtPct, pctColor } from '@/utils/format'
import { useChartColors, withAlpha } from '@/theme/tokens'

/** 行业热力图取成交额前多少只（后端 /api/quotes 上限 500） */
const HEAT_N = 500

interface IndNode {
  name: string
  /** 该行业在 HEAT_N 只里的成交额合计 */
  amount: number
  /** 该行业在 HEAT_N 只里的家数 */
  n: number
  /** 按成交额**加权**的涨跌幅 */
  pct: number
}

/** 60 日迷你走势。用 inline SVG 而不是 ECharts：
 *  ① 一行里要放 7~8 个，每个起一个 ECharts 实例太重；
 *  ② SVG 的 stroke 可以直接吃 CSS 变量，主题切换不用重渲染。 */
function Spark({ pts, up }: { pts: number[]; up: boolean }) {
  const w = 66
  const h = 20
  if (pts.length < 2) return <div style={{ width: w, height: h }} />
  const lo = Math.min(...pts)
  const hi = Math.max(...pts)
  const rng = hi - lo || 1
  const d = pts
    .map((v, i) => {
      const x = (i / (pts.length - 1)) * w
      const y = h - 2 - ((v - lo) / rng) * (h - 4)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: 'block', marginTop: 2 }}>
      <polyline
        points={d}
        fill="none"
        stroke={up ? 'var(--aq-up)' : 'var(--aq-down)'}
        strokeWidth={1}
      />
    </svg>
  )
}

/** 一档涨跌幅的"代表值"（%），用于给色块定深浅。开区间取那个有限端点。 */
function bucketMid(lo: number | null, hi: number | null): number {
  if (lo === null && hi === null) return 0
  if (lo === null) return hi as number
  if (hi === null) return lo as number
  return (lo + hi) / 2
}

/** 幅度 → 不透明度（0 附近最淡、越远越浓；上限 0.72 保证块内文字可读）。
 *  ⚠️ pct 与 cap **必须同单位**：分档条传百分数（-8 / 10），行业图传小数（0.012 / 0.03）。 */
function alphaOf(pct: number, cap = 10): number {
  return 0.12 + 0.6 * Math.min(Math.abs(pct) / cap, 1)
}

/** 色块上该用黑字还是白字 —— 看**合成之后的亮度**，不看 alpha。
 *
 *  为什么不能用「alpha > 0.45 就白字」这种阈值：同一个 alpha=0.5，
 *  浅色主题下合成到白底上是**浅粉/浅绿**，深色主题下合成到 #1f1f1f 上是**深色调**，
 *  同一条阈值必然在其中一边翻车。第一版就是这么写的，浅色主题下
 *  "浅绿块 + 白字" 几乎读不出来（截图才发现）。
 *
 *  底色一律传 cc.surface（卡片底色），与 withAlpha() 的合成环境保持一致。 */
function inkOn(hex: string, a: number, surfaceHex: string): string {
  const ch = [0, 1, 2].map((i) => {
    const px = (h: string) => parseInt(h.replace('#', '').slice(i * 2, i * 2 + 2), 16)
    const v = (a * px(hex) + (1 - a) * px(surfaceHex)) / 255
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
  })
  const lum = 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
  return lum > 0.42 ? '#141414' : '#ffffff'
}

/** 多股同列的一列：15 行压到 26px 行高，一屏扫完。
 *
 *  `main` 决定**哪一列当主角**：涨/跌幅榜的主角是涨跌幅（带红绿），
 *  成交额榜的主角是成交额。第一版把三列都写成"主角=涨跌幅 + 副列=成交额"，
 *  于是成交额榜里涨跌幅**出现了两次**（截图才看出来）——数据没错，是排版废话。 */
function RankList({
  title,
  rows,
  main,
}: {
  title: string
  rows: Quote[]
  main: 'pct' | 'amount'
}) {
  return (
    <Card
      size="small"
      title={title}
      styles={{ body: { padding: '6px 0' } }}
      // 没有表头，就得在标题旁边说清两列是什么，否则读的人只能靠颜色猜
      extra={
        <span className="dim" style={{ fontSize: 11 }}>
          {main === 'pct' ? '涨跌幅 ｜ 成交额' : '成交额 ｜ 涨跌幅'}
        </span>
      }
    >
      {rows.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无数据" />
      ) : (
        rows.map((q, i) => (
          <Link key={q.symbol} to={`/stock/${q.symbol}`} className="board-row">
            <span className="mono dim board-rank">{String(i + 1).padStart(2, '0')}</span>
            <span className="board-name" title={q.name}>
              {q.name}
            </span>
            <span className="mono dim board-code">{q.symbol}</span>
            <span className="mono board-val" style={{ color: main === 'pct' ? pctColor(q.pct_chg) : undefined }}>
              {main === 'pct' ? fmtPct(q.pct_chg) : fmtAmount(q.amount)}
            </span>
            <span
              className="mono board-val2"
              style={{ color: main === 'amount' ? pctColor(q.pct_chg) : undefined }}
            >
              {main === 'pct' ? fmtAmount(q.amount) : fmtPct(q.pct_chg)}
            </span>
          </Link>
        ))
      )}
    </Card>
  )
}

export default function Board() {
  // ECharts 读不到 CSS 变量 —— 图表里的文字 / 边框 / tooltip 全部按主题取一份 hex。
  const cc = useChartColors()

  const { data, isLoading, dataUpdatedAt } = useQuery({
    queryKey: ['market'], // 与「市场与池」共用缓存，两页互切不重复扫盘
    queryFn: () => api.marketOverview(15),
  })

  const { data: heat } = useQuery({
    queryKey: ['board-heat', HEAT_N],
    queryFn: () => api.quotes({ sort: 'amount', order: 'desc', limit: HEAT_N }),
  })

  /** 按行业聚合：面积=成交额，颜色=成交额加权涨跌幅。
   *  ⚠️ 权重用成交额而不是等权：等权会把"一堆小票同涨"放大成行业级行情，
   *  而这张图要回答的是"钱往哪儿去了"。 */
  const inds = useMemo<IndNode[]>(() => {
    const m = new Map<string, { amount: number; n: number; wsum: number }>()
    for (const q of heat?.items ?? []) {
      const key = (q.industry ?? '').trim() || '未标注行业'
      const cur = m.get(key) ?? { amount: 0, n: 0, wsum: 0 }
      cur.amount += q.amount
      cur.n += 1
      cur.wsum += q.amount * q.pct_chg
      m.set(key, cur)
    }
    return [...m.entries()]
      .map(([name, v]) => ({
        name,
        amount: v.amount,
        n: v.n,
        pct: v.amount > 0 ? v.wsum / v.amount : 0,
      }))
      .sort((a, b) => b.amount - a.amount)
  }, [heat])

  if (isLoading) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (!data) return <Empty description="暂无数据（快照可能还没建好）" />

  const b = data.breadth
  const u = data.universe
  const total = b.up + b.down + b.flat || 1
  const share = (n: number) => (n / total) * 100

  // ---------------- 行业热力图 ----------------
  // 颜色标尺的上限用 **85 分位**而不是最大值：一个极端行业（小行业 + 单日 20% 波动）
  // 就能把最大值顶得很高，其余几十个行业于是全被压成一片浅色 —— 图还在，信息没了。
  const absP = inds.map((i) => Math.abs(i.pct)).sort((a, b) => a - b)
  const capPct = Math.max(
    0.005,
    absP.length >= 5 ? absP[Math.floor(0.85 * (absP.length - 1))] : (absP[absP.length - 1] ?? 0.005),
  )
  const treeOption = {
    animation: false,
    tooltip: {
      backgroundColor: cc.tooltipBg,
      borderColor: cc.tooltipBorder,
      textStyle: { color: cc.text },
      formatter: (p: { name: string; data?: IndNode }) => {
        const d = p.data
        if (!d || d.amount === undefined) return p.name
        return [
          `<b>${d.name}</b>`,
          `成交额 ${fmtAmount(d.amount)}（${d.n} 只）`,
          `加权涨跌 <span style="color:${d.pct >= 0 ? cc.up : cc.down}">${fmtPct(d.pct)}</span>`,
        ].join('<br/>')
      },
    },
    series: [
      {
        type: 'treemap',
        roam: false,
        nodeClick: false, // 点一下就下钻到"行业内部"会暗示层级里有东西，其实没有
        breadcrumb: { show: false },
        left: 0,
        right: 0,
        top: 0,
        bottom: 0,
        itemStyle: { borderColor: cc.surface, borderWidth: 1, gapWidth: 1 },
        data: inds.map((d) => {
          const a = alphaOf(d.pct, capPct)
          const fill = d.pct >= 0 ? cc.up : cc.down
          return {
            name: d.name,
            value: d.amount,
            amount: d.amount,
            n: d.n,
            pct: d.pct,
            itemStyle: { color: withAlpha(fill, a) },
            label: {
              show: true,
              fontSize: 11,
              overflow: 'truncate',
              color: inkOn(fill, a, cc.surface),
            },
          }
        }),
      },
    ],
  }

  return (
    <div>
      {/* ---------- 页头：这页是什么、不承诺什么 ---------- */}
      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: '10px 14px' } }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 600 }}>盘后看板</span>
          <span className="board-tag">日频截面</span>
          <span className="board-tag">asof {data.asof}</span>
          <span className="board-tag warn">非实时</span>
          <span className="dim" style={{ marginLeft: 'auto', fontSize: 11 }}>
            本页取数于 {new Date(dataUpdatedAt).toLocaleTimeString('zh-CN')}
          </span>
        </div>
        <div className="dim" style={{ marginTop: 6, fontSize: 12, lineHeight: 1.8 }}>
          读的是<b>收盘后的日频截面</b>：同一个交易日内 09:00 打开和 23:00 打开，这页长得一样。
          它<b>不推送、不轮询、不产生任何信号</b>——这是刻意的：本地只有日频收盘数据，
          没有 tick / 逐笔源，任何"实时"数字都会是假的。
        </div>
        <div className="dim" style={{ fontSize: 12, lineHeight: 1.8 }}>
          与「市场与池」<b>同源</b>（同一份全池快照），本页只是密度更高的另一种读法：
          那页负责分析与下钻，这页负责一屏扫完今天发生了什么。要搜索 / 筛选个股请去
          <Link to="/stocks"> 全市场行情</Link>。
        </div>
      </Card>

      {/* ---------- 指数条（盯盘软件顶栏那一行） ---------- */}
      <Card size="small" styles={{ body: { padding: 0 } }} style={{ marginBottom: 12 }}>
        <div style={{ display: 'flex', overflowX: 'auto' }}>
          {data.indices.map((i: IndexQuote) => {
            const up = (i.pct_chg ?? 0) >= 0
            return (
              <div
                key={i.symbol}
                style={{
                  flex: '1 0 150px',
                  padding: '8px 12px',
                  borderRight: '0.5px solid var(--aq-border)',
                }}
              >
                <div className="dim" style={{ fontSize: 11 }}>
                  {i.name}
                </div>
                <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8 }}>
                  <span className="mono" style={{ fontSize: 15 }}>
                    {i.available ? (i.close ?? 0).toFixed(2) : '—'}
                  </span>
                  <span
                    className="mono"
                    style={{
                      fontSize: 12,
                      color: i.available ? pctColor(i.pct_chg ?? 0) : 'var(--aq-text-3)',
                    }}
                  >
                    {i.available ? fmtPct(i.pct_chg ?? 0) : '未落盘'}
                  </span>
                </div>
                {i.available && i.closes ? (
                  <Spark pts={i.closes} up={up} />
                ) : (
                  <div style={{ height: 22 }} />
                )}
              </div>
            )
          })}
        </div>
      </Card>

      {/* ---------- 市场温度带 ---------- */}
      <Card
        size="small"
        title="市场温度带"
        style={{ marginBottom: 12 }}
        extra={
          <span className="dim" style={{ fontSize: 12 }}>
            中位 {fmtPct(b.median_pct)} ｜ 均值 {fmtPct(b.mean_pct)} ｜ 近似涨停{' '}
            <span className="mono">{b.limit_up_approx}</span> / 跌停{' '}
            <span className="mono">{b.limit_down_approx}</span>
          </span>
        }
      >
        <div
          style={{
            display: 'flex',
            height: 30,
            borderRadius: 6,
            overflow: 'hidden',
            border: '0.5px solid var(--aq-border)',
          }}
        >
          <div
            style={{ width: `${share(b.up)}%`, background: 'var(--aq-up)' }}
            title={`上涨 ${b.up} 只`}
          />
          <div
            style={{ width: `${share(b.flat)}%`, background: 'var(--aq-flat)' }}
            title={`平盘 ${b.flat} 只`}
          />
          <div
            style={{ width: `${share(b.down)}%`, background: 'var(--aq-down)' }}
            title={`下跌 ${b.down} 只`}
          />
        </div>
        <div style={{ display: 'flex', gap: 16, marginTop: 8, fontSize: 12 }}>
          <span>
            <span style={{ color: 'var(--aq-up)' }}>● 上涨</span>{' '}
            <span className="mono">{b.up}</span>{' '}
            <span className="dim">({share(b.up).toFixed(1)}%)</span>
          </span>
          <span>
            <span style={{ color: 'var(--aq-flat)' }}>● 平盘</span>{' '}
            <span className="mono">{b.flat}</span>
          </span>
          <span>
            <span style={{ color: 'var(--aq-down)' }}>● 下跌</span>{' '}
            <span className="mono">{b.down}</span>{' '}
            <span className="dim">({share(b.down).toFixed(1)}%)</span>
          </span>
          <span className="dim">合计 {u.n_active} 只（停牌 {u.n_suspended} 只单列）</span>
        </div>
      </Card>

      {/* ---------- 涨跌幅分档热力条 ---------- */}
      <Card
        size="small"
        title="涨跌幅分档（格子深浅 = 离 0 的远近，数字 = 家数）"
        style={{ marginBottom: 12 }}
      >
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(12, 1fr)', gap: 3 }}>
          {data.distribution.map((d) => {
            const mid = bucketMid(d.lo, d.hi)
            const a = alphaOf(mid)
            const fill = mid <= 0 ? cc.down : cc.up
            return (
              <Tooltip
                key={d.label}
                title={`${d.label} ｜ ${d.count} 只（占 ${((d.count / total) * 100).toFixed(1)}%）`}
              >
                <div
                  // 类名只为可验证性而存在：端到端自检要能直接读这一格的
                  // 实际前景/背景色（断言"浅色块上不是白字"），否则只能靠肉眼看截图。
                  className="board-heat"
                  style={{
                    height: 58,
                    borderRadius: 4,
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: withAlpha(fill, a),
                    color: inkOn(fill, a, cc.surface),
                    cursor: 'help',
                  }}
                >
                  <span className="mono" style={{ fontSize: 14 }}>
                    {d.count}
                  </span>
                  <span style={{ fontSize: 10, opacity: 0.85 }}>{d.label}</span>
                </div>
              </Tooltip>
            )
          })}
        </div>
      </Card>

      {/* ---------- 行业热力图 ---------- */}
      <Card
        size="small"
        style={{ marginBottom: 12 }}
        title={`行业热力图 · 成交额前 ${HEAT_N} 只`}
        extra={
          <span className="dim" style={{ fontSize: 12 }}>
            面积 = 成交额合计 ｜ 颜色 = <b>成交额加权</b>涨跌幅 ｜ 图内 {inds.length} 个行业
          </span>
        }
      >
        {inds.length > 0 ? (
          <ReactECharts option={treeOption} style={{ height: 320 }} notMerge />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="行业数据未落盘" />
        )}
        <div className="dim" style={{ fontSize: 12, lineHeight: 1.8, marginTop: 4 }}>
          行业取自证监会行业分类（本地 <span className="mono">stock_list.parquet</span>）。
          本图<b>只覆盖成交额排名前 {HEAT_N} 只</b>（全市场 {u.n_active} 只），
          排名 {HEAT_N} 名以外的长尾<b>不出现</b>——所以它能说明"大钱今天在哪些行业里动"，
          <b>不能</b>据此推断全市场行业强弱。加权用成交额而不是等权：
          等权会把"一堆小票同涨"放大成行业级行情。
        </div>
      </Card>

      {/* ---------- 多股同列：三榜并排 ---------- */}
      <Row gutter={[12, 12]}>
        <Col xs={24} md={8}>
          <RankList title="涨幅榜" rows={data.top_gainers} main="pct" />
        </Col>
        <Col xs={24} md={8}>
          <RankList title="跌幅榜" rows={data.top_losers} main="pct" />
        </Col>
        <Col xs={24} md={8}>
          <RankList title="成交额榜" rows={data.top_amount} main="amount" />
        </Col>
      </Row>

      <div className="dim" style={{ marginTop: 10, fontSize: 12, lineHeight: 1.8 }}>
        <div>
          口径：现价 = 后复权价 ÷ 复权因子；涨跌幅 = close ÷ pre_close − 1（除权日不失真）。
          涨跌家数只统计最新交易日<b>有成交</b>的 {u.n_active} 只（停牌 {u.n_suspended} 只单列）。
        </div>
        <div>
          已知局限：ST 过滤用的是<b>当前名称快照</b>（时变 ST 数据覆盖 12.9%，见「研究进展」页）；
          「近似涨/跌停」按 |涨跌幅| ≥ 9.8% 统计，<b>刻意不用</b> bars 的 limit_up/limit_down 字段
          （该字段口径已被证伪）。
        </div>
      </div>
    </div>
  )
}
