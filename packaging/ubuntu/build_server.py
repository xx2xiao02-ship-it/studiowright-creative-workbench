"""Create an Ubuntu self-extracting installer from the captured live sources."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

BASE = Path(__file__).resolve().parent
DIST = BASE / 'dist'
OLD_ID = 'kbnibljeinmjmmpholicjkaiehmilbbk'
SNAPSHOT_MANIFEST = 'snapshot-manifest.json'


def snapshot_entry(root, name):
    relative = PurePosixPath(name)
    if (not relative.parts or relative.is_absolute() or '..' in relative.parts
            or relative.as_posix() != name or '\\' in name or ':' in name
            or name == SNAPSHOT_MANIFEST):
        raise ValueError(f'无效的快照路径：{name}')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f'快照文件不能是符号链接：{name}')
    if not path.is_file():
        raise ValueError(f'快照文件不存在：{name}')
    return path


def tracked_snapshot_names(root):
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--', '.'])
    return {name for name in tracked.decode('utf-8').split('\x00') if name}


def verify_snapshot(root, destination=None):
    manifest_path = root / SNAPSHOT_MANIFEST
    if root.is_symlink() or manifest_path.is_symlink():
        raise ValueError('快照目录和清单不能是符号链接。')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('快照清单为空或格式无效。')
    if any((path / '.git').exists() for path in (root, *root.parents)):
        tracked = tracked_snapshot_names(root)
        changed = (tracked - {SNAPSHOT_MANIFEST}).symmetric_difference(manifest)
        if SNAPSHOT_MANIFEST in tracked and changed:
            raise ValueError('快照清单与 Git 跟踪文件不一致：' + '、'.join(sorted(changed))
                             + '。确认源码后用 --refresh-snapshot 更新清单。')
    for name, expected in manifest.items():
        source = snapshot_entry(root, name)
        data = source.read_bytes()
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError(f'快照文件指纹格式错误：{name}')
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'快照文件与清单不符：{name}。确认源码后用 --refresh-snapshot 更新清单。')
        if destination is not None:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            shutil.copymode(source, target)
    if destination is not None:
        shutil.copyfile(manifest_path, destination / SNAPSHOT_MANIFEST)
    return len(manifest)


def refresh_snapshot(root):
    if root.is_symlink() or (root / SNAPSHOT_MANIFEST).is_symlink():
        raise ValueError('快照目录和清单不能是符号链接。')
    names = tracked_snapshot_names(root) - {SNAPSHOT_MANIFEST}
    if not names:
        raise ValueError('此目录没有 Git 跟踪的快照文件。')
    manifest = {
        name: hashlib.sha256(snapshot_entry(root, name).read_bytes()).hexdigest()
        for name in sorted(names)
    }
    (root / SNAPSHOT_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
    return len(manifest)


def replace_exact(path, before, after, count, already=()):
    text = path.read_text(encoding='utf-8')
    if (text.count(before) == 0 and text.count(after) >= count) or any(marker in text for marker in already):
        return
    if text.count(before) != count:
        raise RuntimeError('程序版本已变化，停止打包：' + str(path.name))
    path.write_text(text.replace(before, after), encoding='utf-8')


def build(STAGE, archive, output):
    extension = json.loads((BASE / 'client-extension.json').read_text(encoding='utf-8'))['id']
    if len(extension) != 32 or any(c not in 'abcdefghijklmnop' for c in extension):
        raise RuntimeError('插件身份格式不正确。')
    STAGE.mkdir()
    shutil.copyfile(BASE / 'server/installer.py', STAGE / 'installer.py')
    shutil.copyfile(BASE / 'server/discovery.py', STAGE / 'discovery.py')
    shutil.copyfile(BASE / 'server/discovery_install.py', STAGE / 'discovery_install.py')
    verify_snapshot(BASE / 'snapshot', STAGE / 'snapshot')
    shutil.copytree(BASE / 'wheelhouse-linux', STAGE / 'wheels')
    server = STAGE / 'snapshot/flow/console/server.py'
    app = STAGE / 'snapshot/flow/console/static/app.js'
    replace_exact(server, OLD_ID, extension, 2)
    replace_exact(app, OLD_ID, extension, 1)
    replace_exact(app, 'Flow2API 插件', 'StudioWright 账号助手', 4)
    replace_exact(app, '请安装或重新加载 1.4.2', '请安装或重新加载账号助手', 1)
    replace_exact(app, "if(['succeeded','running'].includes(job.status))return;",
                  "if(job.status!=='waiting')return;", 1,
                  already=("if(job.status!=='waiting'){",))
    replace_exact(app, '未安装或旧版插件，请打开此用户的扩展管理页，安装／重新加载 1.4.2；完成后这里会自动继续。',
                  '请确认已在此用户的扩展管理页安装并启用账号助手。', 1,
                  already=('如尚未安装插件，请在此 Chrome 用户下安装或重新加载插件。连接成功后会自动继续。',))
    retry = ("let ready=false;for(let i=0;i<75;i++){try{await pluginCommand('" + extension +
             "',{action:'sw_probe'},3000);ready=true;break;}catch(e){await report('waiting',e.message);"
             "await new Promise(r=>setTimeout(r,8000));}}\n"
             "  if(!ready)throw Error('等待插件超过 10 分钟，请安装后从本机账号管理重新同步。');")
    retry_current = ("   let ready=false;\n"
                     "   for(let i=0;i<75;i++){\n"
                     "    try{await pluginCommand('" + extension + "',{action:'sw_probe'},3000);ready=true;break;}\n"
                     "    catch(e){await report('waiting',e.message);await new Promise(resolve=>setTimeout(resolve,8000));}\n"
                     "   }\n"
                     "   if(!ready)throw Error('等待插件超过 10 分钟，请安装后从本机账号管理重新同步。');\n")
    single = ("try{await pluginCommand('" + extension + "',{action:'sw_probe'},3000);}"
              "catch(e){throw Error('插件未连接，请安装或重新加载后，在本机账号管理中手动重新发起。');}")
    app_text = app.read_text(encoding='utf-8')
    retry_before = retry if retry in app_text else retry_current if retry_current in app_text else None
    if retry_before is not None:
        replace_exact(app, retry_before, single, 1)
    replace_exact(server,
                  'return JSONResponse({"ok": True, "application": "studiowright-console", "test": test})',
                  'return JSONResponse({"ok": True, "application": "studiowright-console", "test": test, "bridge_id": "' + extension + '"})',
                  1)
    replace_exact(server,
                  '        plugin_path = request.url.path == "/api/plugin/session" or request.url.path.startswith(\n',
                  '        if request.method == "GET" and request.url.path == "/health" and request.headers.get("x-studiowright-probe") == "1":\n'
                  '            return await call_next(request)\n'
                  '        plugin_path = request.url.path == "/api/plugin/session" or request.url.path.startswith(\n', 1)
    original_manifest = STAGE / 'snapshot/snapshot-manifest.json'
    captured = json.loads(original_manifest.read_text(encoding='utf-8'))
    for changed in ('flow/console/server.py', 'flow/console/static/app.js'):
        captured[changed] = hashlib.sha256((STAGE / 'snapshot' / changed).read_bytes()).hexdigest()
    original_manifest.write_text(json.dumps(captured, indent=2, sort_keys=True), encoding='utf-8')
    notice = ('StudioWright 算力中心 Ubuntu 服务端安装包\n\n'
              'Ubuntu 24.04 桌面版（x86-64、GNOME 46）\n'
              '安装系统依赖：bash 安装包.run --mode system-deps\n'
              '复用现有服务：bash 安装包.run --mode reuse\n'
              '复用并开启局域网发现：bash 安装包.run --mode reuse --enable-discovery\n'
              '全新安装并保持接电时常开：bash 安装包.run --mode new --keep-awake-on-ac\n'
              '保留系统原有电源策略：bash 安装包.run --mode new\n'
              '仅查看状态：bash 安装包.run --mode inspect\n\n'
              '复用模式不修改现有服务或数据；启用发现会新增独立的局域网发现服务。全新安装发现已有程序、数据、服务或端口时会停止。\n'
              '含开机恢复、GNOME 窗口定位及桌面输入助手。安装后需重新登录桌面并授权输入。\n'
              'Python 依赖离线提供，系统依赖从 Ubuntu 仓库安装，Chrome 须事先安装。\n'
              '账号、密钥、素材和历史记录不在安装包中。\n')
    (STAGE / '使用说明.txt').write_text(notice, encoding='utf-8')
    manifest = {}
    for path in sorted(STAGE.rglob('*')):
        if path.is_file() and not path.is_symlink() and path.name != 'manifest.json':
            manifest[path.relative_to(STAGE).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (STAGE / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8')
    with tarfile.open(archive, 'w:gz', compresslevel=1) as tar:
        for path in sorted(STAGE.rglob('*')):
            if path.is_file() and not path.is_symlink():
                tar.add(path, arcname=path.relative_to(STAGE).as_posix(), recursive=False)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    lines = [
        '#!/bin/sh',
        'set -eu',
        'payload_line=PAYLOAD_LINE',
        'expected=' + sha,
        'actual=$(tail -n +"$payload_line" "$0" | sha256sum | cut -d " " -f 1)',
        'if [ "$actual" != "$expected" ]; then echo "安装包校验失败。" >&2; exit 1; fi',
        'scratch=$(mktemp -d "${TMPDIR:-/tmp}/studiowright-installer.XXXXXXXX")',
        'cleanup() { case "$scratch" in "${TMPDIR:-/tmp}"/studiowright-installer.*) rm -rf -- "$scratch";; *) echo "临时目录未清理" >&2;; esac; }',
        'trap cleanup EXIT HUP INT TERM',
        'tail -n +"$payload_line" "$0" | tar -xzf - -C "$scratch"',
        'if command -v python3.12 >/dev/null 2>&1; then runner=python3.12; else runner=python3; fi',
        '"$runner" -B "$scratch/installer.py" "$@"',
        'exit $?']
    header = ('\n'.join(lines).replace('PAYLOAD_LINE', str(len(lines) + 1)) + '\n').encode('utf-8')
    with output.open('xb') as stream, archive.open('rb') as payload:
        stream.write(header)
        shutil.copyfileobj(payload, stream, 1024 * 1024)
    print(json.dumps({'server_installer': str(output), 'files': len(manifest),
                      'bytes': output.stat().st_size, 'extension_id': extension}))


def main():
    parser = argparse.ArgumentParser(description='校验或打包 Ubuntu 服务端快照。')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-snapshot', action='store_true', help='校验当前快照和清单。')
    mode.add_argument('--refresh-snapshot', action='store_true', help='按 Git 跟踪文件更新快照清单。')
    args = parser.parse_args()
    if args.check_snapshot or args.refresh_snapshot:
        snapshot = BASE / 'snapshot'
        count = refresh_snapshot(snapshot) if args.refresh_snapshot else verify_snapshot(snapshot)
        print(f'快照清单包含 {count} 个文件。')
        return
    DIST.mkdir(exist_ok=True)
    version = (BASE.parents[1] / 'VERSION').read_text(encoding='utf-8').strip()
    output = DIST / f'StudioWright-Ubuntu-24.04-Setup-{version}.run'
    if output.exists():
        raise SystemExit(f'安装包已存在，未覆盖：{output}')
    with tempfile.TemporaryDirectory(prefix='studiowright-build-', dir=DIST) as directory:
        temp = Path(directory)
        build(temp / 'payload', temp / 'payload.tar.gz', output)


if __name__ == '__main__':
    main()
