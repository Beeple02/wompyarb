CREATE TABLE IF NOT EXISTS assets (
  symbol TEXT PRIMARY KEY,
  ks_series TEXT, pm_prefix TEXT,
  ks_index TEXT, pm_index TEXT,
  ks_tie_rule TEXT, pm_tie_rule TEXT,
  ks_void_rule TEXT, pm_void_rule TEXT,
  g1_same_index BOOLEAN, g2_same_tie_void BOOLEAN,
  rules_verified_on DATE, rules_verified_by TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS windows (
  id BIGSERIAL PRIMARY KEY,
  asset TEXT NOT NULL,
  t_start TIMESTAMPTZ NOT NULL,
  t_end TIMESTAMPTZ NOT NULL,
  ks_ticker TEXT, pm_slug TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (asset, t_start)
);

CREATE TABLE IF NOT EXISTS quotes (
  id BIGSERIAL PRIMARY KEY,
  window_id BIGINT NOT NULL REFERENCES windows(id),
  venue TEXT NOT NULL,
  ts_recv TIMESTAMPTZ NOT NULL,
  best_ask_up DOUBLE PRECISION, best_ask_down DOUBLE PRECISION,
  depth_up DOUBLE PRECISION, depth_down DOUBLE PRECISION,
  ladders JSONB
);
CREATE INDEX IF NOT EXISTS quotes_w_v_ts ON quotes (window_id, venue, ts_recv);
CREATE INDEX IF NOT EXISTS quotes_ts ON quotes (ts_recv);

CREATE TABLE IF NOT EXISTS signals (
  id BIGSERIAL PRIMARY KEY,
  window_id BIGINT NOT NULL REFERENCES windows(id),
  ts TIMESTAMPTZ NOT NULL,
  secs_to_close DOUBLE PRECISION,
  status TEXT NOT NULL, reason TEXT,
  direction TEXT, n DOUBLE PRECISION,
  vwap_pm DOUBLE PRECISION, vwap_ks DOUBLE PRECISION,
  fees_total DOUBLE PRECISION, cost_per DOUBLE PRECISION,
  gross DOUBLE PRECISION, net DOUBLE PRECISION, net_bps DOUBLE PRECISION,
  p_dis DOUBLE PRECISION, slippage DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS signals_ts ON signals (ts);
CREATE INDEX IF NOT EXISTS signals_status ON signals (status, ts);

CREATE TABLE IF NOT EXISTS paper_trades (
  id BIGSERIAL PRIMARY KEY,
  window_id BIGINT NOT NULL UNIQUE REFERENCES windows(id),
  ts TIMESTAMPTZ NOT NULL,
  direction TEXT NOT NULL, n DOUBLE PRECISION NOT NULL,
  vwap_pm DOUBLE PRECISION, vwap_ks DOUBLE PRECISION,
  fees_total DOUBLE PRECISION, expected_net DOUBLE PRECISION,
  status TEXT NOT NULL DEFAULT 'open',
  payout DOUBLE PRECISION, pnl DOUBLE PRECISION, settled_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS resolutions (
  window_id BIGINT PRIMARY KEY REFERENCES windows(id),
  ks_result TEXT, pm_result TEXT,
  ks_source TEXT, pm_source TEXT,          -- official | inferred
  dis_kind TEXT,                           -- agree | PU_KD | PD_KU
  complete BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor TEXT, event TEXT, payload JSONB
);
