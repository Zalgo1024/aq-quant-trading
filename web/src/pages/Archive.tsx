// 研究产物归档 —— 「我以前跑过什么，**哪些数字现在还引用得起**」。
//
// 为什么这不是「实验室」的附属页：
//   实验室（/lab）回答的是「结论是哪个脚本跑出来的」——它管**代码证据**；
//   归档回答的是「哪一次运行的结果还能写进文章」——它管**运行产物**。
//   两者会各自腐烂：某次重跑之后，README 那句「这个产物可引用」就变成了一句假话，
//   而它既不会报错，也不会缺数据。本页把「口径判定」做成机械的，并与人工表逐行对撞，
//   漂移就摆在明面上 —— 与探针台账同一套思路。
//
// ⛔ 数据全在 runtime/ 下，而 runtime/ 被 gitignore。
//   ⇒ 干净克隆 / 换机器打开本页**必然为空**，这是正确行为，不是 bug。
//   后端因此返回 available=false + 原因；本页据此显示原因，
//   **绝不把空表渲染成「你没跑过任何实验」**。
//
// 视觉约定：可引用 / 不可引用用**填充强度与左侧色条明度**区分，不用红绿
//   （红绿在本项目是涨跌专用）。不可引用项照常显示数字 —— 它们是「不能引用的示范」。
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Space, Table, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import type { ArchiveCscv, ArchiveFactorRun } from '@/types'

/** 数字 → 定长字符串；null/undefined 一律显示「—」，**不显示 0**
 *  （0 和「没算过」在读者眼里是两件事，混掉就等于编数）。 */
function n(x: number | null | undefined, d = 2): string {
  return typeof x === 'number' && Number.isFinite(x) ? x.toFixed(d) : '—'
}

/** 路径只留最后一段：`runtime\factor_research\full_neu_v2` → `full_neu_v2`。
 *  整条路径塞进 190px 的列里会被裁掉前半截，读者只看到一个没有上下文的片段
 *  （实测：只显示成 `full_neu_main`）——不如只放尾段 + Tooltip 给全路径。 */
function tailPath(p?: string): string {
  if (!p) return ''
  const seg = p.replace(/\\/g, '/').split('/').filter(Boolean)
  return seg[seg.length - 1] ?? ''
}

/** 一件事的「有 / 没有」：实心 = 有，空心 = 没有。不用色相。 */
function Flag({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return (
    <span className={`chip ${ok ? 'chip-ok' : 'chip-none'}`}>
      <i />
      {ok ? yes : no}
    </span>
  )
}

const STATE_LABEL: Record<string, { text: string; cls: string }> = {
  neutralized: { text: '中性化', cls: 'chip-ok' },
  raw: { text: 'raw（旧口径）', cls: 'chip-mid' },
  unknown: { text: '口径未知', cls: 'chip-none' },
}

export default function ArchivePage() {
  const { data, isLoading } = useQuery({ queryKey: ['archive'], queryFn: api.archive })

  // 可引用在前：这一页的用途就是「先看能引用的，再看为什么其余不能引用」。
  const cscvRows = useMemo(() => {
    const rows = [...(data?.cscv ?? [])]
    rows.sort((a, b) => {
      if (a.quotable !== b.quotable) return a.quotable ? -1 : 1
      return (b.mtime ?? '').localeCompare(a.mtime ?? '')
    })
    return rows
  }, [data])

  const cscvCols: ColumnsType<ArchiveCscv> = [
    {
      title: '产物',
      dataIndex: 'name',
      width: 250,
      render: (v: string, r) => (
        <div style={{ display: 'flex', gap: 8, alignItems: 'stretch' }}>
          {/* 左侧色条：实心 = 可引用 · 半实心 = 不可引用。明度区分，不占色相。 */}
          <span
            style={{
              width: 2,
              flex: 'none',
              borderRadius: 2,
              background: r.quotable ? 'var(--aq-cred-strong)' : 'var(--aq-cred-mid)',
            }}
          />
          <div style={{ minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 12, wordBreak: 'break-all' }}>
              {v}
            </div>
            <div className="dim" style={{ fontSize: 11, marginTop: 2 }}>
              {r.mtime.slice(0, 10)} · {(r.bytes / 1024).toFixed(1)} KB
              {r.universe ? ` · ${r.universe}` : ''}
            </div>
          </div>
        </div>
      ),
    },
    {
      title: '口径',
      dataIndex: 'quotable',
      width: 108,
      render: (q: boolean, r) => {
        const cl = r.caliber ?? {}
        const key = cl.neutralize ?? 'unknown'
        const tip = (
          <div style={{ fontSize: 12, lineHeight: 1.7 }}>
            <div>
              打分口径：<b>{key}</b>
              {typeof cl.n_with_field === 'number' && typeof cl.n_configs === 'number' ? (
                <>
                  {' '}
                  （{cl.n_with_field}/{cl.n_configs} 个配置带 score_neutralize 字段）
                </>
              ) : null}
            </div>
            <div>超额口径：{cl.excess_caliber || '无 excess_caliber 字段'}</div>
            {r.blockers?.length ? (
              <div style={{ marginTop: 4 }}>
                {r.blockers.map((b, i) => (
                  <div key={i}>· {b}</div>
                ))}
              </div>
            ) : (
              <div style={{ marginTop: 4 }}>无阻断项 —— 可以对外引用这些数字。</div>
            )}
          </div>
        )
        return (
          <Tooltip title={tip}>
            <span className={`chip ${q ? 'chip-ok' : 'chip-mid'}`} style={{ cursor: 'help' }}>
              <i />
              {q ? '可引用' : '不可引用'}
            </span>
          </Tooltip>
        )
      },
    },
    {
      title: '区间 / 基准 / 权重来源',
      width: 188,
      render: (_, r) => (
        <div style={{ fontSize: 11, lineHeight: 1.75 }}>
          <div className="mono">
            {r.period?.start || '—'} ~ {r.period?.end || '—'}
          </div>
          <div className="dim mono">{r.benchmark || '无基准'}</div>
          <Tooltip title={r.weight_basis || '产物 json 里没写 weight_basis'}>
            <div className="dim" style={{ cursor: 'help' }}>
              权重 <span className="mono">{tailPath(r.weight_basis) || '—'}</span>
            </div>
          </Tooltip>
        </div>
      ),
    },
    {
      title: '指标（不可引用时仅为历史留痕）',
      width: 200,
      render: (_, r) => (
        // 数字照常显示：它们是「不能引用的示范」。视觉轻重由左侧色条与口径列承担。
        <div style={{ fontSize: 11, lineHeight: 1.75 }} className="mono">
          <div>
            <span className="dim">PBO </span>
            {n(r.pbo_main, 3)}
            <span className="dim"> / 超额 </span>
            {n(r.pbo_excess, 3)}
          </div>
          <div>
            <span className="dim">best t </span>
            {n(r.best_t)}
            <span className="dim"> · DSR </span>
            {n(r.dsr_main)}
          </div>
        </div>
      ),
    },
    {
      title: '可复核性',
      width: 176,
      render: (_, r) => (
        <Space size={4} wrap>
          <Flag
            ok={!!r.has_returns || (r.n_rets_cache ?? 0) > 0}
            yes="可低成本重算"
            no="只能重跑"
          />
          <Flag ok={!!r.weight_basis_exists} yes="权重基准在盘" no="权重基准已丢" />
          {r.parse_error && (
            <Tooltip title={r.parse_error}>
              <span className="chip chip-mid" style={{ cursor: 'help' }}>
                <i />
                产物损坏
              </span>
            </Tooltip>
          )}
        </Space>
      ),
    },
  ]

  const runCols: ColumnsType<ArchiveFactorRun> = [
    {
      title: '轮次',
      dataIndex: 'name',
      width: 250,
      render: (v: string, r) => (
        <div style={{ display: 'flex', gap: 8, alignItems: 'stretch' }}>
          <span
            style={{
              width: 2,
              flex: 'none',
              borderRadius: 2,
              background:
                r.caliber.state === 'neutralized' ? 'var(--aq-cred-strong)' : 'var(--aq-cred-mid)',
            }}
          />
          <div style={{ minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 12, wordBreak: 'break-all' }}>
              {v}
            </div>
            <div className="dim" style={{ fontSize: 11, marginTop: 2 }}>
              {r.mtime.slice(0, 10)} · {r.n_files} 个文件 · {r.universe || '—'}
            </div>
          </div>
        </div>
      ),
    },
    {
      title: '口径',
      width: 120,
      render: (_, r) => {
        const s = STATE_LABEL[r.caliber.state] ?? { text: r.caliber.state, cls: 'chip-none' }
        return (
          <Tooltip title={r.caliber.note || '中性化口径：IC 与打分在同一面板上估，可引用。'}>
            <span className={`chip ${s.cls}`} style={{ cursor: 'help' }}>
              <i />
              {s.text}
            </span>
          </Tooltip>
        )
      },
    },
    {
      title: '区间',
      width: 168,
      render: (_, r) => (
        <span className="mono dim" style={{ fontSize: 11 }}>
          {r.start || '—'} ~ {r.end || '—'}
        </span>
      ),
    },
    {
      title: '规模',
      width: 190,
      render: (_, r) => (
        <div style={{ fontSize: 11, lineHeight: 1.75 }} className="mono dim">
          <div>
            {r.n_symbols ?? '—'} 只 · {r.n_days ?? '—'} 日 · {r.n_factors ?? '—'} 因子
          </div>
          <div>
            {r.n_rows != null ? r.n_rows.toLocaleString() : '—'} 行面板
          </div>
        </div>
      ),
    },
    {
      title: '显著 / 强',
      width: 110,
      render: (_, r) => (
        <span className="mono" style={{ fontSize: 11 }}>
          {r.n_significant ?? '—'} / {r.n_strong ?? '—'}
        </span>
      ),
    },
    {
      title: '产物',
      width: 150,
      render: (_, r) => (
        <Space size={4} wrap>
          <Flag ok={r.has_report} yes="报告" no="无报告" />
          <Flag ok={r.has_summary} yes="明细" no="无明细" />
          {!r.has_meta && <Flag ok={false} yes="" no="无 meta" />}
        </Space>
      ),
    },
  ]

  if (data && !data.available) {
    // 「读不到」≠「没有」。把后端给的原因原样显示，别让空表看起来像"跑过的实验一个都没有"。
    return (
      <Alert
        type="warning"
        showIcon
        message="研究产物归档不可用（这台机器上没有 runtime/ 运行历史）"
        description={
          <div style={{ fontSize: 12, lineHeight: 1.8 }}>
            {data.unavailable_reason}
            <div className="dim" style={{ marginTop: 6 }}>
              这不是缺陷：归档是**本机运行历史**，不是仓库内容。要看到内容，需在本机跑过
              <span className="mono"> runtime/cscv/*.json </span>或
              <span className="mono"> runtime/factor_research/*/ </span>。
            </div>
          </div>
        }
      />
    )
  }

  const s = data?.summary
  const drift = data?.readme_drift ?? []

  return (
    <div>
      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: '10px 16px' } }}>
        <Space size={22} wrap>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              CSCV 产物
            </span>{' '}
            <span className="mono">{s?.n_cscv ?? 0}</span>
          </span>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              可引用
            </span>{' '}
            <span className="mono">{s?.n_quotable ?? 0}</span>
          </span>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              不可引用
            </span>{' '}
            <span className="mono">{s?.n_stale ?? 0}</span>
          </span>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              因子轮次
            </span>{' '}
            <span className="mono">{s?.n_factor_runs ?? 0}</span>
          </span>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              最后更新
            </span>{' '}
            <span className="mono">{(s?.latest_mtime || '—').slice(0, 16).replace('T', ' ')}</span>
          </span>
          {/* README 人工表 vs 机械判定：对不上就是"人写的表已腐烂"，属红色级问题，
              但仍用明度表达（红绿留给涨跌）。 */}
          {!data?.readme_available ? (
            <Tooltip title="README 第 5 节的「历史结果口径清点」表没解析出来 —— 无法与机械判定对撞。缺了这层对撞，人工表腐烂时不会有人发现。">
              <span className="chip chip-none" style={{ cursor: 'help' }}>
                <i />
                README 表不可用
              </span>
            </Tooltip>
          ) : drift.length ? (
            <Tooltip
              title={
                <div style={{ fontSize: 12 }}>
                  {drift.map((d, i) => (
                    <div key={i}>
                      · {d.name}：
                      {d.kind === 'missing_on_disk'
                        ? 'README 列了但磁盘上没有'
                        : `README 写 ${d.readme ? '可引用' : '不可引用'}、实算 ${d.computed ? '可引用' : '不可引用'}`}
                    </div>
                  ))}
                </div>
              }
            >
              <span className="chip chip-mid" style={{ cursor: 'help' }}>
                <i />
                README 表与实算不一致 <span className="mono">{drift.length}</span>
              </span>
            </Tooltip>
          ) : (
            <span className="chip chip-ok">
              <i />
              README 表 {data?.readme_n_rows ?? 0} 行 · 判定零漂移
            </span>
          )}
          {(s?.n_missing_weight_basis ?? 0) > 0 && (
            <Tooltip title="这些产物的权重基准目录已不在磁盘上 —— 数字本身还能读，但这次运行无法原样复现。">
              <span className="chip chip-mid" style={{ cursor: 'help' }}>
                <i />
                权重基准已丢 <span className="mono">{s?.n_missing_weight_basis}</span>
              </span>
            </Tooltip>
          )}
          {(s?.n_without_returns ?? 0) > 0 && (
            <Tooltip title="没有 *_returns.csv 也没有旁边目录的收益缓存 ⇒ 想复核只能整轮重跑，不能只重算统计量。">
              <span className="chip chip-none" style={{ cursor: 'help' }}>
                <i />
                只能重跑 <span className="mono">{s?.n_without_returns}</span>
              </span>
            </Tooltip>
          )}
        </Space>
      </Card>

      {data?.caveat && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message={data.caveat} />
      )}

      <Card
        size="small"
        title="CSCV 交叉验证产物（runtime/cscv/）"
        style={{ marginBottom: 12 }}
      >
        <Table
          rowKey="name"
          size="small"
          loading={isLoading}
          columns={cscvCols}
          dataSource={cscvRows}
          scroll={{ x: 940 }}
          locale={{ emptyText: 'runtime/cscv/ 下没有 *.json' }}
          pagination={{ pageSize: 20, size: 'small', showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
        />
      </Card>

      <Card size="small" title="因子研究轮次（runtime/factor_research/）" style={{ marginBottom: 12 }}>
        <Table
          rowKey="path"
          size="small"
          loading={isLoading}
          columns={runCols}
          dataSource={data?.factor_runs ?? []}
          scroll={{ x: 900 }}
          locale={{ emptyText: 'runtime/factor_research/ 下没有轮次目录' }}
          pagination={{ pageSize: 20, size: 'small', showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
        />
      </Card>

      {(data?.readme_unlisted?.length ?? 0) > 0 && (
        <Card size="small" title="磁盘有、README 表未列（提示级）" style={{ marginBottom: 12 }}>
          <div className="dim2" style={{ fontSize: 12, marginBottom: 8 }}>
            这些产物存在于本机，但有没有把口径状态写进 README 第 5 节 ——
            读者据此无从判断能不能引用。属提示，不是错误。
          </div>
          <Space size={6} wrap>
            {data!.readme_unlisted.map((x) => (
              <span key={x} className="chip chip-none mono" style={{ fontSize: 11 }}>
                <i />
                {x}
              </span>
            ))}
          </Space>
        </Card>
      )}

      {(data?.cache_only_dirs?.length ?? 0) > 0 && (
        <Card size="small" title="只剩收益缓存、结果 json 已丢" style={{ marginBottom: 12 }}>
          <div className="dim2" style={{ fontSize: 12, marginBottom: 8 }}>
            收益还在 ⇒ 统计量可重算；但当时的口径标记已经丢了，
            <b>即使重算也拿不回「当时那版结论」</b>。
          </div>
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            {data!.cache_only_dirs.map((c) => (
              <div key={c.path} className="mono dim" style={{ fontSize: 11 }}>
                {c.name} · {c.n_files} 个文件 · {c.mtime.slice(0, 10)}
              </div>
            ))}
          </Space>
        </Card>
      )}

      {(data?.rules?.length ?? 0) > 0 && (
        <Card size="small" title="口径判定规则（规则写在 README 第 5 节，本页只做机械执行）">
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            {data!.rules.map((r, i) => (
              <div key={i} style={{ fontSize: 12, lineHeight: 1.75 }}>
                <span style={{ color: 'var(--aq-cred-mid)' }}>—</span>{' '}
                <span className="dim2">{r}</span>
              </div>
            ))}
          </Space>
          <div className="dim" style={{ fontSize: 11, marginTop: 8 }}>
            归档根目录 <span className="mono">{data?.root}</span>（在 .gitignore 内）。
            <Link to="/lab" style={{ fontSize: 11, marginLeft: 8 }}>
              结论 ↔ 探针脚本 见「实验室」
            </Link>
          </div>
        </Card>
      )}
    </div>
  )
}
