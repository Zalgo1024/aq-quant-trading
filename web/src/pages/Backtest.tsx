import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Card, Form, InputNumber, DatePicker, Button, Row, Col, Statistic, Table, message, Spin, Empty, Space, Tag } from 'antd'
import ReactECharts from 'echarts-for-react'
import dayjs from 'dayjs'
import api from '@/services/api'
import NotActionable from '@/components/NotActionable'
import type { BacktestResponse } from '@/types'
import { fmtPct, fmtMoney } from '@/utils/format'
import { UP_COLOR, DOWN_COLOR } from '@/utils/format'
import { useChartColors, withAlpha } from '@/theme/tokens'

export default function Backtest() {
  const [form] = Form.useForm()
  const [result, setResult] = useState<BacktestResponse | null>(null)
  // ECharts 读不到 CSS 变量，配色必须单独按主题取一份（见 theme/tokens.ts）
  const cc = useChartColors()

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
      max_symbols: vals.max_symbols || undefined,
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
          backgroundColor: cc.tooltipBg,
          borderColor: cc.tooltipBorder,
          textStyle: { color: cc.text },
        },
        legend: { data: ['策略净值', '回撤'], textStyle: { color: cc.textSecondary }, top: 0 },
        grid: [
          { left: 70, right: 30, top: 36, height: '52%' },
          { left: 70, right: 30, top: '72%', height: '18%' },
        ],
        xAxis: [
          {
            type: 'category',
            data: result.equity.map((p) => p.time.slice(0, 10)),
            axisLabel: { color: cc.textSecondary, fontSize: 10 },
            axisLine: { lineStyle: { color: cc.split } },
          },
          {
            type: 'category',
            gridIndex: 1,
            data: result.drawdown.map((p) => p.time.slice(0, 10)),
            axisLabel: { show: false },
            axisLine: { lineStyle: { color: cc.split } },
          },
        ],
        yAxis: [
          {
            type: 'value',
            scale: true,
            splitLine: { lineStyle: { color: cc.split } },
            axisLabel: { color: cc.textSecondary, fontSize: 10, formatter: (v: number) => `${(v / 1e4).toFixed(0)}万` },
          },
          {
            gridIndex: 1,
            type: 'value',
            axisLabel: { color: cc.textSecondary, fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
            splitLine: { lineStyle: { color: cc.split } },
          },
        ],
        series: [
          {
            name: '策略净值',
            type: 'line',
            showSymbol: false,
            data: result.equity.map((p) => p.equity),
            lineStyle: { color: cc.up, width: 2 },
            areaStyle: { color: withAlpha(cc.up, 0.08) },
          },
          {
            name: '回撤',
            type: 'line',
            xAxisIndex: 1,
            yAxisIndex: 1,
            showSymbol: false,
            data: result.drawdown.map((p) => p.equity),
            lineStyle: { color: cc.down, width: 1.5 },
            areaStyle: { color: withAlpha(cc.down, 0.18) },
          },
        ],
      }
    : null

  return (
    <div>
      <NotActionable
        claim="多因子模型提供了独立于风格的 alpha"
        extra={
          <span>
            回测本身也很慢：全池 5000+ 只建一次因子面板需数分钟，请求超时上限 180 秒；
            想快速看引擎行为请把「股票池上限」设成 300~500（截断按代码序，有系统性偏置，
            不能用于下结论）。本页只用于验证回测引擎本身是否按预期执行。
          </span>
        }
      />
      <Card title="回测配置" size="small">
        <Form
          form={form}
          layout="inline"
          onFinish={onSubmit}
          initialValues={{
            range: [dayjs('2023-01-01'), dayjs('2024-06-30')],
            top_k: 5,
            initial_cash: 1_000_000,
            // 默认截断到 300 只：不截断会跑几分钟并可能超时。
            // 结果页会显式标出"截断口径"，避免被当成有效结论。
            max_symbols: 300,
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
          <Form.Item name="max_symbols" label="股票池上限" tooltip="留空=用配置（0，不截断，很慢）；填 300~500 可快速自检，但截断按代码序有系统性偏置">
            <InputNumber min={0} max={6000} step={100} style={{ width: 110 }} placeholder="0=不截断" />
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

          {/* 过程诊断：只看 metrics 无法判断回测有没有真的按预期执行
              （曾出现 890 笔订单全被拒、仓位恒为 0，但收益曲线"看起来正常"）。 */}
          <Card
            title="过程诊断（判断回测是否真的执行了）"
            size="small"
            style={{ marginTop: 16 }}
          >
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <div style={{ fontSize: 12, color: '#999' }}>
                用时 <span className="mono">{result.elapsed_seconds ?? '-'}</span> 秒 ｜
                股票池上限{' '}
                <span className="mono">
                  {(result.max_symbols ?? 0) === 0 ? '不截断' : result.max_symbols}
                </span>
                {(result.max_symbols ?? 0) > 0 && (
                  <Tag color="orange" style={{ marginLeft: 8 }}>
                    截断口径（按代码序，有偏置，不可作结论）
                  </Tag>
                )}
              </div>
              {result.diagnostics && Object.keys(result.diagnostics).length > 0 ? (
                <pre className="mono codeblock" style={{ maxHeight: 220 }}>
                  {JSON.stringify(result.diagnostics, null, 2)}
                </pre>
              ) : (
                <span style={{ fontSize: 12, color: '#8c8c8c' }}>
                  引擎未返回 diagnostics —— 不能判断订单是否真的成交，请谨慎解读。
                </span>
              )}
            </Space>
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
