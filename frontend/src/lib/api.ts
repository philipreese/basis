import type { components } from './api-types';

export type Position = components['schemas']['PositionSchema'];
export type PortfolioConfig = components['schemas']['PortfolioConfigSchema'];
export type PlaybookDefinition = components['schemas']['PlaybookDefinitionSchema'];
export type OptionLeg = components['schemas']['OptionLegSchema'];
export type OperationalJournalEntry = components['schemas']['OperationalJournalEntrySchema'];

// Extended MarketState — supersedes the generated type which lacks Sprint 3 fields
export interface MarketState {
  current_regime: 'CALM_BULL' | 'HIGH_VOL_NEUTRAL' | 'TRENDING_BEAR' | 'EVENT_CATALYST';
  spy_price: number;
  spy_sma20: number;
  vix_close: number;
  underlying_ivrs: Record<string, number>;
  spy_daily_return: number;
  catalyst_dates: string[];
  regime_scores: Record<string, number>;
}

export interface RegimeInfo {
  label: string;
  color: string;
  description: string;
}

export const REGIME_DISPLAY: Record<string, RegimeInfo> = {
  CALM_BULL:        { label: 'CALM BULL',        color: 'emerald', description: 'Bullish trend, low volatility — income strategies favoured' },
  HIGH_VOL_NEUTRAL: { label: 'HIGH VOL NEUTRAL', color: 'amber',   description: 'Range-bound, elevated volatility — Iron Condors, CSPs' },
  TRENDING_BEAR:    { label: 'TRENDING BEAR',    color: 'rose',    description: 'Bearish trend — reduce risk, avoid new income positions' },
  EVENT_CATALYST:   { label: 'EVENT CATALYST',   color: 'violet',  description: 'Upcoming catalyst — long volatility strategies only' },
};

export interface ScannedPosition {
  position_id: string;
  underlying: string;
  strategy_type: string;
  contracts: number;
  max_loss: number;
  max_profit: number;
  entry_premium: number;
  current_value_per_share: number;
  expiration_date: string;
  priority: 'P1 — CLOSE NOW' | 'P2 — CLOSE SOON' | 'P2 — REVIEW' | 'P3 — MONITOR' | 'OK';
  action: string;
  reason: string;
  math_detail: string;
  legs: OptionLeg[];
}

export interface PortfolioGreeks {
  net_delta: number;
  net_theta: number;
  net_vega: number;
  net_gamma: number;
}

export interface SafeguardWarning {
  type: string;
  severity: 'WARNING' | 'CRITICAL';
  message: string;
}

export interface PortfolioObservation {
  scanned_positions: ScannedPosition[];
  greeks: PortfolioGreeks;
  safeguards: SafeguardWarning[];
  market_state: MarketState;
}

const API_BASE = '/api';

export async function getPortfolioConfig(): Promise<PortfolioConfig> {
  const res = await fetch(`${API_BASE}/portfolio/config`);
  if (!res.ok) throw new Error('Failed to fetch portfolio config');
  return res.json();
}

export async function updatePortfolioConfig(config: PortfolioConfig): Promise<PortfolioConfig> {
  const res = await fetch(`${API_BASE}/portfolio/config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  if (!res.ok) throw new Error('Failed to update portfolio config');
  return res.json();
}

export async function getPositions(): Promise<Position[]> {
  const res = await fetch(`${API_BASE}/positions`);
  if (!res.ok) throw new Error('Failed to fetch positions');
  return res.json();
}

export async function getPosition(id: string): Promise<Position> {
  const res = await fetch(`${API_BASE}/positions/${id}`);
  if (!res.ok) throw new Error('Failed to fetch position');
  return res.json();
}

export async function createPosition(pos: Position): Promise<Position> {
  const res = await fetch(`${API_BASE}/positions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(pos),
  });
  if (!res.ok) throw new Error('Failed to create position');
  return res.json();
}

export async function getPlaybooks(): Promise<PlaybookDefinition[]> {
  const res = await fetch(`${API_BASE}/playbooks`);
  if (!res.ok) throw new Error('Failed to fetch playbooks');
  return res.json();
}

export async function createPlaybook(pb: PlaybookDefinition): Promise<PlaybookDefinition> {
  const res = await fetch(`${API_BASE}/playbooks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(pb),
  });
  if (!res.ok) throw new Error('Failed to create playbook');
  return res.json();
}

export async function getMarketState(): Promise<MarketState> {
  const res = await fetch(`${API_BASE}/market/state`);
  if (!res.ok) throw new Error('Failed to fetch market state');
  return res.json();
}

export async function updateMarketState(state: MarketState): Promise<MarketState> {
  const res = await fetch(`${API_BASE}/market/state`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(state),
  });
  if (!res.ok) throw new Error('Failed to update market state');
  return res.json();
}

export async function getPortfolioObservation(): Promise<PortfolioObservation> {
  const res = await fetch(`${API_BASE}/portfolio/observation`);
  if (!res.ok) throw new Error('Failed to fetch portfolio observation');
  return res.json();
}

export async function fetchLiveMarketData(): Promise<MarketState> {
  const res = await fetch(`${API_BASE}/market/fetch`, { method: 'POST' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(err.detail ?? 'Failed to fetch live market data');
  }
  return res.json();
}

// ---- Sprint 4: Layer C types ----

export type StrikeDerivedParams = components['schemas']['StrikeDerivedParams'];
export type CandidateCard = components['schemas']['CandidateCard'];
export type OpportunityScanResult = components['schemas']['OpportunityScanResult'];
export type TradeSpecLeg = components['schemas']['TradeSpecLeg'];
export type TradeSpec = components['schemas']['TradeSpec'];
export type HardBlock = components['schemas']['HardBlock'];
export type TradeWarning = components['schemas']['TradeWarning'];
export type TradeSpecResult = components['schemas']['TradeSpecResult'];

export async function scanOpportunities(): Promise<OpportunityScanResult> {
  const res = await fetch(`${API_BASE}/opportunity/scan`);
  if (!res.ok) throw new Error('Failed to scan opportunities');
  return res.json();
}

export async function getTradeSpec(playbookId: string): Promise<TradeSpecResult> {
  const res = await fetch(`${API_BASE}/opportunity/spec/${encodeURIComponent(playbookId)}`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(`Failed to generate trade spec for ${playbookId}`);
  return res.json();
}

// ---- Sprint 5: Post-Mortem, Opportunity Ledger, Performance Diagnostics ----

export interface ClosePositionRequest {
  current_value_per_share: number;
  exit_trigger: 'PROFIT_TARGET' | 'LOSS_LIMIT' | 'TIME_RULE' | 'CATALYST_RULE' | 'MANUAL';
  actual_underlying_move_pct: number;
  lesson_tags: string[];
}

export interface ClosurePostMortem {
  id: string;
  position_id: string;
  outcome: 'WIN' | 'LOSS' | 'BREAKEVEN';
  realized_pnl: number;
  actual_underlying_move_pct: number;
  exit_date: string;
  exit_trigger: 'PROFIT_TARGET' | 'LOSS_LIMIT' | 'TIME_RULE' | 'CATALYST_RULE' | 'MANUAL';
  lesson_tags: string[];
  user_override_logged: boolean;
  playbook_id?: string | null;
  playbook_version?: string | null;
}

export interface OpportunityRecord {
  id: string;
  playbook_id: string;
  playbook_version: string;
  generated_at: string;
  accepted: boolean;
  outcome_if_taken?: number | null;
  bypass_reason?: string | null;
}

export interface PlaybookMetrics {
  playbook_id: string;
  playbook_version: string;
  total_trades: number;
  win_rate?: number | null;
  profit_factor?: number | null;
  avg_return_on_risk?: number | null;
  cagr: string;
  max_drawdown: string;
  sharpe: string;
}

export interface PerformanceDiagnostics {
  generated_at: string;
  playbook_metrics: PlaybookMetrics[];
  benchmarks: {
    spy_cagr?: number | null;
    bxm_cagr?: number | null;
    note: string;
  };
}

export async function closePosition(positionId: string, req: ClosePositionRequest): Promise<ClosurePostMortem> {
  const res = await fetch(`${API_BASE}/positions/${encodeURIComponent(positionId)}/close`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(err.detail ?? 'Failed to close position');
  }
  return res.json();
}

export async function getPostMortems(): Promise<ClosurePostMortem[]> {
  const res = await fetch(`${API_BASE}/positions/post-mortems`);
  if (!res.ok) throw new Error('Failed to fetch post-mortems');
  return res.json();
}

export async function getPostMortem(positionId: string): Promise<ClosurePostMortem> {
  const res = await fetch(`${API_BASE}/positions/${encodeURIComponent(positionId)}/post-mortem`);
  if (!res.ok) throw new Error('Failed to fetch post-mortem');
  return res.json();
}

export async function getOpportunityLedger(): Promise<OpportunityRecord[]> {
  const res = await fetch(`${API_BASE}/opportunity/ledger`);
  if (!res.ok) throw new Error('Failed to fetch opportunity ledger');
  return res.json();
}

export async function createOpportunityRecord(record: Omit<OpportunityRecord, 'id'>): Promise<OpportunityRecord> {
  const res = await fetch(`${API_BASE}/opportunity/ledger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: '', ...record }),
  });
  if (!res.ok) throw new Error('Failed to create opportunity record');
  return res.json();
}

export async function updateOpportunityOutcome(recordId: string, outcome_if_taken: number): Promise<OpportunityRecord> {
  const res = await fetch(`${API_BASE}/opportunity/ledger/${encodeURIComponent(recordId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ outcome_if_taken }),
  });
  if (!res.ok) throw new Error('Failed to update opportunity outcome');
  return res.json();
}

export async function getPerformanceDiagnostics(): Promise<PerformanceDiagnostics> {
  const res = await fetch(`${API_BASE}/performance/diagnostics`);
  if (!res.ok) throw new Error('Failed to fetch performance diagnostics');
  return res.json();
}

// ---- Supervision console (#73): trading control, books, audit, executor status ----

export type TradingControl = components['schemas']['TradingControlSchema'];
export type ControlState = TradingControl['state'];
export type TradingControlView = components['schemas']['TradingControlView'];
export type LiveGateChecklist = components['schemas']['LiveGateChecklistSchema'];
export type BookSummary = components['schemas']['BookSummarySchema'];
export type AuditEvent = components['schemas']['AuditEventSchema'];
export type ExecutorStatus = components['schemas']['ExecutorStatusSchema'];

export async function getTradingControl(): Promise<TradingControlView> {
  const res = await fetch(`${API_BASE}/trading-control`);
  if (!res.ok) throw new Error('Failed to fetch trading control');
  return res.json();
}

export async function updateTradingControl(scope: string, state: ControlState, reason: string): Promise<TradingControlView> {
  const res = await fetch(`${API_BASE}/trading-control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope, state, reason }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(typeof err.detail === 'string' ? err.detail : 'Failed to update trading control');
  }
  return res.json();
}

export async function getBooks(): Promise<BookSummary[]> {
  const res = await fetch(`${API_BASE}/books`);
  if (!res.ok) throw new Error('Failed to fetch books');
  const data: { books: BookSummary[] } = await res.json();
  return data.books;
}

export interface AuditEventFilters {
  book_id?: string;
  date?: string;
  event_type?: string;
  limit?: number;
}

export async function getAuditEvents(filters: AuditEventFilters = {}): Promise<AuditEvent[]> {
  const params = new URLSearchParams();
  if (filters.book_id) params.set('book_id', filters.book_id);
  if (filters.date) params.set('date', filters.date);
  if (filters.event_type) params.set('event_type', filters.event_type);
  if (filters.limit) params.set('limit', String(filters.limit));
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/audit-events${qs ? `?${qs}` : ''}`);
  if (!res.ok) throw new Error('Failed to fetch audit events');
  return res.json();
}

export async function getExecutorStatus(): Promise<ExecutorStatus> {
  const res = await fetch(`${API_BASE}/executor/status`);
  if (!res.ok) throw new Error('Failed to fetch executor status');
  return res.json();
}

export async function refreshPositionPrices(): Promise<Position[]> {
  const res = await fetch(`${API_BASE}/positions/refresh`, { method: 'POST' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(err.detail ?? 'Failed to refresh position prices');
  }
  return res.json();
}
