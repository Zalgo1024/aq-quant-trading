import { useQuery } from '@tanstack/react-query'
import { Card, Input, Table, Tag, Progress, Space, Tooltip, Button } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link, useNavigate } from 'react-router-dom'
import { useMemo, useState } from 'react'
import api from '@/services/api'
import type { Signal } from '@/types'
import { DIRECTION_LABEL, fmtPct } from '@/utils/format'

export default function StockList() {
  const nav = useNavigate()
  const [kw, setKw] = useState('')

  const { data: stocks } = useQuery({ queryKey: ['stocks'], queryFn: api.stocks })
  // 用 signals 接口拿到模型评分（含方向与置信度）
  const { data: preds, isLoading } = useQuery({
    queryKey: ['signals-all'],
    queryFn: () => api.signals(30),
  })

  const nameOf = useMemo(() => {
    const m: Record<string, string> = {}
    ;(stocks ?? []).forEach((s) => (m[s.symbol] = s.name))
    return m
  }, [stocks])

  const industryOf = useMemo(() => {
    const m: Record<string, string> = {}
    ;(stocks ?? []).forEach((s) => (m[s.symbol] = s.industry ?? '-'))
    return m
  }, [stocks])

  const rows: Signal[] = preds ?? []

  const columns: ColumnsType<any> = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 90,
      render: (v: string) => <Link to={`/stock/${v}`}>{v}</Link>,
    },
    {
      title: '名称',
      width: 110,
      render: (_: unknown, r: any) => nameOf[r.symbol] ?? r.symbol,
    },
    {
      title: '行业',
      width: 100,
      render: (_: unknown, r: any) => industryOf[r.symbol] ?? '-',
    },
    {
      title: 'AI 评分',
      dataIndex: 'strength',
      width: 160,
      sorter: (a: any, b: any) => (a.strength ?? 0) - (b.strength ?? 0),
      defaultSortOrder: 'descend',
      render: (v: number) => (
        <Tooltip title={v?.toFixed(4)}>
          <Progress
            percent={Math.round((v ?? 0) * 100)}
            size="small"
            strokeColor={v >= 0.55 ? '#f5222d' : v <= 0.45 ? '#52c41a' : '#8c8c8c'}
            format={(p) => `${p}`}
          />
        </Tooltip>
      ),
    },
    {
      title: '置信度',
      dataIndex: 'confidence',
      width: 100,
      align: 'right',
      render: (v: number) => <span className="mono">{fmtPct(v)}</span>,
    },
    {
      title: '方向',
      dataIndex: 'side',
      width: 90,
      filters: [
        { text: '买入', value: 'BUY' },
        { text: '卖出', value: 'SELL' },
      ],
      onFilter: (v: any, r: any) => r.side === v,
      render: (v: string) => (
        <Tag color={v === 'BUY' ? 'red' : v === 'SELL' ? 'green' : 'default'}>
          {DIRECTION_LABEL[v] ?? v}
        </Tag>
      ),
    },
    {
      title: '主因子',
      dataIndex: 'trigger_factor',
      width: 140,
      render: (v: string) => <span style={{ fontSize: 12, color: '#8c8c8c' }}>{v}</span>,
    },
    {
      title: '操作',
      width: 90,
      render: (_: unknown, r: any) => (
        <Button type="link" size="small" onClick={() => nav(`/stock/${r.symbol}`)}>
          详情
        </Button>
      ),
    },
  ]

  const filtered = rows.filter((r: any) => {
    if (!kw) return true
    const n = nameOf[r.symbol] ?? ''
    return r.symbol.includes(kw) || n.includes(kw)
  })

  return (
    <Card
      title="自选 / AI 选股"
      size="small"
      extra={
        <Space>
          <Input.Search
            placeholder="搜索代码或名称"
            allowClear
            style={{ width: 220 }}
            onChange={(e) => setKw(e.target.value.trim())}
          />
        </Space>
      }
    >
      <Table
        rowKey="symbol"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={filtered}
        pagination={{ pageSize: 15, size: 'small', showSizeChanger: false }}
      />
    </Card>
  )
}
