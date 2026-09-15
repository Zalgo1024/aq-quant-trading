import { useQuery } from '@tanstack/react-query'
import { Card, Table, Tag, Space, Alert } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import type { Anomaly } from '@/types'
import { SEVERITY_LABEL } from '@/utils/format'

export default function AnomalyPage() {
  const { data, isLoading } = useQuery({ queryKey: ['anomaly'], queryFn: api.anomaly })

  const columns: ColumnsType<Anomaly> = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 100,
      render: (v: string) => <Link to={`/stock/${v}`}>{v}</Link>,
    },
    { title: '类型', dataIndex: 'type', width: 110 },
    {
      title: 'z-score',
      dataIndex: 'z_score',
      width: 110,
      align: 'right',
      sorter: (a, b) => Math.abs(a.z_score) - Math.abs(b.z_score),
      defaultSortOrder: 'descend',
      render: (v: number) => (
        <span className="mono" style={{ color: Math.abs(v) >= 3 ? '#f5222d' : '#faad14' }}>
          {v.toFixed(2)}
        </span>
      ),
    },
    {
      title: '级别',
      dataIndex: 'severity',
      width: 90,
      filters: [
        { text: '严重', value: 'severe' },
        { text: '警告', value: 'warn' },
        { text: '提示', value: 'info' },
      ],
      onFilter: (v, r) => r.severity === v,
      render: (v: string) => (
        <Tag color={v === 'severe' ? 'red' : v === 'warn' ? 'orange' : 'default'}>
          {SEVERITY_LABEL[v] ?? v}
        </Tag>
      ),
    },
    { title: '详情', dataIndex: 'detail' },
  ]

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="异动检测说明"
        description="基于滚动窗口 z-score 检测当日价格异动（复用统计异常检测思路）。P1 起将叠加量能异动、Benford 定律、卡方检验等多维检测。"
      />
      <Card title="异动预警" size="small">
        <Table
          rowKey={(r) => `${r.symbol}-${r.time}`}
          size="small"
          loading={isLoading}
          columns={columns}
          dataSource={data ?? []}
          locale={{ emptyText: '当前无显著异动' }}
          pagination={{ pageSize: 15, size: 'small' }}
        />
      </Card>
    </div>
  )
}
