// 结论台账 —— 全项目最有价值的资产：每条"我们验证过什么、结论是什么、证据是什么"。
//
// 从旧版「研究进展」页里的一张内嵌表，提升为独立页面 + 首页摘要两处复用。
//
// 视觉约定：**判定不使用色相**。颜色在本项目里只表达涨跌（A 股约定），
// 用红/绿标"已证伪"会与价格涨跌串味。这里改用左侧的明度色条 + 文字本身
// 表达轻重（已证伪 = 深条 + 正常字重；已封存 = 浅条 + 次级色）。
import { Table, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import CredibilityBadge from './CredibilityBadge'
import type { Verdict } from '@/types'

/** 公开仓库的 blob 根路径，用于把 docs/xxx.md 变成可点的源文档 */
export const REPO_BLOB = 'https://github.com/Zalgo1024/aq-quant-trading/blob/master'

const NEG = /证伪|否决|失败|封存/

interface Props {
  verdicts: Verdict[]
  pageSize?: number
  /** 窄栏（首页摘要）里关掉来源列 */
  showSource?: boolean
}

export default function VerdictTable({ verdicts, pageSize, showSource = true }: Props) {
  const columns: ColumnsType<Verdict> = [
    {
      title: '待检验命题',
      dataIndex: 'claim',
      width: 232,
      render: (v: string, r) => (
        <div style={{ display: 'flex', gap: 8, alignItems: 'stretch' }}>
          <span
            style={{
              width: 2,
              flex: 'none',
              borderRadius: 2,
              alignSelf: 'stretch',
              background: NEG.test(r.verdict) ? 'var(--aq-cred-mid)' : 'var(--aq-cred-strong)',
            }}
          />
          <span style={{ fontSize: 13, lineHeight: 1.6 }}>{v}</span>
        </div>
      ),
    },
    {
      title: '判定',
      dataIndex: 'verdict',
      width: 88,
      render: (v: string) => {
        const neg = NEG.test(v)
        return (
          <span
            style={{
              fontSize: 12,
              fontWeight: neg ? 500 : 400,
              color: neg ? 'var(--aq-text)' : 'var(--aq-text-3)',
            }}
          >
            {v}
          </span>
        )
      },
    },
    {
      title: '证据',
      dataIndex: 'evidence',
      render: (v: string, r) => (
        <div style={{ fontSize: 12, lineHeight: 1.7 }}>
          <span style={{ color: 'var(--aq-text-2)' }}>{v}</span>
          {showSource && r.source && (
            <>
              {' '}
              <Tooltip title="打开源文档">
                <Typography.Link
                  href={`${REPO_BLOB}/${r.source}`}
                  target="_blank"
                  rel="noreferrer"
                  style={{ fontSize: 11 }}
                >
                  {r.source}
                </Typography.Link>
              </Tooltip>
            </>
          )}
        </div>
      ),
    },
    {
      title: '可信度',
      width: 92,
      render: (_, r) => <CredibilityBadge source={r} />,
    },
  ]

  return (
    <Table
      rowKey="claim"
      size="small"
      columns={columns}
      dataSource={verdicts}
      pagination={
        pageSize
          ? { pageSize, size: 'small', showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }
          : false
      }
      locale={{ emptyText: '暂无结论（docs/研究进展.json 为空）' }}
    />
  )
}
