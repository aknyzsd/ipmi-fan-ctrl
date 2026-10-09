// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 aknyzsd

import { useEffect, useRef } from 'react';
import { Card } from 'antd';
import { logClass } from '../utils';

interface LogItem {
  id: number;
  msg: string;
}

interface Props {
  logs: LogItem[];
}

/** 日志框：自动滚动到底 + 按关键词着色 */
export function LogBox({ logs }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [logs]);

  const colorMap: Record<string, string> = {
    err: '#ff6b6b',
    warn: '#ffd93d',
    ok: '#00d4aa',
  };

  return (
    <Card title="日志" size="small" style={{ marginBottom: 16 }}>
      <div
        ref={boxRef}
        style={{
          background: 'rgba(0,0,0,.4)',
          borderRadius: 8,
          padding: 10,
          height: 180,
          overflowY: 'auto',
          fontFamily: "'Cascadia Code', Consolas, monospace",
          fontSize: 11,
          color: '#8a8a8a',
          lineHeight: 1.6,
        }}
      >
        {logs.map((l) => {
          const cls = logClass(l.msg);
          return (
            <div
              key={l.id}
              style={cls ? { color: colorMap[cls] } : undefined}
            >
              {l.msg}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
