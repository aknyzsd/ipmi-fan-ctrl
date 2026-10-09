// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 aknyzsd

import { Card, Slider, InputNumber, Button, Tooltip } from 'antd';
import { SettingOutlined } from '@ant-design/icons';
import type { Params } from '../types';

interface Props {
  params: Params;
  onChange: (key: keyof Params, value: number) => void;
  onApply: () => void;
  applying?: boolean;
}

const HELP: Record<string, string> = {
  kp: '比例增益。误差每1℃加Kp%PWM，越大响应越快但易振荡',
  ki: '积分增益。消除稳态误差(温度长期偏离的累积修正)，过大会振荡',
  kd: '微分增益。抑制突变预判趋势，温度噪声大时易放大干扰，默认关',
  load_kf: 'CPU负载前馈增益。CPU一忙就提前加速风扇不等温度升。0.3=80%负载加24%PWM',
  delta_t_k: '进排风温差前馈增益。温差大说明整机热负荷高(含硬盘/显卡)，超10℃基准才加成',
};

function ParamRow({
  label,
  help,
  children,
}: {
  label: string;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 0',
        fontSize: 13,
      }}
    >
      <span style={{ width: 88, color: '#aab', flexShrink: 0 }}>
        {label}
        {help && (
          <Tooltip title={help}>
            <span
              style={{
                display: 'inline-block',
                marginLeft: 3,
                width: 14,
                height: 14,
                lineHeight: '14px',
                textAlign: 'center',
                borderRadius: '50%',
                background: '#555',
                color: '#fff',
                fontSize: 10,
                cursor: 'help',
              }}
            >
              ?
            </span>
          </Tooltip>
        )}
      </span>
      <div style={{ flex: 1 }}>{children}</div>
    </div>
  );
}

/** 参数调节面板：目标/PWM 上下限/PID/前馈 */
export function ParamsPanel({ params, onChange, onApply, applying }: Props) {
  return (
    <Card
      title="参数调节"
      size="small"
      style={{ marginBottom: 16 }}
      extra={
        <Button
          type="primary"
          size="small"
          icon={<SettingOutlined />}
          loading={applying}
          onClick={onApply}
        >
          应用参数
        </Button>
      }
    >
      <ParamRow label="目标温度">
        <Slider
          min={40}
          max={75}
          value={params.target}
          onChange={(v) => onChange('target', v)}
          tooltip={{ formatter: (v) => `${v}℃` }}
        />
      </ParamRow>
      <ParamRow label="PWM 下限">
        <Slider
          min={0}
          max={50}
          value={params.pwm_min}
          onChange={(v) => onChange('pwm_min', v)}
          tooltip={{ formatter: (v) => `${v}%` }}
        />
      </ParamRow>
      <ParamRow label="PWM 上限">
        <Slider
          min={50}
          max={100}
          value={params.pwm_max}
          onChange={(v) => onChange('pwm_max', v)}
          tooltip={{ formatter: (v) => `${v}%` }}
        />
      </ParamRow>
      <ParamRow label="Kp" help={HELP.kp}>
        <InputNumber
          value={params.kp}
          step={0.1}
          onChange={(v) => v != null && onChange('kp', v)}
          style={{ width: '100%' }}
        />
      </ParamRow>
      <ParamRow label="Ki" help={HELP.ki}>
        <InputNumber
          value={params.ki}
          step={0.05}
          onChange={(v) => v != null && onChange('ki', v)}
          style={{ width: '100%' }}
        />
      </ParamRow>
      <ParamRow label="Kd" help={HELP.kd}>
        <InputNumber
          value={params.kd}
          step={0.1}
          onChange={(v) => v != null && onChange('kd', v)}
          style={{ width: '100%' }}
        />
      </ParamRow>
      <ParamRow label="负载前馈" help={HELP.load_kf}>
        <InputNumber
          value={params.load_kf}
          step={0.05}
          onChange={(v) => v != null && onChange('load_kf', v)}
          style={{ width: '100%' }}
        />
      </ParamRow>
      <ParamRow label="温差前馈" help={HELP.delta_t_k}>
        <InputNumber
          value={params.delta_t_k}
          step={0.1}
          onChange={(v) => v != null && onChange('delta_t_k', v)}
          style={{ width: '100%' }}
        />
      </ParamRow>
    </Card>
  );
}
