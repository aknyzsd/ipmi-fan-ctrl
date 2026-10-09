// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 aknyzsd

import { Card, Button, Tag, Space, Empty, message } from 'antd';
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { useEffect, useState, useCallback } from 'react';
import { api } from '../api/client';
import type { CalibrationData } from '../types';

// 风扇曲线颜色（最多6个风扇）
const FAN_COLORS = ['#1668dc', '#52c41a', '#fa8c16', '#eb2f96', '#722ed1', '#13c2c2'];

const HEALTH_COLOR: Record<string, string> = {
  ok: 'green',
  abnormal: 'orange',
  fault: 'red',
};

const HEALTH_LABEL: Record<string, string> = {
  ok: '正常',
  abnormal: '异常',
  fault: '故障',
};

interface Props {
  onLog?: (msg: string) => void;
}

/** 风扇标定面板：PWM→RPM 曲线 + 风扇健康状态 + 重新标定按钮 */
export function CalibrationPanel({ onLog }: Props) {
  const [data, setData] = useState<CalibrationData | null>(null);
  const [calibrating, setCalibrating] = useState(false);

  const fetchCalibration = useCallback(async () => {
    try {
      const result = await api.getCalibration();
      setData(result);
    } catch {
      // 静默失败，不打扰用户
    }
  }, []);

  useEffect(() => {
    fetchCalibration();
    // 每10秒刷新一次标定数据（标定进行中时能看到进度）
    const timer = setInterval(fetchCalibration, 10000);
    return () => clearInterval(timer);
  }, [fetchCalibration]);

  const handleRecalibrate = async () => {
    setCalibrating(true);
    try {
      const result = await api.control('calibrate');
      if (result.error) {
        message.error(result.error);
      } else {
        message.success('标定已启动，约35秒完成');
        onLog?.('手动触发风扇标定');
      }
    } catch (e) {
      message.error(`触发失败: ${e}`);
    } finally {
      // 标定约35秒，40秒后恢复按钮状态
      setTimeout(() => setCalibrating(false), 40000);
    }
  };

  const calibration = data?.calibration ?? {};
  const health = data?.health ?? {};
  const fanNames = Object.keys(health);
  const pwmPoints = Object.keys(calibration).map(Number).sort((a, b) => a - b);

  // 计算曲线图坐标
  const hasData = pwmPoints.length > 0 && fanNames.length > 0;
  const maxRpm = hasData
    ? Math.max(...fanNames.flatMap((name) => pwmPoints.map((pwm) => calibration[String(pwm)]?.[name] ?? 0)))
    : 0;
  const chartW = 320;
  const chartH = 160;
  const padding = 30;

  //1. 生成折线 path
  const buildPath = (fanName: string) => {
    const points = pwmPoints.map((pwm) => {
      const rpm = calibration[String(pwm)]?.[fanName] ?? 0;
      const x = padding + (pwm / 100) * (chartW - padding * 2);
      const y = chartH - padding - (rpm / maxRpm) * (chartH - padding * 2);
      return `${x},${y}`;
    });
    return points.join(' ');
  };

  return (
    <Card
      title="风扇标定"
      size="small"
      style={{ marginBottom: 16 }}
      extra={
        <Space>
          <Button
            size="small"
            icon={<ThunderboltOutlined />}
            loading={calibrating}
            onClick={handleRecalibrate}
          >
            {calibrating ? '标定中…' : '重新标定'}
          </Button>
          <Button size="small" icon={<ReloadOutlined />} onClick={fetchCalibration} />
        </Space>
      }
    >
      {!hasData ? (
        <Empty description="暂无标定数据，点击「重新标定」开始" />
      ) : (
        <>
          {/* 风扇健康状态 */}
          <div style={{ marginBottom: 12 }}>
            <Space wrap>
              {fanNames.map((name) => (
                <Tag key={name} color={HEALTH_COLOR[health[name]] ?? 'default'}>
                  {name}
                  <span style={{ marginLeft: 4, fontSize: 11 }}>
                    {HEALTH_LABEL[health[name]] ?? health[name]}
                  </span>
                </Tag>
              ))}
            </Space>
          </div>

          {/* 标定曲线 SVG */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16 }}>
            <svg width={chartW} height={chartH} style={{ flexShrink: 0 }}>
              {/* 坐标轴 */}
              <line x1={padding} y1={chartH - padding} x2={chartW - padding} y2={chartH - padding} stroke="#555" />
              <line x1={padding} y1={padding} x2={padding} y2={chartH - padding} stroke="#555" />
              {/* 轴标签 */}
              <text x={chartW / 2} y={chartH - 4} fill="#888" fontSize={10} textAnchor="middle">PWM (%)</text>
              <text x={4} y={chartH / 2} fill="#888" fontSize={10} textAnchor="middle" transform={`rotate(-90 4 ${chartH / 2})`}>RPM</text>
              {/* 刻度 */}
              {[0, 25, 50, 75, 100].map((pwm) => {
                const x = padding + (pwm / 100) * (chartW - padding * 2);
                return (
                  <g key={pwm}>
                    <line x1={x} y1={chartH - padding} x2={x} y2={chartH - padding + 3} stroke="#555" />
                    <text x={x} y={chartH - padding + 14} fill="#888" fontSize={9} textAnchor="middle">{pwm}</text>
                  </g>
                );
              })}
              {/* 风扇折线 */}
              {fanNames.map((name, i) => (
                <g key={name}>
                  <polyline
                    points={buildPath(name)}
                    fill="none"
                    stroke={FAN_COLORS[i % FAN_COLORS.length]}
                    strokeWidth={1.5}
                  />
                  {pwmPoints.map((pwm) => {
                    const rpm = calibration[String(pwm)]?.[name] ?? 0;
                    const x = padding + (pwm / 100) * (chartW - padding * 2);
                    const y = chartH - padding - (rpm / maxRpm) * (chartH - padding * 2);
                    return <circle key={pwm} cx={x} cy={y} r={2} fill={FAN_COLORS[i % FAN_COLORS.length]} />;
                  })}
                </g>
              ))}
            </svg>

            {/* 图例 + 数值表 */}
            <div style={{ fontSize: 12 }}>
              {fanNames.map((name, i) => (
                <div key={name} style={{ marginBottom: 4 }}>
                  <span style={{ color: FAN_COLORS[i % FAN_COLORS.length], marginRight: 6 }}>●</span>
                  <span style={{ color: '#ccc' }}>{name}</span>
                </div>
              ))}
            </div>
          </div>

          {/* 标定数据表 */}
          <div style={{ marginTop: 12, overflowX: 'auto' }}>
            <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={{ textAlign: 'left', padding: '2px 6px', borderBottom: '1px solid #333' }}>PWM</th>
                  {fanNames.map((name) => (
                    <th key={name} style={{ textAlign: 'right', padding: '2px 6px', borderBottom: '1px solid #333' }}>{name}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pwmPoints.map((pwm) => (
                  <tr key={pwm}>
                    <td style={{ padding: '2px 6px', color: '#888' }}>{pwm}%</td>
                    {fanNames.map((name) => (
                      <td key={name} style={{ textAlign: 'right', padding: '2px 6px', color: '#ccc' }}>
                        {calibration[String(pwm)]?.[name]?.toFixed(0) ?? '—'}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Card>
  );
}
