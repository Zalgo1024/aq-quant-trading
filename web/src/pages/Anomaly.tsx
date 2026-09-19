import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Slider, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import type { Anomaly } from '@/types'
import { BOARD_LABEL, SEVERITY_LABEL, fmtAmount, fmtPct, pctColor } from '@/utils/format'

/**
 * 全市场**统计偏离扫描**。
 *
 * 旧实现只扫 `get_stock_list()[:30]`（按代码序前 30 只），却把它叫做"异动预警"。
 * 现在覆盖全部活跃股（默认 5548 只），且 z 值在行情快照构建时一并算好，
 * 不额外读盘 —— 页面打开即出结果。
 *
 * 措辞从「预警」改成「扫描」是有意的：**预警暗示"该动手了"，而本页没有任何
 * 被验证过的择时含义**（60 日收益偏离的 z 值，本项目从未检验过它有无预测力）。
 * 它属于「工具箱」，不属于「证据」，更不属于「结论」——
 * 因此标题旁挂的是「工具 · 非研究结论」，而不是任何带结论色彩的徽章。
 */
export default function AnomalyPage() {
  const [minZ, setMinZ] = useState(2.5)
  const [top, setTop] = useState(50)

  const { data, isLoading } = useQuery({
    queryKey: ['anomaly', top, minZ],
    queryFn: () => api.anomaly(top, minZ),
  })

  const columns: ColumnsType<Anomaly> = [
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
      width: 76,
      render: (v?: string) => <Tag style={{ fontSize: 11 }}>{BOARD_LABEL[v ?? ''] ?? v ?? '-'}</Tag>,
    },
    {
      title: '当日涨跌',
      dataIndex: 'pct_chg',
      width: 100,
      align: 'right',
      sorter: (a, b) => (a.pct_chg ?? 0) - (b.pct_chg ?? 0),
      render: (v?: number) => (
        <span className="mono" style={{ color: pctColor(v ?? 0) }}>{fmtPct(v ?? 0)}</span>
      ),
    },
    {
      title: 'z-score',
      dataIndex: 'z_score',
      width: 100,
      align: 'right',
      defaultSortOrder: 'descend',
      sorter: (a, b) => Math.abs(a.z_score) - Math.abs(b.z_score),
      render: (v: number) => {
        // 用明度表达强度，**不用红黄** —— 红绿在本项目是涨跌专用。
        // 一列红色的"严重"标签挨着红色的 +3.2%，读者分不清哪个红是什么意思。
        const a = Math.abs(v)
        return (
          <span
            className="mono"
            style={{
              color: a >= 4 ? 'var(--aq-cred-strong)' : a >= 3 ? 'var(--aq-text-2)' : 'var(--aq-text-3)',
            }}
          >
            {v > 0 ? '+' : ''}
            {v.toFixed(2)}
          </span>
        )
      },
    },
    {
      title: '级别',
      dataIndex: 'severity',
      width: 86,
      filters: [
        { text: '严重', value: 'severe' },
        { text: '警告', value: 'warn' },
        { text: '提示', value: 'info' },
      ],
      onFilter: (v, r) => r.severity === v,
      render: (v: string) => {
        // 级别也是「状态」，走全站同一套填充词汇（实心 / 半填充 / 空心），不占色相。
        const cls = v === 'severe' ? 'sev-3' : v === 'warn' ? 'sev-2' : 'sev-1'
        return <span className={`chip ${cls}`}>{SEVERITY_LABEL[v] ?? v}</span>
      },
    },
    {
      title: '现价',
      dataIndex: 'close',
      width: 90,
      align: 'right',
      render: (v?: number) => <span className="mono">{v?.toFixed(2) ?? '-'}</span>,
    },
    {
      title: '成交额',
      dataIndex: 'amount',
      width: 100,
      align: 'right',
      render: (v?: number) => <span className="mono">{v ? fmtAmount(v) : '-'}</span>,
    },
  ]

  return (
    <div>
      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: '10px 16px' } }}>
        <Space size={24} wrap>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              扫描范围
            </Typography.Text>{' '}
            <span className="mono">{data?.n_scanned ?? 0}</span>{' '}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              只活跃股（全市场）
            </Typography.Text>
          </span>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              数据截至
            </Typography.Text>{' '}
            <span className="mono">{data?.asof ?? '—'}</span>
          </span>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              命中
            </Typography.Text>{' '}
            <span className="mono">{data?.items.length ?? 0}</span>
          </span>
          <Space size={8}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              |z| 阈值
            </Typography.Text>
            <Slider
              min={1.5}
              max={6}
              step={0.5}
              value={minZ}
              onChange={setMinZ}
              style={{ width: 140 }}
              marks={{ 1.5: '1.5', 3: '3', 6: '6' }}
            />
          </Space>
          <Space size={8}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              条数
            </Typography.Text>
            <Slider
              min={10}
              max={200}
              step={10}
              value={top}
              onChange={setTop}
              style={{ width: 110 }}
            />
          </Space>
          {data?.caveat && (
            <Tooltip title={data.caveat}>
              <Tag color="default" style={{ cursor: 'help' }}>
                口径说明
              </Tag>
            </Tooltip>
          )}
        </Space>
      </Card>

      {data?.caveat && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="这是统计偏离，不是买卖信号"
          description={data.caveat}
        />
      )}

      <Card
        size="small"
        title={
          <Space size={8}>
            异动扫描
            <Tooltip title="本页是工具，不是研究成果：z 值只描述「今天的收益偏离过去 60 日多远」，本项目从未检验过它有没有预测力。它不出现在结论台账里。">
              <span className="chip chip-none" style={{ cursor: 'help' }}>
                <i />
                工具 · 非研究结论
              </span>
            </Tooltip>
          </Space>
        }
      >
        <Table
          rowKey={(r) => `${r.symbol}-${r.time}-${r.z_score}`}
          size="small"
          loading={isLoading}
          columns={columns}
          dataSource={data?.items ?? []}
          locale={{ emptyText: '当前无超过阈值的异动（可下调 |z| 阈值）' }}
          scroll={{ x: 1000 }}
          pagination={{ pageSize: 20, size: 'small', showSizeChanger: false }}
        />
      </Card>
    </div>
  )
}
