# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 aknyzsd

"""温控策略：多传感器融合，输出目标 PWM。

- CPU 温度 → PID 反馈主控（消除稳态误差）
- CPU 负载 → 前馈（负载一上来就提前加速，不用等温度升）
- 进排风温差 → 前馈（整机实际热负荷，比功耗准，含硬盘/显卡热量）
- 紧急温度 → 触发回退 iDRAC 自动控制
"""
import logging
import json
import time
from collections import deque
from dataclasses import dataclass
from .ipmi import SensorSnapshot
from .pid import PIDController

logger = logging.getLogger(__name__)


@dataclass
class StrategyResult:
    pwm: int                      # 目标 PWM；-1 表示紧急回退不设手动
    cpu_temp: float | None
    cpu_usage: float | None
    delta_t: float | None
    power: float | None
    inlet_temp: float | None      # 进风温度（GUI 显示）
    exhaust_temp: float | None    # 排风温度（GUI 显示）
    fan_rpm: float | None         # 最高风扇转速（GUI 显示）
    fan_readings: dict[str, float]  # 各风扇名→RPM，前端逐个展示
    pid_p: float
    pid_i: float
    pid_d: float
    feedforward: float
    emergency: bool
    reason: str


class ThermalStrategy:
    def __init__(self, target_cpu_temp: float, emergency_temp: float,
                 inlet_safe_max: float, pwm_min: int, pwm_max: int,
                 pid: PIDController, load_kf: float, delta_t_k: float):
        self.target = target_cpu_temp
        self.emergency_temp = emergency_temp
        self.inlet_safe_max = inlet_safe_max
        self.pwm_min = pwm_min
        self.pwm_max = pwm_max
        self.pid = pid
        self.load_kf = load_kf      # 负载前馈增益
        self.delta_t_k = delta_t_k  # 温差前馈增益

    def compute(self, snap: SensorSnapshot, dt: float) -> StrategyResult:
        cpu_temp = snap.cpu_temp_max

        # 紧急：温度超阈值或读不到 → 回退自动控制（绝不烧硬件）
        if cpu_temp is None:
            return self._emergency(snap, "读不到 CPU 温度")
        if cpu_temp >= self.emergency_temp:
            return self._emergency(snap, f"CPU {cpu_temp:.1f}℃ ≥ 紧急阈值 {self.emergency_temp}℃")

        # PID 主控：error = 当前 - 目标（过热为正 → 输出正 → 加速）
        error = cpu_temp - self.target
        pid_out = self.pid.update(error, dt)

        # 负载前馈：CPU 负载高时提前加 PWM，不等温度升
        ff_load = self.load_kf * (snap.cpu_usage or 0.0)
        # 进排风温差前馈：温差大说明整机热负荷重（含硬盘/显卡，比功耗准）
        # 温差前馈：超过基准 10℃ 才加（正常低负载温差 ~8℃ 不加，避免无故加速）
        ff_dt = self.delta_t_k * max(0.0, (snap.delta_t or 0.0) - 10.0)
        feedforward = ff_load + ff_dt

        # 基准 pwm_min + PID 偏置 + 前馈，再钳位
        pwm = self.pwm_min + pid_out + feedforward
        pwm = int(round(max(self.pwm_min, min(self.pwm_max, pwm))))

        return StrategyResult(
            pwm=pwm, cpu_temp=cpu_temp, cpu_usage=snap.cpu_usage,
            delta_t=snap.delta_t, power=snap.power_watts,
            inlet_temp=snap.inlet_temp, exhaust_temp=snap.exhaust_temp,
            fan_rpm=max(snap.fan_rpms) if snap.fan_rpms else None,
            fan_readings=snap.fan_readings,
            pid_p=self.pid.last_p, pid_i=self.pid.last_i, pid_d=self.pid.last_d,
            feedforward=feedforward, emergency=False, reason="",
        )

    def _emergency(self, snap: SensorSnapshot, reason: str) -> StrategyResult:
        return StrategyResult(
            pwm=-1, cpu_temp=snap.cpu_temp_max, cpu_usage=snap.cpu_usage,
            delta_t=snap.delta_t, power=snap.power_watts,
            inlet_temp=snap.inlet_temp, exhaust_temp=snap.exhaust_temp,
            fan_rpm=max(snap.fan_rpms) if snap.fan_rpms else None,
            fan_readings=snap.fan_readings,
            pid_p=0, pid_i=0, pid_d=0, feedforward=0,
            emergency=True, reason=reason,
        )


class QuietStrategy:
    """动态模式：双向逐级调速，温度低递减探底、温度缓升递增应对。

    回调条件(温差>15℃/温升>2℃/min)触发时回 safe_pwm 保持稳定，解除后重新递减。
    负载突增时暂停递减观察，排除负载干扰。
    安全值持久化到 fan_safe_pwm.json，下次启动直接用。
    """

    def __init__(self, pwm_min: int, pwm_max: int,
                 pid_strategy: ThermalStrategy,
                 safe_pwm_file: str = "fan_safe_pwm.json",
                 initial_pwm: int | None = None):
        self.pwm_min = pwm_min
        self.pwm_max = pwm_max
        self.pid_strategy = pid_strategy
        self.safe_pwm_file = safe_pwm_file

        self.safe_pwm = self._load_safe_pwm()
        if initial_pwm is not None and self.safe_pwm == pwm_min:
            self.safe_pwm = max(pwm_min, min(pwm_max, initial_pwm))
        self.current_pwm = self.safe_pwm
        self.state = "descending"  # descending / callback_holding / fallback_pid(保留不触发)

        self._temp_history: deque[float] = deque(maxlen=10)
        self._exhaust_history: deque[float] = deque(maxlen=10)
        self._power_history: deque[float] = deque(maxlen=10)
        self._stable_count = 0
        self._ascend_hold = 0  # 递增后保持期计数器：>0时不递减，防止递增后立即递减震荡
        self._callback_count = 0
        self._observe_count = 0
        self._cooldown_count = 0  # 切回 descending 后的冷却观察期，期间不递减不递增
        self._prev_cpu_usage: float | None = None
        self._prev_power: float | None = None
        self._pid_stable_start: float | None = None
        self._pid_stable_rates: deque[float] = deque(maxlen=10)  # 温升率历史，迟滞带判据
        self._last_temp: float | None = None
        self._orig_pid_pwm_min: int | None = None  # fallback_pid 用（方案A遗留，不再触发）
        self._callback_offset = 15  # 方案B：回调时 current_pwm+固定偏置顶着，不切PID
        self._callback_hold_start: float | None = None  # callback_holding 状态起始时间
        self._callback_trigger = ""  # 触发回调的判据描述，保持期间日志带上便于排查
        self._callback_pending = 0  # 温升率/功耗率超阈值的连续次数，需累计2次才触发回调（防单次抖动）

    def _load_safe_pwm(self) -> int:
        try:
            with open(self.safe_pwm_file) as f:
                pwm = json.load(f).get("safe_pwm", self.pwm_min)
                return max(self.pwm_min, min(self.pwm_max, int(pwm)))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return self.pwm_min

    def _save_safe_pwm(self) -> None:
        try:
            with open(self.safe_pwm_file, "w") as f:
                json.dump({"safe_pwm": self.safe_pwm}, f)
        except OSError:
            pass

    def _temp_rise_rate(self, dt: float) -> float:
        """最近 CPU 温度上升率 ℃/min。"""
        return self._rise_rate(self._temp_history, dt)

    @staticmethod
    def _rise_rate(history: deque, dt: float) -> float:
        if len(history) < 3:
            return 0.0
        vals = list(history)
        n = len(vals)
        # 最小二乘线性回归取斜率，抗 ±1℃ 采样噪声（原首末两点法把 37→41 抖动算成 4℃/min 误触发回调）
        xs = list(range(n))
        mean_x = (n - 1) / 2.0
        mean_y = sum(vals) / n
        num = sum((xs[i] - mean_x) * (vals[i] - mean_y) for i in range(n))
        den = sum((x - mean_x) ** 2 for x in xs)
        if den == 0:
            return 0.0
        slope_per_sample = num / den  # 每采样周期变化量 ℃
        return slope_per_sample * (60.0 / dt)  # 转 ℃/min

    def compute(self, snap: SensorSnapshot, dt: float) -> StrategyResult:
        cpu_temp = snap.cpu_temp_max
        delta_t = snap.delta_t
        fan_rpm = max(snap.fan_rpms) if snap.fan_rpms else None

        # 紧急：温度超阈值或读不到 → 回退自动控制
        if cpu_temp is None:
            return self._emergency(snap, "读不到 CPU 温度")
        if cpu_temp >= self.pid_strategy.emergency_temp:
            return self._emergency(snap, f"CPU {cpu_temp:.1f}℃ ≥ 紧急阈值")

        self._temp_history.append(cpu_temp)
        if snap.exhaust_temp is not None:
            self._exhaust_history.append(snap.exhaust_temp)
        if snap.power_watts is not None:
            self._power_history.append(snap.power_watts)

        prev_pwm = self.current_pwm  # 本周期动作前 PWM，日志显示变化用

        # ── PID 回退模式：委托给 ThermalStrategy ──
        if self.state == "fallback_pid":
            result = self.pid_strategy.compute(snap, dt)
            # 跟踪 PID 输出，切回 descending 时保留这个值，避免从高 PWM 跳回 safe_pwm
            self.current_pwm = result.pwm
            # 记录温升率用于迟滞带判据（切回需最近 5 周期温升率都 < 0.5℃/min）
            self._pid_stable_rates.append(self._temp_rise_rate(dt))
            # 切回条件：温度稳定 + 低于目标有余量 + 稳定足够久 + 迟滞带满足
            temp_stable = (self._last_temp is not None
                           and abs(cpu_temp - self._last_temp) < 1.5)
            below_target = cpu_temp < self.pid_strategy.target - 3
            if temp_stable and below_target:
                if self._pid_stable_start is None:
                    self._pid_stable_start = time.monotonic()
                else:
                    rates_calm = (len(self._pid_stable_rates) >= 5
                                  and all(r < 0.5 for r in list(self._pid_stable_rates)[-5:]))
                    if time.monotonic() - self._pid_stable_start > 120 and rates_calm:
                        self.state = "descending"
                        self._callback_count = 0
                        self._pid_stable_start = None
                        self._pid_stable_rates.clear()
                        self._cooldown_count = 5  # 冷却观察期，防止刚切回又递减触发回调
                        # 恢复 PID 原本的 pwm_min，避免影响 PID 模式正常行为
                        if self._orig_pid_pwm_min is not None:
                            self.pid_strategy.pwm_min = self._orig_pid_pwm_min
                            self._orig_pid_pwm_min = None
                        result.reason = f"PID→动态 PWM{self.current_pwm}%保持: 稳定120s+低于目标3℃+温升率收敛 重新递减"
            else:
                self._pid_stable_start = None
            self._last_temp = cpu_temp
            return result

        # ── 回调保持：固定PWM顶着热负荷，不切PID（方案B）──
        if self.state == "callback_holding":
            # 记录温升率用于迟滞带判据
            self._pid_stable_rates.append(self._temp_rise_rate(dt))
            # 解除条件：温度降到目标-5℃以下 且 最近5周期温升率<0.5℃/min（迟滞带）
            below_target = cpu_temp < self.pid_strategy.target - 5
            rates_calm = (len(self._pid_stable_rates) >= 5
                          and all(r < 0.5 for r in list(self._pid_stable_rates)[-5:]))
            if below_target and rates_calm:
                self.state = "descending"
                self._callback_count = 0
                self._callback_hold_start = None
                self._callback_trigger = ""
                self._callback_pending = 0
                self._pid_stable_rates.clear()
                self._cooldown_count = 5  # 冷却观察期，防止刚解除又递减触发回调
                self.current_pwm = self.safe_pwm  # 回退到安全底重新探底，避免回调高位（如69%）残留导致 descending 慢降
                return self._result(snap, cpu_temp, fan_rpm,
                                    f"回调解除 PWM{prev_pwm}→{self.safe_pwm}(-{prev_pwm-self.safe_pwm}): 温度{cpu_temp:.1f}℃<目标-5℃ 重新递减")
            return self._result(snap, cpu_temp, fan_rpm,
                                f"回调保持 PWM{self.current_pwm}% [{self._callback_trigger}] CPU{cpu_temp:.0f}℃")

        # ── 负载突增检测（排除干扰）──
        load_surge = False
        surge_desc = ""
        if self._prev_cpu_usage is not None and snap.cpu_usage is not None:
            if snap.cpu_usage - self._prev_cpu_usage > 30:
                load_surge = True
                surge_desc = f"CPU {self._prev_cpu_usage:.0f}%→{snap.cpu_usage:.0f}%"
        if self._prev_power is not None and snap.power_watts is not None:
            if snap.power_watts - self._prev_power > 50:
                load_surge = True
                surge_desc = f"功率 {self._prev_power:.0f}W→{snap.power_watts:.0f}W"
        self._prev_cpu_usage = snap.cpu_usage
        self._prev_power = snap.power_watts

        if load_surge:
            self._observe_count = 3
            return self._result(snap, cpu_temp, fan_rpm,
                                f"负载突增 PWM{self.current_pwm}%保持: {surge_desc} 暂停观察")

        if self._observe_count > 0:
            self._observe_count -= 1
            return self._result(snap, cpu_temp, fan_rpm,
                                f"观察期剩余{self._observe_count}周期")

        # ── 回调条件 ──
        callback_reason = None
        if delta_t is not None and delta_t > 15.0:
            # 温差是强热失控信号，立即触发，不需持续性/温度门限
            callback_reason = f"温差{delta_t:.1f}℃>15℃(进{snap.inlet_temp:.0f}℃排{snap.exhaust_temp:.0f}℃)"
            self._callback_pending = 0
        else:
            cpu_rate = self._temp_rise_rate(dt)
            exhaust_rate = self._rise_rate(self._exhaust_history, dt)
            power_rate = self._rise_rate(self._power_history, dt)
            # 温升率/功耗率需连续3次超阈值才触发，靠最小二乘回归+持续性抗抖动，无温度门限
            rate_triggered = None
            if cpu_rate > 4.0:
                t0, t1 = self._temp_history[0], self._temp_history[-1]
                rate_triggered = f"CPU温升{cpu_rate:.1f}℃/min>4(温度{t0:.0f}→{t1:.0f}℃)"
            elif exhaust_rate > 3.0:
                e0, e1 = self._exhaust_history[0], self._exhaust_history[-1]
                rate_triggered = f"排风温升{exhaust_rate:.1f}℃/min>3(排风{e0:.0f}→{e1:.0f}℃)"
            elif power_rate > 150.0:
                p0, p1 = self._power_history[0], self._power_history[-1]
                rate_triggered = f"功耗升{power_rate:.0f}W/min>150(功率{p0:.0f}→{p1:.0f}W)"
            if rate_triggered:
                self._callback_pending += 1
                if self._callback_pending >= 3:
                    callback_reason = rate_triggered
            else:
                self._callback_pending = 0

        if callback_reason:
            # 方案B：回调不切PID，改用 safe_pwm+固定偏置顶着（基于safe_pwm而非current_pwm，防多次回调滚雪球）
            # PID 按 error=温度-目标 控制，温度低于目标时反而减速，与回调"立即加速"需求矛盾
            # 固定偏置立即加速，不依赖PID方向；基于safe_pwm每次回调回到同一安全底+偏置，不累加
            new_pwm = min(self.safe_pwm + self._callback_offset, self.pwm_max)
            self.current_pwm = new_pwm
            self.state = "callback_holding"
            self._callback_hold_start = time.monotonic()
            self._pid_stable_rates.clear()
            self._callback_trigger = callback_reason  # 存下判据，保持期间日志可追溯
            return self._result(snap, cpu_temp, fan_rpm,
                                f"回调 PWM{prev_pwm}→{new_pwm}(+{new_pwm-prev_pwm}): {callback_reason}")

        # ── 冷却观察期：刚从 PID 切回 descending，不递减不递增，等温度稳定 ──
        # 防止刚切回就递减把温度推升，又立刻触发回调形成震荡
        if self._cooldown_count > 0:
            self._cooldown_count -= 1
            return self._result(snap, cpu_temp, fan_rpm,
                                f"冷却观察期剩余{self._cooldown_count}周期")

        # ── 安全：递减或从监控回到递减 ──
        if self.state == "monitoring":
            self.state = "descending"
            self._callback_count = 0

        reason = ""
        if self.state == "descending":
            self._stable_count += 1
            if self._stable_count >= 3:
                self._stable_count = 0
                if self._ascend_hold > 0:
                    self._ascend_hold -= 1
                cpu_rate = self._temp_rise_rate(dt)
                near_target = cpu_temp > self.pid_strategy.target - 5
                # 温差反馈：温差大说明整机热负荷重，在回调(15℃)前提前递增，避免到15℃突然跳变
                dt_val = delta_t if delta_t is not None else 0.0
                dt_ascend = dt_val > 12.0    # 温差>12℃触发递增（即使温升率不快，提前应对热负荷）
                dt_heavy = dt_val > 10.0     # 温差>10℃抑制递减（热负荷偏重不该继续探底）
                # 递增判据：温升率快 或 温差大，且接近目标
                if (cpu_rate > 0.5 or dt_ascend) and near_target and self.current_pwm < self.pwm_max:
                    # 步长：温差>14或温升率>2→+3，温差>12或温升率>1→+2，否则+1
                    step = 3 if (dt_val > 14 or cpu_rate > 2.0) else (2 if (dt_val > 12 or cpu_rate > 1.0) else 1)
                    before = self.current_pwm
                    self.current_pwm = min(self.pwm_max, self.current_pwm + step)
                    self._ascend_hold = 3  # 递增后3个检查周期不递减，让PWM升够高温度稳住
                    prev_t = self._temp_history[-2] if len(self._temp_history) >= 2 else cpu_temp
                    trigger = f"温差{dt_val:.0f}℃>12" if (dt_ascend and cpu_rate <= 0.5) else f"CPU温升{cpu_rate:.1f}℃/min"
                    return self._result(snap, cpu_temp, fan_rpm,
                                        f"递增 PWM{before}→{self.current_pwm}(+{self.current_pwm-before}): {trigger} 距目标{self.pid_strategy.target-cpu_temp:.0f}℃ ΔT={dt_val:.0f}℃ (温度{prev_t:.0f}→{cpu_temp:.0f}℃)")
                # 递减需温度<目标-5℃ 且 温升率≤0.5℃/min 且 非递增保持期 且 温差<10℃(热负荷轻才探底)
                if (self.current_pwm > self.pwm_min and cpu_temp < self.pid_strategy.target - 5
                        and cpu_rate <= 0.5 and self._ascend_hold == 0 and not dt_heavy):
                    before = self.current_pwm
                    self.current_pwm -= 1
                    reason = f"递减 PWM{before}→{self.current_pwm}(-1) ΔT={dt_val:.0f}℃"

        return self._result(snap, cpu_temp, fan_rpm, reason)

    def _result(self, snap: SensorSnapshot, cpu_temp: float,
                fan_rpm: float | None, reason: str) -> StrategyResult:
        return StrategyResult(
            pwm=self.current_pwm, cpu_temp=cpu_temp, cpu_usage=snap.cpu_usage,
            delta_t=snap.delta_t, power=snap.power_watts,
            inlet_temp=snap.inlet_temp, exhaust_temp=snap.exhaust_temp,
            fan_rpm=fan_rpm,
            fan_readings=snap.fan_readings,
            pid_p=0, pid_i=0, pid_d=0, feedforward=0,
            emergency=False, reason=reason,
        )

    def _emergency(self, snap: SensorSnapshot, reason: str) -> StrategyResult:
        return StrategyResult(
            pwm=-1, cpu_temp=snap.cpu_temp_max, cpu_usage=snap.cpu_usage,
            delta_t=snap.delta_t, power=snap.power_watts,
            inlet_temp=snap.inlet_temp, exhaust_temp=snap.exhaust_temp,
            fan_rpm=max(snap.fan_rpms) if snap.fan_rpms else None,
            fan_readings=snap.fan_readings,
            pid_p=0, pid_i=0, pid_d=0, feedforward=0,
            emergency=True, reason=reason,
        )
