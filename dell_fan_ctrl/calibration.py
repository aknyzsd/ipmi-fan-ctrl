"""风扇标定模块：启动时测多段 PWM→RPM 曲线，用于检测 iDRAC 抢权偏移和风扇健康。

标定流程：设固定 PWM → 等转速稳定 → 读各风扇 RPM → 保存到 JSON。
运行时用标定表对比实际 RPM，偏差超阈值判定 iDRAC 抢权，自动重新夺回手动控制。
"""
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认标定点：覆盖低中高速全范围
DEFAULT_PWM_POINTS = [10, 20, 30, 50, 70, 90, 100]
# 默认每个点等几秒让 RPM 稳定（实测5秒不足，先试10秒）
DEFAULT_SETTLE_SEC = 10
# 偏移检测阈值：实际 RPM 与标定值偏差超 30% 判定 iDRAC 抢权
DEFAULT_DRIFT_THRESHOLD = 0.30
# 风扇健康：偏离所有风扇均值超 30% 判定异常
DEFAULT_HEALTH_THRESHOLD = 0.30


class FanCalibrator:
    """风扇标定器：跑标定、持久化、偏移检测、健康检查。"""

    def __init__(
        self,
        pwm_points: list[int] | None = None,
        settle_sec: float = DEFAULT_SETTLE_SEC,
        drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
        health_threshold: float = DEFAULT_HEALTH_THRESHOLD,
        save_path: str = "fan_calibration.json",
    ):
        self.pwm_points = pwm_points or DEFAULT_PWM_POINTS
        self.settle_sec = settle_sec
        self.drift_threshold = drift_threshold
        self.health_threshold = health_threshold
        self.save_path = Path(save_path)
        # 标定数据：{str(pwm): {fan_name: rpm}}，JSON 键必须是字符串
        self._data: dict[str, dict[str, float]] = {}
        # 风扇健康状态：{fan_name: "ok"|"abnormal"|"fault"}
        self._health: dict[str, str] = {}
        # 启动时尝试加载已有标定
        self.load()

    # ── 标定流程 ──

    def calibrate(self, client) -> dict[str, dict[str, float]]:
        """跑标定：逐个设 PWM、等稳定、读 RPM。标定完返回数据并保存。

        会暂时中断温控（约 len(pwm_points)*settle_sec 秒），调用方需确保此时不跑策略。
        """
        data: dict[str, dict[str, float]] = {}
        logger.info("开始风扇标定：%d 个 PWM 点，每点等 %.1f 秒", len(self.pwm_points), self.settle_sec)

        # 预热：先设中间 PWM 等稳定，确保 iDRAC 已切手动模式（避免首点读到自动模式残留转速）
        try:
            client.set_pwm(30)
            time.sleep(self.settle_sec)
        except Exception:
            pass

        for pwm in self.pwm_points:
            try:
                client.set_pwm(pwm)  # set_pwm 内部已含 disable_auto，确保手动模式
                time.sleep(self.settle_sec)  # 等BMC传感器读数追上（传感器滞后~5-10秒，非机械惯性）
                snap = client.read_sensors()
                fan_readings = snap.fan_readings
                if not fan_readings:
                    logger.warning("PWM=%d%% 时未读到风扇 RPM，跳过该点", pwm)
                    continue
                data[str(pwm)] = dict(fan_readings)
                logger.info("标定 PWM=%d%%: %s", pwm, fan_readings)
            except Exception as e:
                logger.error("标定 PWM=%d%% 失败: %s", pwm, e)

        if not data:
            logger.error("标定失败：未收集到任何数据")
            return {}

        self._data = data
        self._health = self._compute_health(data)
        self.save()
        logger.info("标定完成：%d 个点，风扇健康: %s", len(data), self._health)
        return data

    # ── 持久化 ──

    def save(self) -> None:
        """保存标定数据+健康状态到 JSON。"""
        try:
            payload = {"calibration": self._data, "health": self._health}
            self.save_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info("标定数据已保存到 %s", self.save_path)
        except Exception as e:
            logger.error("保存标定数据失败: %s", e)

    def load(self) -> bool:
        """从 JSON 加载已有标定数据。返回是否加载成功。"""
        try:
            if not self.save_path.exists():
                return False
            payload = json.loads(self.save_path.read_text(encoding="utf-8"))
            self._data = payload.get("calibration", {})
            self._health = payload.get("health", {})
            if self._data:
                logger.info("加载已有标定数据：%d 个点，风扇健康: %s", len(self._data), self._health)
                return True
        except Exception as e:
            logger.error("加载标定数据失败: %s", e)
        return False

    # ── 偏移检测（运行时每周期调）──

    def expected_rpm(self, pwm: int, fan_name: str) -> float | None:
        """线性插值估算某风扇在指定 PWM 下的预期 RPM。

        标定点不一定覆盖所有 PWM 值，用相邻两点线性插值。
        """
        if not self._data:
            return None
        # 取该风扇所有标定点 (pwm, rpm)
        points: list[tuple[int, float]] = []
        for pwm_str, fans in self._data.items():
            if fan_name in fans:
                points.append((int(pwm_str), fans[fan_name]))
        if not points:
            return None
        points.sort()
        # 精确匹配
        for p, r in points:
            if p == pwm:
                return r
        # 外推：PWM 低于最低点或高于最高点，用边界值
        if pwm <= points[0][0]:
            return points[0][1]
        if pwm >= points[-1][0]:
            return points[-1][1]
        # 线性插值：找相邻两点
        for i in range(len(points) - 1):
            p1, r1 = points[i]
            p2, r2 = points[i + 1]
            if p1 <= pwm <= p2:
                ratio = (pwm - p1) / (p2 - p1) if p2 != p1 else 0
                return r1 + (r2 - r1) * ratio
        return None

    def check_drift(self, current_pwm: int, current_fan_readings: dict[str, float]) -> dict[str, float]:
        """对比当前各风扇 RPM 与标定预期值，返回偏移率字典。

        偏移率 = (实际 - 预期) / 预期。|偏移率| > threshold 判定 iDRAC 抢权。
        """
        if not self._data or not current_fan_readings:
            return {}
        drifts: dict[str, float] = {}
        for fan_name, actual_rpm in current_fan_readings.items():
            expected = self.expected_rpm(current_pwm, fan_name)
            if expected is None or expected <= 0:
                continue
            drift = (actual_rpm - expected) / expected
            drifts[fan_name] = drift
        return drifts

    def is_drifted(self, current_pwm: int, current_fan_readings: dict[str, float]) -> bool:
        """检测是否发生 iDRAC 抢权：任一风扇偏移率超阈值即判定。"""
        drifts = self.check_drift(current_pwm, current_fan_readings)
        if not drifts:
            return False
        max_drift = max(abs(d) for d in drifts.values())
        if max_drift > self.drift_threshold:
            logger.warning(
                "检测到风扇偏移(疑似iDRAC抢权): PWM=%d%% max_drift=%.1f%% (阈值%.0f%%) %s",
                current_pwm, max_drift * 100, self.drift_threshold * 100,
                {k: f"{v*100:.0f}%" for k, v in drifts.items()},
            )
            return True
        return False

    # ── 风扇健康检查 ──

    def _compute_health(self, data: dict[str, dict[str, float]]) -> dict[str, str]:
        """根据标定数据计算各风扇健康状态。

        - fault: 任一标定点 RPM=0
        - abnormal: 平均 RPM 偏离所有风扇均值 > threshold
        - ok: 正常
        """
        if not data:
            return {}
        # 收集每个风扇在所有标定点的平均 RPM
        fan_avg: dict[str, float] = {}
        all_fans: set[str] = set()
        for fans in data.values():
            all_fans.update(fans.keys())
        for fan_name in all_fans:
            rpms = [fans[fan_name] for fans in data.values() if fan_name in fans]
            if not rpms:
                continue
            fan_avg[fan_name] = sum(rpms) / len(rpms)

        if not fan_avg:
            return {}

        # 全体风扇平均 RPM
        overall_avg = sum(fan_avg.values()) / len(fan_avg)
        health: dict[str, str] = {}
        for fan_name, avg_rpm in fan_avg.items():
            if avg_rpm <= 0:
                health[fan_name] = "fault"
            elif overall_avg > 0 and abs(avg_rpm - overall_avg) / overall_avg > self.health_threshold:
                health[fan_name] = "abnormal"
            else:
                health[fan_name] = "ok"
        return health

    # ── 访问器 ──

    @property
    def data(self) -> dict[str, dict[str, float]]:
        return self._data

    @property
    def health(self) -> dict[str, str]:
        return self._health

    @property
    def has_data(self) -> bool:
        return bool(self._data)
