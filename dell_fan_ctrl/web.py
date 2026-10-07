"""WebUI：浏览器界面 + REST API + SSE 实时推送，零依赖。

用法：python -m dell_fan_ctrl.web [config.ini] [port]
然后浏览器打开 http://localhost:8080

后端：标准库 http.server，前端：单 HTML + 原生 JS + SSE。
温控循环在后台线程跑，SSE 推送实时数据到浏览器。
"""
import sys
import os
import json
import time
import queue
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

from .config import load as load_config, save as save_config, ConfigError, Config, PWM_HARM_FLOOR
from .controller import Controller
from .ipmi import IpmiError
from .thermal import StrategyResult

logger = logging.getLogger(__name__)

# React 构建产物目录：dell_fan_ctrl/../webui/dist
_DIST_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "webui", "dist"))


# ─── 共享状态 ───────────────────────────────────────────────
class WebState:
    def __init__(self):
        self.latest_result: StrategyResult | None = None
        self.config: Config | None = None
        self.running = False
        self.quiet_mode = False
        self.logs: list[str] = []
        self.controller_thread: ControllerThread | None = None
        self.config_path: str = ""
        self.disk_temps: dict = {}
        self.disk_thread = None
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def push(self, event: str, data: dict):
        msg = {"event": event, "data": data, "ts": datetime.now().strftime("%H:%M:%S")}
        with self._lock:
            for q in self._subscribers:
                q.put(msg)

    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.logs.append(line)
        if len(self.logs) > 200:
            self.logs.pop(0)
        self.push("log", {"message": line})


# ─── 硬盘温度监控线程 ───────────────────────────────────────
class DiskMonitorThread(threading.Thread):
    def __init__(self, state: "WebState", interval: int = 20):
        super().__init__(daemon=True)
        self.state = state
        self.interval = interval
        self._stop = threading.Event()

    def run(self):
        from .diskmon import collect, grouped
        while not self._stop.is_set():
            try:
                g = grouped(collect())
                self.state.disk_temps = g
                self.state.push("disks", {"disks": g})
            except Exception as e:
                logger.warning(f"盘温采集失败: {e}")
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


# ─── 后台温控线程 ───────────────────────────────────────────
class ControllerThread(threading.Thread):
    def __init__(self, cfg: Config, quiet_mode: bool, state: WebState):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.quiet_mode = quiet_mode
        self.state = state
        self.controller: Controller | None = None
        self._stop = threading.Event()

    def run(self):
        try:
            self.controller = Controller(self.cfg, quiet_mode=self.quiet_mode)
        except IpmiError as e:
            self.state.add_log(f"连接失败: {e}")
            self.state.push("status", {"running": False, "error": str(e)})
            return

        try:
            self.controller.client.disable_auto()
            self.state.add_log("已关闭 iDRAC 自动控制，接管手动")
            self.state.push("status", {"running": True})
        except IpmiError as e:
            self.state.add_log(f"无法接管: {e}")
            self.state.push("status", {"running": False, "error": str(e)})
            return

        # 启动时自动跑一遍风扇标定（约35秒），已有标定数据则跳过
        if not self.controller.calibrator.has_data:
            self.state.add_log("首次启动，开始风扇标定…")
            self.controller.calibrator.calibrate(self.controller.client)
            try:
                self.controller.client.set_pwm(self.cfg.pwm_min)
            except IpmiError:
                pass
            self.state.add_log("风扇标定完成")

        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                if self.controller._calibrating:
                    # 手动标定进行中，暂停策略只等待
                    if self._stop.wait(1.0):
                        break
                    continue
                result = self.controller.step()
                self.state.latest_result = result
                self.state.push("data", self._result_dict(result))
                if result.reason:
                    self.state.add_log(result.reason)
            except IpmiError as e:
                self.state.add_log(f"采样失败（保持上次 PWM）: {e}")
            if self._stop.wait(max(0.5, self.cfg.interval - (time.monotonic() - t0))):
                break

        self.controller.shutdown()
        self.state.add_log("已停止，风扇设回手动安全值")

    def stop(self):
        self._stop.set()

    def apply_params(self, params: dict):
        if self.controller is None:
            return
        s = self.controller.strategy
        if "target" in params: s.target = params["target"]
        if "load_kf" in params: s.load_kf = params["load_kf"]
        if "delta_t_k" in params: s.delta_t_k = params["delta_t_k"]
        if "pwm_min" in params:
            s.pwm_min = params["pwm_min"]
            s.pid.out_max = float(s.pwm_max - params["pwm_min"])
        if "pwm_max" in params:
            s.pwm_max = params["pwm_max"]
            s.pid.out_max = float(params["pwm_max"] - s.pwm_min)
        if "kp" in params: s.pid.kp = params["kp"]
        if "ki" in params: s.pid.ki = params["ki"]
        if "kd" in params: s.pid.kd = params["kd"]

    @staticmethod
    def _result_dict(r: StrategyResult) -> dict:
        return {
            "cpu_temp": r.cpu_temp, "inlet_temp": r.inlet_temp,
            "exhaust_temp": r.exhaust_temp, "delta_t": r.delta_t,
            "cpu_usage": r.cpu_usage, "power": r.power,
            "pwm": r.pwm, "fan_rpm": r.fan_rpm,
            "fan_readings": r.fan_readings,
            "pid_p": r.pid_p, "pid_i": r.pid_i, "pid_d": r.pid_d,
            "feedforward": r.feedforward, "emergency": r.emergency,
            "reason": r.reason,
        }


# ─── HTTP 请求处理 ─────────────────────────────────────────
class WebHandler(BaseHTTPRequestHandler):
    state: WebState

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
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path.startswith("/assets/"):
            self._serve_static(self.path[1:])
        elif self.path == "/api/status":
            self._handle_status()
        elif self.path == "/api/snapshot":
            self._handle_snapshot()
        elif self.path == "/api/config":
            self._handle_get_config()
        elif self.path == "/api/calibration":
            self._handle_calibration()
        elif self.path == "/api/events":
            self._handle_sse()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/control":
            self._handle_control()
        elif self.path == "/api/params":
            self._handle_params()
        elif self.path == "/api/config":
            self._handle_post_config()
        elif self.path == "/api/config/save":
            self._handle_save_config()
        else:
            self._json(404, {"error": "not found"})

    def _serve_html(self):
        # 伺服 React 构建产物 index.html
        index_path = os.path.join(_DIST_DIR, "index.html")
        if not os.path.isfile(index_path):
            self._json(200, {
                "error": "前端未构建",
                "hint": "请在 webui/ 目录执行: npm install && npm run build",
            })
            return
        with open(index_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, rel_path: str):
        # 伺服 dist/ 下的静态资源（Vite 产物：assets/xxx.js|css）
        # 防路径穿越：规范化后必须仍在 _DIST_DIR 内
        full = os.path.normpath(os.path.join(_DIST_DIR, rel_path))
        if not full.startswith(_DIST_DIR + os.sep) or not os.path.isfile(full):
            self.send_error(404)
            return
        with open(full, "rb") as f:
            body = f.read()
        ctype, _ = mimetypes.guess_type(full)
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_status(self):
        r = self.state.latest_result
        base = {"running": self.state.running, "quiet_mode": self.state.quiet_mode}
        if r:
            base.update(ControllerThread._result_dict(r))
        self._json(200, base)

    def _handle_snapshot(self):
        cfg = self.state.config
        if cfg is None or not cfg.password:
            self._json(200, {})
            return
        try:
            from .ipmi import IpmiClient
            c = IpmiClient(cfg.ip, cfg.user, cfg.password)
            snap = c.read_sensors()
            self._json(200, {
                "cpu_temp": snap.cpu_temp_max, "inlet_temp": snap.inlet_temp,
                "exhaust_temp": snap.exhaust_temp, "delta_t": snap.delta_t,
                "cpu_usage": snap.cpu_usage, "power": snap.power_watts,
                "fan_rpm": max(snap.fan_rpms) if snap.fan_rpms else None,
                "fan_readings": snap.fan_readings,
            })
        except Exception as e:
            self._json(200, {"error": str(e)})

    def _handle_calibration(self):
        """返回风扇标定数据+健康状态。"""
        ctrl = self.state.controller_thread.controller if self.state.controller_thread else None
        if not ctrl or not ctrl.calibrator.has_data:
            self._json(200, {"calibration": {}, "health": {}})
            return
        self._json(200, {
            "calibration": ctrl.calibrator.data,
            "health": ctrl.calibrator.health,
        })

    def _handle_get_config(self):
        cfg = self.state.config
        if cfg is None:
            self._json(200, {})
            return
        # password 从 JSON 读原始值（${...} 引用或明文），不是展开后的
        import json as _json
        raw_pwd = "${DELL_BMC_PASSWORD}"
        try:
            with open(self.state.config_path, "r", encoding="utf-8") as f:
                raw_pwd = _json.load(f).get("ipmi", {}).get("password", "${DELL_BMC_PASSWORD}")
        except (OSError, _json.JSONDecodeError):
            pass
        self._json(200, {
            "ip": cfg.ip, "user": cfg.user, "password": raw_pwd, "ipmi_mode": cfg.ipmi_mode,
            "target_cpu_temp": cfg.target_cpu_temp,
            "emergency_temp": cfg.emergency_temp,
            "inlet_safe_max": cfg.inlet_safe_max, "interval": cfg.interval,
            "kp": cfg.kp, "ki": cfg.ki, "kd": cfg.kd,
            "pwm_min": cfg.pwm_min, "pwm_max": cfg.pwm_max,
            "load_kf": cfg.load_kf, "delta_t_k": cfg.delta_t_k,
            "log_level": cfg.log_level, "log_file": cfg.log_file,
            "probe_urls": cfg.probe_urls,
        })

    def _handle_post_config(self):
        try:
            data = self._read_body()
            # 这里只更新内存中的 config，不写盘（密码安全）
            cfg = self.state.config
            for key in ("target_cpu_temp", "emergency_temp", "inlet_safe_max", "interval",
                        "kp", "ki", "kd", "load_kf", "delta_t_k"):
                if key in data:
                    setattr(cfg, key, data[key])
            for key in ("pwm_min", "pwm_max"):
                if key in data:
                    setattr(cfg, key, int(data[key]))
            self._json(200, {"status": "ok"})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def _handle_control(self):
        try:
            data = self._read_body()
            action = data.get("action")
            if action == "start":
                self._start(data)
            elif action == "stop":
                self._stop()
            elif action == "mode":
                quiet = data.get("quiet", False)
                self.state.quiet_mode = quiet
                if self.state.controller_thread and self.state.controller_thread.controller:
                    self.state.controller_thread.controller.quiet_mode = quiet
                    self.state.add_log(f"已切换至{'动态' if quiet else 'PID'}模式")
                self._json(200, {"status": "ok"})
            elif action == "calibrate":
                # 手动触发风扇标定（约35秒），在独立线程跑不阻塞 HTTP 响应
                ctrl = self.state.controller_thread.controller if self.state.controller_thread else None
                if not ctrl:
                    self._json(400, {"error": "控制器未运行"})
                    return
                import threading
                def _run():
                    try:
                        self.state.add_log("开始风扇标定…")
                        ctrl.run_calibration()
                        self.state.add_log("风扇标定完成")
                    except Exception as ex:
                        self.state.add_log(f"标定失败: {ex}")
                threading.Thread(target=_run, daemon=True).start()
                self._json(200, {"status": "ok", "message": "标定已启动，约35秒完成"})
            else:
                self._json(400, {"error": "unknown action"})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def _handle_save_config(self):
        try:
            data = self._read_body()
            # 密码：前端传了就用前端的，没传保留 JSON 原值
            if not data.get("password"):
                import json as _json
                try:
                    with open(self.state.config_path, "r", encoding="utf-8") as f:
                        data["password"] = _json.load(f).get("ipmi", {}).get("password", "${DELL_BMC_PASSWORD}")
                except (OSError, _json.JSONDecodeError):
                    data["password"] = "${DELL_BMC_PASSWORD}"
            save_config(self.state.config_path, data)
            # 重新加载配置到内存
            try:
                self.state.config = load_config(self.state.config_path, allow_missing_password=True)
            except ConfigError:
                pass
            self.state.add_log(f"配置已保存到 {self.state.config_path}")
            self._json(200, {"status": "ok"})
        except ConfigError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def _start(self, data: dict):
        if self.state.running:
            self._json(200, {"error": "已在运行"})
            return

        cfg = self.state.config
        quiet = data.get("quiet", False)

        if not cfg.password:
            self._json(200, {"error": "BMC 密码未配置，请在编辑配置里设置"})
            return

        self.state.quiet_mode = quiet
        self.state.controller_thread = ControllerThread(cfg, quiet, self.state)
        self.state.controller_thread.start()
        self.state.running = True
        self.state.add_log(f"正在连接 BMC…（{'动态' if quiet else 'PID'}模式）")
        self._json(200, {"status": "starting"})

    def _stop(self):
        if self.state.controller_thread:
            self.state.controller_thread.stop()
            self.state.controller_thread.join(timeout=10)
            self.state.controller_thread = None
        self.state.running = False
        self.state.push("status", {"running": False})
        self._json(200, {"status": "stopped"})

    def _handle_params(self):
        if not self.state.controller_thread:
            self._json(200, {"message": "（未运行，参数将在下次启动时生效）"})
            return
        try:
            params = self._read_body()
            self.state.controller_thread.apply_params(params)
            self._json(200, {"message": f"参数已应用: 目标={params.get('target','?')}℃"})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = self.state.subscribe()
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.state.unsubscribe(q)

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s - %s", self.address_string(), fmt % args)


# ─── Web 服务器 ─────────────────────────────────────────────
class WebServer:
    def __init__(self, cfg: Config, host: str = "0.0.0.0", port: int = 8080, config_path: str = ""):
        self.cfg = cfg
        self.host = host
        self.port = port
        self.state = WebState()
        self.state.config = cfg
        self.state.config_path = config_path

    def run(self):
        WebHandler.state = self.state
        if self.cfg.password:
            self.state.controller_thread = ControllerThread(self.cfg, False, self.state)
            self.state.controller_thread.start()
            self.state.running = True
            self.state.add_log("已自动启动温控（PID模式）")
        # 启动硬盘温度监控（低频采集，纯展示不影响控制）
        self.state.disk_thread = DiskMonitorThread(self.state)
        self.state.disk_thread.start()
        server = ThreadingHTTPServer((self.host, self.port), WebHandler)
        url = f"http://{'localhost' if self.host == '0.0.0.0' else self.host}:{self.port}"
        print(f"WebUI 已启动: {url}")
        print(f"浏览器打开上面的地址即可使用（Ctrl+C 退出）")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n正在停止…")
            if self.state.controller_thread:
                self.state.controller_thread.stop()
                self.state.controller_thread.join(timeout=10)
            if self.state.disk_thread:
                self.state.disk_thread.stop()
            server.shutdown()


# ─── 入口 ───────────────────────────────────────────────────
def main() -> None:
    # config_path：命令行传了就用命令行的，否则用默认 ~/dell_fan_ctrl/config.json
    config_path = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        cfg = load_config(config_path, allow_missing_password=True)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        sys.exit(1)

    # 实际配置文件路径（load 后确定）
    if config_path is None:
        from .config import DEFAULT_CONFIG_PATH
        config_path = DEFAULT_CONFIG_PATH

    # 端口优先级：命令行 > config.json web.port > 默认 8089
    port = int(sys.argv[2]) if len(sys.argv) > 2 else cfg.web_port

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(cfg.log_file, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("pyghmi").setLevel(logging.WARNING)

    web = WebServer(cfg, cfg.web_host, port, config_path)
    web.run()


if __name__ == "__main__":
    main()
