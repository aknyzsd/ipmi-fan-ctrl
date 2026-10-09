# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 aknyzsd

"""硬盘温度监控：采集所有盘温度，按旋转/非旋转自动分组，纯监控不影响控制。

NVMe 走 hwmon（/sys/block/nvmeN/device/hwmon*/temp1_input，毫秒级），
SATA 走 smartctl -A（每盘 1~2 秒，盘温变化慢可接受）。
旋转判断走 /sys/block/<dev>/queue/rotational（1=机械 0=固态），零依赖。
采集频率由调用方控制（盘温变化慢，20 秒一轮足够）。
"""
import glob
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DiskTemp:
    device: str              # /dev/sda, /dev/nvme0
    name: str                # sda, nvme0
    model: str               # 型号，读不到为空
    temp: float | None       # 温度 ℃，读不到为 None
    kind: str                # "hdd"(机械) / "ssd"(固态SATA) / "nvme"


def _is_rotational(name: str) -> bool:
    """读 /sys/block/<name>/queue/rotational 判断是否旋转盘。"""
    try:
        with open(f"/sys/block/{name}/queue/rotational") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def _read_nvme_hwmon(name: str) -> float | None:
    """读 NVMe hwmon 温度（毫秒级）。name 如 nvme0。"""
    for p in glob.glob(f"/sys/class/nvme/{name}/device/hwmon*/temp1_input"):
        try:
            with open(p) as f:
                return int(f.read().strip()) / 1000.0
        except OSError:
            continue
    return None


def _read_smart_temp(device: str) -> float | None:
    """用 smartctl -A 读盘温度（SATA/NVMe 通用，较慢）。"""
    if shutil.which("smartctl") is None:
        return None
    try:
        r = subprocess.run(
            ["smartctl", "-A", device],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    for line in r.stdout.splitlines():
        low = line.lower()
        # SATA: "Current Drive Temperature:     40 C"
        # NVMe: "Temperature:                        48 Celsius"
        if "current drive temperature" in low or low.startswith("temperature"):
            parts = line.split(":")
            if len(parts) >= 2:
                tokens = parts[1].strip().split()
                if tokens:
                    try:
                        return float(tokens[0])
                    except ValueError:
                        continue
    return None


def _list_block_devs() -> list[str]:
    """列出 sd*（/sys/block）和 nvme* 控制器（/sys/class/nvme，/sys/block 只有命名空间）。"""
    names = []
    for name in sorted(os.listdir("/sys/block")):
        if name.startswith("sd"):
            names.append(name)
    # NVMe 控制器在 /sys/class/nvme，/sys/block 下只有命名空间 nvme0n1
    if os.path.isdir("/sys/class/nvme"):
        for name in sorted(os.listdir("/sys/class/nvme")):
            if name.startswith("nvme"):
                names.append(name)
    return names


def _read_model(name: str) -> str:
    """读盘型号：SATA 走 /sys/block/<name>/device/model，NVMe 走 /sys/class/nvme/<name>/model。"""
    paths = [f"/sys/block/{name}/device/model"]
    if name.startswith("nvme"):
        paths.insert(0, f"/sys/class/nvme/{name}/model")
    for p in paths:
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            continue
    return ""


def collect() -> list[DiskTemp]:
    """采集所有盘温度，返回 DiskTemp 列表。单盘失败温度为 None，不抛异常。"""
    results = []
    for name in _list_block_devs():
        device = f"/dev/{name}"
        model = _read_model(name)
        if name.startswith("nvme"):
            # NVMe 优先 hwmon（快），没有回退 smartctl
            temp = _read_nvme_hwmon(name)
            if temp is None:
                temp = _read_smart_temp(device)
            kind = "nvme"
        else:
            kind = "hdd" if _is_rotational(name) else "ssd"
            temp = _read_smart_temp(device)
        results.append(DiskTemp(device=device, name=name, model=model, temp=temp, kind=kind))
    return results


def grouped(disks: list[DiskTemp]) -> dict:
    """按 kind 分组，返回 {hdd:[...], ssd:[...], nvme:[...]}，每项 dict 便于 JSON 序列化。"""
    out: dict[str, list] = {"hdd": [], "ssd": [], "nvme": []}
    for d in disks:
        out.setdefault(d.kind, []).append({
            "name": d.name, "model": d.model, "temp": d.temp,
        })
    return out
