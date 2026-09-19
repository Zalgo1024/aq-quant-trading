// 顶部研究状态条 —— 一行回答三个问题：
//   1) 走到哪一步了   → 三阶段结论 chip（A/B/C）
//   2) 数据有多新     → 数据截至 / 全市场只数 / 流动性池
//   3) 有没有故障     → 快照未就绪 / 后端代码已过期
//
// 全站常驻。旧版 Header 显示的是"总资产 ¥0.00"（账户没投钱，会被误读成
// "钱没了"），后来改成数据口径，但漏了最关键的一问：我在哪一步。
import { useQuery } from '@tanstack/react-query'
import { Segmented, Tooltip, Typography } from 'antd'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import { useTheme, type ThemeMode } from '@/theme/store'
import type { Stage } from '@/types'

const DEAD = /否决|失败|封存/
const OK = /完成|通过|已收口/

function stageChip(s: Stage): { cls: string; text: string; hint: string } {
  const o = s.outcome
  if (o) {
    const dead = DEAD.test(o)
    const ok = OK.test(o) && !dead
    return {
      cls: dead ? 'chip-mid' : ok ? 'chip-ok' : 'chip-none',
      text: o,
      hint: s.outcome_reason || s.goal,
    }
  }
  // 没有人工结论时退化为按进度推断（不编造结论，只说进度）
  if (s.progress >= 1) return { cls: 'chip-ok', text: '完成', hint: s.goal }
  return { cls: 'chip-none', text: `${s.n_done}/${s.n_items} 项`, hint: s.goal }
}

export default function StatusBar() {
  const { mode, setMode } = useTheme()
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })
  const { data: status } = useQuery({ queryKey: ['project-status'], queryFn: api.projectStatus })

  const stages = status?.stages ?? []
  const snapErr = health?.snapshot_error
  const stale = health?.code?.backend_stale
  const dim: React.CSSProperties = { fontSize: 12, color: 'var(--aq-text-3)' }

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        flexWrap: 'wrap',
        width: '100%',
        minWidth: 0,
      }}
    >
      {/* ---- 走到哪一步 ---- */}
      {stages.map((s) => {
        const c = stageChip(s)
        return (
          <Tooltip key={s.id} title={`${s.name}：${c.hint}`}>
            <Link to="/status" className={`chip ${c.cls}`} style={{ cursor: 'pointer' }}>
              <i />
              <b className="mono" style={{ fontWeight: 500 }}>{s.id}</b>
              {c.text}
            </Link>
          </Tooltip>
        )
      })}

      <span style={{ width: 1, height: 16, background: 'var(--aq-border)' }} />

      {/* ---- 数据有多新 ---- */}
      {snapErr ? (
        <Tooltip title={snapErr}>
          <span className="chip chip-mid" style={{ cursor: 'help' }}>
            <i />
            行情快照未就绪
          </span>
        </Tooltip>
      ) : (
        <Typography.Text style={dim}>
          数据截至 <span className="mono">{health?.data_asof || '—'}</span>
          {' · '}全市场 <span className="mono">{health?.n_active ?? 0}</span>
          {' · '}流动性池 <span className="mono">{health?.n_liquid ?? 0}</span>
        </Typography.Text>
      )}

      {stale && (
        <Tooltip title="端口上跑的是旧后端代码（源码已改但进程未重启）。用 启动.bat 会停掉旧进程再起新的。">
          <span className="chip chip-mid" style={{ cursor: 'help' }}>
            <i />
            后端代码已过期
          </span>
        </Tooltip>
      )}

      {/* ---- 右侧：主题 / 后台 / 版本 ---- */}
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 12 }}>
        <Segmented
          size="small"
          value={mode}
          onChange={(v) => setMode(v as ThemeMode)}
          options={[
            { label: '浅色', value: 'light' },
            { label: '深色', value: 'dark' },
          ]}
        />
        <Link to="/admin" style={dim}>
          后台
        </Link>
        <Typography.Text style={{ ...dim, fontSize: 11 }}>
          v{health?.version ?? '-'}
        </Typography.Text>
      </div>
    </div>
  )
}
