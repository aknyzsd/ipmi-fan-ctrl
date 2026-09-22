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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

from .config import load as load_config, save as save_config, ConfigError, Config, PWM_HARM_FLOOR
from .controller import Controller
from .ipmi import IpmiError
from .thermal import StrategyResult

logger = logging.getLogger(__name__)


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

        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
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
            "pid_p": r.pid_p, "pid_i": r.pid_i, "pid_d": r.pid_d,
            "feedforward": r.feedforward, "emergency": r.emergency,
            "reason": r.reason,
        }


# ─── HTML 前端 ─────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dell 风扇 PID 温控</title>
<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:linear-gradient(135deg,#0a0e27,#1a1a3e);color:#e0e0e0;min-height:100vh;padding:16px}
#app{max-width:960px;margin:0 auto}
.header{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.header h1{font-size:20px;background:linear-gradient(90deg,#00d4aa,#00a8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.btn{padding:8px 20px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;transition:all .2s}
.btn-primary{background:linear-gradient(135deg,#00d4aa,#00a8cc);color:#fff}
.btn-danger{background:linear-gradient(135deg,#ff6b6b,#ee5a24);color:#fff}
.btn-secondary{background:rgba(255,255,255,.1);color:#e0e0e0;border:1px solid rgba(255,255,255,.15)}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.3)}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none}
.checkbox{display:flex;align-items:center;gap:6px;font-size:14px;cursor:pointer}
.checkbox input{width:16px;height:16px;accent-color:#00d4aa}
.status{display:flex;align-items:center;gap:6px;margin-left:auto;font-size:14px}
.dot{width:10px;height:10px;border-radius:50%;box-shadow:0 0 8px currentColor}
.dot-on{background:#00d4aa;color:#00d4aa}.dot-off{background:#666;color:#666}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:640px){.cols{grid-template-columns:1fr}}
.card{background:rgba(30,35,70,.6);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:16px}
.card h2{font-size:15px;color:#00d4aa;margin-bottom:12px;display:flex;align-items:center;gap:6px}
.dv{display:flex;justify-content:space-between;align-items:center;padding:5px 0;font-size:13px;border-bottom:1px solid rgba(255,255,255,.04)}
.dv:last-child{border:none}
.dv .v{font-family:'Cascadia Code',Consolas,monospace;font-weight:700;color:#00d4aa;font-size:14px}
.pv{display:flex;justify-content:space-between;align-items:center;padding:6px 0;font-size:13px}
.pv label{width:88px;color:#aab;display:flex;align-items:center;gap:3px}
.pv input[type=range]{flex:1;max-width:140px;accent-color:#00a8ff;margin:0 8px}
.pv input[type=number]{width:75px;background:rgba(0,0,0,.3);color:#00d4aa;border:1px solid rgba(255,255,255,.1);border-radius:4px;padding:4px 6px;font-family:monospace}
.pv .unit{color:#888;font-size:12px;width:40px;text-align:right}
.log-box{background:rgba(0,0,0,.4);border-radius:8px;padding:10px;height:180px;overflow-y:auto;font-family:'Cascadia Code',Consolas,monospace;font-size:11px;color:#8a8a8a;line-height:1.6}
.log-box .warn{color:#ffd93d}.log-box .err{color:#ff6b6b}.log-box .ok{color:#00d4aa}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;justify-content:center;align-items:center;z-index:99;backdrop-filter:blur(4px)}
.modal{background:rgba(30,35,70,.95);border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:24px;min-width:340px}
.modal h2{color:#00d4aa;margin-bottom:16px;font-size:16px}
.modal .pv label{width:82px}
.modal .btns{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
.cfg-input{flex:1;background:rgba(0,0,0,.3);color:#00d4aa;border:1px solid rgba(255,255,255,.1);border-radius:4px;padding:4px 8px;font-family:monospace;font-size:13px}
.help{display:inline-block;width:14px;height:14px;line-height:14px;text-align:center;border-radius:50%;background:#555;color:#fff;font-size:10px;cursor:help;flex-shrink:0}
.help:hover::after{content:attr(data-tip);position:absolute;left:28px;top:-8px;background:#1a1a2e;color:#e0e0e0;padding:8px 12px;border-radius:6px;font-size:11px;line-height:1.5;width:230px;z-index:200;box-shadow:0 4px 12px rgba(0,0,0,.5);pointer-events:none}
</style>
</head>
<body>
<div id="app">
  <div class="header">
    <button class="btn btn-secondary" @click="openConfig">编辑配置</button>
    <h1> Dell 风扇温控</h1>
      <label class="checkbox"><input type="checkbox" v-model="quiet" @change="toggleMode"> 动态模式</label>

    <div class="status">
      <span class="dot" :class="running?'dot-on':'dot-off'"></span>
      <span>{{ running ? '运行中' : '未运行' }}</span>
    </div>
  </div>

  <div class="cols">
    <div class="card">
      <h2>实时数据</h2>
      <div class="dv">CPU 温度<span class="v">{{ fmt(d.cpu_temp) }}℃</span></div>
      <div class="dv">进风温度<span class="v">{{ fmt(d.inlet_temp) }}℃</span></div>
      <div class="dv">排风温度<span class="v">{{ fmt(d.exhaust_temp) }}℃</span></div>
      <div class="dv">温差 ΔT<span class="v">{{ fmt(d.delta_t) }}℃</span></div>
      <div class="dv">CPU 负载<span class="v">{{ fmt(d.cpu_usage) }}%</span></div>
      <div class="dv">整机功耗<span class="v">{{ fmt(d.power) }}W</span></div>
      <div class="dv">当前 PWM<span class="v">{{ d.emergency ? '紧急回退' : (d.pwm!=null ? d.pwm+'%' : '—') }}</span></div>
      <div class="dv">风扇转速<span class="v">{{ fmt(d.fan_rpm) }} RPM</span></div>
      <div class="dv">PID (P/I/D)<span class="v">{{ fmt(d.pid_p) }} / {{ fmt(d.pid_i) }} / {{ fmt(d.pid_d) }}</span></div>
      <div class="dv">前馈<span class="v">{{ fmt(d.feedforward) }}%</span></div>
    </div>

    <div class="card">
      <h2>参数调节</h2>
      <div class="pv"><label>目标温度</label><input type="range" v-model.number="p.target" min="40" max="75"><span class="unit">{{ p.target }}℃</span></div>
      <div class="pv"><label>PWM 下限</label><input type="range" v-model.number="p.pwm_min" min="20" max="50"><span class="unit">{{ p.pwm_min }}%</span></div>
      <div class="pv"><label>PWM 上限</label><input type="range" v-model.number="p.pwm_max" min="50" max="100"><span class="unit">{{ p.pwm_max }}%</span></div>
      <div class="pv"><label>Kp<span class="help" data-tip="比例增益。误差每1℃加Kp%PWM，越大响应越快但易振荡">?</span></label><input type="number" v-model.number="p.kp" step="0.1"></div>
      <div class="pv"><label>Ki<span class="help" data-tip="积分增益。消除稳态误差(温度长期偏离的累积修正)，过大会振荡">?</span></label><input type="number" v-model.number="p.ki" step="0.05"></div>
      <div class="pv"><label>Kd<span class="help" data-tip="微分增益。抑制突变预判趋势，温度噪声大时易放大干扰，默认关">?</span></label><input type="number" v-model.number="p.kd" step="0.1"></div>
      <div class="pv"><label>负载前馈<span class="help" data-tip="CPU负载前馈增益。CPU一忙就提前加速风扇不等温度升。0.3=80%负载加24%PWM">?</span></label><input type="number" v-model.number="p.load_kf" step="0.05"></div>
      <div class="pv"><label>温差前馈<span class="help" data-tip="进排风温差前馈增益。温差大说明整机热负荷高(含硬盘/显卡)，超10℃基准才加成">?</span></label><input type="number" v-model.number="p.delta_t_k" step="0.1"></div>
      <div class="pv"><span style="flex:1"></span><button class="btn btn-secondary" @click="applyParams">应用参数</button></div>
    </div>
  </div>

  <div class="card">
    <h2>日志</h2>
    <div class="log-box" ref="logBox">
      <div v-for="l in logs" :key="l" v-html="l"></div>
    </div>
  </div>

  <div class="modal-bg" v-if="showConfig">
    <div class="modal" style="min-width:520px;max-height:90vh;overflow-y:auto">
      <h2>编辑配置</h2>
      <div style="font-size:12px;color:#8a8a8a;margin-bottom:8px">IPMI 连接</div>
      <div class="pv"><label>BMC IP</label><input v-model="ce.ip" class="cfg-input"></div>
      <div class="pv"><label>用户名</label><input v-model="ce.user" class="cfg-input"></div>
      <div class="pv"><label>密码</label><input type="password" v-model="ce.password" class="cfg-input" placeholder="明文或${DELL_BMC_PASSWORD}"></div>
      <div style="font-size:12px;color:#8a8a8a;margin:8px 0">控制参数</div>
      <div class="pv"><label>目标温度</label><input type="number" v-model.number="ce.target_cpu_temp" step="1" class="cfg-input"><span class="unit">℃</span></div>
      <div class="pv"><label>紧急温度</label><input type="number" v-model.number="ce.emergency_temp" step="1" class="cfg-input"><span class="unit">℃</span></div>
      <div class="pv"><label>进风上限</label><input type="number" v-model.number="ce.inlet_safe_max" step="1" class="cfg-input"><span class="unit">℃</span></div>
      <div class="pv"><label>采样间隔</label><input type="number" v-model.number="ce.interval" step="0.5" class="cfg-input"><span class="unit">秒</span></div>
      <div style="font-size:12px;color:#8a8a8a;margin:8px 0">PID 参数</div>
      <div class="pv"><label>Kp<span class="help" data-tip="比例增益。误差每1℃加Kp%PWM，越大响应越快但易振荡">?</span></label><input type="number" v-model.number="ce.kp" step="0.1" class="cfg-input"></div>
      <div class="pv"><label>Ki<span class="help" data-tip="积分增益。消除稳态误差(温度长期偏离的累积修正)，过大会振荡">?</span></label><input type="number" v-model.number="ce.ki" step="0.05" class="cfg-input"></div>
      <div class="pv"><label>Kd<span class="help" data-tip="微分增益。抑制突变预判趋势，温度噪声大时易放大干扰，默认关">?</span></label><input type="number" v-model.number="ce.kd" step="0.1" class="cfg-input"></div>
      <div class="pv"><label>PWM 下限</label><input type="number" v-model.number="ce.pwm_min" step="1" class="cfg-input"><span class="unit">%</span></div>
      <div class="pv"><label>PWM 上限</label><input type="number" v-model.number="ce.pwm_max" step="1" class="cfg-input"><span class="unit">%</span></div>
      <div style="font-size:12px;color:#8a8a8a;margin:8px 0">前馈参数</div>
      <div class="pv"><label>负载前馈<span class="help" data-tip="CPU负载前馈增益。CPU一忙就提前加速风扇不等温度升。0.3=80%负载加24%PWM">?</span></label><input type="number" v-model.number="ce.load_kf" step="0.05" class="cfg-input"></div>
      <div class="pv"><label>温差前馈<span class="help" data-tip="进排风温差前馈增益。温差大说明整机热负荷高(含硬盘/显卡)，超10℃基准才加成">?</span></label><input type="number" v-model.number="ce.delta_t_k" step="0.1" class="cfg-input"></div>
      <div style="font-size:12px;color:#8a8a8a;margin:8px 0">日志</div>
      <div class="pv"><label>日志级别</label><select v-model="ce.log_level" class="cfg-input"><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select></div>
      <div class="pv"><label>日志文件</label><input v-model="ce.log_file" class="cfg-input"></div>
      <div class="btns"><button class="btn btn-primary" @click="saveConfig">保存到文件</button><button class="btn btn-secondary" @click="showConfig=false">取消</button></div>
    </div>
  </div>
</div>

<script>
const {createApp, ref, reactive, onMounted, nextTick} = Vue;
createApp({
  setup() {
    const d = reactive({cpu_temp:null,inlet_temp:null,exhaust_temp:null,delta_t:null,cpu_usage:null,power:null,pwm:null,fan_rpm:null,pid_p:0,pid_i:0,pid_d:0,feedforward:0,emergency:false,reason:''});
    const p = reactive({target:55,pwm_min:27,pwm_max:100,kp:2.0,ki:0.1,kd:0.0,load_kf:0.3,delta_t_k:1.0});
    const running = ref(false), quiet = ref(false), showConfig = ref(false);
    const logs = ref([]);
    const ce = reactive({ip:'',user:'',target_cpu_temp:55,emergency_temp:80,inlet_safe_max:40,interval:3,kp:2,ki:0.1,kd:0,pwm_min:27,pwm_max:100,load_kf:0.3,delta_t_k:1,log_level:'INFO',log_file:'dell_fan.log',password:'${DELL_BMC_PASSWORD}'});
    const logBox = ref(null);
    const fmt = v => v==null ? '—' : (typeof v=='number' ? v.toFixed(1) : v);

    const addLog = (msg) => {
      let cls = '';
      if (msg.includes('错误') || msg.includes('失败') || msg.includes('❌')) cls = 'err';
      else if (msg.includes('⚠') || msg.includes('回调') || msg.includes('切换')) cls = 'warn';
      else if (msg.includes('已') || msg.includes('OK')) cls = 'ok';
      logs.value.push(cls ? `<span class="${cls}">${msg}</span>` : msg);
      if (logs.value.length > 200) logs.value.shift();
      nextTick(() => { if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight; });
    };

    const start = async () => {
      const resp = await fetch('/api/control', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'start',quiet:quiet.value})});
      const r = await resp.json();
      if (r.error) addLog('[错误] ' + r.error);
    };

    const stop = async () => {
      await fetch('/api/control', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'stop'})});
    };

    const toggleMode = async () => {
      addLog(quiet.value ? '动态模式已开启' : '动态模式已关闭');
      if (running.value) {
        await fetch('/api/control', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'mode',quiet:quiet.value})});
      }
    };

    const applyParams = async () => {
      const resp = await fetch('/api/params', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...p})});
      const r = await resp.json();
      addLog(r.message || r.error || '参数已应用');
    };

    const openConfig = async () => {
      const resp = await fetch('/api/config');
      const cfg = await resp.json();
      Object.assign(ce, cfg);
      if (!ce.password) ce.password = '${DELL_BMC_PASSWORD}';
      showConfig.value = true;
    };

    const saveConfig = async () => {
      const resp = await fetch('/api/config/save', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ce)});
      const r = await resp.json();
      if (r.error) addLog('[错误] ' + r.error);
      else { addLog('配置已保存到文件'); showConfig.value = false; }
    };

    onMounted(() => {
      fetch('/api/config').then(r=>r.json()).then(cfg=>{
        if (cfg.target_cpu_temp) p.target=cfg.target_cpu_temp;
        if (cfg.pwm_min) p.pwm_min=cfg.pwm_min;
        if (cfg.pwm_max) p.pwm_max=cfg.pwm_max;
        if (cfg.kp!=null) p.kp=cfg.kp;
        if (cfg.ki!=null) p.ki=cfg.ki;
        if (cfg.kd!=null) p.kd=cfg.kd;
        if (cfg.load_kf!=null) p.load_kf=cfg.load_kf;
        if (cfg.delta_t_k!=null) p.delta_t_k=cfg.delta_t_k;
      });
      fetch('/api/status').then(r=>r.json()).then(s=>{
        running.value=s.running;
        Object.assign(d, s);
        if (!s.running && !s.cpu_temp) {
          addLog('正在读取 BMC 传感器…');
          fetch('/api/snapshot').then(r=>r.json()).then(snap=>{
            if (snap.error) addLog('[错误] 读取失败: '+snap.error);
            else { Object.assign(d, snap); addLog('BMC 数据已同步'); }
          });
        }
      });
      const es = new EventSource('/api/events');
      es.onmessage = e => {
        const msg = JSON.parse(e.data);
        if (msg.event==='data') Object.assign(d, msg.data);
        else if (msg.event==='log') addLog(msg.data.message);
        else if (msg.event==='status') { running.value=msg.data.running; if(msg.data.error) addLog('[错误] '+msg.data.error); }
      };
    });

    return {d,p,running,quiet,showConfig,ce,logs,logBox,fmt,addLog,toggleMode,start,stop,applyParams,openConfig,saveConfig};
  }
}).mount('#app');
</script>
</body>
</html>
"""


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
        elif self.path == "/api/status":
            self._handle_status()
        elif self.path == "/api/snapshot":
            self._handle_snapshot()
        elif self.path == "/api/config":
            self._handle_get_config()
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
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
            })
        except Exception as e:
            self._json(200, {"error": str(e)})

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
            "ip": cfg.ip, "user": cfg.user, "password": raw_pwd,
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
