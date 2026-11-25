"""Check the packaged HID helper without sending mouse or keyboard events."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def verify(module):
    with tempfile.TemporaryDirectory(prefix='sw-hid-', dir='/tmp') as directory:
        runtime = Path(directory)
        with subprocess.Popen([sys.executable, '-B', str(module), directory],
                              stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as helper:
            try:
                deadline = time.monotonic() + 5
                status_path = runtime / 'status.json'
                while not status_path.is_file() and helper.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not status_path.is_file():
                    raise RuntimeError('输入助手未返回就绪状态。')
                report = json.loads(status_path.read_text(encoding='utf-8'))
                if report.get('state') != 'ready' or report.get('pid') != helper.pid:
                    raise RuntimeError('输入助手启动失败：' + str(report.get('reason') or '状态不可用'))
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(2)
                    connection.connect(str(runtime / 'hid.sock'))
                    connection.sendall(b'{"action":"status"}\n')
                    with connection.makefile('rb') as response:
                        if json.loads(response.readline(256)).get('ok') is not True:
                            raise RuntimeError('输入助手未通过连接验证。')
            finally:
                if helper.poll() is None:
                    helper.terminate()
                    try:
                        helper.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        helper.kill()
                        helper.wait()
    print('已创建虚拟鼠标和键盘，输入助手连接通过。')


if __name__ == '__main__':
    try:
        verify(Path(sys.argv[1]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'硬件输入验证未通过：{error}', file=sys.stderr)
        raise SystemExit(1)
