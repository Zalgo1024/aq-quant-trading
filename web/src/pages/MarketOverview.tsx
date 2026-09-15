import { useQuery } from '@tanstack/react-query'
import { Card, Col, Row, Statistic, Table, Tag, Spin, Empty, List, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import ReactECharts from 'echarts-for-react'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import type { Quote } from '@/types'
import { fmtAmount, fmtPct, pctColor } from '@/utils/format'

export default function MarketOverview() {
  const { data, isLoading } = useQuery({ queryKey: ['market'], queryFn: api.marketOverview })
  const { data: signals } = useQuery({ queryKey: ['signals'], queryFn: () => api.signals(8) })

  if (isLoading) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (!data) return <Empty description="暂无数据" />

  const columns: ColumnsType<Quote> = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 90,
      render: (v: string, r) => <Link to={`/stock/${v}`}>{v}</Link>,
    },
    { title: '名称', dataIndex: 'name', width: 110 },
    {
      title: '现价',
      dataIndex: 'close',
      width: 90,
      align: 'right',
      render: (v: number) => <span className="mono">{v.toFixed(2)}</span>,
    },
    {
      title: '涨跌幅',
      dataIndex: 'pct_chg',
      width: 100,
      align: 'right',
      sorter: (a: Quote, b: Quote) => a.pct_chg - b.pct_chg,
      defaultSortOrder: 'descend',
      render: (v: number) => (
        <span className="mono" style={{ color: pctColor(v) }}>
          {fmtPct(v)}
        </span>
      ),
    },
    {
      title: '成交额',
      dataIndex: 'amount',
      width: 100,
      align: 'right',
      render: (v: number) => <span className="mono">{fmtAmount(v)}</span>,
    },
  ]

  // 涨跌家数饼图（红涨绿跌）
  const pieOption = {
    tooltip: { trigger: 'item' },
    series: [
      {
        type: 'pie',
        radius: ['45%', '70%'],
        label: { color: '#e8e8e8', formatter: '{b}\n{c}' },
        data: [
          { value: data.up_count, name: '上涨', itemStyle: { color: '#f5222d' } },
          { value: data.down_count, name: '下跌', itemStyle: { color: '#52c41a' } },
          { value: data.flat_count, name: '平盘', itemStyle: { color: '#8c8c8c' } },
        ],
      },
    ],
  }

  return (
    <div>
      {/* 指数卡片 */}
      <Row gutter={[16, 16]}>
        {data.indices.map((idx) => (
          <Col xs={24} sm={12} md={8} key={idx.symbol}>
            <Card size="small" styles={{ body: { padding: 16 } }}>
              <Statistic
                title={idx.name}
                value={idx.pct_chg * 100}
                precision={2}
                suffix="%"
                valueStyle={{ color: pctColor(idx.pct_chg), fontFamily: 'monospace' }}
              />
            </Card>
          </Col>
        ))}
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        {/* 涨跌家数 */}
        <Col xs={24} md={8}>
          <Card title="涨跌家数" size="small">
            <ReactECharts option={pieOption} style={{ height: 240 }} />
          </Card>
        </Col>

        {/* 今日信号 */}
        <Col xs={24} md={16}>
          <Card
            title="AI 信号（Top 8）"
            size="small"
            extra={<Link to="/signals">全部 &gt;</Link>}
          >
            <List
              size="small"
              dataSource={signals ?? []}
              locale={{ emptyText: '暂无信号' }}
              renderItem={(s) => (
                <List.Item>
                  <Link to={`/stock/${s.symbol}`} style={{ flex: 1 }}>
                    {s.symbol}
                  </Link>
                  <Tag color={s.side === 'BUY' ? 'red' : 'green'}>
                    {s.side === 'BUY' ? '买入' : '卖出'}
                  </Tag>
                  <Typography.Text type="secondary" style={{ fontSize: 12, width: 110 }}>
                    {s.trigger_factor}
                  </Typography.Text>
                  <span className="mono">{(s.strength * 100).toFixed(1)}</span>
                </List.Item>
              )}
            />
          </Card>
        </Col>
      </Row>

      {/* 行情表 */}
      <Card title="个股行情" size="small" style={{ marginTop: 16 }}>
        <Table
          rowKey="symbol"
          size="small"
          columns={columns}
          dataSource={data.quotes}
          pagination={{ pageSize: 10, size: 'small' }}
        />
      </Card>
    </div>
  )
}
