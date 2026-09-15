import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, Col, Row, Descriptions, Tag, Spin, Empty, Progress, Space, Button } from 'antd'
import ReactECharts from 'echarts-for-react'
import api from '@/services/api'
import { fmtAmount, fmtPct, pctColor, DIRECTION_LABEL } from '@/utils/format'

export default function StockDetail() {
  const { code = '' } = useParams()
  const { data: detail, isLoading } = useQuery({
    queryKey: ['stock-detail', code],
    queryFn: () => api.stockDetail(code, 250),
  })
  const { data: pred } = useQuery({
    queryKey: ['stock-pred', code],
    queryFn: () => api.stockPrediction(code),
    retry: 0,
  })

  if (isLoading) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (!detail || detail.bars.length === 0) return <Empty description="暂无行情数据" />

  const bars = detail.bars
  const dates = bars.map((b) => b.time.slice(0, 10))
  const ohlc = bars.map((b) => [b.open, b.close, b.low, b.high])
  const volumes = bars.map((b, i) => ({
    value: b.volume,
    itemStyle: { color: b.close >= b.open ? '#f5222d' : '#52c41a' },
  }))

  const last = bars[bars.length - 1]
  const first = bars[0]
  const pct = first.close ? last.close / first.close - 1 : 0

  // K 线 + 成交量
  const klineOption = {
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(30,30,30,0.95)',
      borderColor: '#444',
      textStyle: { color: '#e8e8e8' },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 60, right: 20, top: 20, height: '58%' },
      { left: 60, right: 20, top: '76%', height: '18%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#555' } },
        axisLabel: { color: '#999', fontSize: 10 },
      },
      {
        type: 'category',
        gridIndex: 1,
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#555' } },
        axisLabel: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        splitLine: { lineStyle: { color: '#2a2a2a' } },
        axisLabel: { color: '#999', fontSize: 10 },
      },
      {
        gridIndex: 1,
        splitNumber: 2,
        axisLabel: { show: false },
        splitLine: { show: false },
        axisLine: { lineStyle: { color: '#555' } },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 16 },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: ohlc,
        itemStyle: {
          color: '#f5222d',      // 阳线（涨）红
          color0: '#52c41a',     // 阴线（跌）绿
          borderColor: '#f5222d',
          borderColor0: '#52c41a',
        },
      },
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes,
      },
    ],
  }

  // 因子贡献瀑布图（横向条形，红正绿负）
  const contribs = (pred?.factor_contrib ?? []).slice().sort((a, b) => b.contrib - a.contrib)
  const factorOption = {
    animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 110, right: 40, top: 10, bottom: 24 },
    xAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: '#2a2a2a' } },
      axisLabel: { color: '#999', fontSize: 10 },
    },
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
          itemStyle: {
            color: c.value >= 0.5 ? '#f5222d' : '#52c41a', // 因子得分高于中性→红
          },
        })),
        label: {
          show: true,
          position: 'right',
          color: '#ccc',
          fontSize: 10,
          formatter: (p: any) => p.value,
        },
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
          <span className="mono" style={{ fontSize: 20, color: pctColor(pct) }}>
            {last.close.toFixed(2)}
          </span>
          <span className="mono" style={{ color: pctColor(pct) }}>
            {fmtPct(last.close / (last.open || 1) - 1)}
          </span>
          <span style={{ color: '#8c8c8c', fontSize: 12 }}>
            区间({dates[0]} ~ {dates[dates.length - 1]}) <span className="mono">{fmtPct(pct)}</span>
          </span>
          <Link to="/stocks">
            <Button size="small">返回列表</Button>
          </Link>
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={16}>
          <Card title="K 线 · 成交量" size="small">
            <ReactECharts option={klineOption} style={{ height: 460 }} notMerge lazyUpdate />
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          {/* AI 评分卡 */}
          <Card title="AI 评分" size="small" style={{ marginBottom: 16 }}>
            {pred ? (
              <>
                <Row gutter={16}>
                  <Col span={12}>
                    <div style={{ color: '#8c8c8c', fontSize: 12 }}>综合评分</div>
                    <div
                      className="mono"
                      style={{
                        fontSize: 26,
                        color: pred.score >= 0.55 ? '#f5222d' : pred.score <= 0.45 ? '#52c41a' : '#8c8c8c',
                      }}
                    >
                      {(pred.score * 100).toFixed(1)}
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
                  <Progress
                    percent={Math.round(pred.confidence * 100)}
                    size="small"
                    strokeColor="#1668dc"
                    showInfo={false}
                  />
                </div>
                <div style={{ marginTop: 12, fontSize: 12, color: '#8c8c8c', lineHeight: 1.7 }}>
                  {pred.explain}
                </div>
                <div style={{ marginTop: 6, fontSize: 11, color: '#595959' }}>
                  模型版本：{pred.model_version}
                </div>
              </>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无预测" />
            )}
          </Card>

          {/* 因子贡献（可解释性） */}
          <Card title="因子贡献归因" size="small" extra={<span style={{ fontSize: 11, color: '#8c8c8c' }}>红=偏多 绿=偏空</span>}>
            {contribs.length ? (
              <ReactECharts option={factorOption} style={{ height: 300 }} notMerge />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无因子数据" />
            )}
          </Card>
        </Col>
      </Row>

      {/* 最新因子原始值 */}
      <Card title="最新因子原始值" size="small" style={{ marginTop: 16 }}>
        <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3, lg: 4 }} bordered>
          {Object.entries(detail.factors).map(([k, v]) => (
            <Descriptions.Item key={k} label={k}>
              <span className="mono">{v === null || v === undefined ? '-' : Number(v).toFixed(4)}</span>
            </Descriptions.Item>
          ))}
        </Descriptions>
      </Card>
    </div>
  )
}
