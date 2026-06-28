import { apiGet, apiPost } from './client';
import type { BackgroundTask } from './tasks';
export type { BackgroundTask, TaskState } from './tasks';

export type StartRadarScanRequest = {
  force_refresh?: boolean;
};

export type StartRadarScanResponse = {
  ok: boolean;
  task: BackgroundTask;
};

export type RadarScan = {
  scan_id: string;
  state: string;
  source?: string;
  top50_count: number;
  top4_count?: number;
  market_heat: number;
  alert_count?: number;
  duration_ms?: number;
  error?: string;
  metadata?: Record<string, unknown>;
  started_at?: string | null;
  completed_at?: string | null;
};

export type RadarCandidate = {
  scan_id?: string;
  symbol: string;
  base_asset?: string;
  rank: number;
  score: number;
  direction: string;
  stage?: string;
  trigger_mode?: string;
  price?: number;
  change_5m?: number;
  change_15m?: number;
  change_1h?: number;
  oi_change?: number;
  fund_confirm_count?: number;
  fund_confirm_total?: number;
  fake_breakout_risk?: string;
  ai_candidate?: boolean;
  market_structure?: Record<string, unknown>;
  score_features?: Record<string, unknown>;
  score_explain?: Record<string, unknown>;
  raw?: Record<string, unknown>;
};

export type LatestRadarScanResponse = {
  ok: boolean;
  scan: RadarScan | null;
  candidates: RadarCandidate[];
};

export function startRadarScan(payload: StartRadarScanRequest = {}): Promise<StartRadarScanResponse> {
  return apiPost<StartRadarScanResponse>('/radar/scans', payload);
}

export function getLatestRadarScan(): Promise<LatestRadarScanResponse> {
  return apiGet<LatestRadarScanResponse>('/radar/scans/latest');
}
