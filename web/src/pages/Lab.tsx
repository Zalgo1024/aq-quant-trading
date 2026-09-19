// 实验室 —— 「这些结论是哪个脚本跑出来的、怎么复现」。
//
// 为什么值得单独一页：本项目最贵的资产不是代码，也不是数据，而是**已经判死的结论**。
// 一条「我们验证过 X，结论是不成立，证据在这里」如果能被独立复核，它就还有价值；
// 复核不了，它就只是一句话。这一页把「结论 ↔ 探针脚本 ↔ 文档 ↔ 复现命令」
// 串成一条可点、可查、可执行的链。
//
// 数据来源刻意分两半：登记信息读 `scripts/probes/README.md`（人写的），
// 文件状态由后端实时扫盘。**两者对照出的偏差会显式列出来** ——
// 台账过期时既不报错也不缺数据，只能靠交叉校对抓出来。
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Input, Segmented, Space, Table, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import api from '@/services/api'
import type { ProbeEntry } from '@/types'
import { REPO_BLOB } from '@/components/VerdictTable'

/** 缺失/漂移提示：一律用明度 chip，不用红绿（红绿被涨跌占用）。 */
function Drift({ label, items, hint }: { label: string; items: string[]; hint: string }) {
  if (!items.length) return null
  return (
    <Tooltip title={hint}>
      <span className="chip chip-mid" style={{ cursor: 'help' }}>
        <i />
        {label} <span className="mono">{items.length}</span>
      </span>
    </Tooltip>
  )
}

export default function LabPage() {
  const [kw, setKw] = useState('')
  const [onlyStar, setOnlyStar] = useState<'ALL' | 'STAR'>('ALL')

  const { data, isLoading } = useQuery({ queryKey: ['probes'], queryFn: api.probes })

  const entries = data?.entries ?? []
  const rows = useMemo(() => {
    const k = kw.trim().toLowerCase()
    return entries.filter((e) => {
      if (onlyStar === 'STAR' && !e.starred) return false
      if (!k) return true
      return (
        e.script.toLowerCase().includes(k) ||
        e.question.toLowerCase().includes(k) ||
        e.deps.toLowerCase().includes(k) ||
        e.docstring.toLowerCase().includes(k)
      )
    })
  }, [entries, kw, onlyStar])

  const nStar = entries.filter((e) => e.starred).length
  const nUnresolvedDoc = entries.filter((e) => !e.doc_path).length

  const columns: ColumnsType<ProbeEntry> = [
    {
      title: '探针脚本',
      dataIndex: 'script',
      width: 226,
      render: (v: string, r) => (
        <div style={{ display: 'flex', gap: 8, alignItems: 'stretch' }}>
          <span
            style={{
              width: 2,
              flex: 'none',
              borderRadius: 2,
              background: r.starred ? 'var(--aq-cred-strong)' : 'var(--aq-cred-mid)',
            }}
          />
          <div style={{ minWidth: 0 }}>
            <Typography.Link
              className="mono"
              href={`${REPO_BLOB}/scripts/probes/${v}`}
              target="_blank"
              rel="noreferrer"
              style={{ fontSize: 12, wordBreak: 'break-all' }}
            >
              {v}
            </Typography.Link>
            <div className="dim" style={{ fontSize: 11, marginTop: 2 }}>
              {r.exists ? (
                <>
                  {r.n_lines} 行 · {r.mtime.slice(0, 10)}
                </>
              ) : (
                <span>磁盘上没有这个文件</span>
              )}
            </div>
          </div>
        </div>
      ),
    },
    {
      title: '回答什么问题',
      dataIndex: 'question',
      render: (v: string, r) => (
        <div style={{ fontSize: 12, lineHeight: 1.7 }}>
          <div>{v || '—'}</div>
          {r.docstring && (
            <div className="dim" style={{ fontSize: 11, marginTop: 2 }}>
              {r.docstring}
            </div>
          )}
        </div>
      ),
    },
    {
      title: '依赖',
      dataIndex: 'deps',
      width: 190,
      render: (v: string) => (
        <span className="mono dim" style={{ fontSize: 11, lineHeight: 1.6 }}>
          {v || '—'}
        </span>
      ),
    },
    {
      title: '支撑的结论',
      dataIndex: 'doc_ref',
      width: 200,
      render: (v: string, r) =>
        r.doc_path ? (
          <Tooltip title={`打开 ${r.doc_path}${r.doc_section ? ` §${r.doc_section}` : ''}`}>
            <Typography.Link
              href={`${REPO_BLOB}/${r.doc_path}`}
              target="_blank"
              rel="noreferrer"
              style={{ fontSize: 11 }}
            >
              {v}
            </Typography.Link>
          </Tooltip>
        ) : (
          <span className="dim" style={{ fontSize: 11 }}>
            {v}
          </span>
        ),
    },
  ]

  if (data && !data.available) {
    // 解析不出台账 ≠ 台账是空的。把原因原样显示，别让空表看起来像"没有探针"。
    return (
      <Alert
        type="warning"
        showIcon
        message="探针台账不可用"
        description={data.reason ?? '未知原因'}
      />
    )
  }

  return (
    <div>
      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: '10px 16px' } }}>
        <Space size={22} wrap>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              已登记
            </span>{' '}
            <span className="mono">{data?.n_registered ?? 0}</span>
          </span>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              磁盘实际
            </span>{' '}
            <span className="mono">{data?.n_on_disk ?? 0}</span>
          </span>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              可点回文档
            </span>{' '}
            <span className="mono">{data?.n_resolved_doc ?? 0}</span>
            {nUnresolvedDoc > 0 && (
              <span className="dim" style={{ fontSize: 11 }}>
                {' '}
                （{nUnresolvedDoc} 条只有文字引用）
              </span>
            )}
          </span>
          <span>
            <span className="dim" style={{ fontSize: 12 }}>
              重点
            </span>{' '}
            <span className="mono">{nStar}</span>
          </span>
          <Drift
            label="清单有磁盘无"
            items={data?.missing ?? []}
            hint="README 登记了这些脚本，但磁盘上找不到 —— 文档正在引用一个不存在的证据，无法复核。"
          />
          <Drift
            label="磁盘有清单无"
            items={data?.unregistered ?? []}
            hint="这些脚本存在于仓库，但没进 README 台账 —— 证据做了却没人看得到。"
          />
          <Drift
            label="引用文档不存在"
            items={data?.broken_doc ?? []}
            hint="台账里指向的文档路径解析不到文件。"
          />
          {(data?.missing?.length ?? 0) + (data?.unregistered?.length ?? 0) > 0 ? (
            <span className="chip chip-mid">
              <i />
              台账已漂移
            </span>
          ) : (
            <span className="chip chip-ok">
              <i />
              台账与磁盘一致
            </span>
          )}
        </Space>
      </Card>

      {data?.caveat && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message={data.caveat} />
      )}

      <Card
        size="small"
        title="探针台账"
        extra={
          <Space size={8}>
            <Input
              size="small"
              allowClear
              placeholder="搜脚本 / 问题 / 依赖"
              value={kw}
              onChange={(e) => setKw(e.target.value)}
              style={{ width: 200 }}
            />
            <Segmented
              size="small"
              value={onlyStar}
              onChange={(v) => setOnlyStar(v as 'ALL' | 'STAR')}
              options={[
                { label: `全部 ${entries.length}`, value: 'ALL' },
                { label: `重点 ${nStar}`, value: 'STAR' },
              ]}
            />
          </Space>
        }
      >
        <Table
          rowKey="script"
          size="small"
          loading={isLoading}
          columns={columns}
          dataSource={rows}
          locale={{ emptyText: '没有匹配的探针' }}
          pagination={{ pageSize: 15, size: 'small', showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
        />
      </Card>

      {(data?.conventions?.length ?? 0) > 0 && (
        <Card size="small" title="三条约定（写探针前先看）" style={{ marginTop: 12 }}>
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            {data!.conventions!.map((c) => (
              <div key={c} style={{ fontSize: 12, lineHeight: 1.75 }}>
                <span style={{ color: 'var(--aq-cred-mid)' }}>—</span>{' '}
                <span className="dim2">{c}</span>
              </div>
            ))}
          </Space>
        </Card>
      )}

      {data?.prereq && (
        <Card size="small" title="复现前置（数据不在仓库里，需自行拉取）" style={{ marginTop: 12 }}>
          <pre className="mono codeblock">{data.prereq}</pre>
          <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
            探针依赖 <span className="mono">data_cache/</span>（在 .gitignore 内）。
            缺数据时它们应当报错或跳过，<b>而不是给一个看起来正常但其实是空集的数字</b>。
          </div>
        </Card>
      )}

      {(data?.pitfalls?.length ?? 0) > 0 && (
        <Card size="small" title="通用陷阱（都在这堆脚本里踩过）" style={{ marginTop: 12 }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {data!.pitfalls!.map((p, i) => (
              <div key={i} style={{ fontSize: 12, lineHeight: 1.8 }}>
                <span className="mono dim" style={{ marginRight: 6 }}>
                  {i + 1}
                </span>
                <span className="dim2">{p}</span>
              </div>
            ))}
          </Space>
        </Card>
      )}
    </div>
  )
}
