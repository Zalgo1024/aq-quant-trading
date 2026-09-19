// 主题 token 映射。
//
// 两套消费方：
// - antd 组件：走 `antdConfig(mode)`（ConfigProvider theme）
// - ECharts：**读不到 CSS 变量**，必须给最终 hex，走 `useChartColors()`
//
// ⚠️ 红涨绿跌是本项目的硬约定（A 股口径），两套主题下都保持"涨=红、跌=绿"，
// 只调整明度以保证对比度：浅色画布上用深一档的红/绿，深色画布上用亮一档。
import { theme as antdTheme, type ThemeConfig } from 'antd'
import { useTheme, type ThemeMode } from './store'

const UP: Record<ThemeMode, string> = { light: '#cf1322', dark: '#f5222d' }
const DOWN: Record<ThemeMode, string> = { light: '#389e0d', dark: '#52c41a' }
const FLAT: Record<ThemeMode, string> = { light: '#8c8c8c', dark: '#8c8c8c' }

export function antdConfig(mode: ThemeMode): ThemeConfig {
  const dark = mode === 'dark'
  return {
    algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      colorPrimary: '#1668dc',
      // 语义 token 借用 antd 的位置，但含义按 A 股口径命名：
      // colorError = 涨（红）、colorSuccess = 跌（绿）
      colorError: UP[mode],
      colorSuccess: DOWN[mode],
      colorWarning: '#faad14',
      borderRadius: 8,
      fontSize: 13,
      fontFamily:
        "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",
    },
    components: {
      Layout: {
        headerBg: dark ? '#1f1f1f' : '#ffffff',
        bodyBg: dark ? '#141414' : '#f5f5f7',
        footerBg: dark ? '#1f1f1f' : '#ffffff',
        siderBg: dark ? '#141414' : '#ffffff',
      },
      Menu: {
        itemHeight: 34,
        itemMarginInline: 6,
      },
    },
  }
}

/** ECharts 等"读不到 CSS 变量"的场景需要的是最终 hex，不能用 var()。 */
export interface ChartColors {
  up: string
  down: string
  flat: string
  /** 坐标轴 / 图例文字 */
  text: string
  /** 次级文字（轴标签） */
  textSecondary: string
  /** 网格分割线 */
  split: string
  primary: string
  warn: string
  /** 图表所在卡片的底色（tooltip / 空心柱需要） */
  surface: string
  /** tooltip 底色。旧实现三处图表都硬编码 rgba(30,30,30,0.95)，
   *  浅色主题下就是"白页面上飘一块黑板"，且里面的 #ccc 图例在白底上几乎读不出来。 */
  tooltipBg: string
  tooltipBorder: string
}

const CHART: Record<ThemeMode, ChartColors> = {
  light: {
    up: UP.light,
    down: DOWN.light,
    flat: FLAT.light,
    text: 'rgba(0,0,0,0.88)',
    textSecondary: 'rgba(0,0,0,0.45)',
    split: 'rgba(0,0,0,0.08)',
    primary: '#1668dc',
    warn: '#d48806',
    surface: '#ffffff',
    tooltipBg: 'rgba(255,255,255,0.96)',
    tooltipBorder: 'rgba(0,0,0,0.15)',
  },
  dark: {
    up: UP.dark,
    down: DOWN.dark,
    flat: FLAT.dark,
    text: 'rgba(255,255,255,0.85)',
    textSecondary: 'rgba(255,255,255,0.45)',
    split: 'rgba(255,255,255,0.10)',
    primary: '#1668dc',
    warn: '#faad14',
    surface: '#1f1f1f',
    tooltipBg: 'rgba(30,30,30,0.95)',
    tooltipBorder: '#444444',
  },
}

export function chartColors(mode: ThemeMode): ChartColors {
  return CHART[mode]
}

/** 给 hex 加透明度（ECharts 的 areaStyle 需要 rgba，不能直接用 8 位 hex）。 */
export function withAlpha(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/** 图表配色的 hook 版本（随主题切换自动重渲染）。 */
export function useChartColors(): ChartColors {
  return CHART[useTheme((s) => s.mode)]
}

export const THEME_UP = UP
export const THEME_DOWN = DOWN
