// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 aknyzsd

import { Card } from 'antd';
import type { DiskGroup } from '../types';

interface Props {
  disks: DiskGroup;
}

const GROUP_LABEL: Record<keyof DiskGroup, string> = {
  hdd: '机械盘',
  ssd: '固态盘',
  nvme: 'NVMe',
};

/** 硬盘温度面板：按旋转/非旋转分组展示 */
export function DiskPanel({ disks }: Props) {
  const groups = (['hdd', 'ssd', 'nvme'] as const).filter(
    (k) => disks[k]?.length,
  );
  if (groups.length === 0) return null;

  return (
    <Card title="硬盘温度" size="small" style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap' }}>
        {groups.map((g) => (
          <div key={g}>
            <div style={{ color: '#888', fontSize: 12, marginBottom: 4 }}>
              {GROUP_LABEL[g]}
            </div>
            {disks[g].map((d) => (
              <div
                key={d.name}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 16,
                  padding: '5px 0',
                  fontSize: 13,
                  minWidth: 120,
                }}
              >
                <span>{d.name}</span>
                <span
                  style={{
                    fontFamily: "'Cascadia Code', Consolas, monospace",
                    fontWeight: 700,
                    color: '#00d4aa',
                  }}
                >
                  {d.temp != null ? `${d.temp}℃` : '—'}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </Card>
  );
}
