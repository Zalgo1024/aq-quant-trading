-- ============================================================================
-- A 股 AI 量化交易系统 —— 数据库 Schema
-- 目标库：PostgreSQL 14+ ，时序表启用 TimescaleDB 扩展
--
-- 说明：
--   * 行情/因子等高频写入表使用 TimescaleDB 超表（hypertable）；
--   * 维表、交易表使用普通表；
--   * 所有价格均为「后复权真实成交价」，涨跌停价单独存储；
--   * 时点一致性（Point-in-Time）：财务表必须带 announce_date（披露日）。
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================================
-- 1. 维表
-- ============================================================================

-- 股票池
CREATE TABLE IF NOT EXISTS stock_universe (
    symbol       VARCHAR(12) PRIMARY KEY,          -- 如 600000 / 000001
    name         VARCHAR(64)  NOT NULL,
    exchange     VARCHAR(8),                       -- SH / SZ / BJ
    board        VARCHAR(16),                      -- MAIN / STAR / CHINEXT / BSE
    industry     VARCHAR(64),                      -- 申万一级行业
    list_date    DATE,
    delist_date  DATE,                             -- 非空表示已退市（防生存者偏差）
    is_st        BOOLEAN DEFAULT FALSE,
    status       VARCHAR(16) DEFAULT 'L',          -- L 上市 / D 退市 / P 暂停
    updated_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_universe_industry ON stock_universe (industry);
CREATE INDEX IF NOT EXISTS idx_universe_status   ON stock_universe (status);

-- 交易日历
CREATE TABLE IF NOT EXISTS trade_calendar (
    cal_date    DATE PRIMARY KEY,
    is_open     BOOLEAN NOT NULL DEFAULT TRUE,
    prev_open   DATE,
    next_open   DATE
);

-- ============================================================================
-- 2. 行情（时序）
-- ============================================================================

CREATE TABLE IF NOT EXISTS daily_bar (
    time        TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(12) NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,                            -- 股
    amount      NUMERIC(20,2),                     -- 元
    pre_close   NUMERIC(12,4),
    adj_factor  NUMERIC(12,6),                     -- 复权因子
    limit_up    NUMERIC(12,4),
    limit_down  NUMERIC(12,4),
    is_trading  BOOLEAN DEFAULT TRUE,              -- FALSE = 停牌
    PRIMARY KEY (time, symbol)
);

SELECT create_hypertable('daily_bar', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_daily_bar_symbol ON daily_bar (symbol, time DESC);

-- 分钟线（P6 可选）
CREATE TABLE IF NOT EXISTS minute_bar (
    time        TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(12) NOT NULL,
    freq        VARCHAR(8)  NOT NULL,              -- 1m / 5m / 15m / 30m / 60m
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,
    amount      NUMERIC(20,2),
    PRIMARY KEY (time, symbol, freq)
);
SELECT create_hypertable('minute_bar', 'time', if_not_exists => TRUE);

-- ============================================================================
-- 3. 基本面（时点一致性关键）
-- ============================================================================

CREATE TABLE IF NOT EXISTS fundamental (
    symbol          VARCHAR(12) NOT NULL,
    report_date     DATE NOT NULL,                 -- 报告期
    announce_date   DATE NOT NULL,                 -- 实际披露日（PIT 关键！）
    pe_ttm          NUMERIC(14,4),
    pb              NUMERIC(14,4),
    ps_ttm          NUMERIC(14,4),
    roe             NUMERIC(10,4),
    gross_margin    NUMERIC(10,4),
    revenue         NUMERIC(20,2),
    revenue_yoy     NUMERIC(10,4),
    net_profit      NUMERIC(20,2),
    net_profit_yoy  NUMERIC(10,4),
    total_mv        NUMERIC(20,2),                 -- 总市值
    circ_mv         NUMERIC(20,2),                 -- 流通市值
    PRIMARY KEY (symbol, report_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_announce ON fundamental (announce_date);

-- ============================================================================
-- 4. 因子
-- ============================================================================

CREATE TABLE IF NOT EXISTS factor (
    time         TIMESTAMPTZ NOT NULL,
    symbol       VARCHAR(12) NOT NULL,
    factor_name  VARCHAR(64) NOT NULL,
    value        DOUBLE PRECISION,
    PRIMARY KEY (time, symbol, factor_name)
);
SELECT create_hypertable('factor', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_factor_name ON factor (factor_name, time DESC);

-- 因子元信息与验证结果
CREATE TABLE IF NOT EXISTS factor_meta (
    factor_name  VARCHAR(64) PRIMARY KEY,
    direction    SMALLINT DEFAULT 1,               -- +1 越大越好 / -1 越小越好
    ic           NUMERIC(10,6),
    rank_ic      NUMERIC(10,6),
    icir         NUMERIC(10,6),
    turnover     NUMERIC(10,6),
    updated_at   TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- 5. 预测 / 信号
-- ============================================================================

CREATE TABLE IF NOT EXISTS prediction (
    id             BIGSERIAL PRIMARY KEY,
    time           TIMESTAMPTZ NOT NULL,
    symbol         VARCHAR(12) NOT NULL,
    score          NUMERIC(10,6),                  -- 综合评分
    confidence     NUMERIC(10,6),
    direction      VARCHAR(8),                     -- BUY / SELL / HOLD
    factor_contrib JSONB,                          -- [{name,weight,value,contrib}]
    model_version  VARCHAR(64),
    explain        TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_symbol_time ON prediction (symbol, time DESC);
CREATE INDEX IF NOT EXISTS idx_pred_time        ON prediction (time DESC);

CREATE TABLE IF NOT EXISTS signal (
    id             BIGSERIAL PRIMARY KEY,
    time           TIMESTAMPTZ NOT NULL,
    symbol         VARCHAR(12) NOT NULL,
    side           VARCHAR(8) NOT NULL,            -- BUY / SELL
    strength       NUMERIC(10,6),
    confidence     NUMERIC(10,6),
    trigger_factor VARCHAR(64),
    source         VARCHAR(32) DEFAULT 'model'
);
CREATE INDEX IF NOT EXISTS idx_signal_time ON signal (time DESC);

-- ============================================================================
-- 6. 交易（订单 / 成交 / 持仓 / 账户）
-- ============================================================================

CREATE TABLE IF NOT EXISTS account (
    account_id   VARCHAR(32) PRIMARY KEY,
    broker       VARCHAR(16) DEFAULT 'sim',        -- sim / qmt / ptrade / jq
    cash         NUMERIC(20,2) DEFAULT 0,
    frozen       NUMERIC(20,2) DEFAULT 0,
    total_asset  NUMERIC(20,2) DEFAULT 0,
    realized_pnl NUMERIC(20,2) DEFAULT 0,
    updated_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "order" (
    oid          VARCHAR(32) PRIMARY KEY,
    account_id   VARCHAR(32) NOT NULL REFERENCES account(account_id),
    time         TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol       VARCHAR(12) NOT NULL,
    side         VARCHAR(8)  NOT NULL,             -- BUY / SELL
    qty          INTEGER     NOT NULL,
    price        NUMERIC(12,4),
    order_type   VARCHAR(8)  DEFAULT 'LIMIT',      -- LIMIT / MARKET
    status       VARCHAR(16) NOT NULL,             -- PENDING/SUBMITTED/FILLED/UNFILLED/REJECTED/CANCELLED
    strategy_id  VARCHAR(64),
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_time ON "order" (time DESC);
CREATE INDEX IF NOT EXISTS idx_order_sym  ON "order" (symbol, time DESC);

CREATE TABLE IF NOT EXISTS fill (
    fid            BIGSERIAL PRIMARY KEY,
    oid            VARCHAR(32) REFERENCES "order"(oid),
    time           TIMESTAMPTZ NOT NULL,
    symbol         VARCHAR(12) NOT NULL,
    side           VARCHAR(8)  NOT NULL,
    filled_qty     INTEGER     NOT NULL,
    avg_price      NUMERIC(12,4) NOT NULL,
    commission     NUMERIC(14,2) DEFAULT 0,
    stamp_tax      NUMERIC(14,2) DEFAULT 0,
    transfer_fee   NUMERIC(14,2) DEFAULT 0,
    slippage_cost  NUMERIC(14,2) DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fill_time ON fill (time DESC);

CREATE TABLE IF NOT EXISTS position (
    account_id   VARCHAR(32) NOT NULL REFERENCES account(account_id),
    symbol       VARCHAR(12) NOT NULL,
    qty          INTEGER DEFAULT 0,
    available    INTEGER DEFAULT 0,                -- T+1 解冻后可卖
    avg_cost     NUMERIC(12,4) DEFAULT 0,
    last_price   NUMERIC(12,4) DEFAULT 0,
    realized_pnl NUMERIC(20,2) DEFAULT 0,
    updated_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account_id, symbol)
);

-- 组合净值快照（时序，用于画权益曲线）
CREATE TABLE IF NOT EXISTS portfolio_snapshot (
    time         TIMESTAMPTZ NOT NULL,
    account_id   VARCHAR(32) NOT NULL,
    total_asset  NUMERIC(20,2),
    cash         NUMERIC(20,2),
    market_value NUMERIC(20,2),
    pnl          NUMERIC(20,2),
    holdings     JSONB,
    PRIMARY KEY (time, account_id)
);
SELECT create_hypertable('portfolio_snapshot', 'time', if_not_exists => TRUE);

-- 风控阈值
CREATE TABLE IF NOT EXISTS risk_limit (
    account_id            VARCHAR(32) PRIMARY KEY REFERENCES account(account_id),
    single_stock_max      NUMERIC(6,4) DEFAULT 0.10,
    industry_max          NUMERIC(6,4) DEFAULT 0.30,
    total_position_max    NUMERIC(6,4) DEFAULT 0.95,
    stop_loss             NUMERIC(6,4) DEFAULT 0.08,
    max_drawdown          NUMERIC(6,4) DEFAULT 0.20,
    liquidity_min_turnover NUMERIC(20,2) DEFAULT 100000000,
    updated_at            TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- 7. 异常检测
-- ============================================================================

CREATE TABLE IF NOT EXISTS anomaly (
    id        BIGSERIAL PRIMARY KEY,
    time      TIMESTAMPTZ NOT NULL,
    symbol    VARCHAR(12),
    type      VARCHAR(32),                         -- 价格异动 / 量能异动 / 疑似操纵 / 风格切换
    z_score   NUMERIC(10,4),
    severity  VARCHAR(8) DEFAULT 'info',           -- info / warn / severe
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_anomaly_time ON anomaly (time DESC);

-- ============================================================================
-- 8. 回测 / 模型（MLOps）
-- ============================================================================

CREATE TABLE IF NOT EXISTS backtest_run (
    run_id      VARCHAR(32) PRIMARY KEY,
    start_date  DATE,
    end_date    DATE,
    config      JSONB,
    metrics     JSONB,
    report_path TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_registry (
    model_id     VARCHAR(64) PRIMARY KEY,
    name         VARCHAR(64),
    version      VARCHAR(32),
    model_type   VARCHAR(32),                      -- lightgbm / lstm / llm ...
    params       JSONB,
    metrics      JSONB,
    path         TEXT,
    trained_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- 数据更新日志
CREATE TABLE IF NOT EXISTS data_update_log (
    id          BIGSERIAL PRIMARY KEY,
    source      VARCHAR(32),
    task        VARCHAR(64),
    symbols     INTEGER,
    rows        INTEGER,
    status      VARCHAR(16),
    message     TEXT,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
