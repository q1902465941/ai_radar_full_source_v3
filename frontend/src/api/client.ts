const API_PREFIX = '/api/v2';

type JsonObject = Record<string, unknown>;

function storedApiToken(): string {
  if (typeof window === 'undefined' || !window.localStorage) return '';
  return window.localStorage.getItem('api_token') || '';
}

function buildHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers || {});
  const token = storedApiToken();
  if (token && !headers.has('X-API-Token')) {
    headers.set('X-API-Token', token);
  }
  return headers;
}

function readError(data: JsonObject, fallback: string): string {
  const detail = data.detail;
  const error = data.error;
  if (typeof detail === 'string') return detail;
  if (typeof error === 'string') return error;
  return fallback;
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  const text = await response.text();
  const data = text ? JSON.parse(text) as JsonObject : {};
  if (!response.ok) {
    throw new Error(readError(data, `HTTP ${response.status}`));
  }
  return data as T;
}

export async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    headers: buildHeaders(init),
  });
  return parseJsonResponse<T>(response);
}

export async function apiPost<T>(path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const headers = buildHeaders(init);
  if (!headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    method: 'POST',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parseJsonResponse<T>(response);
}
