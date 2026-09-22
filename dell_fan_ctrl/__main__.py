"""入口：加载配置、装信号处理器、优雅退出。

用法：python -m dell_fan_ctrl [config.json]
不传参数则用默认 ~/dell_fan_ctrl/config.json。
退出时（Ctrl+C / SIGTERM）先交还 iDRAC 自动控制，避免风扇锁手动。
"""
import sys
import signal
import logging

from .config import load as load_config, ConfigError
from .controller import Controller


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(cfg.log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    ctrl = Controller(cfg)

    # 信号 → 抛 KeyboardInterrupt，由 finally 统一走 shutdown
    def _on_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        ctrl.run()
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.shutdown()


if __name__ == "__main__":
    main()
