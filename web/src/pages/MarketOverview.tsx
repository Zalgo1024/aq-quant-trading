import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Row,
  Segmented,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import ReactECharts from 'echarts-for-react'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import type { DistBucket, IndexQuote, Quote } from '@/types'
import { BOARD_LABEL, fmtAmount, fmtCount, fmtPct, pctColor } from '@/utils/format'
import { useChartColors, withAlpha } from '@/theme/tokens'

/** 分布柱 -> 查询区间（后端要小数，左闭右开） */
function bucketToRange(b: DistBucket | null) {
  if (!b) return {}
  return {
    min_pct: b.lo === null ? undefined : b.lo / 100,
    max_pct: b.hi === null ? undefined : b.hi / 100,
  }
}

export default function MarketOverview() {
  const qc = useQueryClient()
  // ECharts 读不到 CSS 变量 —— 轴文字 / 分割线 / tooltip 全部按主题取一份 hex。
  // 旧实现把这三样硬编码成深色版：浅色主题下图例文字 #ccc 落在白底上几乎读不出来。
  const cc = useChartColors()
  const AXIS = { color: cc.textSecondary, fontSize: 10 }
  const SPLIT = { lineStyle: { color: cc.split } }
  const [selIdx, setSelIdx] = useState<string>('')
  const [bucket, setBucket] = useState<DistBucket | null>(null)
  const [kw, setKw] = useState('')
  const [board, setBoard] = useState('')
  const [sort, setSort] = useState('amount')
  const [rebuilding, setRebuilding] = useState(false)

  const { data, isLoading, dataUpdatedAt } = useQuery({
    queryKey: ['market'],
    queryFn: () => api.marketOverview(15),
  })

  const range = bucketToRange(bucket)
  const { data: quotes, isFetching: qLoading } = useQuery({
    queryKey: ['quotes', kw, board, sort, bucket?.label ?? ''],
    queryFn: () =>
      api.quotes({
        keyword: kw,
        board,
        sort,
        order: sort === 'pct_chg' ? 'desc' : 'desc',
        limit: 100,
        ...range,
      }),
    enabled: !!data,
  })

  const idx: IndexQuote | undefined = useMemo(() => {
    if (!data) return undefined
    const avail = data.indices.filter((i) => i.available)
    return avail.find((i) => i.symbol === selIdx) ?? avail[0]
  }, [data, selIdx])

  if (isLoading) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (!data) return <Empty description="暂无数据（快照可能还没建好）" />

  const { breadth: b, universe: u } = data

  // ---------------- 指数走势 -----------------
  const idxOption = idx?.closes
    ? {
        animation: false,
        tooltip: {
          trigger: 'axis',
          backgroundColor: cc.tooltipBg,
          borderColor: cc.tooltipBorder,
          textStyle: { color: cc.text },
        },
        grid: { left: 60, right: 20, top: 16, bottom: 28 },
        xAxis: {
          type: 'category',
          data: idx.spark_dates ?? [],
          axisLabel: { ...AXIS, interval: Math.floor((idx.spark_dates?.length ?? 60) / 6) },
          axisLine: { lineStyle: { color: cc.split } },
        },
        yAxis: { type: 'value', scale: true, splitLine: SPLIT, axisLabel: AXIS },
        series: [
          {
            type: 'line',
            name: idx.name,
            data: idx.closes,
            showSymbol: false,
            smooth: true,
            lineStyle: { color: (idx.pct_chg ?? 0) >= 0 ? cc.up : cc.down, width: 2 },
            areaStyle: {
              color: withAlpha((idx.pct_chg ?? 0) >= 0 ? cc.up : cc.down, 0.1),
            },
          },
        ],
      }
    : null

  // ---------------- 涨跌家数 -----------------
  const donut = {
    tooltip: { trigger: 'item', backgroundColor: cc.tooltipBg, textStyle: { color: cc.text } },
    series: [
      {
        type: 'pie',
        radius: ['48%', '72%'],
        avoidLabelOverlap: true,
        label: { color: cc.textSecondary, fontSize: 11, formatter: '{b}\n{c}' },
        data: [
          { value: b.up, name: '上涨', itemStyle: { color: cc.up } },
          { value: b.down, name: '下跌', itemStyle: { color: cc.down } },
          { value: b.flat, name: '平盘', itemStyle: { color: cc.flat } },
        ],
      },
    ],
  }

  // ---------------- 涨跌分布（可点击下钻）-----------------
  const distOption = {
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: cc.tooltipBg,
      borderColor: cc.tooltipBorder,
      textStyle: { color: cc.text },
      formatter: (ps: { name: string; value: number }[]) =>
        `${ps[0].name}<br/>${ps[0].value} 只（点击下钻）`,
    },
    grid: { left: 44, right: 12, top: 16, bottom: 30 },
    xAxis: {
      type: 'category',
      data: data.distribution.map((d) => d.label),
      axisLabel: { ...AXIS, rotate: 40, fontSize: 9 },
      axisLine: { lineStyle: { color: cc.split } },
    },
    yAxis: { type: 'value', splitLine: SPLIT, axisLabel: AXIS },
    series: [
      {
        type: 'bar',
        data: data.distribution.map((d) => ({
          value: d.count,
          itemStyle: {
            color: withAlpha(
              d.label.startsWith('-') || d.label.startsWith('<') ? cc.down : cc.up,
              0.85,
            ),
            borderColor: bucket?.label === d.label ? cc.text : 'transparent',
            borderWidth: bucket?.label === d.label ? 2 : 0,
          },
        })),
      },
    ],
  }

  // ---------------- 榜单 -----------------
  const listCols: ColumnsType<Quote> = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 84,
      render: (v: string) => <Link to={`/stock/${v}`} className="mono">{v}</Link>,
    },
    { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
    {
      title: '板块',
      dataIndex: 'board',
      width: 70,
      render: (v: string) => <Tag style={{ fontSize: 11 }}>{BOARD_LABEL[v] ?? v}</Tag>,
    },
    {
      title: '现价',
      dataIndex: 'close',
      width: 84,
      align: 'right',
      render: (v: number) => <span className="mono">{v.toFixed(2)}</span>,
    },
    {
      title: '涨跌幅',
      dataIndex: 'pct_chg',
      width: 88,
      align: 'right',
      render: (v: number) => (
        <span className="mono" style={{ color: pctColor(v) }}>{fmtPct(v)}</span>
      ),
    },
    {
      title: '成交额',
      dataIndex: 'amount',
      width: 96,
      align: 'right',
      render: (v: number) => <span className="mono">{fmtAmount(v)}</span>,
    },
  ]

  const tableCols: ColumnsType<Quote> = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 84,
      fixed: 'left',
      render: (v: string) => <Link to={`/stock/${v}`} className="mono">{v}</Link>,
    },
    { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
    {
      title: '板块',
      dataIndex: 'board',
      width: 74,
      render: (v: string) => <Tag style={{ fontSize: 11 }}>{BOARD_LABEL[v] ?? v}</Tag>,
    },
    {
      title: '行业',
      dataIndex: 'industry',
      width: 150,
      ellipsis: true,
      render: (v: string) => <span style={{ fontSize: 12, color: '#999' }}>{v || '-'}</span>,
    },
    {
      title: '现价',
      dataIndex: 'close',
      width: 90,
      align: 'right',
      sorter: (a, b2) => a.close - b2.close,
      render: (v: number) => <span className="mono">{v.toFixed(2)}</span>,
    },
    {
      title: '涨跌幅',
      dataIndex: 'pct_chg',
      width: 96,
      align: 'right',
      defaultSortOrder: 'descend',
      sorter: (a, b2) => a.pct_chg - b2.pct_chg,
      render: (v: number) => (
        <span className="mono" style={{ color: pctColor(v) }}>{fmtPct(v)}</span>
      ),
    },
    {
      title: '成交额',
      dataIndex: 'amount',
      width: 100,
      align: 'right',
      sorter: (a, b2) => a.amount - b2.amount,
      render: (v: number) => <span className="mono">{fmtAmount(v)}</span>,
    },
    {
      title: '异动z',
      dataIndex: 'z_score',
      width: 80,
      align: 'right',
      render: (v: number) => (
        <Tooltip title="当日收益相对自身前 60 日分布的偏离（σ）">
          <span
            className="mono"
            style={{ color: Math.abs(v) >= 3 ? 'var(--aq-cred-strong)' : 'var(--aq-text-3)' }}
          >
            {v.toFixed(2)}
          </span>
        </Tooltip>
      ),
    },
    {
      title: '流动性池',
      dataIndex: 'in_liquid',
      width: 90,
      align: 'center',
      filters: [
        { text: '在池内', value: true },
        { text: '不在池', value: false },
      ],
      onFilter: (val, r) => r.in_liquid === val,
      render: (v: boolean) => (v ? <Tag color="blue">在池</Tag> : <Tag>—</Tag>),
    },
  ]

  const onRebuild = async () => {
    setRebuilding(true)
    try {
      const r = await api.rebuildSnapshot()
      message.success(`快照已重建：${r.n_symbols} 只，用时 ${r.seconds}s`)
      await qc.invalidateQueries()
    } catch (e) {
      message.error(`重建失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRebuilding(false)
    }
  }

  const totalBreadth = b.up + b.down + b.flat || 1

  return (
    <div>
      {/* ---------- 状态条：数据截至 / 池规模 / 刷新 ---------- */}
      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: '10px 16px' } }}>
        <Space size={24} wrap>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              数据截至
            </Typography.Text>{' '}
            <span className="mono" style={{ fontSize: 13 }}>{data.asof}</span>
          </span>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              全市场
            </Typography.Text>{' '}
            <span className="mono">{u.n_active}</span>{' '}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              只活跃 / 停牌 {u.n_suspended}
            </Typography.Text>
          </span>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              流动性池（主口径）
            </Typography.Text>{' '}
            <span className="mono">{u.n_liquid_snapshot}</span>
          </span>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              页面取数于
            </Typography.Text>{' '}
            <span className="mono" style={{ fontSize: 12 }}>
              {new Date(dataUpdatedAt).toLocaleTimeString('zh-CN')}
            </span>
          </span>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={rebuilding}
            onClick={onRebuild}
          >
            重建快照
          </Button>
          <Tooltip title={data.caliber_note}>
            <Tag color="default" style={{ cursor: 'help' }}>
              口径说明
            </Tag>
          </Tooltip>
        </Space>
      </Card>

      {/* ---------- 指数卡（点击切换下方走势）---------- */}
      <Row gutter={[12, 12]}>
        {data.indices.map((i) => {
          const active = idx?.symbol === i.symbol
          return (
            <Col xs={12} sm={8} md={4} key={i.symbol}>
              <Card
                size="small"
                hoverable={i.available}
                onClick={() => i.available && setSelIdx(i.symbol)}
                styles={{ body: { padding: '10px 12px' } }}
                style={{
                  borderColor: active ? '#1668dc' : undefined,
                  opacity: i.available ? 1 : 0.55,
                }}
              >
                <div style={{ fontSize: 12, color: '#999' }}>{i.name}</div>
                {i.available ? (
                  <Statistic
                    value={i.pct_chg! * 100}
                    precision={2}
                    suffix="%"
                    valueStyle={{
                      color: pctColor(i.pct_chg!),
                      fontFamily: 'monospace',
                      fontSize: 20,
                    }}
                  />
                ) : (
                  <div style={{ marginTop: 8 }}>
                    <Tag color="default">未落盘</Tag>
                  </div>
                )}
                {i.available && (
                  <div className="mono dim" style={{ fontSize: 12 }}>
                    {i.close?.toFixed(2)}
                  </div>
                )}
              </Card>
            </Col>
          )
        })}
      </Row>

      {/* ---------- 指数走势 + 涨跌家数 ---------- */}
      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={16}>
          <Card
            size="small"
            title={idx ? `${idx.name} · 近 60 个交易日` : '指数走势'}
            extra={
              idx && (
                <span className="mono" style={{ fontSize: 12, color: pctColor(idx.pct_chg ?? 0) }}>
                  当日 {fmtPct(idx.pct_chg ?? 0)} ｜ 60日 {fmtPct(idx.range_60d ?? 0)}
                </span>
              )
            }
          >
            {idxOption ? (
              <ReactECharts option={idxOption} style={{ height: 260 }} notMerge />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="指数行情未落盘" />
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card
            size="small"
            title="涨跌家数"
            extra={
              <span className="dim" style={{ fontSize: 12 }}>
                中位 {fmtPct(b.median_pct)}
              </span>
            }
          >
            <Row>
              <Col span={14}>
                <ReactECharts option={donut} style={{ height: 200 }} />
              </Col>
              <Col span={10}>
                <div style={{ paddingTop: 16, fontSize: 12, lineHeight: 2 }}>
                  <div>
                    <span style={{ color: 'var(--aq-up)' }}>● 上涨</span>{' '}
                    <span className="mono">{b.up}</span>{' '}
                    <Typography.Text type="secondary">
                      ({((b.up / totalBreadth) * 100).toFixed(1)}%)
                    </Typography.Text>
                  </div>
                  <div>
                    <span style={{ color: 'var(--aq-down)' }}>● 下跌</span>{' '}
                    <span className="mono">{b.down}</span>{' '}
                    <Typography.Text type="secondary">
                      ({((b.down / totalBreadth) * 100).toFixed(1)}%)
                    </Typography.Text>
                  </div>
                  <div>
                    <span style={{ color: 'var(--aq-flat)' }}>● 平盘</span>{' '}
                    <span className="mono">{b.flat}</span>
                  </div>
                  <div className="dim" style={{ marginTop: 8, fontSize: 11 }}>
                    近似涨/跌停 <span className="mono">{b.limit_up_approx}</span> /{' '}
                    <span className="mono">{b.limit_down_approx}</span>
                  </div>
                </div>
              </Col>
            </Row>
          </Card>
        </Col>
      </Row>

      {/* ---------- 涨跌分布（点柱子下钻）---------- */}
      <Card
        size="small"
        title="涨跌分布（点击柱子下钻到该区间个股）"
        style={{ marginTop: 12 }}
        extra={
          bucket && (
            <Space size={8}>
              <Tag color="blue">已筛选：{bucket.label}</Tag>
              <Button size="small" type="link" onClick={() => setBucket(null)}>
                清除
              </Button>
            </Space>
          )
        }
      >
        <ReactECharts
          option={distOption}
          style={{ height: 200 }}
          notMerge
          onEvents={{
            click: (p: { name: string }) => {
              const hit = data.distribution.find((d) => d.label === p.name)
              if (!hit) return
              setBucket(bucket?.label === hit.label ? null : hit)
            },
          }}
        />
      </Card>

      {/* ---------- 三榜 ---------- */}
      <Card size="small" style={{ marginTop: 12 }}>
        <Tabs
          size="small"
          items={[
            {
              key: 'gain',
              label: `涨幅榜 Top${data.top_gainers.length}`,
              children: (
                <Table
                  rowKey="symbol"
                  size="small"
                  columns={listCols}
                  dataSource={data.top_gainers}
                  pagination={false}
                />
              ),
            },
            {
              key: 'lose',
              label: `跌幅榜 Top${data.top_losers.length}`,
              children: (
                <Table
                  rowKey="symbol"
                  size="small"
                  columns={listCols}
                  dataSource={data.top_losers}
                  pagination={false}
                />
              ),
            },
            {
              key: 'amount',
              label: `成交额榜 Top${data.top_amount.length}`,
              children: (
                <Table
                  rowKey="symbol"
                  size="small"
                  columns={listCols}
                  dataSource={data.top_amount}
                  pagination={false}
                />
              ),
            },
          ]}
        />
      </Card>

      {/* ---------- 全市场个股表（真实全池 + 搜索 + 板块 + 分布下钻）---------- */}
      <Card
        size="small"
        title="全市场个股"
        style={{ marginTop: 12 }}
        extra={
          <Space wrap>
            <Segmented
              size="small"
              value={board}
              onChange={(v) => setBoard(String(v))}
              options={[
                { label: '全部', value: '' },
                { label: '主板', value: 'MAIN' },
                { label: '创业板', value: 'CHINEXT' },
                { label: '科创板', value: 'STAR' },
                { label: '北交所', value: 'BSE' },
              ]}
            />
            <Segmented
              size="small"
              value={sort}
              onChange={(v) => setSort(String(v))}
              options={[
                { label: '按成交额', value: 'amount' },
                { label: '按涨跌幅', value: 'pct_chg' },
                { label: '按代码', value: 'symbol' },
              ]}
            />
            <Input
              size="small"
              allowClear
              prefix={<SearchOutlined />}
              placeholder="代码 / 名称"
              style={{ width: 160 }}
              onChange={(e) => setKw(e.target.value.trim())}
            />
          </Space>
        }
      >
        {bucket && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 10 }}
            message={`已下钻：涨跌幅 ${bucket.label}（共 ${bucket.count} 只）`}
            action={
              <Button size="small" type="link" onClick={() => setBucket(null)}>
                清除筛选
              </Button>
            }
          />
        )}
        <Table
          rowKey="symbol"
          size="small"
          loading={qLoading}
          columns={tableCols}
          dataSource={quotes?.items ?? []}
          scroll={{ x: 1100 }}
          pagination={{
            pageSize: 20,
            size: 'small',
            showSizeChanger: false,
            showTotal: (t) =>
              `共 ${quotes?.total ?? 0} 只（当前页 ${quotes?.items.length ?? 0} 条，服务端按条件返回前 100）`,
          }}
        />
      </Card>

      <div className="dim" style={{ marginTop: 10, fontSize: 12, lineHeight: 1.8 }}>
        <div>
          口径：现价 = 后复权价 ÷ 复权因子；涨跌幅 = close ÷ pre_close − 1（除权日不失真）。
          涨跌家数只统计最新交易日**有成交**的 {u.n_active} 只（停牌 {u.n_suspended} 只单列）。
        </div>
        <div>
          已知局限：ST 过滤用的是**当前名称快照**（时变 ST 数据覆盖 12.9%，见「研究进展」页）；
          「近似涨/跌停」按 |涨跌幅| ≥ 9.8% 统计，**刻意不用** bars 的 limit_up/limit_down
          字段（该字段口径已被证伪）。全市场只数合计 {fmtCount(u.n_symbols)}。
        </div>
      </div>
    </div>
  )
}
