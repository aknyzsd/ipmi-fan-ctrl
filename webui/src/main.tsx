// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 aknyzsd

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider, theme, App as AntApp } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: '#00d4aa',
          colorInfo: '#00a8ff',
          borderRadius: 8,
        },
      }}
    >
      <AntApp>
        <App />
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
);
