"""Install or safely reuse StudioWright on Ubuntu.

The installer never imports existing account data into its payload. Reuse is
read-only. A fresh install refuses every pre-existing application/state path,
unit and listening port before writing anything.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import runpy
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

PACKAGE = Path(__file__).resolve().parent
UNITS = ('studiowright-console.service', 'creative-unified-workbench.service',
         'creative-unified-api.service')
PORTS = (18888, 18920, 18922)
DESKTOP_UNITS = ('studiowright-flow-browser-recovery.service', 'studiowright-boot-readiness.service')
FOCUS_EXTENSION = 'flow-window-focus@studiowright.local'
SYSTEM_PACKAGES = ('python3.12-venv', 'python3-gi', 'xvfb', 'xauth', 'xclip', 'wl-clipboard',
                   'libglib2.0-bin', 'xdg-desktop-portal-gnome', 'openssh-server')


def command(*args, check=True, timeout=30, env=None):
    return subprocess.run([str(x) for x in args], check=check, timeout=timeout,
                          capture_output=True, text=True, env=env)


def unit_state(unit):
    try:
        p = command('systemctl', '--user', 'is-active', unit, check=False, timeout=5)
        return p.stdout.strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def health(port, expected):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open('http://127.0.0.1:%d/health' % port, timeout=3) as response:
            value = json.load(response)
        return isinstance(value, dict) and value.get('application') == expected
    except (OSError, ValueError, URLError):
        return False


def workbench_health():
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open('http://127.0.0.1:18920/api/health', timeout=3) as response:
            value = json.load(response)
        return value.get('application') == 'unified-workbench' and value.get('deployment') == 'ubuntu'
    except (OSError, ValueError, URLError):
        return False


def status():
    return {
        'services': {u: unit_state(u) for u in UNITS},
        # The legacy Flow health endpoint creates a web session when accessed
        # locally. Read the service state and listening port instead.
        'flow': unit_state('studiowright-console.service') == 'active' and occupied(18888),
        'workbench': workbench_health(),
        'api': health(18922, 'studiowright-unified-api'),
    }


def verify_payload():
    manifest_path = PACKAGE / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not manifest:
        raise RuntimeError('安装包清单不正确。')
    for name, expected in manifest.items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or not isinstance(expected, str):
            raise RuntimeError('安装包清单含不安全路径。')
        path = PACKAGE / rel
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('安装包文件缺失：' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('安装包文件校验失败：' + name)


def check_reuse():
    info = status()
    if all(info[name] for name in ('flow', 'workbench', 'api')):
        print('已识别现有服务，直接复用；未更改程序、账号、素材或记录。')
        return True
    print('现有服务尚未全部就绪，未进行安装或修改。')
    print(json.dumps(info, ensure_ascii=False))
    return False


def occupied(port):
    with socket.socket() as sock:
        sock.settimeout(.3)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def paths(home):
    share = home / '.local/share'
    units = home / '.config/systemd/user'
    return {'flow': share / 'studiowright-evaluation',
            'gemini': share / 'gemini-hanaoka-trial',
            'workbench': share / 'creative-unified-workbench',
            'state': home / '.local/state/creative-unified-workbench',
            'extension': share / 'gnome-shell/extensions' / FOCUS_EXTENSION,
            'units': units}


def preflight_new(home):
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise RuntimeError('此安装包目前支持 Ubuntu x86-64。')
    release = platform.freedesktop_os_release()
    if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04' or os.geteuid() == 0:
        raise RuntimeError('请以普通桌面用户在 Ubuntu 24.04 上安装。')
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('当前安装包需要 Python 3.12 和 python3.12-venv。')
    needed = ('systemctl', 'dpkg-deb', 'Xvfb', 'xauth', 'gsettings', 'gdbus', 'gnome-shell', 'wl-copy')
    missing = [name for name in needed if not shutil.which(name)]
    if missing:
        raise RuntimeError('缺少系统组件：' + '、'.join(missing))
    if not (shutil.which('google-chrome') or shutil.which('google-chrome-stable') or shutil.which('chromium')):
        raise RuntimeError('请先安装 Google Chrome 或 Chromium，再运行安装。')
    try:
        import ensurepip  # noqa: F401
        if command('systemctl', '--user', 'show-environment', check=False).returncode:
            raise RuntimeError('用户服务管理器不可用，请以普通桌面用户登录 Ubuntu。')
    except ImportError:
        raise RuntimeError('缺少 Python 虚拟环境组件，请安装 python3.12-venv。') from None
    roots = paths(home)
    if not command('gnome-shell', '--version').stdout.strip().split()[-1].startswith('46.'):
        raise RuntimeError('此安装包的桌面组件适用于 Ubuntu 24.04 的 GNOME 46。')
    conflict = [str(roots[k]) for k in ('flow', 'gemini', 'workbench', 'state', 'extension')
                if roots[k].exists() or roots[k].is_symlink()]
    conflict += [str(roots['units'] / u) for u in (*UNITS, *DESKTOP_UNITS)
                 if (roots['units'] / u).exists() or (roots['units'] / u).is_symlink()]
    conflict += ['端口 ' + str(p) for p in PORTS if occupied(p)]
    for u in (*UNITS, *DESKTOP_UNITS):
        if command('systemctl', '--user', 'cat', u, check=False).returncode == 0:
            conflict.append('服务 ' + u)
    if conflict:
        raise RuntimeError('检测到现有程序、数据或端口，已停止以保护现有内容：' + '、'.join(conflict))
    for name in ('flow',):
        wheel_dir = PACKAGE / 'wheels' / name
        if not wheel_dir.is_dir() or not any(wheel_dir.glob('*.whl')):
            raise RuntimeError('安装包缺少离线依赖：' + name)
    return roots


def copy_new(source, target):
    if target.exists() or target.is_symlink():
        raise RuntimeError('目标已存在，未覆盖：' + str(target))
    shutil.copytree(source, target, symlinks=False)


def unit_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def write_unit(unit_dir, name, program, workdir, environment=(), after='network.target',
               target='default.target', oneshot=False):
    directory = str(workdir)
    if not Path(directory).is_absolute() or any(c in directory for c in ('\r', '\n')):
        raise ValueError('invalid systemd working directory')
    parts = ['[Unit]', 'Description=StudioWright ' + name, 'After=' + after]
    if target == 'graphical-session.target':
        parts.append('PartOf=graphical-session.target')
    parts.extend(['', '[Service]', 'Type=oneshot' if oneshot else 'Type=simple',
                  'WorkingDirectory=' + directory.replace('%', '%%')])
    parts.extend('Environment=' + unit_quote(k + '=' + str(v)) for k, v in environment)
    parts.append('ExecStart=' + ' '.join(unit_quote(v).replace('%', '%%') for v in program))
    parts.extend(['RemainAfterExit=yes', 'TimeoutStartSec=1500'] if oneshot else
                 ['Restart=on-failure', 'RestartSec=3'])
    parts.extend(['NoNewPrivileges=yes', 'UMask=0077', '', '[Install]', 'WantedBy=' + target, ''])
    with (unit_dir / name).open('x', encoding='utf-8') as stream:
        stream.write('\n'.join(parts))


def make_venv(python, target, requirements, wheels):
    command(python, '-m', 'venv', target, timeout=90)
    interp = target / 'bin/python'
    command(interp, '-m', 'pip', 'install', '--no-input', '--no-index',
            '--find-links', wheels, '-r', requirements, timeout=300)
    return interp


def install_desktop(roots, keep_awake):
    state = roots['state']
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    for device in ('keyboard', 'pointer'):
        runtime = state / ('desktop-' + device)
        runtime.mkdir(mode=0o700)
        shutil.copyfile(PACKAGE / 'snapshot/workbench-helpers' / (device + '_helper.py'),
                        runtime / 'helper.py')
    copy_new(PACKAGE / 'snapshot/desktop' / FOCUS_EXTENSION, roots['extension'])
    schema = 'org.gnome.shell'
    raw = command('gsettings', 'get', schema, 'enabled-extensions').stdout.strip()
    extensions = ast.literal_eval(raw.removeprefix('@as '))
    if FOCUS_EXTENSION not in extensions:
        command('gsettings', 'set', schema, 'enabled-extensions',
                json.dumps(extensions + [FOCUS_EXTENSION]))
    if keep_awake:
        command('gsettings', 'set', 'org.gnome.desktop.session', 'idle-delay', '0')
        command('gsettings', 'set', 'org.gnome.settings-daemon.plugins.power',
                'sleep-inactive-ac-type', 'nothing')


def new_install(home, keep_awake=False):
    roots = preflight_new(home)
    verify_payload()
    flow, work, units = (roots[k] for k in ('flow', 'workbench', 'units'))
    # Every protected path and port has been checked before the first write.
    flow.mkdir(mode=0o700, parents=True, exist_ok=False)
    print('正在安装 Flow 程序和运行环境……', flush=True)
    copy_new(PACKAGE / 'snapshot/flow/source', flow / 'source')
    copy_new(PACKAGE / 'snapshot/flow/console', flow / 'console')
    copy_new(PACKAGE / 'snapshot/flow/remote-tools', flow / 'remote-tools')
    for item in (flow / 'remote-tools/packages').glob('*.deb'):
        command('dpkg-deb', '-x', item, flow / 'remote-tools', timeout=30)
    for name in ('data', 'output', 'material-library', 'bin', 'console-data'):
        (flow / name).mkdir(mode=0o700)
    browser = shutil.which('google-chrome') or shutil.which('google-chrome-stable') or shutil.which('chromium')
    (flow / 'bin/google-chrome').symlink_to(browser)
    flow_python = make_venv(sys.executable, flow / 'venv',
                            PACKAGE / 'snapshot/flow/requirements-frozen.txt',
                            PACKAGE / 'wheels/flow')
    copy_new(PACKAGE / 'snapshot/workbench', work)
    units.mkdir(parents=True, exist_ok=True)
    pythonpath = flow / 'source/src'
    common = [('PYTHONPATH', os.pathsep.join(map(str, (pythonpath, work, flow / 'console')))), ('SW_ROOT', flow),
              ('PATH', str(flow / 'bin') + ':' + os.environ.get('PATH', '/usr/bin:/bin'))]
    release_file = PACKAGE / 'release.json'
    if release_file.is_file():
        release = json.loads(release_file.read_text(encoding='utf-8'))
        if re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', release.get('version') or ''):
            common.append(('SW_SOURCE_VERSION', release['version']))
        if re.fullmatch(r'[0-9a-f]{40}', release.get('commit') or ''):
            common.append(('SW_SOURCE_COMMIT', release['commit']))
    write_unit(units, UNITS[0], [flow_python, flow / 'console/server.py'], flow / 'console', common)
    write_unit(units, UNITS[1], [flow_python, '-B', work / 'server.py', '--port', '18920',
                                '--flow-port', '18888', '--deployment', 'ubuntu'],
               work, common, 'network.target studiowright-console.service')
    write_unit(units, UNITS[2], [flow_python, '-B', work / 'unified_api.py'], work, common,
               'network.target creative-unified-workbench.service')
    install_desktop(roots, keep_awake)
    write_unit(units, DESKTOP_UNITS[0], [flow_python, '-B', work / 'flow_browser_recovery.py'],
               work, common, target='graphical-session.target')
    write_unit(units, DESKTOP_UNITS[1], [flow_python, '-B', work / 'boot_flow_readiness.py'],
               work, common, 'creative-unified-workbench.service ' + DESKTOP_UNITS[0],
               target='graphical-session.target', oneshot=True)
    command('systemctl', '--user', 'daemon-reload')
    for unit in UNITS:
        command('systemctl', '--user', 'enable', '--now', unit, timeout=45)
    command('systemctl', '--user', 'enable', *DESKTOP_UNITS)
    if unit_state('graphical-session.target') == 'active':
        command('systemctl', '--user', 'start', '--no-block', *DESKTOP_UNITS)
    for _ in range(30):
        info = status()
        if all(info[n] for n in ('flow', 'workbench', 'api')):
            print('服务已启动。请重新登录 GNOME 以加载桌面扩展，并按 Ubuntu 提示授权桌面输入。')
            print('请接入账号；Gemini 需配置官方 Antigravity CLI 并单独授权。')
            return
        time.sleep(1)
    raise RuntimeError('程序已安装，但服务尚未全部就绪，请检查服务状态；安装数据已保留。')


def main():
    parser = argparse.ArgumentParser(description='创作工作台 Ubuntu 服务端安装')
    parser.add_argument('--mode', choices=('reuse', 'new', 'inspect', 'system-deps', 'input-setup', 'input-check'))
    parser.add_argument('--keep-awake-on-ac', action='store_true',
                        help='全新安装时关闭桌面空闲计时和接通电源时的自动休眠')
    parser.add_argument('--enable-discovery', action='store_true', help='为现有服务启用局域网发现')
    parser.add_argument('--discovery-ssh-port', type=int, help='局域网发现使用的 SSH 端口')
    args = parser.parse_args()
    if not args.mode:
        print('1. 识别并复用已有服务（不会修改现有内容）')
        print('2. 在全新环境安装')
        print('3. 复用已有服务并启用局域网发现')
        choice = input('请选择 1、2 或 3：').strip()
        args.mode = {'1': 'reuse', '2': 'new', '3': 'reuse'}.get(choice)
        if choice == '3':
            args.enable_discovery = True
        if not args.mode:
            raise SystemExit('未执行安装。')
    verify_payload()
    if args.keep_awake_on_ac and args.mode != 'new':
        parser.error('--keep-awake-on-ac 仅用于 --mode new')
    if args.mode == 'system-deps':
        release = platform.freedesktop_os_release()
        if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04':
            raise RuntimeError('系统依赖模式适用于 Ubuntu 24.04。')
        subprocess.run(['sudo', 'apt-get', 'update'], check=True)
        subprocess.run(['sudo', 'apt-get', 'install', *SYSTEM_PACKAGES], check=True)
    elif args.mode in ('input-setup', 'input-check'):
        if os.geteuid() == 0:
            raise RuntimeError('请以运行工作台的普通桌面用户执行此模式。')
        release = platform.freedesktop_os_release()
        if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04':
            raise RuntimeError('硬件输入安装和验证适用于 Ubuntu 24.04。')
        if args.mode == 'input-setup':
            subprocess.run(['sudo', 'bash', str(PACKAGE / 'hid-input/setup.sh')], check=True)
            print('硬件输入规则已安装。重新登录 Ubuntu 桌面后运行 --mode input-check。')
        else:
            module = PACKAGE / 'snapshot/workbench/uinput_hid.py'
            subprocess.run([sys.executable, '-B', str(PACKAGE / 'hid-input/verify.py'), str(module)], check=True)
    elif args.mode == 'inspect':
        print(json.dumps(status(), ensure_ascii=False))
    elif args.mode == 'reuse':
        if not check_reuse():
            raise SystemExit(2)
        if args.enable_discovery:
            enable_discovery(args.discovery_ssh_port)
    else:
        new_install(Path.home(), keep_awake=args.keep_awake_on_ac)
        enable_discovery(args.discovery_ssh_port)


def enable_discovery(ssh_port=None):
    module = runpy.run_path(str(PACKAGE / 'discovery_install.py'))
    result = module['install'](PACKAGE / 'discovery.py', ssh_port)
    print('局域网发现已开启；Windows 桌面版可自动查找。')
    return result


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('安装未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1)
