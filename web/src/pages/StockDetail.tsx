import { useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Tag,
  Spin,
  Empty,
  Progress,
  Space,
  Button,
  Segmented,
  Row,
} from 'antd'
import ReactECharts from 'echarts-for-react'
import api from '@/services/api'
import { fmtAmount, fmtPct, pctColor, DIRECTION_LABEL } from '@/utils/format'

const AXIS = { color: '#999', fontSize: 10 }
const SPLIT = { lineStyle: { color: '#2a2a2a' } }

/** 简单移动平均（用前复权收盘价算） */
function ma(values: number[], n: number): (number | null)[] {
  return values.map((_, i) => {
    if (i < n - 1) return null
    let s = 0
    for (let k = i - n + 1; k <= i; k++) s += values[k]
    return +(s / n).toFixed(3)
  })
}

export default function StockDetail() {
  const { code = '' } = useParams()
  const [indicator, setIndicator] = useState<'VOL' | 'MACD'>('VOL')

  const { data: detail, isLoading } = useQuery({
    queryKey: ['stock-detail', code],
    queryFn: () => api.stockDetail(code, 250),
  })
  const { data: pred } = useQuery({
    queryKey: ['stock-pred', code],
    queryFn: () => api.stockPrediction(code),
    retry: 0,
  })

  const bars = detail?.bars ?? []
  const ohlc = useMemo(() => bars.map((b) => [b.open, b.close, b.low, b.high]), [bars])
  const closes = useMemo(() => bars.map((b) => b.close), [bars])

  if (isLoading) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (!detail || bars.length === 0) return <Empty description="暂无行情数据" />

  const dates = bars.map((b) => b.time.slice(0, 10))
  const last = bars[bars.length - 1]
  const first = bars[0]
  const periodPct = first.close ? last.close / first.close - 1 : 0
  const dayPct = last.close / (last.open || 1) - 1

  const ma5 = ma(closes, 5)
  const ma20 = ma(closes, 20)
  const ma60 = ma(closes, 60)

  // 指标副图：成交量 或 MACD
  let subSeries: Record<string, unknown>[] = []
  let subYAxis: Record<string, unknown> = {}
  if (indicator === 'VOL') {
    subSeries = [
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: bars.map((b) => ({
          value: b.volume,
          itemStyle: { color: b.close >= b.open ? '#f5222d' : '#52c41a' },
        })),
      },
    ]
    subYAxis = { splitNumber: 2, axisLabel: { show: false }, splitLine: { show: false } }
  } else {
    // MACD(12,26,9)
    const ema = (arr: number[], n: number) => {
      const k = 2 / (n + 1)
      const out: number[] = []
      arr.forEach((v, i) => out.push(i === 0 ? v : v * k + out[i - 1] * (1 - k)))
      return out
    }
    const e12 = ema(closes, 12)
    const e26 = ema(closes, 26)
    const dif = e12.map((v, i) => +(v - e26[i]).toFixed(4))
    const dea = ema(dif, 9).map((v) => +v.toFixed(4))
    const macd = dif.map((v, i) => +((v - dea[i]) * 2).toFixed(4))
    subSeries = [
      { name: 'MACD', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
        data: macd.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? '#f5222d' : '#52c41a' } })) },
      { name: 'DIF', type: 'line', xAxisIndex: 1, yAxisIndex: 1, showSymbol: false,
        data: dif, lineStyle: { color: '#faad14', width: 1 } },
      { name: 'DEA', type: 'line', xAxisIndex: 1, yAxisIndex: 1, showSymbol: false,
        data: dea, lineStyle: { color: '#1668dc', width: 1 } },
    ]
    subYAxis = { splitNumber: 3, axisLabel: AXIS, splitLine: SPLIT, scale: true }
  }

  const klineOption = {
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(30,30,30,0.95)',
      borderColor: '#444',
      textStyle: { color: '#e8e8e8' },
    },
    legend: { data: ['K线', 'MA5', 'MA20', 'MA60'], textStyle: { color: '#bbb' }, top: 0, itemGap: 14 },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 62, right: 20, top: 30, height: '54%' },
      { left: 62, right: 20, top: '72%', height: '18%' },
    ],
    xAxis: [
      { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#555' } }, axisLabel: AXIS },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#555' } }, axisLabel: { show: false } },
    ],
    yAxis: [
      { scale: true, splitLine: SPLIT, axisLabel: AXIS },
      { gridIndex: 1, ...subYAxis },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 55, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 16 },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: ohlc,
        itemStyle: { color: '#f5222d', color0: '#52c41a', borderColor: '#f5222d', borderColor0: '#52c41a' },
      },
      { name: 'MA5', type: 'line', data: ma5, showSymbol: false, lineStyle: { width: 1, color: '#faad14' } },
      { name: 'MA20', type: 'line', data: ma20, showSymbol: false, lineStyle: { width: 1, color: '#1668dc' } },
      { name: 'MA60', type: 'line', data: ma60, showSymbol: false, lineStyle: { width: 1, color: '#c41d7f' } },
      ...subSeries,
    ],
  }

  // 因子贡献（后端 contrib 已是加权贡献，value 是 [0,1] 定向得分）
  const contribs = (pred?.factor_contrib ?? []).slice().sort((a, b) => b.contrib - a.contrib).slice(0, 12)
  const factorOption = {
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(30,30,30,0.95)',
      textStyle: { color: '#eee' },
    },
    grid: { left: 116, right: 48, top: 10, bottom: 24 },
    xAxis: { type: 'value', splitLine: SPLIT, axisLabel: AXIS },
    yAxis: {
      type: 'category',
      data: contribs.map((c) => c.name),
      axisLabel: { color: '#ccc', fontSize: 11 },
      axisLine: { lineStyle: { color: '#555' } },
    },
    series: [
      {
        type: 'bar',
        data: contribs.map((c) => ({
          value: Number(c.contrib.toFixed(4)),
          itemStyle: { color: c.value >= 0.5 ? '#f5222d' : '#52c41a' },
        })),
        label: { show: true, position: 'right', color: '#ccc', fontSize: 10 },
      },
    ],
  }

  return (
    <div>
      <Card size="small" style={{ marginBottom: 12 }}>
        <Space size={16} wrap>
          <span style={{ fontSize: 18, fontWeight: 600 }}>
            {detail.name} <span className="mono" style={{ color: '#999' }}>{detail.symbol}</span>
          </span>
          <span className="mono" style={{ fontSize: 20, color: pctColor(dayPct) }}>
            {last.close.toFixed(2)}
          </span>
          <span className="mono" style={{ color: pctColor(dayPct) }}>{fmtPct(dayPct)}</span>
          <span style={{ color: '#8c8c8c', fontSize: 12 }}>
            区间({detail.first_date} ~ {detail.last_date}) <span className="mono">{fmtPct(periodPct)}</span>
          </span>
          <Tag color="default">{detail.price_caliber}</Tag>
          <Link to="/stocks">
            <Button size="small">返回全市场</Button>
          </Link>
        </Space>
      </Card>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={16}>
          <Card
            size="small"
            title="K 线 · 均线 · 指标"
            extra={
              <Segmented
                size="small"
                value={indicator}
                onChange={(v) => setIndicator(v as 'VOL' | 'MACD')}
                options={[
                  { label: '成交量', value: 'VOL' },
                  { label: 'MACD', value: 'MACD' },
                ]}
              />
            }
          >
            <ReactECharts option={klineOption} style={{ height: 480 }} notMerge lazyUpdate />
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card
            title="因子打分"
            size="small"
            style={{ marginBottom: 12 }}
            extra={
              pred && (
                <Tag color={pred.in_sample ? 'blue' : 'default'}>
                  {pred.in_sample ? '在样本内' : '样本外（并入截面重算）'}
                </Tag>
              )
            }
          >
            {pred ? (
              <>
                <Row gutter={16}>
                  <Col span={12}>
                    <div style={{ color: '#8c8c8c', fontSize: 12 }}>横截面得分</div>
                    <div
                      className="mono"
                      style={{
                        fontSize: 26,
                        color: pred.score >= 0.55 ? '#f5222d' : pred.score <= 0.45 ? '#52c41a' : '#8c8c8c',
                      }}
                    >
                      {pred.score.toFixed(4)}
                    </div>
                  </Col>
                  <Col span={12}>
                    <div style={{ color: '#8c8c8c', fontSize: 12 }}>方向</div>
                    <Tag
                      color={pred.direction === 'BUY' ? 'red' : pred.direction === 'SELL' ? 'green' : 'default'}
                      style={{ marginTop: 6, fontSize: 14, padding: '2px 10px' }}
                    >
                      {DIRECTION_LABEL[pred.direction]}
                    </Tag>
                  </Col>
                </Row>
                <div style={{ marginTop: 12 }}>
                  <div style={{ color: '#8c8c8c', fontSize: 12, marginBottom: 4 }}>
                    置信度 {(pred.confidence * 100).toFixed(1)}%
                  </div>
                  <Progress percent={Math.round(pred.confidence * 100)} size="small" strokeColor="#1668dc" showInfo={false} />
                </div>
                <div style={{ marginTop: 12, fontSize: 12, color: '#bbb', lineHeight: 1.7 }}>
                  {pred.explain}
                </div>
                <div style={{ marginTop: 8, fontSize: 11, color: '#8c8c8c', lineHeight: 1.7 }}>
                  样本 {pred.sample_size ?? '-'} 只 ｜ 权重来源{' '}
                  <span className="mono">{pred.weight_source ?? '-'}</span>
                  <br />
                  {pred.sample_caliber}
                </div>
                {pred.caveat && (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginTop: 10 }}
                    message={<span style={{ fontSize: 12 }}>该分数未被证明有预测力</span>}
                    description={<span style={{ fontSize: 11, lineHeight: 1.7 }}>{pred.caveat}</span>}
                  />
                )}
              </>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无打分" />
            )}
          </Card>

          <Card size="small" title="因子贡献（Top 12）" extra={<span style={{ fontSize: 11, color: '#8c8c8c' }}>红=偏多 绿=偏空</span>}>
            {contribs.length ? (
              <ReactECharts option={factorOption} style={{ height: 320 }} notMerge />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无因子数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Card size="small" title="最新因子原始值" style={{ marginTop: 12 }}
            extra={
              <span style={{ fontSize: 11, color: '#8c8c8c' }}>
                后复权收盘 <span className="mono">{detail.last_close_adj}</span> ÷ 复权因子{' '}
                <span className="mono">{detail.adj_factor_last}</span> = 真实价{' '}
                <span className="mono">{detail.last_close_raw}</span>
              </span>
            }
      >
        <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3, lg: 4 }} bordered>
          {Object.entries(detail.factors).map(([k, v]) => (
            <Descriptions.Item key={k} label={k}>
              <span className="mono">{v === null || v === undefined ? '-' : Number(v).toFixed(4)}</span>
            </Descriptions.Item>
          ))}
        </Descriptions>
      </Card>

      <Card size="small" style={{ marginTop: 12 }}>
        <Space size={20} wrap>
          <span style={{ fontSize: 12, color: '#8c8c8c' }}>
            最新成交额 <span className="mono">{fmtAmount(last.amount)}</span>
          </span>
          <span style={{ fontSize: 12, color: '#8c8c8c' }}>
            最新成交量 <span className="mono">{fmtAmount(last.volume)}</span>
          </span>
          <span style={{ fontSize: 12, color: '#8c8c8c' }}>
            区间涨跌 <span className="mono">{fmtPct(periodPct)}</span>
          </span>
          <span style={{ fontSize: 12, color: '#8c8c8c' }}>
            共 <span className="mono">{bars.length}</span> 个交易日
          </span>
        </Space>
      </Card>
    </div>
  )
}
