// 主题模式（浅色 / 深色）—— 全局唯一状态源。
//
// 设计要点
// --------
// 1. 落 localStorage，刷新不丢；读失败退化为默认值，**不抛错**
//    （隐私模式下 localStorage 访问会抛 SecurityError）。
// 2. 同步写 `document.documentElement.dataset.theme` —— global.css 里
//    全部颜色变量都挂在 `[data-theme=...]` 选择器上，CSS 侧零 JS 参与。
// 3. 模块被 import 时立即 apply 一次，保证首帧就是正确主题（不闪白）。
// 4. ECharts 读不到 CSS 变量，所以图表配色单独走 `theme/tokens.ts`。
import { create } from 'zustand'

export type ThemeMode = 'light' | 'dark'

const KEY = 'aq-theme'
export const DEFAULT_MODE: ThemeMode = 'light'

function read(): ThemeMode {
  try {
    const v = localStorage.getItem(KEY)
    if (v === 'light' || v === 'dark') return v
  } catch {
    // localStorage 不可用 —— 退化为默认值，不影响功能
  }
  return DEFAULT_MODE
}

function apply(m: ThemeMode): void {
  try {
    document.documentElement.dataset.theme = m
  } catch {
    // 非浏览器环境（SSR / 测试）静默跳过
  }
}

interface ThemeState {
  mode: ThemeMode
  setMode: (m: ThemeMode) => void
  toggle: () => void
}

export const useTheme = create<ThemeState>((set, get) => ({
  mode: read(),
  setMode: (m) => {
    try {
      localStorage.setItem(KEY, m)
    } catch {
      // 存不进去也不影响本次会话
    }
    apply(m)
    set({ mode: m })
  },
  toggle: () => get().setMode(get().mode === 'dark' ? 'light' : 'dark'),
}))

apply(useTheme.getState().mode)
