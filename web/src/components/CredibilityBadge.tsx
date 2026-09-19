// 可信度徽章 —— 全站统一的"这个数字能不能引用"标记。
//
// 为什么不用颜色：红绿已经被「涨跌」占用（A 股硬约定）。如果可信度也用红绿，
// 屏幕上就会出现"这个红色标签到底是涨还是已证伪"的歧义。
// 所以这里改用**填充程度**（实心 / 半实心 / 空心）区分，色相留给涨跌。
//
// 三态来源：
//   quotable === false   → 不可引用（有偏口径）
//   available === false  → 无数据（页面显式标注，而不是返回 0）
//   其余                  → 可引用
import { Tooltip } from 'antd'
import { CRED_HINT, CRED_LABEL, credOf, type CredKind } from '@/utils/format'

const TONE: Record<CredKind, string> = { ok: 'chip-ok', biased: 'chip-mid', none: 'chip-none' }

interface Props {
  /** 直接指定；不传则由 source 推导 */
  kind?: CredKind
  /** 接口对象（含 quotable / available 字段） */
  source?: { quotable?: boolean; available?: boolean }
  /** 只显示圆点、不显示文字（用在很窄的表格列） */
  dotOnly?: boolean
}

export default function CredibilityBadge({ kind, source, dotOnly = false }: Props) {
  const k: CredKind = kind ?? (source ? credOf(source) : 'ok')
  return (
    <Tooltip title={CRED_HINT[k]}>
      <span
        className={`chip ${TONE[k]}`}
        style={{ cursor: 'help', padding: dotOnly ? '1px 4px' : undefined }}
        aria-label={CRED_LABEL[k]}
      >
        <i />
        {!dotOnly && CRED_LABEL[k]}
      </span>
    </Tooltip>
  )
}
