import { describe, expect, it, vi } from 'vitest';
import { getTaskStatus } from './tasks';

describe('tasks API', () => {
  it('loads background task status from the v2 API', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () => JSON.stringify({
        task_id: 'task-1',
        kind: 'radar_scan',
        state: 'running',
        metadata: { force_refresh: true },
        result: null,
        error: '',
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const task = await getTaskStatus('task-1');

    expect(fetchMock).toHaveBeenCalledWith('/api/v2/tasks/task-1', { headers: new Headers() });
    expect(task.kind).toBe('radar_scan');
    expect(task.state).toBe('running');
  });
});
