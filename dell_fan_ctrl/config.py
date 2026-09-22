"""配置加载与校验（JSON 版）。

配置文件默认放 ~/dell_fan_ctrl/config.json，和代码分离。
首次运行若检测到项目目录下的旧 config.ini，自动迁移成 JSON。
密码用 ${ENV_VAR} 语法从环境变量读，绝不明文落盘。
"""
import json
import os
import re
from dataclasses import dataclass
from configparser import ConfigParser, Error as _CfgError

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

PWM_HARM_FLOOR = 0

CONFIG_DIR = os.path.join(os.path.expanduser("~"), "dell_fan_ctrl")
DEFAULT_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    ip: str
    user: str
    password: str
    target_cpu_temp: float
    emergency_temp: float
    inlet_safe_max: float
    interval: float
    kp: float
    ki: float
    kd: float
    pwm_min: int
    pwm_max: int
    load_kf: float
    delta_t_k: float
    log_level: str
    log_file: str
    probe_urls: list[str]
    web_host: str
    web_port: int


def _expand_env(value: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        val = os.environ.get(name)
        if val is None:
            raise ConfigError(f"环境变量 {name} 未设置（配置引用了 ${{{name}}}）")
        return val
    return _ENV_PATTERN.sub(repl, value)


def _default_json() -> dict:
    """生成默认配置字典，首次运行时写入。"""
    return {
        "ipmi": {"ip": "192.168.152.203", "user": "root", "password": "${DELL_BMC_PASSWORD}"},
        "control": {"target_cpu_temp": 55, "emergency_temp": 80, "inlet_safe_max": 40, "interval": 3},
        "pid": {"kp": 2, "ki": 0.1, "kd": 0, "pwm_min": 27, "pwm_max": 100},
        "feedforward": {"load_kf": 0.3, "delta_t_k": 1},
        "logging": {"level": "INFO", "file": "dell_fan.log"},
        "web": {"host": "0.0.0.0", "port": 8089},
        "probes": [],
    }


def _migrate_from_ini(ini_path: str) -> dict:
    """读旧 config.ini，转成 JSON 字典结构。"""
    cp = ConfigParser()
    cp.read(ini_path, encoding="utf-8-sig")
    d = _default_json()
    if cp.has_section("ipmi"):
        d["ipmi"]["ip"] = cp.get("ipmi", "ip", fallback=d["ipmi"]["ip"])
        d["ipmi"]["user"] = cp.get("ipmi", "user", fallback=d["ipmi"]["user"])
        d["ipmi"]["password"] = cp.get("ipmi", "password", fallback=d["ipmi"]["password"])
    if cp.has_section("control"):
        d["control"]["target_cpu_temp"] = cp.getfloat("control", "target_cpu_temp", fallback=55)
        d["control"]["emergency_temp"] = cp.getfloat("control", "emergency_temp", fallback=80)
        d["control"]["inlet_safe_max"] = cp.getfloat("control", "inlet_safe_max", fallback=40)
        d["control"]["interval"] = cp.getfloat("control", "interval", fallback=3)
    if cp.has_section("pid"):
        d["pid"]["kp"] = cp.getfloat("pid", "kp", fallback=2)
        d["pid"]["ki"] = cp.getfloat("pid", "ki", fallback=0.1)
        d["pid"]["kd"] = cp.getfloat("pid", "kd", fallback=0)
        d["pid"]["pwm_min"] = cp.getint("pid", "pwm_min", fallback=27)
        d["pid"]["pwm_max"] = cp.getint("pid", "pwm_max", fallback=100)
    if cp.has_section("feedforward"):
        d["feedforward"]["load_kf"] = cp.getfloat("feedforward", "load_kf", fallback=0.3)
        d["feedforward"]["delta_t_k"] = cp.getfloat("feedforward", "delta_t_k", fallback=1)
    if cp.has_section("logging"):
        d["logging"]["level"] = cp.get("logging", "level", fallback="INFO")
        d["logging"]["file"] = cp.get("logging", "file", fallback="dell_fan.log")
    if cp.has_section("web"):
        d["web"]["host"] = cp.get("web", "host", fallback="0.0.0.0")
        d["web"]["port"] = cp.getint("web", "port", fallback=8089)
    if cp.has_section("probes"):
        d["probes"] = [cp.get("probes", key) for key in cp.options("probes")]
    return d


def _ensure_config(path: str) -> dict:
    """确保配置文件存在：有旧 INI 就迁移，否则生成默认。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 找旧 config.ini：先看项目目录，再看当前目录
    for ini_candidate in [
        os.path.join(os.path.dirname(__file__), "..", "config.ini"),
        "config.ini",
    ]:
        ini_candidate = os.path.abspath(ini_candidate)
        if os.path.exists(ini_candidate):
            d = _migrate_from_ini(ini_candidate)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
            return d
    # 没有旧 INI，生成默认
    d = _default_json()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    return d


def load(path: str | None = None, allow_missing_password: bool = False) -> Config:
    if path is None:
        path = DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        d = _ensure_config(path)
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ConfigError(f"配置文件解析失败: {e}") from e

    try:
        ipmi = d["ipmi"]
        ip = ipmi["ip"]
        user = ipmi["user"]
        try:
            password = _expand_env(ipmi["password"])
        except ConfigError:
            if allow_missing_password:
                password = None
            else:
                raise

        ctrl = d.get("control", {})
        target_cpu_temp = float(ctrl.get("target_cpu_temp", 55))
        emergency_temp = float(ctrl.get("emergency_temp", 80))
        inlet_safe_max = float(ctrl.get("inlet_safe_max", 40))
        interval = float(ctrl.get("interval", 3))

        pid = d.get("pid", {})
        kp = float(pid.get("kp", 2))
        ki = float(pid.get("ki", 0.1))
        kd = float(pid.get("kd", 0))
        pwm_min = int(pid.get("pwm_min", 27))
        pwm_max = int(pid.get("pwm_max", 100))

        ff = d.get("feedforward", {})
        load_kf = float(ff.get("load_kf", 0.3))
        delta_t_k = float(ff.get("delta_t_k", 1))

        lg = d.get("logging", {})
        log_level = lg.get("level", "INFO")
        log_file = lg.get("file", "dell_fan.log")

        probe_urls = d.get("probes", [])

        web = d.get("web", {})
        web_host = web.get("host", "0.0.0.0")
        web_port = int(web.get("port", 8089))
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"配置项缺失或错误: {e}") from e

    if pwm_min < PWM_HARM_FLOOR:
        raise ConfigError(f"pwm_min={pwm_min} 不能为负")
    if pwm_max > 100 or pwm_min >= pwm_max:
        raise ConfigError(f"PWM 范围非法: min={pwm_min} max={pwm_max}")
    if emergency_temp <= target_cpu_temp:
        raise ConfigError(f"紧急温度 {emergency_temp} 必须高于目标温度 {target_cpu_temp}")

    return Config(ip=ip, user=user, password=password,
                  target_cpu_temp=target_cpu_temp, emergency_temp=emergency_temp,
                  inlet_safe_max=inlet_safe_max, interval=interval,
                  kp=kp, ki=ki, kd=kd, pwm_min=pwm_min, pwm_max=pwm_max,
                  load_kf=load_kf, delta_t_k=delta_t_k,
                  log_level=log_level, log_file=log_file,
                  probe_urls=probe_urls,
                  web_host=web_host, web_port=web_port)


def save(path: str | None, s: dict) -> None:
    """把配置字典写回 JSON 文件。

    s 的 key 和 Config 字段对应（flat），这里转成嵌套 JSON 结构。
    password 传原始 ${...} 引用，不写明文。
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH
    if s["pwm_min"] < PWM_HARM_FLOOR:
        raise ConfigError(f"pwm_min={s['pwm_min']} 不能为负")
    if s["pwm_max"] > 100 or s["pwm_min"] >= s["pwm_max"]:
        raise ConfigError(f"PWM 范围非法: min={s['pwm_min']} max={s['pwm_max']}")
    if s["emergency_temp"] <= s["target_cpu_temp"]:
        raise ConfigError(f"紧急温度必须高于目标温度")

    d = {
        "ipmi": {"ip": s["ip"], "user": s["user"], "password": s["password"]},
        "control": {
            "target_cpu_temp": s["target_cpu_temp"],
            "emergency_temp": s["emergency_temp"],
            "inlet_safe_max": s["inlet_safe_max"],
            "interval": s["interval"],
        },
        "pid": {
            "kp": s["kp"], "ki": s["ki"], "kd": s["kd"],
            "pwm_min": s["pwm_min"], "pwm_max": s["pwm_max"],
        },
        "feedforward": {"load_kf": s["load_kf"], "delta_t_k": s["delta_t_k"]},
        "logging": {"level": s["log_level"], "file": s["log_file"]},
        "web": {"host": s.get("web_host", "0.0.0.0"), "port": s.get("web_port", 8089)},
        "probes": s.get("probe_urls", []),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
