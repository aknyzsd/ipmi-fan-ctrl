# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 aknyzsd

"""PID 控制器，带积分抗饱和与输出钳位。

用于 CPU 温度反馈控制：error = 当前温度 - 目标温度（过热为正），
输出为相对 PWM 偏置（叠加到 pwm_min 基准上）。
"""


class PIDController:
    def __init__(self, kp: float, ki: float, kd: float, out_min: float, out_max: float):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.out_min = out_min
        self.out_max = out_max
        self._integral = 0.0
        self._prev_error = 0.0
        # 上次各分量，供日志/调试
        self.last_p = 0.0
        self.last_i = 0.0
        self.last_d = 0.0

    def update(self, error: float, dt: float) -> float:
        p = self.kp * error
        # 积分累加
        self._integral += error * dt
        i = self.ki * self._integral
        # 微分；dt=0 时跳过避免除零
        d = self.kd * (error - self._prev_error) / dt if dt > 0 else 0.0
        self._prev_error = error

        out = p + i + d
        out_clamped = max(self.out_min, min(self.out_max, out))

        # 抗积分饱和：输出已饱和且积分仍往饱和方向推 → 撤销本次积分，防止 windup
        if out != out_clamped and (out - out_clamped) * i > 0:
            self._integral -= error * dt
            i = self.ki * self._integral

        self.last_p, self.last_i, self.last_d = p, i, d
        return out_clamped

    def reset(self) -> None:
        self._integral = 0.0
        self._prev_error = 0.0

    def preset_output(self, target_out: float, error: float) -> None:
        """bumpless transfer：预设积分项使下次 update 输出 ≈ target_out。

        切 PID 时调用，避免从动态模式的低 PWM 跳到 PID 高输出造成风扇骤响。
        out = Kp*error + Ki*integral（d=0 因 prev_error 同步设为 error）
        → integral = (target_out - Kp*error) / Ki
        """
        if self.ki != 0:
            self._integral = (target_out - self.kp * error) / self.ki
        else:
            self._integral = 0.0
        # 钳位积分项，使 Ki*integral 落在输出范围内，防 windup
        if self.ki != 0:
            i_min = self.out_min / self.ki
            i_max = self.out_max / self.ki
            self._integral = max(i_min, min(i_max, self._integral))
        self._prev_error = error
