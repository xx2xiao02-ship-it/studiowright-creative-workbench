"""Reconcile a verified source snapshot with one existing Ubuntu service group."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import ProxyHandler, Request, build_opener

from build_server import SNAPSHOT_MANIFEST, verify_snapshot

GROUPS = {
    'flow': ('studiowright-console',),
    'workbench': ('creative-unified-workbench', 'creative-unified-api'),
}
HEALTH = {
    'studiowright-console': (18888, '/health', 'studiowright-console'),
    'creative-unified-workbench': (18920, '/api/health', 'unified-workbench'),
    'creative-unified-api': (18922, '/health', 'studiowright-unified-api'),
}


class RollbackFailed(RuntimeError):
    """The maintenance marker must survive an unhealthy rollback."""


def paths(home):
    flow = home / '.local/share/studiowright-evaluation'
    workbench = home / '.local/share/creative-unified-workbench'
    state = home / '.local/state/creative-unified-workbench'
    return {'flow/console': flow / 'console', 'flow/source': flow / 'source', 'workbench': workbench}, state


def digest(path):
    if path.is_symlink():
        raise ValueError(f'部署路径含符号链接：{path.name}')
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def runtime_file(home, name):
    for prefix, root in paths(home)[0].items():
        if name.startswith(prefix + '/'):
            path = root / name.removeprefix(prefix + '/')
            if not path.resolve().is_relative_to(root) or not path.parent.is_dir():
                raise ValueError(f'部署位置无法确认：{name}')
            return path
    raise ValueError(f'不支持发布此文件：{name}')


def unit_file(home, unit):
    root = home / '.config/systemd/user'
    path = root / f'{unit}.service.d/70-studiowright-release.conf'
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'服务配置路径无法确认：{unit}')
    return path


def plan(home, snapshot, group):
    verify_snapshot(snapshot)
    checksums = json.loads((snapshot / SNAPSHOT_MANIFEST).read_text(encoding='utf-8'))
    prefixes = ('flow/console/', 'flow/source/') if group == 'flow' else ('workbench/',)
    changes = {}
    for name, expected in checksums.items():
        if name.startswith(prefixes):
            target = runtime_file(home, name)
            current = digest(target)
            if current != expected:
                changes[name] = {'before': current, 'after': expected}
    return changes


def active_tasks(home):
    roots, state = paths(home)
    flow = roots['flow/console'].parent
    checks = (
        (flow / 'console-data/console.db', "SELECT count(*) FROM tasks WHERE status='running'"),
        (flow / 'console-data/console.db', "SELECT count(*) FROM flow_task_execution_rows WHERE status='running'"),
        (flow / 'data/studiowright.db', "SELECT count(*) FROM jobs WHERE status='running'"),
        (state / 'real-pool.sqlite3', "SELECT count(*) FROM requests WHERE status IN ('dispatching','running')"),
        (state / 'real-pool.sqlite3', "SELECT count(*) FROM material_analyses WHERE status IN ('preparing','saving')"),
        (state / 'music-jobs.sqlite3', "SELECT count(*) FROM jobs WHERE status IN ('generating','ingesting','recovering')"),
        (state.parent / 'creative-unified-coordinator/coordinator.sqlite3',
         "SELECT count(*) FROM steps WHERE status IN ('dispatching','running')"),
        (Path(os.getenv('STUDIOWRIGHT_MUSIC_HOME') or home / 'tools/FlowMusic2API').expanduser().resolve() / 'data/flowmusic2api.db',
         'SELECT count(*) FROM request_logs WHERE status_code=102'),
    )
    count = 0
    for index, (path, query) in enumerate(checks):
        if index >= 5 and not path.is_file():
            continue
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            count += db.execute(query).fetchone()[0]
    return count


@contextmanager
def pause_dispatch(home, timeout=90):
    _, state = paths(home)
    marker = state / 'maintenance'
    with marker.open('x') as locked:
        locked.write('studiowright release\n')
        owner = os.fstat(locked.fileno())
    safe_to_resume = True
    try:
        deadline = time.monotonic() + timeout
        while True:
            if not active_tasks(home):
                time.sleep(2)
                if not active_tasks(home):
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError('仍有进行中的任务，已保留现有服务。')
            time.sleep(2)
        yield
    except RollbackFailed:
        safe_to_resume = False
        raise
    finally:
        if safe_to_resume and marker.exists() and os.path.samestat(owner, marker.lstat()):
            marker.unlink()


def systemctl(*args):
    subprocess.run(['systemctl', '--user', *args], check=True, timeout=45)


def healthy(units, commit=None):
    opener = build_opener(ProxyHandler({}))
    end = time.monotonic() + 25
    while time.monotonic() < end:
        try:
            for unit in units:
                port, route, application = HEALTH[unit]
                probe = Request(f'http://127.0.0.1:{port}{route}', headers={'X-Studiowright-Probe': '1'})
                with opener.open(probe, timeout=2) as response:
                    value = json.load(response)
                if value.get('application') != application or (commit and value.get('source_commit') != commit):
                    break
            else:
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('服务健康检查超时。')


def write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        staged = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            os.fchmod(temp.fileno(), mode)
            os.fsync(temp.fileno())
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def release_files(home, group, names):
    for name in names:
        yield name, runtime_file(home, name)
    for unit in GROUPS[group]:
        yield f'_units/{unit}', unit_file(home, unit)


def backup_sources(home, group, changes, commit):
    _, state = paths(home)
    parent = state / 'deployment-backups'
    parent.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    backup = Path(tempfile.mkdtemp(prefix=f'{stamp}-{group}-{commit[:12]}-', dir=parent))
    for name, source in release_files(home, group, changes):
        if digest(source):
            write_file(backup / name, source.read_bytes())
    write_file(backup / 'release.json', json.dumps({'group': group, 'commit': commit, 'changes': changes}, indent=2).encode())
    return backup


def restore_sources(home, group, changes, backup):
    for name, path in release_files(home, group, changes):
        previous = backup / name
        if previous.is_file():
            write_file(path, previous.read_bytes())
        else:
            path.unlink(missing_ok=True)


def backup_databases(home, group, backup):
    roots, state = paths(home)
    flow = roots['flow/console'].parent
    databases = (flow / 'console-data/console.db', flow / 'data/studiowright.db') if group == 'flow' else (
        state / 'real-pool.sqlite3', state / 'activation.sqlite3', state / 'music-jobs.sqlite3')
    destination = backup / '_state'
    destination.mkdir(mode=0o700)
    for path in databases:
        if not path.is_file():
            continue
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as source, \
                closing(sqlite3.connect(destination / path.name)) as copy:
            source.backup(copy)


def restore_service(home, group, changes, backup):
    units = GROUPS[group]
    try:
        systemctl('stop', *units)
        restore_sources(home, group, changes, backup)
        systemctl('daemon-reload')
        systemctl('start', *units)
        healthy(units)
    except Exception as exc:
        raise RollbackFailed(f'回退检查未通过，已保留维护模式及备份 {backup}') from exc


def apply(home, snapshot, group, changes, commit, version):
    units = GROUPS[group]
    healthy(units)
    with pause_dispatch(home):
        for name, expected in changes.items():
            if digest(runtime_file(home, name)) != expected['before']:
                raise RuntimeError(f'检查后程序又有修改：{name}')
        backup = backup_sources(home, group, changes, commit)
        print(f'回退目录 {backup}', flush=True)
        try:
            systemctl('stop', *units)
            backup_databases(home, group, backup)
            for name, expected in changes.items():
                data = (snapshot / name).read_bytes()
                if hashlib.sha256(data).hexdigest() != expected['after']:
                    raise RuntimeError(f'部署文件指纹变化：{name}')
                write_file(runtime_file(home, name), data)
            for unit in units:
                write_file(unit_file(home, unit),
                           f'[Service]\nEnvironment=SW_SOURCE_VERSION={version} SW_SOURCE_COMMIT={commit}\n'.encode())
            systemctl('daemon-reload')
            systemctl('start', *units)
            healthy(units, commit)
        except BaseException:
            restore_service(home, group, changes, backup)
            print('已恢复发布前的程序，并重新验证服务。', flush=True)
            raise


def rollback(home, backup):
    _, state = paths(home)
    if not backup.resolve().is_relative_to(state / 'deployment-backups'):
        raise ValueError('请选择该工作台的部署回退目录。')
    release = json.loads((backup / 'release.json').read_text(encoding='utf-8'))
    group, commit, changes = (release[key] for key in ('group', 'commit', 'changes'))
    units = GROUPS[group]
    with pause_dispatch(home):
        for name, expected in changes.items():
            if digest(runtime_file(home, name)) != expected['after']:
                raise RuntimeError(f'当前文件已更新，请重新核对后再回退：{name}')
        healthy(units, commit)
        restore_service(home, group, changes, backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('plan', 'apply'):
        child = commands.add_parser(action)
        child.add_argument('group', choices=GROUPS)
        child.add_argument('snapshot', type=Path)
        if action == 'apply':
            child.add_argument('--commit', required=True)
            child.add_argument('--version', required=True)
    commands.add_parser('rollback').add_argument('backup', type=Path)
    args = parser.parse_args()
    home = Path.home().resolve()
    if args.action == 'rollback':
        rollback(home, args.backup)
        return
    changes = plan(home, args.snapshot, args.group)
    print(json.dumps(changes, ensure_ascii=False, indent=2), flush=True)
    if args.action == 'apply':
        if not re.fullmatch('[0-9a-f]{40}', args.commit) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', args.version):
            parser.error('请提供 Git 的完整提交号和 VERSION 中的版本号。')
        apply(home, args.snapshot, args.group, changes, args.commit, args.version)


if __name__ == '__main__':
    main()
