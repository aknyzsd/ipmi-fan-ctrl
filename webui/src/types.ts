// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 aknyzsd

// 后端 API 返回类型定义（对应 web.py 各 handler）

/** 实时数据 / GET /api/status 返回 */
export interface Status {
  running: boolean;
  quiet_mode: boolean;
  cpu_temp: number | null;
  inlet_temp: number | null;
  exhaust_temp: number | null;
  delta_t: number | null;
  cpu_usage: number | null;
  power: number | null;
  pwm: number | null;
  fan_rpm: number | null;
  fan_readings: Record<string, number>;
  pid_p: number;
  pid_i: number;
  pid_d: number;
  feedforward: number;
  emergency: boolean;
  reason: string;
}

/** GET /api/snapshot 返回（即时传感器快照） */
export interface Snapshot {
  cpu_temp?: number | null;
  inlet_temp?: number | null;
  exhaust_temp?: number | null;
  delta_t?: number | null;
  cpu_usage?: number | null;
  power?: number | null;
  fan_rpm?: number | null;
  error?: string;
}

/** GET /api/config 返回 / POST /api/config/save 入参 */
export interface AppConfig {
  ip: string;
  user: string;
  password: string;
  ipmi_mode: 'network' | 'local';
  target_cpu_temp: number;
  emergency_temp: number;
  inlet_safe_max: number;
  interval: number;
  kp: number;
  ki: number;
  kd: number;
  pwm_min: number;
  pwm_max: number;
  load_kf: number;
  delta_t_k: number;
  log_level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';
  log_file: string;
  probe_urls?: string;
}

/** POST /api/params 入参（参数调节面板） */
export interface Params {
  target: number;
  pwm_min: number;
  pwm_max: number;
  kp: number;
  ki: number;
  kd: number;
  load_kf: number;
  delta_t_k: number;
}

/** 硬盘温度条目 */
export interface DiskInfo {
  name: string;
  temp: number | null;
}

export interface DiskGroup {
  hdd: DiskInfo[];
  ssd: DiskInfo[];
  nvme: DiskInfo[];
}

/** SSE 推送消息统一外壳 */
export interface SSEMessage {
  event: 'data' | 'log' | 'status' | 'disks';
  data: Record<string, unknown>;
  ts: string;
}

/** 通用 API 响应 */
export interface ApiResult {
  status?: string;
  message?: string;
  error?: string;
}

/** GET /api/calibration 返回：标定数据+风扇健康 */
export interface CalibrationData {
  calibration: Record<string, Record<string, number>>; // {pwm: {fanName: rpm}}
  health: Record<string, 'ok' | 'abnormal' | 'fault'>; // {fanName: status}
}
