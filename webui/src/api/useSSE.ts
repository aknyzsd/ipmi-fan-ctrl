// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 aknyzsd

import { useEffect, useRef } from 'react';
import type { SSEMessage } from '../types';

type Handler = (msg: SSEMessage) => void;

/**
 * 订阅 /api/events 的 SSE hook。
 * onMessage 收到解析后的 {event, data, ts}；组件卸载自动断开。
 */
export function useSSE(onMessage: Handler) {
  // 用 ref 持有最新回调，避免 onMessage 变化导致重连
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  useEffect(() => {
    const es = new EventSource('/api/events');
    es.onmessage = (e: MessageEvent) => {
      try {
        const msg = JSON.parse(e.data) as SSEMessage;
        handlerRef.current(msg);
      } catch {
        // 后端心跳行（": heartbeat"）不会触发 onmessage，忽略解析失败
      }
    };
    es.onerror = () => {
      // EventSource 会自动重连，这里不额外处理
    };
    return () => es.close();
  }, []);
}
