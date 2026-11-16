"""启动 wiki_kb_service：python run.py [--host 0.0.0.0] [--port 13105] [--force]"""
import argparse
import socket
import subprocess
import sys
import time

import uvicorn

from app import config


def _pid_on_port(port: int) -> int | None:
    """从 netstat 找出占用该端口的进程 PID（Windows）。"""
    try:
        # 中文 Windows 的 netstat 输出是 GBK/ANSI，这里按字节拿回再容错解码（只关心 ASCII 字段）
        raw = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10).stdout
    except Exception:
        return None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        # Proto  LocalAddress  ForeignAddress  State  PID
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def _check_port(host: str, port: int, force: bool) -> None:
    """启动前探测端口；被占用时给出清晰提示，--force 可结束占用进程。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            pid = _pid_on_port(port)
            if force and pid:
                print(f"[run] 端口 {port} 被 PID {pid} 占用，--force 结束该进程…")
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=False)
                time.sleep(1.5)
                return
            print(
                f"[run] 启动失败：端口 {port} 已被占用（PID {pid}）。\n"
                f"  多半是已有一个 wiki_kb_service 实例在运行——浏览器能打开 http://127.0.0.1:{port} 就说明服务已就绪，无需再启动。\n"
                f"  如确实要重启，任选其一：\n"
                f"    1) python run.py --force          # 自动结束占用进程后启动\n"
                f"    2) taskkill /F /PID {pid}        # 手动结束旧进程，再 python run.py"
            )
            sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--force", action="store_true", help="若端口被占用，自动结束占用进程后启动")
    args = parser.parse_args()
    _check_port(args.host, args.port, args.force)
    lim = config.describe_limits()
    print(
        f"[run] 抽取时间限制：单次请求超时 {lim['request_timeout']}s，重试 {lim['max_retries']} 次，"
        f"并发 {lim['num_workers']} → 单 chunk 最坏 {lim['per_chunk_worst_sec'] // 60} 分钟"
    )
    print(f"[run] 配置来源 {config.BASE_DIR / '.env'}（改完必须重启本服务才生效）")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)
