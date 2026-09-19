// A 股配色与格式化工具：红涨绿跌、¥ 计价
//
// ⚠️ 颜色一律用 CSS 变量（定义在 styles/global.css），这样浅色/深色主题
// 自动切换明度。**不要**在这里写死 hex —— 写死就切不动主题。
// 例外：ECharts 读不到 CSS 变量，图表配色走 `theme/tokens.ts` 的 useChartColors()。

export const UP_COLOR = 'var(--aq-up)' // 涨 = 红
export const DOWN_COLOR = 'var(--aq-down)' // 跌 = 绿
export const FLAT_COLOR = 'var(--aq-flat)'
export const TEXT_2 = 'var(--aq-text-2)'
export const TEXT_3 = 'var(--aq-text-3)'

export function pctColor(v: number): string {
  if (v > 0) return UP_COLOR
  if (v < 0) return DOWN_COLOR
  return FLAT_COLOR
}

export const isUp = (v: number) => v > 0
export const isDown = (v: number) => v < 0

/** ¥1,234,567.89 */
export function fmtMoney(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  return `¥${v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
}

/** 1.23亿 / 4567.89万 */
export function fmtAmount(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return v.toFixed(2)
}

/** +1.23% / -4.56% */
export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  const sign = v > 0 ? '+' : ''
  return `${sign}${(v * 100).toFixed(digits)}%`
}

export const DIRECTION_LABEL: Record<string, string> = {
  BUY: '买入',
  SELL: '卖出',
  HOLD: '观望',
}

export const SEVERITY_LABEL: Record<string, string> = {
  info: '提示',
  warn: '警告',
  severe: '严重',
}

export const BOARD_LABEL: Record<string, string> = {
  MAIN: '主板',
  CHINEXT: '创业板',
  STAR: '科创板',
  BSE: '北交所',
}

export const STAGE_STATUS_LABEL: Record<string, string> = {
  done: '已完成',
  in_progress: '进行中',
  blocked: '受阻',
  todo: '未开始',
}

export const STAGE_STATUS_COLOR: Record<string, string> = {
  done: '#52c41a',
  in_progress: '#1668dc',
  blocked: '#f5222d',
  todo: '#8c8c8c',
}

/** 数据严重程度 -> 颜色（数据缺口用） */
export const GAP_COLOR: Record<string, string> = {
  high: '#f5222d',
  medium: '#faad14',
  low: '#8c8c8c',
}

/* ------------------------------------------------------------------
   可信度三态
   ------------------------------------------------------------------
   刻意**不用色相**表达 —— 红绿已经被「涨跌」占用，再用红绿标状态会串味
   （一个红标签到底是"涨"还是"已证伪"？）。改由徽章的填充程度区分，
   CSS 类见 global.css 的 .cred-ok / .cred-biased / .cred-none。
   ------------------------------------------------------------------ */
export type CredKind = 'ok' | 'biased' | 'none'

export const CRED_LABEL: Record<CredKind, string> = {
  ok: '可引用',
  biased: '不可引用',
  none: '无数据',
}

export const CRED_HINT: Record<CredKind, string> = {
  ok: '口径无已知系统性偏差，数字可对外引用',
  biased:
    '依赖有偏口径（如现役快照、未并入退市股、低于最小样本量），只能作相对比较，绝对数值不可对外引用',
  none: '本地数据缺失。页面显式标注「未落盘」，而不是返回 0 —— 返回 0 会被读成"今天没涨没跌"',
}

/** 由接口字段推导可信度：available=false → 无数据；quotable=false → 不可引用。 */
export function credOf(o: { quotable?: boolean; available?: boolean }): CredKind {
  if (o.available === false) return 'none'
  if (o.quotable === false) return 'biased'
  return 'ok'
}

/** 1234567 -> 123.46万 */
export function fmtCount(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  if (Math.abs(v) >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (Math.abs(v) >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return String(v)
}

