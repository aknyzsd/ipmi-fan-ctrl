"""轻量 HTTP API（服务端）——暂未启用，后续可能做 WebUI 时复用。

当前方向已改为探针客户端（见 probe.py）：程序主动去拉服务器上探针的数据。
本文件保留 ApiServer/SharedState 实现，后续做 WebUI 管理面板时可重新启用。

端点（预留）：
  GET  /api/status     — 当前温控状态
  GET  /api/sensors    — 最新传感器详细数据
  GET  /api/config     — 当前配置参数
  POST /api/probe      — 接收外部温度探针数据
  POST /api/control    — 启停控制
"""
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

logger = logging.getLogger(__name__)


class SharedState:
    """API 和 ControllerWorker 之间的共享状态（线程安全靠 GIL 原子赋值）。"""
    def __init__(self):
        self.latest_result = None
        self.config = None
        self.running = False
        self.quiet_mode = False
        self.external_probes: dict[str, dict] = {}


class ApiHandler(BaseHTTPRequestHandler):
    state: SharedState

    def _json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length > 0 else {}

    def do_GET(self):
        if self.path == "/api/status":
            self._handle_status()
        elif self.path == "/api/sensors":
            self._handle_sensors()
        elif self.path == "/api/config":
            self._handle_config()
        elif self.path == "/":
            self._json(200, {"service": "dell-fan-ctrl", "endpoints": [
                "GET /api/status", "GET /api/sensors", "GET /api/config",
                "POST /api/probe", "POST /api/control"]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/probe":
            self._handle_probe()
        elif self.path == "/api/control":
            self._handle_control()
        else:
            self._json(404, {"error": "not found"})

    def _handle_status(self):
        r = self.state.latest_result
        base = {"running": self.state.running, "quiet_mode": self.state.quiet_mode,
                "external_probes": self.state.external_probes}
        if r is None:
            base["message"] = "no data yet"
            self._json(200, base)
            return
        base.update({
            "cpu_temp": r.cpu_temp, "inlet_temp": r.inlet_temp,
            "exhaust_temp": r.exhaust_temp, "delta_t": r.delta_t,
            "cpu_usage": r.cpu_usage, "power": r.power,
            "pwm": r.pwm, "fan_rpm": r.fan_rpm,
            "emergency": r.emergency, "reason": r.reason,
        })
        self._json(200, base)

    def _handle_sensors(self):
        r = self.state.latest_result
        if r is None:
            self._json(200, {"message": "no data yet"})
            return
        self._json(200, {
            "cpu_temp": r.cpu_temp, "inlet_temp": r.inlet_temp,
            "exhaust_temp": r.exhaust_temp, "delta_t": r.delta_t,
            "cpu_usage": r.cpu_usage, "power": r.power,
            "pwm": r.pwm, "fan_rpm": r.fan_rpm,
            "pid": {"p": r.pid_p, "i": r.pid_i, "d": r.pid_d},
            "feedforward": r.feedforward,
        })

    def _handle_config(self):
        cfg = self.state.config
        if cfg is None:
            self._json(200, {"message": "no config"})
            return
        self._json(200, {
            "ip": cfg.ip, "user": cfg.user,
            "target_cpu_temp": cfg.target_cpu_temp,
            "emergency_temp": cfg.emergency_temp,
            "interval": cfg.interval,
            "kp": cfg.kp, "ki": cfg.ki, "kd": cfg.kd,
            "pwm_min": cfg.pwm_min, "pwm_max": cfg.pwm_max,
            "load_kf": cfg.load_kf, "delta_t_k": cfg.delta_t_k,
        })

    def _handle_probe(self):
        """接收外部温度探针数据，存入 shared_state 供策略参考。"""
        try:
            data = self._read_body()
            source = data.get("source", "unknown")
            sensors = data.get("sensors", {})
            self.state.external_probes[source] = sensors
            logger.info("收到外部探针数据: source=%s sensors=%s", source, sensors)
            self._json(200, {"status": "ok", "source": source})
        except (json.JSONDecodeError, Exception) as e:
            self._json(400, {"error": str(e)})

    def _handle_control(self):
        """启停控制。Body: {"action": "start"|"stop", "quiet": bool}"""
        try:
            data = self._read_body()
            action = data.get("action")
            self._json(200, {"status": "ok", "action": action,
                             "note": "control via API not yet wired to worker"})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        logger.debug("API %s - %s", self.address_string(), fmt % args)


class ApiServer:
    def __init__(self, port: int = 8080, state: SharedState | None = None):
        self.port = port
        self.state = state or SharedState()
        self._server = None
        self._thread = None

    def start(self) -> None:
        ApiHandler.state = self.state
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), ApiHandler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("API 服务已启动: http://0.0.0.0:%d", self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            logger.info("API 服务已停止")
