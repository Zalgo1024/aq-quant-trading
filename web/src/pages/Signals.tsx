import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Col, Row, Segmented, Space, Statistic, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import { useState } from 'react'
import api from '@/services/api'
import type { Signal } from '@/types'
import { DIRECTION_LABEL, fmtPct } from '@/utils/format'

/**
 * 因子打分榜。
 *
 * 页面标题刻意不叫「AI 选股」：本项目的多因子加权在研究中**已被证伪**，
 * 分数是研究中间产物。把局限写在最显眼处，比在角落放一行小字诚实得多
 * —— 否则用户会把它当推荐，而它并没有被证明有预测力。
 */
export default function Signals() {
  const [side, setSide] = useState<'ALL' | 'BUY' | 'SELL'>('ALL')
  const { data, isLoading } = useQuery({
    queryKey: ['signals', 50],
    queryFn: () => api.signals(50),
  })

  const rows = (data?.signals ?? []).filter((s) => side === 'ALL' || s.side === side)

  const columns: ColumnsType<Signal> = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 92,
      render: (v: string) => <Link to={`/stock/${v}`} className="mono">{v}</Link>,
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 110,
      ellipsis: true,
      render: (v: string | undefined, r: Signal) => <Link to={`/stock/${r.symbol}`}>{v ?? r.symbol}</Link>,
    },
    {
      title: '方向',
      dataIndex: 'side',
      width: 84,
      render: (v: string) => (
        <Tag color={v === 'BUY' ? 'red' : 'green'}>{DIRECTION_LABEL[v] ?? v}</Tag>
      ),
    },
    {
      title: '打分',
      dataIndex: 'strength',
      width: 110,
      align: 'right',
      sorter: (a, b) => a.strength - b.strength,
      defaultSortOrder: 'descend',
      render: (v: number) => (
        <Tooltip title={`横截面得分（0~1，0.5 为中性）${v.toFixed(6)}`}>
          <span
            className="mono"
            style={{ color: v >= 0.55 ? '#f5222d' : v <= 0.45 ? '#52c41a' : '#8c8c8c' }}
          >
            {v.toFixed(4)}
          </span>
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
      title: '主因子',
      dataIndex: 'trigger_factor',
      render: (v: string) => <span style={{ fontSize: 12, color: '#999' }}>{v}</span>,
    },
  ]

  return (
    <div>
      {/* 局限声明放最上面。这条不是客套话：模型确实被证伪了。 */}
      {data?.caveat && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="重要：本页分数尚未被证明有预测力（研究结论：已证伪）"
          description={data.caveat}
        />
      )}

      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: '10px 16px' } }}>
        <Space size={28} wrap>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              数据截至
            </Typography.Text>{' '}
            <span className="mono">{data?.asof ?? '—'}</span>
          </span>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              打分样本
            </Typography.Text>{' '}
            <span className="mono">{data?.sample_size ?? 0}</span>{' '}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              / 池内 {data?.universe_total ?? 0} 只
            </Typography.Text>
          </span>
          <Tooltip title={data?.sample_caliber}>
            <span style={{ cursor: 'help' }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                权重来源
              </Typography.Text>{' '}
              <Tag color="blue">{data?.weight_source ?? '-'}</Tag>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                活跃因子 <span className="mono">{data?.n_active_factors ?? 0}</span> 个
              </Typography.Text>
            </span>
          </Tooltip>
        </Space>
      </Card>

      <Row gutter={[12, 12]} style={{ marginBottom: 12 }}>
        <Col xs={8}>
          <Card size="small">
            <Statistic
              title="打分偏多（≥0.55）"
              value={data?.buy_count ?? 0}
              suffix="只"
              valueStyle={{ color: '#f5222d', fontFamily: 'monospace' }}
            />
          </Card>
        </Col>
        <Col xs={8}>
          <Card size="small">
            <Statistic
              title="中性（0.45~0.55）"
              value={data?.hold_count ?? 0}
              suffix="只"
              valueStyle={{ fontFamily: 'monospace' }}
            />
          </Card>
        </Col>
        <Col xs={8}>
          <Card size="small">
            <Statistic
              title="打分偏空（≤0.45）"
              value={data?.sell_count ?? 0}
              suffix="只"
              valueStyle={{ color: '#52c41a', fontFamily: 'monospace' }}
            />
          </Card>
        </Col>
      </Row>

      <Card
        size="small"
        title="因子打分明细"
        extra={
          <Segmented
            size="small"
            value={side}
            onChange={(v) => setSide(v as 'ALL' | 'BUY' | 'SELL')}
            options={[
              { label: `全部 ${data?.signals.length ?? 0}`, value: 'ALL' },
              { label: `偏多 ${data?.buy_count ?? 0}`, value: 'BUY' },
              { label: `偏空 ${data?.sell_count ?? 0}`, value: 'SELL' },
            ]}
          />
        }
      >
        <Table
          rowKey="id"
          size="small"
          loading={isLoading}
          columns={columns}
          dataSource={rows}
          locale={{ emptyText: '该方向下没有打分结果（0.45~0.55 视为中性，不计入多空）' }}
          pagination={{ pageSize: 20, size: 'small', showSizeChanger: false }}
        />
      </Card>
    </div>
  )
}
