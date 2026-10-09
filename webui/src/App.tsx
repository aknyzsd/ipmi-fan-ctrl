// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 aknyzsd

import { useCallback, useEffect, useState } from 'react';
import {
  Layout,
  Row,
  Col,
  Button,
  Switch,
  Badge,
  Space,
  Typography,
  message,
} from 'antd';
import { EditOutlined, PlayCircleOutlined, PauseCircleOutlined } from '@ant-design/icons';
import { api } from './api/client';
import { useSSE } from './api/useSSE';
import { RealtimePanel } from './components/RealtimePanel';
import { ParamsPanel } from './components/ParamsPanel';
import { LogBox } from './components/LogBox';
import { DiskPanel } from './components/DiskPanel';
import { CalibrationPanel } from './components/CalibrationPanel';
import { ConfigModal } from './components/ConfigModal';
import type { Status, Params, AppConfig, DiskGroup } from './types';

const { Title } = Typography;

const EMPTY_STATUS: Status = {
  running: false,
  quiet_mode: false,
  cpu_temp: null,
  inlet_temp: null,
  exhaust_temp: null,
  delta_t: null,
  cpu_usage: null,
  power: null,
  pwm: null,
  fan_rpm: null,
  fan_readings: {},
  pid_p: 0,
  pid_i: 0,
  pid_d: 0,
  feedforward: 0,
  emergency: false,
  reason: '',
};

const DEFAULT_PARAMS: Params = {
  target: 55,
  pwm_min: 27,
  pwm_max: 100,
  kp: 2.0,
  ki: 0.1,
  kd: 0.0,
  load_kf: 0.3,
  delta_t_k: 1.0,
};

const EMPTY_DISKS: DiskGroup = { hdd: [], ssd: [], nvme: [] };

interface LogItem {
  id: number;
  msg: string;
}

let logId = 0;

export default function App() {
  const [status, setStatus] = useState<Status>(EMPTY_STATUS);
  const [params, setParams] = useState<Params>(DEFAULT_PARAMS);
  const [running, setRunning] = useState(false);
  const [quiet, setQuiet] = useState(false);
  const [showConfig, setShowConfig] = useState(false);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [disks, setDisks] = useState<DiskGroup>(EMPTY_DISKS);
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [applying, setApplying] = useState(false);
  const [toggling, setToggling] = useState(false);

  const addLog = useCallback((msg: string) => {
    setLogs((prev) => {
      const next = [...prev, { id: ++logId, msg }];
      return next.length > 200 ? next.slice(next.length - 200) : next;
    });
  }, []);

  // SSE 订阅实时推送
  useSSE((msg) => {
    if (msg.event === 'data') {
      setStatus((prev) => ({ ...prev, ...(msg.data as Partial<Status>) }));
    } else if (msg.event === 'log') {
      addLog(msg.data.message as string);
    } else if (msg.event === 'status') {
      setRunning(Boolean(msg.data.running));
      if (msg.data.error) addLog(`[错误] ${msg.data.error}`);
    } else if (msg.event === 'disks') {
      setDisks(msg.data.disks as DiskGroup);
    }
  });

  // 初始化：拉配置 + 状态，未运行时补一次快照
  useEffect(() => {
    api.getConfig().then((cfg) => {
      setParams({
        target: cfg.target_cpu_temp ?? DEFAULT_PARAMS.target,
        pwm_min: cfg.pwm_min ?? DEFAULT_PARAMS.pwm_min,
        pwm_max: cfg.pwm_max ?? DEFAULT_PARAMS.pwm_max,
        kp: cfg.kp ?? DEFAULT_PARAMS.kp,
        ki: cfg.ki ?? DEFAULT_PARAMS.ki,
        kd: cfg.kd ?? DEFAULT_PARAMS.kd,
        load_kf: cfg.load_kf ?? DEFAULT_PARAMS.load_kf,
        delta_t_k: cfg.delta_t_k ?? DEFAULT_PARAMS.delta_t_k,
      });
    });

    api.getStatus().then((s) => {
      setRunning(s.running);
      setQuiet(s.quiet_mode);
      setStatus(s);
      if (!s.running && !s.cpu_temp) {
        addLog('正在读取 BMC 传感器…');
        api.getSnapshot().then((snap) => {
          if (snap.error) {
            addLog(`[错误] 读取失败: ${snap.error}`);
          } else {
            setStatus((prev) => ({ ...prev, ...snap }));
            addLog('BMC 数据已同步');
          }
        });
      }
    });
  }, [addLog]);

  const handleParamChange = (key: keyof Params, value: number) => {
    setParams((prev) => ({ ...prev, [key]: value }));
  };

  const handleApplyParams = async () => {
    setApplying(true);
    try {
      const r = await api.applyParams(params);
      addLog(r.message || r.error || '参数已应用');
    } finally {
      setApplying(false);
    }
  };

  const handleToggleMode = async (checked: boolean) => {
    setQuiet(checked);
    addLog(checked ? '动态模式已开启' : '动态模式已关闭');
    if (running) {
      await api.control('mode', checked);
    }
  };

  const handleToggleRun = async () => {
    setToggling(true);
    try {
      if (running) {
        await api.control('stop');
      } else {
        const r = await api.control('start', quiet);
        if (r.error) addLog(`[错误] ${r.error}`);
      }
    } finally {
      setToggling(false);
    }
  };

  const handleOpenConfig = async () => {
    const cfg = await api.getConfig();
    if (!cfg.password) cfg.password = '${DELL_BMC_PASSWORD}';
    setConfig(cfg);
    setShowConfig(true);
  };

  const handleSaveConfig = async (cfg: AppConfig) => {
    const r = await api.saveConfig(cfg);
    if (r.error) {
      addLog(`[错误] ${r.error}`);
      message.error(r.error);
    } else {
      addLog('配置已保存到文件');
      setShowConfig(false);
    }
  };

  return (
    <Layout style={{ minHeight: '100vh', padding: 16 }}>
      <div style={{ maxWidth: 960, margin: '0 auto', width: '100%' }}>
        {/* Header */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            marginBottom: 16,
            flexWrap: 'wrap',
          }}
        >
          <Button icon={<EditOutlined />} onClick={handleOpenConfig}>
            编辑配置
          </Button>
          <Title
            level={4}
            style={{
              margin: 0,
              background: 'linear-gradient(90deg,#00d4aa,#00a8ff)',
              WebkitBackgroundClip: 'text',
              WebkitTextFillColor: 'transparent',
            }}
          >
            Dell 风扇温控
          </Title>
          <Space>
            <span style={{ fontSize: 14 }}>动态模式</span>
            <Switch checked={quiet} onChange={handleToggleMode} />
          </Space>
          <Button
            type={running ? 'default' : 'primary'}
            danger={running}
            icon={
              running ? <PauseCircleOutlined /> : <PlayCircleOutlined />
            }
            loading={toggling}
            onClick={handleToggleRun}
          >
            {running ? '停止' : '启动'}
          </Button>
          <div style={{ marginLeft: 'auto' }}>
            <Badge
              status={running ? 'success' : 'default'}
              text={running ? '运行中' : '未运行'}
            />
          </div>
        </div>

        {/* 三栏：实时数据 + 参数调节 */}
        <Row gutter={16}>
          <Col xs={24} sm={12}>
            <RealtimePanel data={status} />
          </Col>
          <Col xs={24} sm={12}>
            <ParamsPanel
              params={params}
              onChange={handleParamChange}
              onApply={handleApplyParams}
              applying={applying}
            />
          </Col>
        </Row>

        {/* 硬盘温度 */}
        <DiskPanel disks={disks} />

        {/* 风扇标定 */}
        <CalibrationPanel onLog={addLog} />

        {/* 日志 */}
        <LogBox logs={logs} />

        {/* 配置弹窗 */}
        <ConfigModal
          open={showConfig}
          config={config}
          onCancel={() => setShowConfig(false)}
          onSave={handleSaveConfig}
        />
      </div>
    </Layout>
  );
}
