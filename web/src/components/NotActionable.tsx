// 「研究中间产物 · 不可据此交易」统一横幅。
//
// 为什么值得做成组件：/signals 与 /backtest 展示的产物**都已经被本项目自己证伪**，
// 但它们的外观（打分榜、绩效指标卡）天然带着"可执行"的暗示。旧版每页各写一段
// 措辞不同的 Alert，结果三页的局限声明不一致 —— 而这类声明一旦措辞不统一，
// 读者就会挑最轻的那一版去信。
//
// 关键设计：**判定与证据从这个接口读，不写死在组件里。**
// 写死的话，哪天结论被推翻，横幅还在理直气壮地说旧话 —— 那就成了新的不实陈述。
// 台账里查不到对应条目时如实显示"查不到"，不编一句听起来很稳的话糊过去。
import { useQuery } from '@tanstack/react-query'
import { Alert, Space, Typography } from 'antd'
import { Link } from 'react-router-dom'
import api from '@/services/api'
import { REPO_BLOB } from './VerdictTable'

interface Props {
  /** 结论台账里「待检验命题」的原文，用来把本条产物的判定取回来 */
  claim: string
  /** 该页特有的口径提醒（如接口返回的 caveat），拼在结论下面 */
  extra?: React.ReactNode
}

export default function NotActionable({ claim, extra }: Props) {
  // 与 StatusBar 同一个 queryKey，所以不会多发请求。
  const { data } = useQuery({ queryKey: ['project-status'], queryFn: api.projectStatus })
  const v = data?.verdicts?.find((x) => x.claim === claim)

  const body = (
    <div style={{ fontSize: 12, lineHeight: 1.8 }}>
      {v ? (
        <div style={{ display: 'flex', gap: 8, alignItems: 'stretch', marginBottom: 4 }}>
          <span
            style={{
              width: 2,
              flex: 'none',
              borderRadius: 2,
              background: 'var(--aq-cred-mid)',
            }}
          />
          <div>
            <span style={{ color: 'var(--aq-text-2)' }}>
              台账条目「{v.claim}」的判定是{' '}
              <b style={{ color: 'var(--aq-text)' }}>{v.verdict}</b>。
            </span>
            <div style={{ color: 'var(--aq-text-3)' }}>证据：{v.evidence}</div>
          </div>
        </div>
      ) : (
        <div style={{ color: 'var(--aq-text-3)' }}>
          结论台账里没找到「{claim}」这条 —— 暂无法给出对应判定（不臆断）。
        </div>
      )}

      {extra && <div style={{ marginTop: 6, color: 'var(--aq-text-2)' }}>{extra}</div>}

      <Space size={14} style={{ marginTop: 6 }}>
        <Link to="/verdicts" style={{ fontSize: 12 }}>
          打开结论台账
        </Link>
        {v?.source && (
          <Typography.Link
            href={`${REPO_BLOB}/${v.source}`}
            target="_blank"
            rel="noreferrer"
            style={{ fontSize: 12 }}
          >
            {v.source}
          </Typography.Link>
        )}
      </Space>
    </div>
  )

  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 12 }}
      message="研究中间产物 · 不可据此交易"
      description={body}
    />
  )
}
