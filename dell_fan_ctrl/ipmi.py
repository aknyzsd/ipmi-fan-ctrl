"""IPMI 客户端：纯 Python（pyghmi），读传感器 + 设风扇 PWM，零二进制依赖。

读传感器用 pyghmi get_sensor_data()（4.4s 读全部 145 个）。
设风扇 PWM / 开关自动控制用 pyghmi raw_command() 发 Dell OEM 0x30 0x30 命令。
不依赖 ipmitool.exe，多平台兼容，无 Cygwin DLL 问题。
"""
import logging
from dataclasses import dataclass, field
from pyghmi.ipmi import command as _pyghmi_command

logger = logging.getLogger(__name__)


class IpmiError(Exception):
    pass


@dataclass
class SensorSnapshot:
    inlet_temp: float | None = None
    exhaust_temp: float | None = None
    cpu_temps: list[float] = field(default_factory=list)
    fan_rpms: list[float] = field(default_factory=list)
    cpu_usage: float | None = None
    sys_usage: float | None = None
    mem_usage: float | None = None
    io_usage: float | None = None
    power_watts: float | None = None
    extra: dict = field(default_factory=dict)  # 外部探针数据

    @property
    def cpu_temp_max(self) -> float | None:
        """取所有 CPU 温度里的最高值，作为主控变量。"""
        return max(self.cpu_temps) if self.cpu_temps else None

    @property
    def delta_t(self) -> float | None:
        """进排风温差：整机实际热负荷的直接测量，比功耗准。"""
        if self.inlet_temp is not None and self.exhaust_temp is not None:
            return self.exhaust_temp - self.inlet_temp
        return None


class IpmiClient:
    def __init__(self, ip: str, user: str, password: str, timeout: int = 15):
        self._ip = ip
        self._user = user
        self._password = password
        self._timeout = timeout
        try:
            self._ipmi = _pyghmi_command.Command(bmc=ip, userid=user, password=password)
        except Exception as e:
            raise IpmiError(f"pyghmi 连接失败: {e}") from e

    def read_sensors(self) -> SensorSnapshot:
        try:
            raw = list(self._ipmi.get_sensor_data())
        except Exception as e:
            raise IpmiError(f"pyghmi 读传感器失败: {e}") from e
        return self._build_snapshot(raw)

    @staticmethod
    def _build_snapshot(readings) -> SensorSnapshot:
        def find(name: str) -> float | None:
            for r in readings:
                if getattr(r, "name", None) == name and r.value is not None:
                    return r.value
            return None

        cpu_temps = [r.value for r in readings
                     if getattr(r, "name", None) == "Temp"
                     and getattr(r, "type", None) == "Temperature"
                     and r.value is not None]
        fan_rpms = [r.value for r in readings
                    if "Fan" in (getattr(r, "name", "") or "")
                    and getattr(r, "type", None) == "Fan"
                    and r.value is not None]

        return SensorSnapshot(
            inlet_temp=find("Inlet Temp"),
            exhaust_temp=find("Exhaust Temp"),
            cpu_temps=cpu_temps,
            fan_rpms=fan_rpms,
            cpu_usage=find("CPU Usage"),
            sys_usage=find("SYS Usage"),
            mem_usage=find("MEM Usage"),
            io_usage=find("IO Usage"),
            power_watts=find("Pwr Consumption"),
        )

    def _raw(self, netfn: int, command: int, data: list[int]) -> None:
        """发 IPMI raw 命令，检查返回码。"""
        try:
            r = self._ipmi.raw_command(netfn=netfn, command=command, data=data)
        except Exception as e:
            raise IpmiError(f"pyghmi raw 失败: {e}") from e
        code = r.get("code", 0) if isinstance(r, dict) else 0
        if code != 0:
            raise IpmiError(f"raw 命令失败: code={code}")

    def set_pwm(self, pwm: int) -> None:
        if not 0 <= pwm <= 100:
            raise IpmiError(f"PWM 越界: {pwm}")
        # Dell OEM：0x30 0x30 0x02 0xff 0x<pwm>
        self._raw(0x30, 0x30, [0x02, 0xff, pwm])

    def enable_auto(self) -> None:
        """交还 iDRAC 自动风扇控制。"""
        self._raw(0x30, 0x30, [0x01, 0x01])

    def disable_auto(self) -> None:
        """关闭 iDRAC 自动控制，拿回手动权。"""
        self._raw(0x30, 0x30, [0x01, 0x00])
