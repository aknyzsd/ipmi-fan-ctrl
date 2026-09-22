"""探针客户端：程序主动去拉服务器上温度/功耗探针的数据。

零依赖（标准库 urllib），探针端只需实现一个 HTTP 接口即可对接。

探针协议（探针端需实现）：
  GET /sensors
  Response 200 (JSON):
  {
    "source": "server-room-probe",       // 探针标识
    "temps": {"ambient": 28.5, "rack_inlet": 30.0, "gpu": 65.0},
    "power": {"total": 350, "gpu": 120},
    "loads": {"gpu": 80, "disk_io": 45},
    "timestamp": "2026-09-23T12:00:00Z"
  }

  字段说明：
    temps  — 额外温度传感器（key=名称, value=℃）
    power  — 额外功耗（key=名称, value=W）
    loads  — 额外负载（key=名称, value=%）
    所有字段均可选，有什么给什么

程序每个采样周期调 fetch_all()，拉到的数据合并到 SensorSnapshot.extra 供策略使用。
"""
import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ProbeData:
    """一个探针返回的数据。"""
    source: str = ""
    temps: dict[str, float] = field(default_factory=dict)
    power: dict[str, float] = field(default_factory=dict)
    loads: dict[str, float] = field(default_factory=dict)

    @property
    def ambient_temp(self) -> float | None:
        """环境温度（优先 ambient > room > rack_inlet）。"""
        for key in ("ambient", "room", "rack_inlet", "env"):
            if key in self.temps:
                return self.temps[key]
        return None


class ProbeClient:
    """探针客户端：定期拉取一个或多个探针的传感器数据。

    用法：
        client = ProbeClient(["http://192.168.1.100:9000", "http://probe2:9000"])
        data = client.fetch_all()  # -> list[ProbeData]
    """

    def __init__(self, urls: list[str], timeout: float = 5.0):
        self.urls = [u.rstrip("/") for u in urls if u.strip()]
        self.timeout = timeout

    def fetch_all(self) -> list[ProbeData]:
        """拉取所有探针数据，失败的探针跳过不影响其他。"""
        results = []
        for url in self.urls:
            try:
                data = self._fetch_one(url)
                results.append(data)
                logger.debug("探针 %s OK: %s", url, data.source)
            except Exception as e:
                logger.warning("探针 %s 拉取失败: %s", url, e)
        return results

    def _fetch_one(self, url: str) -> ProbeData:
        req = urllib.request.Request(
            url + "/sensors", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = json.loads(resp.read())

        return ProbeData(
            source=raw.get("source", url),
            temps=raw.get("temps", {}),
            power=raw.get("power", {}),
            loads=raw.get("loads", {}),
        )

    def fetch_merged(self) -> dict:
        """拉取所有探针并合并成一个字典，供 SensorSnapshot.extra 使用。

        返回: {"temps": {...合并...}, "power": {...}, "loads": {...},
               "ambient_temp": float|None, "sources": [str...]}
        """
        merged = {"temps": {}, "power": {}, "loads": {}, "sources": []}
        ambient = None
        for data in self.fetch_all():
            merged["temps"].update(data.temps)
            merged["power"].update(data.power)
            merged["loads"].update(data.loads)
            merged["sources"].append(data.source)
            if ambient is None and data.ambient_temp is not None:
                ambient = data.ambient_temp
        merged["ambient_temp"] = ambient
        return merged
