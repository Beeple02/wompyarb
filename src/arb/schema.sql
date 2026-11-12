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

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE resolutions ADD COLUMN IF NOT EXISTS ks_strike DOUBLE PRECISION;
ALTER TABLE resolutions ADD COLUMN IF NOT EXISTS ks_settle DOUBLE PRECISION;
ALTER TABLE resolutions ADD COLUMN IF NOT EXISTS pm_strike DOUBLE PRECISION;
ALTER TABLE resolutions ADD COLUMN IF NOT EXISTS pm_settle DOUBLE PRECISION;

-- Marchés journaliers above/below (Kalshi KX*D <-> Polymarket *-above-day-*)
CREATE TABLE IF NOT EXISTS daily_events (
  id BIGSERIAL PRIMARY KEY,
  asset TEXT NOT NULL,
  settle_ts TIMESTAMPTZ NOT NULL,
  ks_event TEXT, pm_event TEXT,
  n_ks INT, n_pm INT, n_overlap INT,
  scanned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (asset, settle_ts)
);
CREATE TABLE IF NOT EXISTS daily_pairs (
  id BIGSERIAL PRIMARY KEY,
  event_id BIGINT NOT NULL REFERENCES daily_events(id),
  asset TEXT NOT NULL,
  settle_ts TIMESTAMPTZ NOT NULL,
  threshold DOUBLE PRECISION NOT NULL,
  ks_ticker TEXT NOT NULL, pm_slug TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE (event_id, threshold)
);
CREATE TABLE IF NOT EXISTS daily_signals (
  id BIGSERIAL PRIMARY KEY,
  pair_id BIGINT NOT NULL REFERENCES daily_pairs(id),
  ts TIMESTAMPTZ NOT NULL,
  secs_to_settle DOUBLE PRECISION,
  status TEXT NOT NULL, reason TEXT, direction TEXT, n DOUBLE PRECISION,
  vwap_pm DOUBLE PRECISION, vwap_ks DOUBLE PRECISION, fees_total DOUBLE PRECISION,
  cost_per DOUBLE PRECISION, gross DOUBLE PRECISION, net DOUBLE PRECISION, net_bps DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS daily_signals_ts ON daily_signals (ts);
CREATE TABLE IF NOT EXISTS daily_trades (
  id BIGSERIAL PRIMARY KEY,
  pair_id BIGINT NOT NULL UNIQUE REFERENCES daily_pairs(id),
  ts TIMESTAMPTZ NOT NULL,
  direction TEXT NOT NULL, n DOUBLE PRECISION NOT NULL,
  vwap_pm DOUBLE PRECISION, vwap_ks DOUBLE PRECISION, fees_total DOUBLE PRECISION,
  expected_net DOUBLE PRECISION,
  status TEXT NOT NULL DEFAULT 'open',
  payout DOUBLE PRECISION, pnl DOUBLE PRECISION, settled_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS daily_resolutions (
  pair_id BIGINT PRIMARY KEY REFERENCES daily_pairs(id),
  ks_result TEXT, pm_result TEXT,
  ks_settle DOUBLE PRECISION, pm_settle DOUBLE PRECISION,
  dis_kind TEXT,
  complete BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Marchés météo « Where will it rain ? » (Kalshi KXRAIN <-> Polymarket international)
CREATE TABLE IF NOT EXISTS weather_pairs (
  id BIGSERIAL PRIMARY KEY,
  city TEXT NOT NULL, day DATE NOT NULL, day_end TIMESTAMPTZ NOT NULL,
  ks_ticker TEXT NOT NULL, pm_event TEXT, pm_slug TEXT NOT NULL, pm_token TEXT NOT NULL,
  ks_station TEXT, pm_station TEXT, station_status TEXT NOT NULL,
  ks_trace TEXT NOT NULL, pm_trace TEXT NOT NULL, safe_direction TEXT,
  tradable BOOLEAN NOT NULL, reason TEXT, ambiguous BOOLEAN NOT NULL DEFAULT FALSE,
  ks_rules TEXT, pm_rules TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (city, day)
);
CREATE TABLE IF NOT EXISTS weather_signals (
  id BIGSERIAL PRIMARY KEY,
  pair_id BIGINT NOT NULL REFERENCES weather_pairs(id),
  ts TIMESTAMPTZ NOT NULL,
  secs_to_end DOUBLE PRECISION,
  status TEXT NOT NULL, reason TEXT, direction TEXT, n DOUBLE PRECISION,
  vwap_pm DOUBLE PRECISION, vwap_ks DOUBLE PRECISION, fees_total DOUBLE PRECISION,
  cost_per DOUBLE PRECISION, gross DOUBLE PRECISION, net DOUBLE PRECISION, net_bps DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS weather_signals_ts ON weather_signals (ts);
CREATE TABLE IF NOT EXISTS weather_trades (
  id BIGSERIAL PRIMARY KEY,
  pair_id BIGINT NOT NULL UNIQUE REFERENCES weather_pairs(id),
  ts TIMESTAMPTZ NOT NULL,
  direction TEXT NOT NULL, n DOUBLE PRECISION NOT NULL,
  vwap_pm DOUBLE PRECISION, vwap_ks DOUBLE PRECISION, fees_total DOUBLE PRECISION,
  expected_net DOUBLE PRECISION,
  status TEXT NOT NULL DEFAULT 'open',
  payout DOUBLE PRECISION, pnl DOUBLE PRECISION, settled_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS weather_resolutions (
  pair_id BIGINT PRIMARY KEY REFERENCES weather_pairs(id),
  ks_result TEXT, pm_result TEXT,
  dis_kind TEXT,
  complete BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Compteurs horaires agrégés (remplacent le stockage ligne par ligne des évaluations rejetées/répétitives)
CREATE TABLE IF NOT EXISTS signal_counts (
  hour TIMESTAMPTZ NOT NULL,
  scope TEXT NOT NULL,          -- crypto15 | daily | weather
  key TEXT NOT NULL,            -- actif ou ville
  status TEXT NOT NULL,
  n BIGINT NOT NULL DEFAULT 0,
  best_bps DOUBLE PRECISION,
  sum_bps DOUBLE PRECISION NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, scope, key, status)
);
-- Index redondants supprimés (coût disque sans usage)
DROP INDEX IF EXISTS quotes_ts;
DROP INDEX IF EXISTS signals_status;
