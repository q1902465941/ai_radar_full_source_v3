import { describe, expect, it, vi } from 'vitest';
import { apiGet } from './client';

describe('apiGet', () => {
  it('prefixes v2 API paths and parses JSON responses', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => JSON.stringify({ ok: true, version: 'v2' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const data = await apiGet<{ ok: boolean; version: string }>('/health');

    expect(fetchMock).toHaveBeenCalledWith('/api/v2/health', { headers: new Headers() });
    expect(data).toEqual({ ok: true, version: 'v2' });
  });

  it('throws backend detail messages for failed responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: async () => JSON.stringify({ detail: 'invalid_api_token' }),
    }));

    await expect(apiGet('/health')).rejects.toThrow('invalid_api_token');
  });
});
