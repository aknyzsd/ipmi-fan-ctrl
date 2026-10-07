"""IPMI 客户端：支持网络(pyghmi)和本机(ipmitool -I open)两种模式。

网络模式：pyghmi 走 RMCP+ 连 BMC，需 ip/user/password，远程或无 /dev/ipmi0 时用。
本机模式：ipmitool -I open 走 /dev/ipmi0，不走网络、不要凭据，部署在被控机本机时用。
设风扇 PWM / 开关自动控制统一用 Dell OEM 0x30 0x30 raw 命令。
"""
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pyghmi.ipmi import command as _pyghmi_command

logger = logging.getLogger(__name__)

_IPMITOOL_OPEN = ["ipmitool", "-I", "open"]


class IpmiError(Exception):
    pass


@dataclass
class SensorSnapshot:
    inlet_temp: float | None = None
    exhaust_temp: float | None = None
    cpu_temps: list[float] = field(default_factory=list)
    fan_rpms: list[float] = field(default_factory=list)
    fan_readings: dict[str, float] = field(default_factory=dict)  # 风扇名→RPM，前端逐个展示
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


def _run_open(args: list[str], timeout: int = 20) -> str:
    """本机模式：跑 ipmitool -I open <args>，返回 stdout。失败抛 IpmiError。"""
    if shutil.which("ipmitool") is None:
        raise IpmiError("本机模式需要 ipmitool，系统未安装")
    try:
        r = subprocess.run(
            _IPMITOOL_OPEN + args,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise IpmiError(f"ipmitool 超时: {e}") from e
    if r.returncode != 0:
        raise IpmiError(f"ipmitool 失败(rc={r.returncode}): {r.stderr.strip()}")
    return r.stdout


def _parse_sensor_text(text: str) -> SensorSnapshot:
    """解析 ipmitool sensor 输出（竖线分隔）为 SensorSnapshot。

    行格式：`Inlet Temp | 24.000 | degrees C | ok | ...`，按名称+单位映射到快照字段。
    """
    snap = SensorSnapshot()
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        name, val_s, unit = parts[0], parts[1], parts[2]
        # 离散值/无读数跳过（na、0x1 等）
        if val_s in ("na", "ns", "") or val_s.startswith("0x"):
            continue
        try:
            val = float(val_s)
        except ValueError:
            continue
        if name == "Inlet Temp" and unit == "degrees C":
            snap.inlet_temp = val
        elif name == "Exhaust Temp" and unit == "degrees C":
            snap.exhaust_temp = val
        elif name == "Temp" and unit == "degrees C":
            snap.cpu_temps.append(val)
        elif name.endswith("RPM") and unit == "RPM":
            snap.fan_rpms.append(val)
            snap.fan_readings[name.removesuffix(" RPM").strip()] = val
        elif name == "CPU Usage" and unit == "percent":
            snap.cpu_usage = val
        elif name == "SYS Usage" and unit == "percent":
            snap.sys_usage = val
        elif name == "MEM Usage" and unit == "percent":
            snap.mem_usage = val
        elif name == "IO Usage" and unit == "percent":
            snap.io_usage = val
        elif name == "Pwr Consumption" and unit == "Watts":
            snap.power_watts = val
    return snap


class IpmiClient:
    def __init__(self, ip: str, user: str, password: str, mode: str = "network", timeout: int = 15):
        self._mode = mode
        self._ip = ip
        self._user = user
        self._password = password
        self._timeout = timeout
        self._ipmi = None
        if mode == "local":
            # 本机模式走 /dev/ipmi0，不需要 pyghmi 连接
            if shutil.which("ipmitool") is None:
                raise IpmiError("本机模式需要 ipmitool，系统未安装")
        else:
            # 网络模式：pyghmi 连 BMC
            try:
                self._ipmi = _pyghmi_command.Command(bmc=ip, userid=user, password=password)
            except Exception as e:
                raise IpmiError(f"pyghmi 连接失败: {e}") from e

    def read_sensors(self) -> SensorSnapshot:
        if self._mode == "local":
            text = _run_open(["sensor"], timeout=20)
            return _parse_sensor_text(text)
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
        fan_readings = {
            (getattr(r, "name", "") or "Fan"): r.value
            for r in readings
            if "Fan" in (getattr(r, "name", "") or "")
            and getattr(r, "type", None) == "Fan"
            and r.value is not None
        }

        return SensorSnapshot(
            inlet_temp=find("Inlet Temp"),
            exhaust_temp=find("Exhaust Temp"),
            cpu_temps=cpu_temps,
            fan_rpms=fan_rpms,
            fan_readings=fan_readings,
            cpu_usage=find("CPU Usage"),
            sys_usage=find("SYS Usage"),
            mem_usage=find("MEM Usage"),
            io_usage=find("IO Usage"),
            power_watts=find("Pwr Consumption"),
        )

    def _raw(self, netfn: int, command: int, data: list[int]) -> None:
        """发 IPMI raw 命令。本机走 ipmitool raw，网络走 pyghmi raw。"""
        if self._mode == "local":
            hex_args = [f"0x{netfn:02x}", f"0x{command:02x}"] + [f"0x{b:02x}" for b in data]
            _run_open(["raw"] + hex_args, timeout=10)
            return
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
        # 先确保手动模式：iDRAC 会周期性抢回自动控制，每周期必须重新夺权
        self._raw(0x30, 0x30, [0x01, 0x00])
        # Dell OEM：0x30 0x30 0x02 0xff 0x<pwm>
        self._raw(0x30, 0x30, [0x02, 0xff, pwm])

    def enable_auto(self) -> None:
        """交还 iDRAC 自动风扇控制。"""
        self._raw(0x30, 0x30, [0x01, 0x01])

    def disable_auto(self) -> None:
        """关闭 iDRAC 自动控制，拿回手动权。"""
        self._raw(0x30, 0x30, [0x01, 0x00])
