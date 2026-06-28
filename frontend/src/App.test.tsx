import React from 'react';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { getDashboardOverview } from './api/dashboard';
import { getLatestRadarScan } from './api/radar';

vi.mock('./api/dashboard', () => ({
  getDashboardOverview: vi.fn(),
}));

vi.mock('./api/radar', () => ({
  getLatestRadarScan: vi.fn(),
  startRadarScan: vi.fn(),
}));

vi.mock('./api/tasks', () => ({
  getTaskStatus: vi.fn(),
}));

const dashboardOverview = {
  ok: true,
  state: { code: 'NEUTRAL', text: 'System observing.' },
  metrics: {
    top50_count: 0,
    ai_candidate_count: 0,
    actionable_count: 0,
    average_score: 0,
    dynamic_stream_count: 0,
    active_coin_count: 0,
    fund_ready_count: 0,
    fake_high_count: 0,
  },
  direction: { long: 0, short: 0, neutral: 0, long_pct: 0, short_pct: 0, neutral_pct: 100 },
  candidates: [],
  scan: {
    last_scan_id: '',
    last_scan_time: '',
    market_heat: 0,
    alert_count: 0,
    scan_status: {},
  },
};

const latestRadarScan = {
  ok: true,
  scan: {
    scan_id: 'scan-1',
    state: 'succeeded',
    top50_count: 1,
    top4_count: 1,
    market_heat: 72,
    alert_count: 1,
  },
  candidates: [
    {
      scan_id: 'scan-1',
      symbol: 'BTCUSDT',
      base_asset: 'BTC',
      rank: 1,
      score: 88.5,
      direction: 'LONG',
      stage: 'observe',
      trigger_mode: 'momentum',
      price: 100,
      change_5m: 1.2,
      change_15m: 2.3,
      oi_change: 3.4,
      fund_confirm_count: 3,
      fund_confirm_total: 4,
      fake_breakout_risk: 'LOW',
      ai_candidate: true,
      market_structure: { action: 'OPEN_LONG', regime: 'breakout', phase: 'actionable' },
    },
  ],
};

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('App', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.mocked(getDashboardOverview).mockResolvedValue(dashboardOverview);
    vi.mocked(getLatestRadarScan).mockResolvedValue(latestRadarScan);
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    root.unmount();
    container.remove();
    vi.clearAllMocks();
  });

  it('renders the React radar view from persisted v2 scan data', async () => {
    await act(async () => {
      root.render(<App />);
      await flush();
    });

    const radarButton = Array.from(container.querySelectorAll('button')).find((button) =>
      button.textContent?.includes('Radar'),
    );
    expect(radarButton).toBeTruthy();

    await act(async () => {
      radarButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await flush();
    });

    expect(getLatestRadarScan).toHaveBeenCalled();
    expect(container.textContent).toContain('Scan Evidence Matrix');
    expect(container.textContent).toContain('BTCUSDT');
    expect(container.textContent).toContain('88.5');
  });
});
