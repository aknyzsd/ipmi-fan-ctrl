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
            pid_p=self.pid.last_p, pid_i=self.pid.last_i, pid_d=self.pid.last_d,
            feedforward=feedforward, emergency=False, reason="",
        )

    def _emergency(self, snap: SensorSnapshot, reason: str) -> StrategyResult:
        return StrategyResult(
            pwm=-1, cpu_temp=snap.cpu_temp_max, cpu_usage=snap.cpu_usage,
            delta_t=snap.delta_t, power=snap.power_watts,
            inlet_temp=snap.inlet_temp, exhaust_temp=snap.exhaust_temp,
            fan_rpm=max(snap.fan_rpms) if snap.fan_rpms else None,
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
        self.state = "descending"  # descending / monitoring / fallback_pid

        self._temp_history: deque[float] = deque(maxlen=10)
        self._exhaust_history: deque[float] = deque(maxlen=10)
        self._power_history: deque[float] = deque(maxlen=10)
        self._stable_count = 0
        self._callback_count = 0
        self._observe_count = 0
        self._prev_cpu_usage: float | None = None
        self._prev_power: float | None = None
        self._pid_stable_start: float | None = None
        self._last_temp: float | None = None

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
        elapsed_min = len(vals) * dt / 60.0
        return (vals[-1] - vals[0]) / elapsed_min if elapsed_min > 0 else 0.0

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

        # ── PID 回退模式：委托给 ThermalStrategy ──
        if self.state == "fallback_pid":
            result = self.pid_strategy.compute(snap, dt)
            # 温度稳定足够久 → 回动态递减
            if self._last_temp is not None and abs(cpu_temp - self._last_temp) < 1.5:
                if self._pid_stable_start is None:
                    self._pid_stable_start = time.monotonic()
                elif time.monotonic() - self._pid_stable_start > 60:
                    self.state = "descending"
                    self._callback_count = 0
                    self._pid_stable_start = None
                    self.current_pwm = self.safe_pwm
                    result.reason = "PID 下温度稳定60s，重新进入动态递减"
            else:
                self._pid_stable_start = None
            self._last_temp = cpu_temp
            return result

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
                                f"负载突增({surge_desc})，暂停观察")

        if self._observe_count > 0:
            self._observe_count -= 1
            return self._result(snap, cpu_temp, fan_rpm,
                                f"观察期剩余{self._observe_count}周期")

        # ── 回调条件 ──
        callback_reason = None
        if delta_t is not None and delta_t > 15.0:
            callback_reason = f"温差{delta_t:.1f}℃>15℃"
        else:
            cpu_rate = self._temp_rise_rate(dt)
            exhaust_rate = self._rise_rate(self._exhaust_history, dt)
            power_rate = self._rise_rate(self._power_history, dt)
            if cpu_rate > 2.0:
                callback_reason = f"CPU温度上升{cpu_rate:.1f}℃/min>2℃/min"
            elif exhaust_rate > 2.0:
                callback_reason = f"排风温度上升{exhaust_rate:.1f}℃/min>2℃/min(含显卡/硬盘热源)"
            elif power_rate > 100.0:
                callback_reason = f"功耗上升{power_rate:.0f}W/min>100W/min(整机热源)"

        if callback_reason:
            self.state = "fallback_pid"
            self._pid_stable_start = None
            return self._result(snap, cpu_temp, fan_rpm,
                                f"回调→切PID: {callback_reason}")

        # ── 安全：递减或从监控回到递减 ──
        if self.state == "monitoring":
            self.state = "descending"
            self._callback_count = 0

        if self.state == "descending":
            self._stable_count += 1
            if self._stable_count >= 3:
                self._stable_count = 0
                cpu_rate = self._temp_rise_rate(dt)
                near_target = cpu_temp > self.pid_strategy.target - 5
                if cpu_rate > 0.5 and near_target and self.current_pwm < self.pwm_max:
                    self.current_pwm += 1
                    return self._result(snap, cpu_temp, fan_rpm,
                                        f"温度缓慢上升{cpu_rate:.1f}℃/min→递增至{self.current_pwm}%")
                if self.current_pwm > self.pwm_min:
                    self.current_pwm -= 1

        return self._result(snap, cpu_temp, fan_rpm, "")

    def _result(self, snap: SensorSnapshot, cpu_temp: float,
                fan_rpm: float | None, reason: str) -> StrategyResult:
        return StrategyResult(
            pwm=self.current_pwm, cpu_temp=cpu_temp, cpu_usage=snap.cpu_usage,
            delta_t=snap.delta_t, power=snap.power_watts,
            inlet_temp=snap.inlet_temp, exhaust_temp=snap.exhaust_temp,
            fan_rpm=fan_rpm,
            pid_p=0, pid_i=0, pid_d=0, feedforward=0,
            emergency=False, reason=reason,
        )

    def _emergency(self, snap: SensorSnapshot, reason: str) -> StrategyResult:
        return StrategyResult(
            pwm=-1, cpu_temp=snap.cpu_temp_max, cpu_usage=snap.cpu_usage,
            delta_t=snap.delta_t, power=snap.power_watts,
            inlet_temp=snap.inlet_temp, exhaust_temp=snap.exhaust_temp,
            fan_rpm=max(snap.fan_rpms) if snap.fan_rpms else None,
            pid_p=0, pid_i=0, pid_d=0, feedforward=0,
            emergency=True, reason=reason,
        )
