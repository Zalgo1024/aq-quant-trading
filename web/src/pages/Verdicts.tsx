// 结论台账 —— 独立页面（首页只放前 5 条摘要）。
//
// 这一页的全部意义：让"我们验证过什么"可检索、可追溯、不可被遗忘。
// 每条都能点进 docs/ 下的源文档，"可引用/不可引用"由接口的 quotable 字段驱动。
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Empty, Segmented, Spin, Typography } from 'antd'
import api from '@/services/api'
import VerdictTable from '@/components/VerdictTable'

const KILLED = /证伪|否决|失败/
const SEALED = /封存/

type Filter = 'ALL' | 'KILLED' | 'SEALED' | 'OTHER'

export default function Verdicts() {
  const [filter, setFilter] = useState<Filter>('ALL')
  const { data, isLoading } = useQuery({
    queryKey: ['project-status'],
    queryFn: api.projectStatus,
  })

  const all = data?.verdicts ?? []
  const counts = useMemo(() => {
    const killed = all.filter((v) => KILLED.test(v.verdict)).length
    const sealed = all.filter((v) => SEALED.test(v.verdict)).length
    return { all: all.length, killed, sealed, other: all.length - killed - sealed }
  }, [all])

  const rows = useMemo(() => {
    if (filter === 'KILLED') return all.filter((v) => KILLED.test(v.verdict))
    if (filter === 'SEALED') return all.filter((v) => SEALED.test(v.verdict))
    if (filter === 'OTHER')
      return all.filter((v) => !KILLED.test(v.verdict) && !SEALED.test(v.verdict))
    return all
  }, [all, filter])

  if (isLoading) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (!data) return <Empty description="暂无状态数据（接口未返回）" />
  if (data.doc_error) {
    return <Alert type="error" showIcon message="状态数据源不可用" description={data.doc_error} />
  }

  return (
    <div>
      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: '12px 16px' } }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            flexWrap: 'wrap',
            justifyContent: 'space-between',
          }}
        >
          <div>
            <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>结论台账</div>
            <Typography.Text style={{ fontSize: 12, color: 'var(--aq-text-3)' }}>
              来源 <span className="mono">docs/研究进展.json</span> · 「已证伪」= 有统计证据表明
              不成立，<b>不是</b>「还没测出来」
            </Typography.Text>
          </div>
          <Segmented
            value={filter}
            onChange={(v) => setFilter(v as Filter)}
            options={[
              { label: `全部 ${counts.all}`, value: 'ALL' },
              { label: `已证伪 ${counts.killed}`, value: 'KILLED' },
              { label: `已封存 ${counts.sealed}`, value: 'SEALED' },
              { label: `其他 ${counts.other}`, value: 'OTHER' },
            ]}
          />
        </div>
      </Card>

      {filter === 'SEALED' ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="已封存 = 不再投入，但也没被证伪"
          description="与「已证伪」不同：封存是判断「投入产出不划算」（如数据功效不足、无成本模型），而不是拿到了反证。若将来数据条件变了，可以重新开预注册文档再试。"
        />
      ) : null}

      <Card size="small">
        <VerdictTable verdicts={rows} pageSize={20} />
      </Card>
    </div>
  )
}
