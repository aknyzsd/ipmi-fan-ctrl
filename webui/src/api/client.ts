import type {
  Status,
  Snapshot,
  AppConfig,
  Params,
  ApiResult,
  CalibrationData,
} from '../types';

/** 统一 fetch 封装：JSON 进 JSON 出，错误抛 Error */
async function request<T>(
  url: string,
  options?: RequestInit,
): Promise<T> {
  const resp = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await resp.text();
  let json: unknown;
  try {
    json = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`非 JSON 响应: ${text.slice(0, 200)}`);
  }
  return json as T;
}

function post(url: string, body: unknown): Promise<ApiResult> {
  return request<ApiResult>(url, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export const api = {
  getStatus: () => request<Status>('/api/status'),
  getSnapshot: () => request<Snapshot>('/api/snapshot'),
  getConfig: () => request<AppConfig>('/api/config'),

  control: (action: 'start' | 'stop' | 'mode' | 'calibrate', quiet?: boolean) =>
    post('/api/control', { action, quiet }),

  applyParams: (params: Params) => post('/api/params', params),

  saveConfig: (config: AppConfig) => post('/api/config/save', config),

  getCalibration: () => request<CalibrationData>('/api/calibration'),
};
