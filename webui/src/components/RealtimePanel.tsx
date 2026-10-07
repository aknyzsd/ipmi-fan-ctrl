import { Card, Statistic, Tag } from 'antd';
import { FireOutlined } from '@ant-design/icons';
import type { Status } from '../types';
import { fmt } from '../utils';

interface Props {
  data: Status;
}

/** 实时数据面板：温度/负载/功耗/PWM/PID/前馈 */
export function RealtimePanel({ data }: Props) {
  const pwmDisplay = data.emergency
    ? '紧急回退'
    : data.pwm != null
      ? `${data.pwm}%`
      : '—';

  return (
    <Card
      title="实时数据"
      size="small"
      style={{ marginBottom: 16 }}
    >
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: '8px 16px',
        }}
      >
        <Statistic title="CPU 温度" value={fmt(data.cpu_temp)} suffix="℃" />
        <Statistic title="进风温度" value={fmt(data.inlet_temp)} suffix="℃" />
        <Statistic title="排风温度" value={fmt(data.exhaust_temp)} suffix="℃" />
        <Statistic title="温差 ΔT" value={fmt(data.delta_t)} suffix="℃" />
        <Statistic title="CPU 负载" value={fmt(data.cpu_usage)} suffix="%" />
        <Statistic title="整机功耗" value={fmt(data.power)} suffix="W" />
        <div>
          <div style={{ color: 'rgba(255,255,255,.45)', fontSize: 12 }}>
            当前 PWM
          </div>
          <div
            style={{
              fontFamily: "'Cascadia Code', Consolas, monospace",
              fontWeight: 700,
              fontSize: 20,
              color: data.emergency ? '#ff6b6b' : '#00d4aa',
            }}
          >
            {data.emergency && <FireOutlined style={{ marginRight: 6 }} />}
            {pwmDisplay}
          </div>
        </div>
        <div>
          <div style={{ color: 'rgba(255,255,255,.45)', fontSize: 12 }}>
            风扇转速
          </div>
          {Object.keys(data.fan_readings || {}).length > 0 ? (
            <div style={{ marginTop: 2 }}>
              {Object.entries(data.fan_readings).map(([name, rpm]) => (
                <div
                  key={name}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    gap: 12,
                    fontSize: 13,
                    lineHeight: 1.6,
                  }}
                >
                  <span style={{ color: '#aab' }}>{name}</span>
                  <span
                    style={{
                      fontFamily: "'Cascadia Code', Consolas, monospace",
                      fontWeight: 700,
                      color: '#00d4aa',
                    }}
                  >
                    {Math.round(rpm)} RPM
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div
              style={{
                fontFamily: "'Cascadia Code', Consolas, monospace",
                fontWeight: 700,
                fontSize: 20,
                color: '#00d4aa',
              }}
            >
              {fmt(data.fan_rpm)} RPM
            </div>
          )}
        </div>
        <div>
          <div style={{ color: 'rgba(255,255,255,.45)', fontSize: 12 }}>
            PID (P/I/D)
          </div>
          <div
            style={{
              fontFamily: "'Cascadia Code', Consolas, monospace",
              fontWeight: 700,
              fontSize: 16,
              color: '#00d4aa',
            }}
          >
            {fmt(data.pid_p)} / {fmt(data.pid_i)} / {fmt(data.pid_d)}
          </div>
        </div>
        <Statistic title="前馈" value={fmt(data.feedforward)} suffix="%" />
      </div>
      {data.reason && (
        <Tag
          color={data.emergency ? 'red' : 'orange'}
          style={{ marginTop: 12 }}
        >
          {data.reason}
        </Tag>
      )}
    </Card>
  );
}
