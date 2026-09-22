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
