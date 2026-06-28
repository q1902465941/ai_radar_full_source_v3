import { apiGet } from './client';

export type TaskState = 'pending' | 'running' | 'succeeded' | 'failed' | string;

export type BackgroundTask = {
  task_id: string;
  kind: string;
  state: TaskState;
  created_at_ms?: number;
  updated_at_ms?: number;
  completed_at_ms?: number | null;
  metadata: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error: string;
};

export function getTaskStatus(taskId: string): Promise<BackgroundTask> {
  return apiGet<BackgroundTask>(`/tasks/${encodeURIComponent(taskId)}`);
}
