// A 股配色与格式化工具：红涨绿跌、¥ 计价

export const UP_COLOR = '#f5222d'   // 涨 = 红
export const DOWN_COLOR = '#52c41a' // 跌 = 绿
export const FLAT_COLOR = '#8c8c8c'

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

/** 1234567 -> 123.46万 */
export function fmtCount(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  if (Math.abs(v) >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (Math.abs(v) >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return String(v)
}

