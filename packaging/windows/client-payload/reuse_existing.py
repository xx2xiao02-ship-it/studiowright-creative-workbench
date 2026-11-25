"""Open the Ubuntu workbench through its own loopback-only SSH tunnel."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import webbrowser


APP_DIR = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'StudioWrightDesktop'
PORT = 18920
FLOW_PORT = 18888
API_PORT = 18922
CHROME_MANAGER = APP_DIR / 'manager_bridge.py'
LEGACY_HOST_KEY_ALIAS = 'xiao-system-product-name.local'


def target_connection_path():
    configured = os.environ.get('STUDIOWRIGHT_WINDOWS_CONNECTION_FILE')
    if configured:
        return Path(configured).expanduser()
    saved = PROFILE_DIR / 'ubuntu-connection.json'
    if saved.is_file():
        return saved
    # Existing workstations can still read their previous address during migration.
    return Path(r'E:\google\unified-workbench\ubuntu-connection.json')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_target(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"host"}:
        raise ValueError("连接信息格式不正确。")
    address = ipaddress.ip_address(data["host"])
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved:
        raise ValueError("Ubuntu 内网地址无效。")
    return str(address)


def health():
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", PORT)) != 0:
            return False
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(f"http://127.0.0.1:{PORT}/api/health", timeout=3) as response:
            data = json.loads(response.read(8192))
        if data.get("application") == "unified-workbench" and data.get("deployment") == "ubuntu":
            return True
    except (URLError, TimeoutError, OSError, ValueError, AttributeError):
        pass
    raise RuntimeError(f"本机端口 {PORT} 被其他程序使用，或 Ubuntu 算力中心暂时不可用。")


def ensure_chrome_manager():
    """Start the existing Windows profile manager without touching Flow service."""
    if os.name != 'nt':
        return False
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def manager_healthy():
        try:
            with opener.open('http://127.0.0.1:18890/health', timeout=2) as response:
                return json.loads(response.read(4096)).get('application') == 'studiowright-chrome-manager'
        except (URLError, TimeoutError, OSError, ValueError, AttributeError):
            return False

    if manager_healthy():
        return True
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', 18890)) == 0:
            return False  # Occupied by another program: never replace it.
    if not CHROME_MANAGER.is_file() or CHROME_MANAGER.is_symlink():
        return False
    executable = Path(sys.executable)
    windowless = executable.with_name('pythonw.exe')
    if windowless.is_file():
        executable = windowless
    subprocess.Popen([str(executable), str(CHROME_MANAGER)], cwd=str(CHROME_MANAGER.parent),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(20):
        if manager_healthy():
            return True
        time.sleep(.1)
    return False


def ssh_command(target, ssh_path=None, config_path=None, port=PORT):
    native_ssh = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/OpenSSH/ssh.exe'
    executable = ssh_path or (str(native_ssh) if os.name == 'nt' and native_ssh.is_file() else shutil.which("ssh"))
    configured = config_path or os.environ.get('STUDIOWRIGHT_SSH_CONFIG')
    config = Path(configured or Path.home() / '.ssh/flow2api_ubuntu.conf').expanduser()
    if not executable or not config.is_file():
        raise RuntimeError("找不到已有的 SSH 连接环境。")
    if port not in (PORT, FLOW_PORT, API_PORT):
        raise ValueError("不支持的本机端口。")
    legacy_identity = [] if configured else ['-o', f'HostKeyAlias={LEGACY_HOST_KEY_ALIAS}']
    return [executable, "-N", "-T", "-F", str(config), "-o", f"HostName={target}", *legacy_identity,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-L", f"127.0.0.1:{port}:127.0.0.1:{port}", "flow-ubuntu"]


def flow_console_health():
    """Check either old or new Flow without creating a web login session."""
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', FLOW_PORT)) != 0:
            return False
    request = Request(f'http://127.0.0.1:{FLOW_PORT}/health', headers={
        'Authorization':'Bearer sw-readonly-probe', 'X-Studiowright-Probe':'1'})
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=3) as response:
            data = json.loads(response.read(8192))
        if data.get('application') == 'studiowright-console' and data.get('test') is False:
            return True
    except HTTPError as error:
        try:
            if error.code == 401 and json.load(error).get('error') == 'API Key 无效或已撤销。':
                return True
        except (OSError, ValueError):
            pass
    except (URLError, TimeoutError, OSError, ValueError, AttributeError):
        pass
    raise RuntimeError(f'本机端口 {FLOW_PORT} 已被占用或 Flow 后台暂不可用。')


def ensure_flow_console():
    """Keep the existing Flow service intact and expose it only on local loopback."""
    if flow_console_health():
        return True
    target = read_target(target_connection_path())
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(ssh_command(target, port=FLOW_PORT), **options)
    for _ in range(50):
        if process.poll() is not None:
            break
        try:
            if flow_console_health():
                return True
        except RuntimeError:
            pass
        time.sleep(.2)
    if process.poll() is None:
        process.terminate()
    return False


def ensure_unified_api():
    """Forward only the authenticated API on an independent local port."""
    def api_health():
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1', API_PORT)) != 0:
                return False
        try:
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(f'http://127.0.0.1:{API_PORT}/health', timeout=3) as response:
                return json.loads(response.read(4096)).get('application') == 'studiowright-unified-api'
        except (URLError, TimeoutError, OSError, ValueError, AttributeError):
            return False
    if api_health():
        return True
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', API_PORT)) == 0:
            return False
    target = read_target(target_connection_path())
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(ssh_command(target, port=API_PORT), **options)
    for _ in range(50):
        if process.poll() is not None:
            break
        if api_health():
            return True
        time.sleep(.2)
    if process.poll() is None:
        process.terminate()
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    process = None
    if not health():
        target = read_target(target_connection_path())
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(ssh_command(target), **options)
        ready = False
        for _ in range(50):
            if process.poll() is not None:
                break
            try:
                if health():
                    ready = True
                    break
            except RuntimeError:
                pass
            time.sleep(0.2)
        if not ready:
            if process.poll() is None:
                process.terminate()
            raise RuntimeError("无法连接 Ubuntu 算力中心，请检查 SSH 连接和 Ubuntu 服务。")
    try:
        if not ensure_flow_console():
            print('Flow 后台入口暂不可用；算力中心仍可继续使用。', file=sys.stderr)
    except (OSError, RuntimeError, ValueError, KeyError):
        print('Flow 后台入口暂不可用；算力中心仍可继续使用。', file=sys.stderr)
    try:
        if not ensure_unified_api():
            print('统一 API 入口暂不可用；算力中心仍可继续使用。', file=sys.stderr)
    except (OSError, RuntimeError, ValueError, KeyError):
        print('统一 API 入口暂不可用；算力中心仍可继续使用。', file=sys.stderr)
    if os.name == 'nt' and not ensure_chrome_manager():
        print('本机 Chrome 账号管理暂时无法启动；算力中心仍可继续使用。', file=sys.stderr)
    if args.no_browser:
        if process is not None:
            print(f"STARTED_PID={process.pid}")
    elif not webbrowser.open(f"http://127.0.0.1:{PORT}/"):
        print(f"请在本机浏览器打开 http://127.0.0.1:{PORT}/")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)
