# IPMI 风扇 PID 温控

通过 IPMI over LAN 连 BMC/iDRAC，读温度/负载/功耗，
用 PID + 负载前馈 + 进排风温差前馈 输出手动风扇 PWM。

## 特性

- 纯 pyghmi（读传感器 4.4s），无外部 ipmitool 依赖
- 单循环线程，PID + 负载前馈 + 温差前馈
- 动态模式：双向逐级调速，温度低探底找安静点，大波动切 PID
- WebUI：Vue 3 CDN + SSE，零构建零额外依赖
- 配置：`~/dell_fan_ctrl/config.json`，和代码分离，升级不丢

## 快速开始

1. 装依赖：
   ```
   pip install pyghmi
   ```
2. 运行 WebUI（推荐）：
   ```
   python -m dell_fan_ctrl.web
   ```
   首次运行自动在 `~/dell_fan_ctrl/config.json` 生成默认配置，改 IP/密码后重启。
   浏览器打开 http://localhost:8089，支持远程/手机访问。
3. 命令行模式（无界面）：
   ```
   python -m dell_fan_ctrl
   ```
4. 退出（Ctrl+C）设回手动 PWM=27%（安静），紧急回退（≥80℃）交还 iDRAC 自动控制。

## 两种调速模式

- **PID 模式**（默认）：PID + 负载前馈 + 温差前馈，维持目标温度
- **动态模式**：双向逐级调速——温度低递减探底找安静点，温度缓升递增跟随，大波动切 PID 接管，PID 下稳定 60s 回动态模式

WebUI 勾选「动态模式」实时切换。

## 安全

- PWM 下限可设 0（快速采样 + 紧急回退兜底），默认 27%
- 紧急温度（默认 80℃）触发回退 iDRAC 自动控制（散热优先于安静）
- 正常退出设回手动 27%（安静），不交还自动

## 调参

- `target_cpu_temp`：目标 CPU 温度，PID 试图维持
- `kp/ki/kd`：PID 参数，默认 2.0/0.1/0.0
- `load_kf`：CPU 负载前馈增益，越大越激进
- `delta_t_k`：进排风温差前馈增益（超过 10℃ 基准才生效）

## 依赖

- Python 3.10+
- pyghmi（纯 Python IPMI）
- WebUI 用 Vue 3 CDN + 标准库 http.server + SSE，零构建零额外依赖

## Disclaimer

本项目为个人开源项目，非官方、不关联 Dell Technologies。
"Dell"、"iDRAC" 等商标归其各自所有者所有，本项目仅描述兼容性。
