import { useQuery } from '@tanstack/react-query'
import { Card, Descriptions, Table, Tag, Row, Col, Statistic, Alert, Empty } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import api from '@/services/api'
import type { Position } from '@/types'
import { fmtMoney, fmtPct, pctColor } from '@/utils/format'

export default function Admin() {
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })
  const { data: cfg } = useQuery({ queryKey: ['config'], queryFn: api.config })
  const { data: account } = useQuery({ queryKey: ['account'], queryFn: api.account })
  const { data: factors } = useQuery({ queryKey: ['factor-analysis'], queryFn: api.factorAnalysis })

  const posCols: ColumnsType<Position> = [
    { title: '代码', dataIndex: 'symbol', width: 100 },
    { title: '持仓', dataIndex: 'qty', width: 90, align: 'right' },
    { title: '可卖(T+1)', dataIndex: 'available', width: 100, align: 'right' },
    {
      title: '成本价',
      dataIndex: 'avg_cost',
      width: 100,
      align: 'right',
      render: (v: number) => <span className="mono">{v.toFixed(2)}</span>,
    },
    {
      title: '现价',
      dataIndex: 'last_price',
      width: 100,
      align: 'right',
      render: (v: number, r) => (
        <span className="mono" style={{ color: pctColor(v - r.avg_cost) }}>
          {v.toFixed(2)}
        </span>
      ),
    },
    {
      title: '浮动盈亏',
      width: 120,
      align: 'right',
      render: (_: unknown, r) => {
        const pnl = (r.last_price - r.avg_cost) * r.qty
        return <span className="mono" style={{ color: pctColor(pnl) }}>{fmtMoney(pnl)}</span>
      },
    },
  ]

  const factorCols: ColumnsType<any> = [
    { title: '因子', dataIndex: 'name' },
    {
      title: '方向',
      dataIndex: 'direction',
      width: 90,
      render: (v: number) => <Tag color={v > 0 ? 'red' : 'green'}>{v > 0 ? '正向' : '反向'}</Tag>,
    },
    { title: 'IC', dataIndex: 'ic', width: 100, align: 'right', render: (v: any) => v ?? '待接入' },
    { title: 'RankIC', dataIndex: 'rank_ic', width: 100, align: 'right', render: (v: any) => v ?? '待接入' },
    { title: 'ICIR', dataIndex: 'icir', width: 100, align: 'right', render: (v: any) => v ?? '待接入' },
  ]

  return (
    <div>
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message="实盘接入说明"
        description="当前 broker 为 sim（模拟）。切换实盘只需修改 config/live.yaml 的 mode: live 与 broker（qmt/ptrade/jq），策略与风控代码零改动。实盘前请完成程序化交易报备并以极小资金试跑。"
      />

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <Card title="系统状态" size="small">
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label="版本">{health?.version ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="运行模式">
                <Tag color={health?.mode === 'live' ? 'red' : health?.mode === 'paper' ? 'orange' : 'blue'}>
                  {health?.mode ?? '-'}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="交易通道">{health?.broker ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="数据源">{health?.data_source ?? '-'}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>

        <Col xs={24} md={12}>
          <Card title="模拟账户" size="small">
            {account ? (
              <Row gutter={16}>
                <Col span={12}>
                  <Statistic title="总资产" value={account.total_asset} precision={2} prefix="¥" />
                </Col>
                <Col span={12}>
                  <Statistic title="可用现金" value={account.cash} precision={2} prefix="¥" />
                </Col>
                <Col span={12} style={{ marginTop: 12 }}>
                  <Statistic title="持仓市值" value={account.market_value} precision={2} prefix="¥" />
                </Col>
                <Col span={12} style={{ marginTop: 12 }}>
                  <Statistic
                    title="浮动盈亏"
                    value={account.unrealized_pnl}
                    precision={2}
                    prefix="¥"
                    valueStyle={{ color: account.unrealized_pnl >= 0 ? '#f5222d' : '#52c41a' }}
                  />
                </Col>
              </Row>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Card>
        </Col>
      </Row>

      <Card title="持仓" size="small" style={{ marginTop: 16 }}>
        <Table
          rowKey="symbol"
          size="small"
          columns={posCols}
          dataSource={account?.positions ?? []}
          locale={{ emptyText: '当前无持仓' }}
          pagination={false}
        />
      </Card>

      <Card
        title="因子列表"
        size="small"
        style={{ marginTop: 16 }}
        extra={<span style={{ fontSize: 12, color: '#8c8c8c' }}>{(factors as any)?.note ?? ''}</span>}
      >
        <Table
          rowKey="name"
          size="small"
          columns={factorCols}
          dataSource={factors?.factors ?? []}
          pagination={false}
        />
      </Card>

      <Card title="当前配置" size="small" style={{ marginTop: 16 }}>
        <pre
          className="mono"
          style={{
            background: '#1a1a1a',
            padding: 12,
            borderRadius: 6,
            fontSize: 12,
            maxHeight: 320,
            overflow: 'auto',
            color: '#bbb',
          }}
        >
          {JSON.stringify(cfg, null, 2)}
        </pre>
      </Card>
    </div>
  )
}
