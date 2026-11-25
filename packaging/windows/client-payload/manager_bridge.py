"""Start a private Windows profile manager without replacing an existing one."""
import importlib.util
import json
import os
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / 'flow-manager/chrome_manager.py'
LEGACY = Path(os.environ.get('STUDIOWRIGHT_LEGACY_EXTENSION') or r'D:\FLOW API\flow2api-token-updater-original-20261029').expanduser()
OLD_ID = 'kbnibljeinmjmmpholicjkaiehmilbbk'


def selected_bridge():
    try:
        # An unauthenticated legacy /health request creates a login session.
        # The invalid bearer is rejected before that side effect by old
        # servers; new servers recognize the explicit read-only probe.
        probe = Request('http://127.0.0.1:18888/health',
                        headers={'Authorization':'Bearer sw-readonly-probe',
                                 'X-Studiowright-Probe':'1'})
        with build_opener(ProxyHandler({})).open(probe, timeout=3) as response:
            info = json.load(response)
        if info.get('application') != 'studiowright-console':
            raise ValueError()
        return info.get('bridge_id')
    except HTTPError as error:
        try:
            value = json.load(error)
            if error.code == 401 and value.get('error') == 'API Key 无效或已撤销。':
                return OLD_ID
        except (OSError, ValueError):
            pass
        return None
    except (OSError, ValueError):
        return None


def main():
    if SCRIPT.is_symlink() or not SCRIPT.is_file():
        raise RuntimeError('本机账号管理组件缺失')
    spec = importlib.util.spec_from_file_location('studiowright_profile_manager', SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError('本机账号管理组件不可用')
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    packaged = json.loads((ROOT / 'extension/manifest.json').read_text(encoding='utf-8'))
    import base64
    import hashlib
    public = base64.b64decode(packaged['key'])
    new_id = ''.join(chr(ord('a') + int(c, 16)) for c in hashlib.sha256(public).hexdigest()[:32])
    selected = selected_bridge()
    manager.EXTENSION_ID = selected or new_id
    manager.EXTENSION = LEGACY if selected == OLD_ID and LEGACY.is_dir() else ROOT / 'extension'
    if selected is None:
        manager.SYNC_BLOCK_REASON = '未能确认后台版本，请检查连接后再操作。'
    elif selected == OLD_ID and not LEGACY.is_dir():
        manager.SYNC_BLOCK_REASON = '现有后台需要原版插件；请先使用后台远程登录。'
    elif selected not in (OLD_ID, new_id):
        manager.SYNC_BLOCK_REASON = '当前账号助手与后台版本不匹配，请核对安装包。'
    else:
        manager.SYNC_BLOCK_REASON = ''
    original_profiles = manager.profiles

    def profiles(root=None):
        result = original_profiles() if root is None else original_profiles(root)
        if manager.SYNC_BLOCK_REASON:
            for entry in result:
                entry['plugin_note'] = manager.SYNC_BLOCK_REASON
        return result

    manager.profiles = profiles
    with ThreadingHTTPServer(('127.0.0.1', 18890), manager.Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
