import { describe, expect, it, vi } from 'vitest';
import { getDashboardOverview } from './dashboard';

describe('getDashboardOverview', () => {
  it('loads dashboard overview from the v2 API', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => JSON.stringify({
        ok: true,
        state: { code: 'WATCH', text: 'Risk checks required.' },
        metrics: {
          top50_count: 3,
          ai_candidate_count: 2,
          actionable_count: 1,
          average_score: 66,
          dynamic_stream_count: 12,
          active_coin_count: 9,
          fund_ready_count: 2,
          fake_high_count: 1,
        },
        direction: {
          long: 1,
          short: 1,
          neutral: 1,
          long_pct: 33,
          short_pct: 33,
          neutral_pct: 34,
        },
        candidates: [],
        scan: {
          last_scan_id: 'scan-dashboard',
          last_scan_time: '2026-06-27 14:30:00',
          market_heat: 71,
          alert_count: 4,
          scan_status: {},
        },
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const overview = await getDashboardOverview();

    expect(fetchMock).toHaveBeenCalledWith('/api/v2/dashboard/overview', { headers: new Headers() });
    expect(overview.state.code).toBe('WATCH');
    expect(overview.metrics.average_score).toBe(66);
  });
});
