"""主控循环：采样 → 策略 → 写 PWM → 日志。

启动时关闭 iDRAC 自动控制拿手动权；异常/退出时交还。
采样失败保持上一次 PWM（不盲动）；紧急温度回退自动控制。
"""
import time
import logging
from .config import Config, PWM_HARM_FLOOR
from .ipmi import IpmiClient, IpmiError
from .pid import PIDController
from .thermal import ThermalStrategy, QuietStrategy, StrategyResult
from .probe import ProbeClient
from .calibration import FanCalibrator

logger = logging.getLogger(__name__)


def _fmt(v: float | None) -> str:
    return f"{v:.1f}" if v is not None else "N/A"


class Controller:
    def __init__(self, cfg: Config, quiet_mode: bool = False):
        self.cfg = cfg
        self.quiet_mode = quiet_mode
        self.client = IpmiClient(cfg.ip, cfg.user, cfg.password, mode=cfg.ipmi_mode)
        # PID 输出范围 [0, pwm_max-pwm_min]：作为叠加到 pwm_min 上的偏置
        pid = PIDController(cfg.kp, cfg.ki, cfg.kd, 0.0, float(cfg.pwm_max - cfg.pwm_min))
        self.strategy = ThermalStrategy(
            cfg.target_cpu_temp, cfg.emergency_temp, cfg.inlet_safe_max,
            cfg.pwm_min, cfg.pwm_max, pid, cfg.load_kf, cfg.delta_t_k,
        )
        self.quiet_strategy = QuietStrategy(
            PWM_HARM_FLOOR, cfg.pwm_max, self.strategy, "fan_safe_pwm.json",
            initial_pwm=cfg.pwm_min)
        self.probe_client = ProbeClient(cfg.probe_urls) if cfg.probe_urls else None
        self._last_time: float | None = None
        # 风扇标定器：启动时标定 PWM→RPM 曲线，运行时检测 iDRAC 抢权偏移
        self.calibrator = FanCalibrator(save_path="fan_calibration.json")
        self._last_pwm: int | None = None  # 上周期设的 PWM，用于偏移检测
        self._calibrating = False  # 标定进行中标志，主循环检测到时暂停策略

    def step(self) -> StrategyResult:
        now = time.monotonic()
        dt = now - self._last_time if self._last_time else self.cfg.interval
        self._last_time = now

        snap = self.client.read_sensors()
        if self.probe_client is not None:
            snap.extra = self.probe_client.fetch_merged()

        # 偏移检测：用上周期设的 PWM 对比当前 RPM，检测 iDRAC 是否抢权
        if self._last_pwm is not None and self.calibrator.has_data and snap.fan_readings:
            self.calibrator.is_drifted(self._last_pwm, snap.fan_readings)

        if self.quiet_mode:
            result = self.quiet_strategy.compute(snap, dt)
        else:
            result = self.strategy.compute(snap, dt)

        if result.emergency:
            logger.warning("紧急回退 → 交还 iDRAC 自动控制：%s", result.reason)
            try:
                self.client.enable_auto()
            except IpmiError as e:
                logger.error("交还自动控制也失败: %s", e)
            return result

        self.client.set_pwm(result.pwm)
        self._last_pwm = result.pwm  # 记录本周期 PWM 供下周期偏移检测
        logger.info(
            "CPU=%.1f℃ usage=%s%% ΔT=%s℃ P=%sW → PWM=%d%% "
            "(P=%.1f I=%.1f D=%.1f FF=%.1f)%s",
            result.cpu_temp, _fmt(result.cpu_usage), _fmt(result.delta_t),
            _fmt(result.power), result.pwm,
            result.pid_p, result.pid_i, result.pid_d, result.feedforward,
            f" [{result.reason}]" if result.reason else "",
        )
        return result

    def run(self) -> None:
        logger.info(
            "启动温控：目标 CPU=%.0f℃，采样间隔=%.1fs，PWM 钳位 [%d%%, %d%%]",
            self.cfg.target_cpu_temp, self.cfg.interval, self.cfg.pwm_min, self.cfg.pwm_max,
        )
        try:
            self.client.disable_auto()
            logger.info("已关闭 iDRAC 自动风扇控制，接管手动")
        except IpmiError as e:
            logger.error("无法关闭自动控制，退出: %s", e)
            return

        # 启动时自动跑一遍风扇标定（约35秒），标定完恢复 pwm_min 再进正常循环
        if not self.calibrator.has_data:
            logger.info("首次启动，开始风扇标定…")
            self.calibrator.calibrate(self.client)
            try:
                self.client.set_pwm(self.cfg.pwm_min)  # 标定完恢复安全值
            except IpmiError:
                pass

        while True:
            t0 = time.monotonic()
            try:
                if self._calibrating:
                    # 手动标定进行中，主循环暂停策略只等待
                    time.sleep(1.0)
                    continue
                self.step()
            except IpmiError as e:
                # 采样/写失败：保持上一次 PWM，不盲动，等下个周期重试
                logger.error("本周期失败（保持上次 PWM）: %s", e)
            time.sleep(max(0.5, self.cfg.interval - (time.monotonic() - t0)))

    def run_calibration(self) -> dict[str, dict[str, float]]:
        """手动触发风扇标定（供 WebUI 调用）。主循环会暂停策略，标定完恢复。"""
        self._calibrating = True
        try:
            logger.info("手动触发风扇标定…")
            data = self.calibrator.calibrate(self.client)
            try:
                self.client.set_pwm(self.cfg.pwm_min)  # 标定完恢复安全值
                self._last_pwm = self.cfg.pwm_min
            except IpmiError:
                pass
            return data
        finally:
            self._calibrating = False

    def shutdown(self) -> None:
        # 正常退出：设回手动 pwm_min（安静），不交还自动控制（用户偏好安静）
        # 紧急回退（温度超阈值）仍走 enable_auto，那是散热优先于安静
        logger.info("退出 → 设回手动 PWM=%d%%（安静模式）", self.cfg.pwm_min)
        try:
            self.client.disable_auto()
            self.client.set_pwm(self.cfg.pwm_min)
        except IpmiError as e:
            logger.error("退出设回手动失败: %s（风扇保持上次状态）", e)
