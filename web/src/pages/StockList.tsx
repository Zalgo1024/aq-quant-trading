import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Input, Table, Tag, Space, Segmented, Switch, Tooltip, Typography } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import type { Quote } from '@/types'
import { BOARD_LABEL, fmtAmount, fmtPct, pctColor } from '@/utils/format'

/**
 * 全市场行情表。
 *
 * 旧实现有两个问题：① 股票列表来自 /api/signals（只覆盖打分样本 30 只），
 * 所以"全市场行情"其实只有 30 行；② 名称/行业要另外拉 5562 条元数据来拼表。
 * 现在一律走后端 /api/quotes：服务端分页 + 搜索 + 板块 + 流动性池过滤，
 * 返回的每条记录自带名称/行业/真实价/涨跌/成交额/异动 z 值。
 */
export default function StockList() {
  const [kw, setKw] = useState('')
  const [board, setBoard] = useState('')
  const [onlyLiquid, setOnlyLiquid] = useState(false)
  const [sort, setSort] = useState('amount')
  const [page, setPage] = useState(1)
  const pageSize = 50

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['quotes-full', kw, board, onlyLiquid, sort, page],
    queryFn: () =>
      api.quotes({
        keyword: kw,
        board,
        only_liquid: onlyLiquid,
        sort,
        order: 'desc',
        limit: pageSize,
        offset: (page - 1) * pageSize,
      }),
  })

  const columns: ColumnsType<Quote> = [
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 84,
      fixed: 'left',
      render: (v: string) => <Link to={`/stock/${v}`} className="mono">{v}</Link>,
    },
    { title: '名称', dataIndex: 'name', width: 110, fixed: 'left', ellipsis: true },
    {
      title: '板块',
      dataIndex: 'board',
      width: 76,
      render: (v: string) => <Tag style={{ fontSize: 11 }}>{BOARD_LABEL[v] ?? v}</Tag>,
    },
    {
      title: '行业',
      dataIndex: 'industry',
      width: 170,
      ellipsis: true,
      render: (v: string) => <span style={{ fontSize: 12, color: '#999' }}>{v || '-'}</span>,
    },
    {
      title: '现价',
      dataIndex: 'close',
      width: 96,
      align: 'right',
      sorter: (a, b) => a.close - b.close,
      render: (v: number) => <span className="mono">{v.toFixed(2)}</span>,
    },
    {
      title: '涨跌幅',
      dataIndex: 'pct_chg',
      width: 100,
      align: 'right',
      sorter: (a, b) => a.pct_chg - b.pct_chg,
      render: (v: number) => (
        <span className="mono" style={{ color: pctColor(v) }}>{fmtPct(v)}</span>
      ),
    },
    {
      title: '成交额',
      dataIndex: 'amount',
      width: 104,
      align: 'right',
      sorter: (a, b) => a.amount - b.amount,
      render: (v: number) => <span className="mono">{fmtAmount(v)}</span>,
    },
    {
      title: '20日均额',
      dataIndex: 'amount_ma20',
      width: 104,
      align: 'right',
      render: (v: number) => <span className="mono">{fmtAmount(v)}</span>,
    },
    {
      title: '异动 z',
      dataIndex: 'z_score',
      width: 84,
      align: 'right',
      sorter: (a, b) => Math.abs(a.z_score) - Math.abs(b.z_score),
      render: (v: number) => (
        <Tooltip title="当日收益相对自身前 60 日分布的偏离（σ）">
          <span className="mono" style={{ color: Math.abs(v) >= 3 ? '#f5222d' : '#8c8c8c' }}>
            {v.toFixed(2)}
          </span>
        </Tooltip>
      ),
    },
    {
      title: '池',
      dataIndex: 'in_liquid',
      width: 70,
      align: 'center',
      render: (v: boolean) =>
        v ? (
          <Tooltip title="属于主口径流动性池（上市满 180 天 + 近 20 日日均成交额 ≥ 2000 万 + 非 ST 快照）">
            <Tag color="blue">在池</Tag>
          </Tooltip>
        ) : (
          <Tag>—</Tag>
        ),
    },
  ]

  return (
    <Card
      size="small"
      title="全市场行情"
      extra={
        <Space wrap>
          <Segmented
            size="small"
            value={board}
            onChange={(v) => {
              setBoard(String(v))
              setPage(1)
            }}
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
            onChange={(v) => {
              setSort(String(v))
              setPage(1)
            }}
            options={[
              { label: '成交额', value: 'amount' },
              { label: '涨跌幅', value: 'pct_chg' },
              { label: '代码', value: 'symbol' },
            ]}
          />
          <Space size={6}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              仅流动性池
            </Typography.Text>
            <Switch
              size="small"
              checked={onlyLiquid}
              onChange={(v) => {
                setOnlyLiquid(v)
                setPage(1)
              }}
            />
          </Space>
          <Input
            size="small"
            allowClear
            prefix={<SearchOutlined />}
            placeholder="代码 / 名称"
            style={{ width: 150 }}
            onChange={(e) => {
              setKw(e.target.value.trim())
              setPage(1)
            }}
          />
        </Space>
      }
    >
      <div style={{ marginBottom: 8, fontSize: 12, color: '#8c8c8c' }}>
        数据截至 <span className="mono">{data?.asof ?? '—'}</span> ｜ 命中{' '}
        <span className="mono">{data?.total ?? 0}</span> 只（服务端分页，每页 {pageSize}）
      </div>
      <Table
        rowKey="symbol"
        size="small"
        loading={isLoading || isFetching}
        columns={columns}
        dataSource={data?.items ?? []}
        scroll={{ x: 1180 }}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: false,
          onChange: setPage,
          showTotal: (t) => `共 ${t} 只`,
        }}
      />
    </Card>
  )
}
