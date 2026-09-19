import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Empty,
  List,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { CheckCircleTwoTone, ClockCircleTwoTone, CloseCircleTwoTone } from '@ant-design/icons'
import api from '@/services/api'
import type { StageItem, Verdict } from '@/types'
import {
  GAP_COLOR,
  STAGE_STATUS_COLOR,
  STAGE_STATUS_LABEL,
} from '@/utils/format'

const STATUS_ICON: Record<string, React.ReactNode> = {
  done: <CheckCircleTwoTone twoToneColor="#52c41a" />,
  in_progress: <ClockCircleTwoTone twoToneColor="#1668dc" />,
  blocked: <CloseCircleTwoTone twoToneColor="#f5222d" />,
  todo: <ClockCircleTwoTone twoToneColor="#8c8c8c" />,
}

// 路线级结论（人工写在 docs/研究进展.json 的 outcome 字段）。
// 与"条目进度"是两件事：B 的 5 个条目全做完了（100%），但 B 这条**路线**被否决了。
// 只显示进度条会读成"B 成功了"，所以这里并列一个结论徽章。
const ROUTE_DEAD = /否决|失败|封存/

function routeChip(outcome: string): { cls: string } {
  return { cls: ROUTE_DEAD.test(outcome) ? 'chip-mid' : 'chip-ok' }
}

export default function ProjectStatus() {
  const { data, isLoading } = useQuery({
    queryKey: ['project-status'],
    queryFn: api.projectStatus,
  })

  if (isLoading) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (!data) return <Empty description="暂无状态数据" />
  if (data.doc_error) {
    return (
      <Alert
        type="error"
        showIcon
        message="状态数据源不可用"
        description={data.doc_error}
      />
    )
  }

  const a = data.data_assets

  const itemCols: ColumnsType<StageItem> = [
    {
      title: '',
      dataIndex: 'status',
      width: 34,
      align: 'center',
      render: (v: string) => STATUS_ICON[v] ?? STATUS_ICON.todo,
    },
    {
      title: '条目',
      dataIndex: 'name',
      width: 220,
      render: (v: string, r) => (
        <Space size={6}>
          <span>{v}</span>
          {r.severity === 'high' && <Tag color="red">高优先</Tag>}
          {r.quotable === false && (
            <Tooltip title="依赖有偏口径，仅可作相对比较，绝对数值不可对外引用">
              <Tag color="orange">不可引用</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 80,
      render: (v: string) => (
        <Tag color={STAGE_STATUS_COLOR[v]} style={{ marginRight: 0 }}>
          {STAGE_STATUS_LABEL[v] ?? v}
        </Tag>
      ),
    },
    {
      title: '说明',
      dataIndex: 'detail',
      render: (v: string, r) => (
        <span style={{ fontSize: 12, lineHeight: 1.7 }}>
          {v}
          {r.source && (
            <>
              {' '}
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                ← {r.source}
              </Typography.Text>
            </>
          )}
        </span>
      ),
    },
  ]

  const verdictCols: ColumnsType<Verdict> = [
    {
      title: '待检验命题',
      dataIndex: 'claim',
      width: 260,
      render: (v: string) => <span style={{ fontWeight: 500 }}>{v}</span>,
    },
    {
      title: '判定',
      dataIndex: 'verdict',
      width: 120,
      render: (v: string) => {
        const color = v.includes('证伪')
          ? 'red'
          : v.includes('封存')
            ? 'default'
            : 'green'
        return <Tag color={color}>{v}</Tag>
      },
    },
    {
      title: '证据',
      dataIndex: 'evidence',
      render: (v: string, r) => (
        <span style={{ fontSize: 12, lineHeight: 1.7 }}>
          {v}
          {r.quotable === false && <Tag color="orange" style={{ marginLeft: 6 }}>不可引用</Tag>}
          {r.source && (
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              {' '}
              ← {r.source}
            </Typography.Text>
          )}
        </span>
      ),
    },
  ]

  return (
    <div>
      {/* ---------- 头部：更新时间 / 仓库状态 ---------- */}
      <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: '10px 16px' } }}>
        <Space size={24} wrap>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              结论更新于
            </Typography.Text>{' '}
            <span className="mono">{data.updated_at || '—'}</span>
          </span>
          <span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              数据统计生成于
            </Typography.Text>{' '}
            <span className="mono" style={{ fontSize: 12 }}>
              {data.generated_at}
            </span>
          </span>
          {data.repo.commit && (
            <Tooltip title={data.repo.subject}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                当前提交 <span className="mono">{data.repo.commit}</span>（
                {data.repo.branch} · {data.repo.date}）
              </Typography.Text>
            </Tooltip>
          )}
        </Space>
      </Card>

      {/* ---------- 阶段进度 ----------
          每阶段独占一行：条目表有「状态 + 名称 + 说明 + 来源」四列文字，
          三列并排会把说明列压成极窄的一条，读起来很累。 */}
      {data.stages.map((s) => (
        <Card
          key={s.id}
          size="small"
          style={{ marginBottom: 12 }}
          title={
            <Space size={8}>
              <Tag color="blue">{s.id}</Tag>
              {s.name}
              {s.outcome && (
                <Tooltip title={s.outcome_reason || s.goal}>
                  <span className={`chip ${routeChip(s.outcome).cls}`} style={{ cursor: 'help' }}>
                    <i />
                    {s.outcome}
                  </span>
                </Tooltip>
              )}
              <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                {s.goal}
              </Typography.Text>
            </Space>
          }
          extra={
            <Space size={12}>
              {!s.outcome || !ROUTE_DEAD.test(s.outcome) ? (
                <Progress
                  percent={Math.round(s.progress * 100)}
                  size="small"
                  style={{ width: 140 }}
                  strokeColor={s.items.some((i) => i.status === 'blocked') ? '#faad14' : '#1668dc'}
                />
              ) : (
                <span style={{ fontSize: 12, color: '#8c8c8c' }}>
                  路线已封存 · 条目 {s.n_done}/{s.n_items} 已做完
                </span>
              )}
              <span style={{ fontSize: 12, color: '#8c8c8c' }}>
                {s.n_done}/{s.n_items} 项完成
              </span>
            </Space>
          }
        >
          <Table
            rowKey="name"
            size="small"
            columns={itemCols}
            dataSource={s.items}
            pagination={false}
            showHeader={false}
          />
        </Card>
      ))}

      {/* ---------- 已封存结论 ---------- */}
      <Card
        title="已封存结论（别再重复检验）"
        size="small"
        style={{ marginTop: 12 }}
        extra={
          <span style={{ fontSize: 12, color: '#8c8c8c' }}>
            「已证伪」= 有统计证据表明不成立，不是「还没测出来」
          </span>
        }
      >
        <Table
          rowKey="claim"
          size="small"
          columns={verdictCols}
          dataSource={data.verdicts}
          pagination={false}
        />
      </Card>

      {/* ---------- 已知缺口 ---------- */}
      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={12}>
          <Card title="已知数据缺口" size="small">
            <List
              size="small"
              dataSource={data.gaps}
              renderItem={(g) => (
                <List.Item>
                  <List.Item.Meta
                    avatar={
                      <Tag color={GAP_COLOR[g.severity] ?? 'default'}>
                        {g.severity === 'high' ? '高' : g.severity === 'medium' ? '中' : '低'}
                      </Tag>
                    }
                    title={<span style={{ fontSize: 13 }}>{g.name}</span>}
                    description={
                      <span style={{ fontSize: 12, lineHeight: 1.7 }}>{g.detail}</span>
                    }
                  />
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card title="下一步（按优先级）" size="small">
            <Timeline
              style={{ marginTop: 8 }}
              items={data.next_steps.map((t) => ({
                color: 'blue',
                children: <span style={{ fontSize: 12, lineHeight: 1.8 }}>{t}</span>,
              }))}
            />
          </Card>
        </Col>
      </Row>

      {/* ---------- 数据资产（实时扫盘）---------- */}
      <Card
        title="数据资产（实时扫盘统计，非常量）"
        size="small"
        style={{ marginTop: 12 }}
        extra={
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {a.bars.first_date} ~ {a.bars.last_date}
          </Typography.Text>
        }
      >
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Statistic title="个股日线" value={a.bars.n_files} suffix="只" />
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>
              活跃 {a.bars.n_active} / 停牌 {a.bars.n_suspended}
            </div>
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Statistic title="退市股行情" value={a.delisted_bars.n_files} suffix={`/${a.delisted_bars.n_listed}`} />
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>已抓 / 清单</div>
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Statistic
              title="ST 逐日覆盖"
              value={(a.st_flags.ratio * 100).toFixed(1)}
              suffix="%"
              valueStyle={{ color: a.st_flags.ratio < 0.9 ? '#faad14' : undefined }}
            />
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>
              {a.st_flags.n_files}/{a.st_flags.n_needed} 只
            </div>
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Statistic title="估值数据" value={a.valuation.n_files} suffix="只" />
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Statistic title="ETF 净值" value={a.etf.n_nav} suffix="只" />
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>
              日线 {a.etf.n_bars} 只
            </div>
          </Col>
          <Col xs={12} sm={8} md={6} lg={4}>
            <Statistic
              title="指数行情"
              value={a.index_bars.n_available}
              suffix={`/${a.index_bars.n_expected}`}
              valueStyle={{ color: '#faad14' }}
            />
            <div style={{ fontSize: 11, color: '#8c8c8c' }}>
              缺 {a.index_bars.missing.join('、') || '无'}
            </div>
          </Col>
        </Row>

        <Descriptions size="small" column={2} style={{ marginTop: 16 }} bordered>
          <Descriptions.Item label="流动性池（主口径）">
            <span className="mono">{a.liquid_snapshot}</span> 只
          </Descriptions.Item>
          <Descriptions.Item label="交易日历">
            <span className="mono">{a.calendar.n_trade_days}</span> 天
          </Descriptions.Item>
          <Descriptions.Item label="已落盘指数" span={2}>
            {a.index_bars.available.map((c) => (
              <Tag key={c} className="mono">
                {c}
              </Tag>
            ))}
            {a.index_bars.missing.map((c) => (
              <Tag key={c} color="default" className="mono">
                {c} 未落盘
              </Tag>
            ))}
          </Descriptions.Item>
          <Descriptions.Item label="口径说明" span={2}>
            <span style={{ fontSize: 12 }}>{a.caliber_note}</span>
          </Descriptions.Item>
        </Descriptions>
      </Card>
    </div>
  )
}
