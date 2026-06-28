import { describe, expect, it, vi } from 'vitest';
import { getLatestRadarScan, startRadarScan } from './radar';

describe('radar API', () => {
  it('starts radar scans through the async task endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => JSON.stringify({
        ok: true,
        task: {
          task_id: 'task-1',
          kind: 'radar_scan',
          state: 'pending',
          metadata: { force_refresh: true },
        },
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const response = await startRadarScan({ force_refresh: true });

    expect(fetchMock).toHaveBeenCalledWith('/api/v2/radar/scans', {
      method: 'POST',
      headers: new Headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ force_refresh: true }),
    });
    expect(response.task.task_id).toBe('task-1');
    expect(response.task.state).toBe('pending');
  });

  it('loads the latest persisted radar scan', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: async () => JSON.stringify({
        ok: true,
        scan: {
          scan_id: 'scan-1',
          state: 'succeeded',
          top50_count: 1,
          market_heat: 72,
        },
        candidates: [
          { symbol: 'BTCUSDT', rank: 1, score: 88.5, direction: 'LONG' },
        ],
      }),
    }));

    const latest = await getLatestRadarScan();

    expect(latest.scan?.scan_id).toBe('scan-1');
    expect(latest.candidates[0].symbol).toBe('BTCUSDT');
  });
});
