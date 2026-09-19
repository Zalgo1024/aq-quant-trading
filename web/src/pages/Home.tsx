// 新首页 —— 研究进展总览。
//
// 为什么首页不是「市场总览」：这个项目最大的风险不是看错行情，是
// **重复检验已经证伪的东西**。而旧版把这条信息埋在「研究进展」页的三级以下，
// 要点两次才看得到。放到首屏，一眼就知道 B、C 都死了、死因是什么。
//
// 布局：主栏（三条路线 + 结论台账摘要） + 右栏（下一步 + 数据健康）。
import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Empty, Spin, Tooltip, Typography } from 'antd'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import VerdictTable from '@/components/VerdictTable'
import type { Stage } from '@/types'

const DEAD = /否决|失败|封存/
const OK = /完成|通过|已收口/

function stageView(s: Stage) {
  const o = s.outcome
  if (o) {
    const dead = DEAD.test(o)
    const ok = OK.test(o) && !dead
    return { cls: dead ? 'chip-mid' : ok ? 'chip-ok' : 'chip-none', text: o, dead, ok }
  }
  if (s.progress >= 1) return { cls: 'chip-ok', text: '完成', dead: false, ok: true }
  return { cls: 'chip-none', text: `进行中 ${s.n_done}/${s.n_items}`, dead: false, ok: false }
}

export default function Home() {
  const { data, isLoading } = useQuery({
    queryKey: ['project-status'],
    queryFn: api.projectStatus,
  })

  if (isLoading) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (!data) return <Empty description="暂无状态数据（接口未返回）" />
  if (data.doc_error) {
    return (
      <Alert type="error" showIcon message="状态数据源不可用" description={data.doc_error} />
    )
  }

  const a = data.data_assets
  const stLow = a.st_flags.ratio < 0.9
  const idxMissing = a.index_bars.n_available < a.index_bars.n_expected
  const highGaps = data.gaps.filter((g) => g.severity === 'high')
  const dim: React.CSSProperties = { fontSize: 12, color: 'var(--aq-text-3)' }

  return (
    <div className="home-grid">
      {/* ================= 主栏 ================= */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
        <Card size="small" styles={{ body: { padding: '10px 16px' } }}>
          <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', ...dim }}>
            <span>
              结论更新于 <span className="mono">{data.updated_at || '—'}</span>
            </span>
            <span>
              统计生成于 <span className="mono">{data.generated_at}</span>
            </span>
            {data.repo.commit && (
              <Tooltip title={data.repo.subject}>
                <span>
                  当前提交 <span className="mono">{data.repo.commit}</span>（
                  {data.repo.branch} · {data.repo.date}）
                </span>
              </Tooltip>
            )}
          </div>
        </Card>

        <Card
          size="small"
          title="三条路线，一次看清"
          extra={
            <Typography.Text style={dim}>
              别再重复检验已证伪的东西
            </Typography.Text>
          }
        >
          {data.stages.map((s, i) => {
            const v = stageView(s)
            return (
              <div
                key={s.id}
                className={i ? 'rowdiv' : undefined}
                style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '9px 0' }}
              >
                <span
                  className="mono"
                  style={{ fontSize: 12, color: 'var(--aq-text-3)', width: 12, flex: 'none' }}
                >
                  {s.id}
                </span>
                <div style={{ flex: 'minmax(0, 1fr)' }}>
                  <div style={{ fontSize: 13 }}>{s.name}</div>
                  <div style={{ fontSize: 11, color: 'var(--aq-text-3)', marginTop: 2 }}>
                    {v.dead && s.outcome_reason ? s.outcome_reason : s.goal}
                  </div>
                </div>
                <Tooltip title={`${s.n_done}/${s.n_items} 项完成`}>
                  <span className={`chip ${v.cls}`} style={{ cursor: 'help' }}>
                    <i />
                    {v.text}
                  </span>
                </Tooltip>
              </div>
            )
          })}
        </Card>

        <Card
          size="small"
          title={`结论台账　（${data.verdicts.length} 条）`}
          extra={
            <Link to="/verdicts" style={{ fontSize: 12 }}>
              查看全部 ›
            </Link>
          }
        >
          <VerdictTable verdicts={data.verdicts.slice(0, 5)} showSource={false} />
        </Card>
      </div>

      {/* ================= 右栏 ================= */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
        <Card size="small" title="下一步">
          <ol
            style={{
              margin: 0,
              paddingLeft: 18,
              fontSize: 12,
              lineHeight: 1.8,
              color: 'var(--aq-text-2)',
            }}
          >
            {data.next_steps.map((t) => (
              <li key={t} style={{ marginBottom: 6 }}>
                {t}
              </li>
            ))}
          </ol>
        </Card>

        <Card
          size="small"
          title="数据健康"
          extra={
            <Link to="/status" style={{ fontSize: 12 }}>
              明细 ›
            </Link>
          }
        >
          <div className="rowdiv" style={{ padding: '6px 0', fontSize: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ flex: 1 }}>ST 逐日覆盖</span>
              <span className="mono">{(a.st_flags.ratio * 100).toFixed(1)}%</span>
              {stLow && (
                <span className="chip chip-mid" style={{ cursor: 'help' }}>
                  <i />
                  不足
                </span>
              )}
            </div>
          </div>
          <div className="rowdiv" style={{ padding: '6px 0', fontSize: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ flex: 1 }}>退市股行情</span>
              <span className="mono">
                {a.delisted_bars.n_files}/{a.delisted_bars.n_listed}
              </span>
            </div>
          </div>
          <div className="rowdiv" style={{ padding: '6px 0', fontSize: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ flex: 1 }}>指数落盘</span>
              <span className="mono">
                {a.index_bars.n_available}/{a.index_bars.n_expected}
              </span>
              {idxMissing && (
                <Tooltip title={`缺 ${a.index_bars.missing.join('、')}`}>
                  <span className="chip chip-none" style={{ cursor: 'help' }}>
                    <i />
                    缺 {a.index_bars.missing.length}
                  </span>
                </Tooltip>
              )}
            </div>
          </div>
          <div className="rowdiv" style={{ padding: '6px 0', fontSize: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ flex: 1 }}>ETF 净值 / 日线</span>
              <span className="mono">
                {a.etf.n_nav} / {a.etf.n_bars}
              </span>
            </div>
          </div>

          {highGaps.length > 0 && (
            <div
              style={{
                marginTop: 10,
                paddingTop: 10,
                borderTop: '0.5px solid var(--aq-border)',
                fontSize: 11,
                lineHeight: 1.7,
                color: 'var(--aq-text-3)',
              }}
            >
              高优先缺口 {highGaps.length} 条：
              {highGaps.map((g) => `「${g.name}」`).join('')}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
