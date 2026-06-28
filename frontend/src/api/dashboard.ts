import { apiGet } from './client';

export type DashboardCandidate = {
  symbol: string;
  base_asset: string;
  direction: 'LONG' | 'SHORT' | 'NEUTRAL' | string;
  score: number;
  action: string;
  regime: string;
  phase: string;
};

export type DashboardOverview = {
  ok: boolean;
  state: {
    code: 'WATCH' | 'FILTERING' | 'NEUTRAL' | string;
    text: string;
  };
  metrics: {
    top50_count: number;
    ai_candidate_count: number;
    actionable_count: number;
    average_score: number;
    dynamic_stream_count: number;
    active_coin_count: number;
    fund_ready_count: number;
    fake_high_count: number;
  };
  direction: {
    long: number;
    short: number;
    neutral: number;
    long_pct: number;
    short_pct: number;
    neutral_pct: number;
  };
  candidates: DashboardCandidate[];
  scan: {
    last_scan_id: string;
    last_scan_time: string;
    market_heat: number;
    alert_count: number;
    scan_status: Record<string, unknown>;
  };
};

export function getDashboardOverview(): Promise<DashboardOverview> {
  return apiGet<DashboardOverview>('/dashboard/overview');
}
