"""PySide6 GUI：实时数据面板 + 参数调节 + 系统托盘。

用法：python -m dell_fan_ctrl.gui [config.ini]
后台 QThread 跑温控循环，Signal 推数据刷新 UI；改参数点"应用"即时生效。
"""
import sys
import logging

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QSlider, QDoubleSpinBox,
    QSpinBox, QGroupBox, QTextEdit, QSystemTrayIcon, QMenu,
    QMessageBox, QFrame, QStyle,
    QDialog, QDialogButtonBox, QFormLayout, QComboBox, QLineEdit, QCheckBox,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction

from .config import load as load_config, save as save_config, ConfigError, Config, PWM_HARM_FLOOR
from .controller import Controller
from .ipmi import IpmiError
from .thermal import StrategyResult


# ─── 后台线程 ───────────────────────────────────────────────
class ControllerWorker(QThread):
    data_changed = Signal(object)   # StrategyResult
    log_message = Signal(str)
    error_occurred = Signal(str)
    status_changed = Signal(str)

    def __init__(self, cfg: Config, quiet_mode: bool = False):
        super().__init__()
        self.cfg = cfg
        self.quiet_mode = quiet_mode
        self.controller: Controller | None = None

    def run(self) -> None:
        try:
            self.controller = Controller(self.cfg, quiet_mode=self.quiet_mode)
        except IpmiError as e:
            self.error_occurred.emit(f"连接失败: {e}")
            return

        try:
            self.controller.client.disable_auto()
            self.log_message.emit("已关闭 iDRAC 自动控制，接管手动")
            self.status_changed.emit("运行中")
        except IpmiError as e:
            self.error_occurred.emit(f"无法接管手动控制: {e}")
            return

        interval_ms = int(self.cfg.interval * 1000)
        while not self.isInterruptionRequested():
            try:
                result = self.controller.step()
                self.data_changed.emit(result)
            except IpmiError as e:
                self.error_occurred.emit(f"采样失败（保持上次 PWM）: {e}")
            # 分段 sleep，能更快响应中断请求
            for _ in range(max(1, interval_ms // 100)):
                if self.isInterruptionRequested():
                    break
                self.msleep(100)

        self.controller.shutdown()
        self.status_changed.emit("已停止")

    def apply_params(self, params: dict) -> None:
        """运行时实时改参数，直接改 strategy/pid 对象属性。"""
        if self.controller is None:
            return
        s = self.controller.strategy
        if "target" in params:
            s.target = params["target"]
        if "load_kf" in params:
            s.load_kf = params["load_kf"]
        if "delta_t_k" in params:
            s.delta_t_k = params["delta_t_k"]
        if "pwm_min" in params:
            s.pwm_min = params["pwm_min"]
            s.pid.out_min = 0.0
            s.pid.out_max = float(s.pwm_max - params["pwm_min"])
        if "pwm_max" in params:
            s.pwm_max = params["pwm_max"]
            s.pid.out_max = float(params["pwm_max"] - s.pwm_min)
        if "kp" in params:
            s.pid.kp = params["kp"]
        if "ki" in params:
            s.pid.ki = params["ki"]
        if "kd" in params:
            s.pid.kd = params["kd"]


# ─── 配置编辑对话框 ─────────────────────────────────────────
class ConfigDialog(QDialog):
    """编辑 ini 全部配置项，保存调 config.save() 写回文件。"""

    def __init__(self, config_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑配置")
        self.config_path = config_path
        self.setMinimumWidth(500)

        from configparser import ConfigParser
        cp = ConfigParser()
        cp.read(config_path, encoding="utf-8-sig")

        layout = QVBoxLayout(self)

        # IPMI 连接
        g1 = QGroupBox("IPMI 连接")
        f1 = QFormLayout(g1)
        self.ed_ip = QLineEdit(cp.get("ipmi", "ip"))
        self.ed_user = QLineEdit(cp.get("ipmi", "user"))
        self.ed_password = QLineEdit(cp.get("ipmi", "password"))
        self.ed_password.setPlaceholderText("${DELL_BMC_PASSWORD}")
        self.ed_ipmitool = QLineEdit(cp.get("ipmi", "ipmitool_path",
                                            fallback="./Dell/SysMgt/bmc/ipmitool.exe"))
        f1.addRow("BMC IP:", self.ed_ip)
        f1.addRow("用户名:", self.ed_user)
        f1.addRow("密码引用:", self.ed_password)
        f1.addRow("ipmitool 路径:", self.ed_ipmitool)
        layout.addWidget(g1)

        # 控制参数
        g2 = QGroupBox("控制参数")
        f2 = QFormLayout(g2)
        self.sp_target = self._dspin(f2, "目标 CPU 温度 (℃)",
                                     cp.getfloat("control", "target_cpu_temp", fallback=55), 30, 90, 1)
        self.sp_emergency = self._dspin(f2, "紧急温度 (℃)",
                                        cp.getfloat("control", "emergency_temp", fallback=80), 50, 100, 1)
        self.sp_inlet_max = self._dspin(f2, "进风安全上限 (℃)",
                                        cp.getfloat("control", "inlet_safe_max", fallback=40), 20, 60, 1)
        self.sp_interval = self._dspin(f2, "采样间隔 (秒)",
                                       cp.getfloat("control", "interval", fallback=3), 0.5, 30, 0.5)
        layout.addWidget(g2)

        # PID 参数
        g3 = QGroupBox("PID 参数")
        f3 = QFormLayout(g3)
        self.sp_kp = self._dspin(f3, "Kp", cp.getfloat("pid", "kp", fallback=2.0), 0, 20, 0.1)
        self.sp_ki = self._dspin(f3, "Ki", cp.getfloat("pid", "ki", fallback=0.1), 0, 5, 0.05)
        self.sp_kd = self._dspin(f3, "Kd", cp.getfloat("pid", "kd", fallback=0.0), 0, 10, 0.1)
        self.sp_pwm_min = QSpinBox()
        self.sp_pwm_min.setRange(PWM_HARM_FLOOR, 100)
        self.sp_pwm_min.setValue(cp.getint("pid", "pwm_min", fallback=27))
        f3.addRow("PWM 下限 (%):", self.sp_pwm_min)
        self.sp_pwm_max = QSpinBox()
        self.sp_pwm_max.setRange(20, 100)
        self.sp_pwm_max.setValue(cp.getint("pid", "pwm_max", fallback=100))
        f3.addRow("PWM 上限 (%):", self.sp_pwm_max)
        layout.addWidget(g3)

        # 前馈参数
        g4 = QGroupBox("前馈参数")
        f4 = QFormLayout(g4)
        self.sp_load_kf = self._dspin(f4, "负载前馈增益",
                                      cp.getfloat("feedforward", "load_kf", fallback=0.3), 0, 2, 0.05)
        self.sp_delta_t_k = self._dspin(f4, "温差前馈增益",
                                        cp.getfloat("feedforward", "delta_t_k", fallback=1.0), 0, 10, 0.1)
        layout.addWidget(g4)

        # 日志
        g5 = QGroupBox("日志")
        f5 = QFormLayout(g5)
        self.cb_level = QComboBox()
        self.cb_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.cb_level.setCurrentText(cp.get("logging", "level", fallback="INFO"))
        f5.addRow("日志级别:", self.cb_level)
        self.ed_logfile = QLineEdit(cp.get("logging", "file", fallback="dell_fan.log"))
        f5.addRow("日志文件:", self.ed_logfile)
        layout.addWidget(g5)

        # 按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _dspin(form: QFormLayout, label: str, val: float, lo: float, hi: float, step: float):
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setDecimals(2)
        sp.setValue(val)
        form.addRow(label, sp)
        return sp

    def get_settings(self) -> dict:
        return {
            "ip": self.ed_ip.text(),
            "user": self.ed_user.text(),
            "password": self.ed_password.text(),
            "ipmitool_path": self.ed_ipmitool.text(),
            "target_cpu_temp": self.sp_target.value(),
            "emergency_temp": self.sp_emergency.value(),
            "inlet_safe_max": self.sp_inlet_max.value(),
            "interval": self.sp_interval.value(),
            "kp": self.sp_kp.value(),
            "ki": self.sp_ki.value(),
            "kd": self.sp_kd.value(),
            "pwm_min": self.sp_pwm_min.value(),
            "pwm_max": self.sp_pwm_max.value(),
            "load_kf": self.sp_load_kf.value(),
            "delta_t_k": self.sp_delta_t_k.value(),
            "log_level": self.cb_level.currentText(),
            "log_file": self.ed_logfile.text(),
        }


# ─── 连接对话框（密码不落盘，只存在内存） ─────────────────────
class ConnectDialog(QDialog):
    """启动时弹出让用户输入 BMC IP/用户名/密码，密码不写 ini。"""

    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("BMC 连接")
        self.setMinimumWidth(360)

        form = QFormLayout(self)
        self.ed_ip = QLineEdit(cfg.ip)
        self.ed_user = QLineEdit(cfg.user)
        self.ed_password = QLineEdit()
        self.ed_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_password.setPlaceholderText("输入 BMC 密码（不保存到文件）")
        form.addRow("BMC IP:", self.ed_ip)
        form.addRow("用户名:", self.ed_user)
        form.addRow("密码:", self.ed_password)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


# ─── 主窗口 ─────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, config_path: str):
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self.worker: ControllerWorker | None = None
        self.setWindowTitle("Dell 风扇 PID 温控")
        self.setMinimumSize(720, 520)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ── 顶部：启动/停止 + 状态 ──
        top = QHBoxLayout()
        self.btn_start = QPushButton("启动")
        self.btn_stop = QPushButton("停止")
        self.btn_config = QPushButton("编辑配置")
        self.btn_stop.setEnabled(False)
        self.lbl_status = QLabel("未运行")
        self.lbl_status.setStyleSheet("color: #888; font-weight: bold;")
        top.addWidget(self.btn_start)
        top.addWidget(self.btn_stop)
        top.addWidget(self.btn_config)
        self.cb_quiet = QCheckBox("静音模式")
        self.cb_quiet.setToolTip("逐级递减风扇转速，温差>15℃或温度快升时回调安全值")
        top.addWidget(self.cb_quiet)
        top.addStretch()
        top.addWidget(QLabel("状态:"))
        top.addWidget(self.lbl_status)
        root.addLayout(top)

        # ── 中部：左数据 + 右参数 ──
        mid = QHBoxLayout()

        # 左：实时数据
        data_group = QGroupBox("实时数据")
        data_grid = QGridLayout(data_group)
        self.data_labels: dict[str, QLabel] = {}
        data_rows = [
            ("cpu_temp", "CPU 温度", "℃"),
            ("inlet_temp", "进风温度", "℃"),
            ("exhaust_temp", "排风温度", "℃"),
            ("delta_t", "温差 ΔT", "℃"),
            ("cpu_usage", "CPU 负载", "%"),
            ("power", "整机功耗", "W"),
            ("pwm", "当前 PWM", "%"),
            ("fan_rpm", "风扇转速", "RPM"),
            ("pid", "PID (P/I/D)", ""),
            ("feedforward", "前馈", "%"),
        ]
        for i, (key, name, unit) in enumerate(data_rows):
            data_grid.addWidget(QLabel(name), i, 0)
            lbl = QLabel("—")
            lbl.setStyleSheet("font-family: monospace; font-size: 14px; font-weight: bold;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            data_grid.addWidget(lbl, i, 1)
            data_grid.addWidget(QLabel(unit), i, 2)
            self.data_labels[key] = lbl
        mid.addWidget(data_group)

        # 右：参数调节
        param_group = QGroupBox("参数调节")
        param_grid = QGridLayout(param_group)

        # 目标温度（滑块 + 标签）
        param_grid.addWidget(QLabel("目标温度"), 0, 0)
        self.sl_target = QSlider(Qt.Orientation.Horizontal)
        self.sl_target.setRange(40, 75)
        self.sl_target.setValue(int(cfg.target_cpu_temp))
        self.lbl_target = QLabel(f"{cfg.target_cpu_temp:.0f} ℃")
        self.sl_target.valueChanged.connect(
            lambda v: self.lbl_target.setText(f"{v} ℃"))
        param_grid.addWidget(self.sl_target, 0, 1)
        param_grid.addWidget(self.lbl_target, 0, 2)

        # PWM 下限（滑块，硬卡 20）
        param_grid.addWidget(QLabel("PWM 下限"), 1, 0)
        self.sl_pwm_min = QSlider(Qt.Orientation.Horizontal)
        self.sl_pwm_min.setRange(PWM_HARM_FLOOR, 50)
        self.sl_pwm_min.setValue(cfg.pwm_min)
        self.lbl_pwm_min = QLabel(f"{cfg.pwm_min} %")
        self.sl_pwm_min.valueChanged.connect(
            lambda v: self.lbl_pwm_min.setText(f"{v} %"))
        param_grid.addWidget(self.sl_pwm_min, 1, 1)
        param_grid.addWidget(self.lbl_pwm_min, 1, 2)

        # PWM 上限
        param_grid.addWidget(QLabel("PWM 上限"), 2, 0)
        self.sl_pwm_max = QSlider(Qt.Orientation.Horizontal)
        self.sl_pwm_max.setRange(50, 100)
        self.sl_pwm_max.setValue(cfg.pwm_max)
        self.lbl_pwm_max = QLabel(f"{cfg.pwm_max} %")
        self.sl_pwm_max.valueChanged.connect(
            lambda v: self.lbl_pwm_max.setText(f"{v} %"))
        param_grid.addWidget(self.sl_pwm_max, 2, 1)
        param_grid.addWidget(self.lbl_pwm_max, 2, 2)

        # PID 参数（DoubleSpinBox）
        self.sp_kp = self._add_spin(param_grid, 3, "Kp", cfg.kp, 0.0, 10.0, 0.1)
        self.sp_ki = self._add_spin(param_grid, 4, "Ki", cfg.ki, 0.0, 2.0, 0.05)
        self.sp_kd = self._add_spin(param_grid, 5, "Kd", cfg.kd, 0.0, 5.0, 0.1)
        self.sp_load_kf = self._add_spin(param_grid, 6, "负载前馈", cfg.load_kf, 0.0, 1.0, 0.05)
        self.sp_delta_t_k = self._add_spin(param_grid, 7, "温差前馈", cfg.delta_t_k, 0.0, 5.0, 0.1)

        self.btn_apply = QPushButton("应用参数")
        param_grid.addWidget(self.btn_apply, 8, 1)
        mid.addWidget(param_group)
        root.addLayout(mid)

        # ── 底部：日志 ──
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        self.log.setStyleSheet("font-family: monospace; font-size: 11px;")
        root.addWidget(self.log)

        # ── 信号连接 ──
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_apply.clicked.connect(self.on_apply)
        self.btn_config.clicked.connect(self.on_edit_config)

        # ── 系统托盘 ──
        self._setup_tray()

    @staticmethod
    def _add_spin(grid: QGridLayout, row: int, name: str,
                  val: float, lo: float, hi: float, step: float) -> QDoubleSpinBox:
        grid.addWidget(QLabel(name), row, 0)
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setDecimals(2)
        sp.setValue(val)
        grid.addWidget(sp, row, 1)
        return sp

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("Dell 风扇温控")
        # 用应用窗口图标；没有图标文件就用文字
        if self.windowIcon().isNull():
            self.tray.setIcon(self.style().standardIcon(
                QStyle.StandardPixmap.SP_ComputerIcon))
        else:
            self.tray.setIcon(self.windowIcon())

        menu = QMenu()
        act_show = QAction("显示", self)
        act_show.triggered.connect(self.showNormal)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_show)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.showNormal() if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)

    # ── 按钮回调 ──
    def on_start(self) -> None:
        # 启动前把 UI 当前参数写回 cfg，这样启动即生效
        self.cfg.target_cpu_temp = float(self.sl_target.value())
        self.cfg.pwm_min = self.sl_pwm_min.value()
        self.cfg.pwm_max = self.sl_pwm_max.value()
        self.cfg.kp = self.sp_kp.value()
        self.cfg.ki = self.sp_ki.value()
        self.cfg.kd = self.sp_kd.value()
        self.cfg.load_kf = self.sp_load_kf.value()
        self.cfg.delta_t_k = self.sp_delta_t_k.value()

        # 密码未设（环境变量没设）→ 弹连接对话框让用户输入，不落盘
        if self.cfg.password is None:
            dlg = ConnectDialog(self.cfg, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            self.cfg.ip = dlg.ed_ip.text()
            self.cfg.user = dlg.ed_user.text()
            self.cfg.password = dlg.ed_password.text()
            if not self.cfg.password:
                self.append_log("密码不能为空")
                return

        self.worker = ControllerWorker(self.cfg, quiet_mode=self.cb_quiet.isChecked())
        self.worker.data_changed.connect(self.on_data)
        self.worker.log_message.connect(self.append_log)
        self.worker.error_occurred.connect(self.on_error)
        self.worker.status_changed.connect(self.on_status)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.tray.show()
        mode = "静音" if self.cb_quiet.isChecked() else "PID"
        self.append_log(f"正在连接 BMC…（{mode}模式）")

    def on_stop(self) -> None:
        if self.worker is None:
            return
        self.append_log("正在停止…")
        self.worker.requestInterruption()
        self.worker.wait(10000)  # 最多等 10s（shutdown 要设 PWM）
        self.worker = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_apply(self) -> None:
        if self.worker is None or self.worker.controller is None:
            self.append_log("（未运行，参数将在下次启动时生效）")
            return
        params = {
            "target": float(self.sl_target.value()),
            "pwm_min": self.sl_pwm_min.value(),
            "pwm_max": self.sl_pwm_max.value(),
            "kp": self.sp_kp.value(),
            "ki": self.sp_ki.value(),
            "kd": self.sp_kd.value(),
            "load_kf": self.sp_load_kf.value(),
            "delta_t_k": self.sp_delta_t_k.value(),
        }
        self.worker.apply_params(params)
        self.append_log(
            f"参数已应用: 目标={params['target']:.0f}℃ "
            f"Kp={params['kp']:.2f} Ki={params['ki']:.2f} Kd={params['kd']:.2f} "
            f"FF负载={params['load_kf']:.2f} FF温差={params['delta_t_k']:.2f} "
            f"PWM[{params['pwm_min']}%,{params['pwm_max']}%]")

    def on_edit_config(self) -> None:
        """打开配置编辑对话框，保存后写回 ini 并刷新 UI。"""
        dlg = ConfigDialog(self.config_path, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        settings = dlg.get_settings()
        try:
            save_config(self.config_path, settings)
        except ConfigError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        # 重新加载并刷新 UI
        try:
            self.cfg = load_config(self.config_path)
        except ConfigError as e:
            QMessageBox.warning(self, "重新加载失败", str(e))
            return
        self._refresh_ui_from_cfg()
        self.append_log(f"配置已保存到 {self.config_path}")
        if self.worker is not None and self.worker.isRunning():
            self.append_log("⚠ 连接参数改动需停止后重新启动才生效")

    def _refresh_ui_from_cfg(self) -> None:
        """从 cfg 刷新所有参数控件的值。"""
        self.sl_target.setValue(int(self.cfg.target_cpu_temp))
        self.sl_pwm_min.setValue(self.cfg.pwm_min)
        self.sl_pwm_max.setValue(self.cfg.pwm_max)
        self.sp_kp.setValue(self.cfg.kp)
        self.sp_ki.setValue(self.cfg.ki)
        self.sp_kd.setValue(self.cfg.kd)
        self.sp_load_kf.setValue(self.cfg.load_kf)
        self.sp_delta_t_k.setValue(self.cfg.delta_t_k)

    # ── 数据刷新 ──
    def on_data(self, r: StrategyResult) -> None:
        def fmt(v, unit=""):
            if v is None:
                return "—"
            return f"{v:.1f}{unit}"

        self.data_labels["cpu_temp"].setText(fmt(r.cpu_temp))
        self.data_labels["inlet_temp"].setText(fmt(r.inlet_temp))
        self.data_labels["exhaust_temp"].setText(fmt(r.exhaust_temp))
        self.data_labels["delta_t"].setText(fmt(r.delta_t))
        self.data_labels["cpu_usage"].setText(fmt(r.cpu_usage))
        self.data_labels["power"].setText(fmt(r.power))
        self.data_labels["pwm"].setText(
            "紧急回退" if r.emergency else str(r.pwm))
        self.data_labels["fan_rpm"].setText(fmt(r.fan_rpm))
        self.data_labels["pid"].setText(
            f"{r.pid_p:.1f} / {r.pid_i:.1f} / {r.pid_d:.1f}")
        self.data_labels["feedforward"].setText(fmt(r.feedforward))

        if r.emergency:
            self.append_log(f"⚠ 紧急回退: {r.reason}")
        elif r.reason:
            self.append_log(f"  {r.reason}")

    def on_error(self, msg: str) -> None:
        self.append_log(f"❌ {msg}")

    def on_status(self, s: str) -> None:
        self.lbl_status.setText(s)
        if s == "运行中":
            self.lbl_status.setStyleSheet("color: #2a2; font-weight: bold;")
        elif s == "已停止":
            self.lbl_status.setStyleSheet("color: #888; font-weight: bold;")

    def append_log(self, msg: str) -> None:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")

    # ── 窗口事件 ──
    def closeEvent(self, event):
        """关窗口 = 最小化到托盘，不退出。"""
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
            self.hide()
            self.tray.showMessage("Dell 风扇温控", "已最小化到系统托盘，双击图标恢复")
        else:
            event.accept()

    def _quit(self) -> None:
        self.on_stop()
        self.tray.hide()
        QApplication.quit()


# ─── 入口 ───────────────────────────────────────────────────
def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Dell 风扇温控")
    app.setQuitOnLastWindowClosed(False)

    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        cfg = load_config(config_path, allow_missing_password=True)
    except ConfigError as e:
        QMessageBox.critical(None, "配置错误", str(e))
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(cfg.log_file, encoding="utf-8")],
    )

    if config_path is None:
        from .config import DEFAULT_CONFIG_PATH
        config_path = DEFAULT_CONFIG_PATH

    win = MainWindow(cfg, config_path)
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
