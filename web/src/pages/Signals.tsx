import { useQuery } from '@tanstack/react-query'
import { Card, Table, Tag, Progress, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import type { Signal } from '@/types'
import { DIRECTION_LABEL, fmtPct } from '@/utils/format'

export default function Signals() {
  const { data, isLoading } = useQuery({ queryKey: ['signals', 50], queryFn: () => api.signals(50) })

  const columns: ColumnsType<Signal> = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 100,
      render: (v: string) => <Link to={`/stock/${v}`}>{v}</Link>,
    },
    {
      title: '方向',
      dataIndex: 'side',
      width: 90,
      filters: [
        { text: '买入', value: 'BUY' },
        { text: '卖出', value: 'SELL' },
      ],
      onFilter: (v, r) => r.side === v,
      render: (v: string) => (
        <Tag color={v === 'BUY' ? 'red' : 'green'}>{DIRECTION_LABEL[v] ?? v}</Tag>
      ),
    },
    {
      title: '信号强度',
      dataIndex: 'strength',
      width: 180,
      sorter: (a, b) => a.strength - b.strength,
      defaultSortOrder: 'descend',
      render: (v: number) => (
        <Tooltip title={v.toFixed(4)}>
          <Progress
            percent={Math.round(v * 100)}
            size="small"
            strokeColor={v >= 0.55 ? '#f5222d' : v <= 0.45 ? '#52c41a' : '#8c8c8c'}
          />
        </Tooltip>
      ),
    },
    {
      title: '置信度',
      dataIndex: 'confidence',
      width: 110,
      align: 'right',
      render: (v: number) => <span className="mono">{fmtPct(v)}</span>,
    },
    {
      title: '触发因子',
      dataIndex: 'trigger_factor',
      render: (v: string) => <span style={{ fontSize: 12, color: '#bbb' }}>{v}</span>,
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 90,
      render: (v: string) => <Tag>{v}</Tag>,
    },
  ]

  return (
    <Card title="信号列表" size="small" extra={<span style={{ fontSize: 12, color: '#8c8c8c' }}>按信号强度降序</span>}>
      <Table
        rowKey="id"
        size="small"
        loading={isLoading}
        columns={columns}
        dataSource={data ?? []}
        pagination={{ pageSize: 15, size: 'small' }}
      />
    </Card>
  )
}
