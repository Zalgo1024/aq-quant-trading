import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Card, Form, InputNumber, DatePicker, Button, Row, Col, Statistic, Table, message, Spin, Empty, Space } from 'antd'
import ReactECharts from 'echarts-for-react'
import dayjs from 'dayjs'
import api from '@/services/api'
import type { BacktestResponse } from '@/types'
import { fmtPct, fmtMoney } from '@/utils/format'
import { UP_COLOR, DOWN_COLOR } from '@/utils/format'

export default function Backtest() {
  const [form] = Form.useForm()
  const [result, setResult] = useState<BacktestResponse | null>(null)

  const mut = useMutation({
    mutationFn: api.runBacktest,
    onSuccess: (d) => {
      setResult(d)
      message.success(`回测完成：${d.run_id}`)
    },
    onError: (e: unknown) => {
      message.error(`回测失败：${e instanceof Error ? e.message : String(e)}`)
    },
  })

  const onSubmit = (vals: any) => {
    mut.mutate({
      start: vals.range?.[0]?.format('YYYY-MM-DD'),
      end: vals.range?.[1]?.format('YYYY-MM-DD'),
      top_k: vals.top_k,
      initial_cash: vals.initial_cash,
      mode: 'backtest',
    })
  }

  const m = result?.metrics

  // 权益曲线 + 回撤
  const equityOption = result
    ? {
        animation: false,
        tooltip: {
          trigger: 'axis',
          backgroundColor: 'rgba(30,30,30,0.95)',
          borderColor: '#444',
          textStyle: { color: '#e8e8e8' },
        },
        legend: { data: ['策略净值', '回撤'], textStyle: { color: '#ccc' }, top: 0 },
        grid: [
          { left: 70, right: 30, top: 36, height: '52%' },
          { left: 70, right: 30, top: '72%', height: '18%' },
        ],
        xAxis: [
          {
            type: 'category',
            data: result.equity.map((p) => p.time.slice(0, 10)),
            axisLabel: { color: '#999', fontSize: 10 },
            axisLine: { lineStyle: { color: '#555' } },
          },
          {
            type: 'category',
            gridIndex: 1,
            data: result.drawdown.map((p) => p.time.slice(0, 10)),
            axisLabel: { show: false },
            axisLine: { lineStyle: { color: '#555' } },
          },
        ],
        yAxis: [
          {
            type: 'value',
            scale: true,
            splitLine: { lineStyle: { color: '#2a2a2a' } },
            axisLabel: { color: '#999', fontSize: 10, formatter: (v: number) => `${(v / 1e4).toFixed(0)}万` },
          },
          {
            gridIndex: 1,
            type: 'value',
            axisLabel: { color: '#999', fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
            splitLine: { lineStyle: { color: '#2a2a2a' } },
          },
        ],
        series: [
          {
            name: '策略净值',
            type: 'line',
            showSymbol: false,
            data: result.equity.map((p) => p.equity),
            lineStyle: { color: '#f5222d', width: 2 },
            areaStyle: { color: 'rgba(245,34,45,0.08)' },
          },
          {
            name: '回撤',
            type: 'line',
            xAxisIndex: 1,
            yAxisIndex: 1,
            showSymbol: false,
            data: result.drawdown.map((p) => p.equity),
            lineStyle: { color: '#52c41a', width: 1.5 },
            areaStyle: { color: 'rgba(82,196,26,0.18)' },
          },
        ],
      }
    : null

  return (
    <div>
      <Card title="回测配置" size="small">
        <Form
          form={form}
          layout="inline"
          onFinish={onSubmit}
          initialValues={{
            range: [dayjs('2023-01-01'), dayjs('2024-06-30')],
            top_k: 5,
            initial_cash: 1_000_000,
          }}
        >
          <Form.Item name="range" label="回测区间" rules={[{ required: true }]}>
            <DatePicker.RangePicker />
          </Form.Item>
          <Form.Item name="top_k" label="持仓数 TopK">
            <InputNumber min={1} max={50} style={{ width: 90 }} />
          </Form.Item>
          <Form.Item name="initial_cash" label="初始资金">
            <InputNumber min={100_000} step={100_000} style={{ width: 140 }} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={mut.isPending}>
              开始回测
            </Button>
          </Form.Item>
        </Form>
        <div style={{ marginTop: 8, fontSize: 12, color: '#8c8c8c' }}>
          回测已内置 A 股规则：T+1、涨跌停封板、印花税（卖出 0.05%）、佣金（万 2.5，最低 5 元）、滑点。
        </div>
      </Card>

      {mut.isPending && (
        <Card size="small" style={{ marginTop: 16 }}>
          <Spin tip="回测运行中…" style={{ display: 'block', margin: '40px auto' }} />
        </Card>
      )}

      {result && m && (
        <>
          <Card title={`绩效指标 · ${result.run_id}`} size="small" style={{ marginTop: 16 }}>
            <Row gutter={[16, 16]}>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="总收益" value={m.total_return * 100} precision={2} suffix="%" valueStyle={{ color: m.total_return >= 0 ? UP_COLOR : DOWN_COLOR }} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="年化收益" value={m.annual_return * 100} precision={2} suffix="%" valueStyle={{ color: m.annual_return >= 0 ? UP_COLOR : DOWN_COLOR }} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="夏普比率" value={m.sharpe} precision={3} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="索提诺" value={m.sortino} precision={3} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="最大回撤" value={m.max_drawdown * 100} precision={2} suffix="%" valueStyle={{ color: DOWN_COLOR }} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="卡玛比率" value={m.calmar} precision={3} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="信息比率" value={m.information_ratio} precision={3} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="Alpha" value={m.alpha * 100} precision={2} suffix="%" />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="Beta" value={m.beta} precision={3} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="换手率" value={m.turnover * 100} precision={1} suffix="%" />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic title="成交笔数" value={result.trade_count} />
              </Col>
              <Col xs={12} sm={8} md={6} lg={4}>
                <Statistic
                  title="防过拟合(CSCV)"
                  value={m.cscv === null || m.cscv === undefined ? '待接入' : m.cscv}
                  valueStyle={{ fontSize: 16 }}
                />
              </Col>
            </Row>
          </Card>

          <Card title="权益曲线 · 回撤" size="small" style={{ marginTop: 16 }}>
            {equityOption ? (
              <ReactECharts option={equityOption} style={{ height: 420 }} notMerge />
            ) : (
              <Empty />
            )}
          </Card>

          <Card size="small" style={{ marginTop: 16 }}>
            <Space>
              <span style={{ fontSize: 12, color: '#8c8c8c' }}>
                区间：{result.start} ~ {result.end} ｜ 最终权益：
                <span className="mono">
                  {fmtMoney(result.equity[result.equity.length - 1]?.equity)}
                </span>
              </span>
            </Space>
          </Card>
        </>
      )}
    </div>
  )
}
