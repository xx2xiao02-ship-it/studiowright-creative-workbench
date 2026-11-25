"""Read-only checks for a managed Flow Chrome profile."""
import asyncio
import base64
import hashlib
from pathlib import Path
import json
import re
import shlex

from adapter import browser_profile_matches
from flow_proxy_guard import mapped_ports


PROFILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
EXTENSION_ID = re.compile(r'[a-p]{32}')
_OFFLINE_PILOT_PROFILES = frozenset({
    'ub-chrome-975371911f',
    'account-2f8797411d41',
    'ub-chrome-424097083b',
    'ub-chrome-e7fc86b674',
    'account-1',
    'ub-chrome-2a0f1cbdca',
    'ub-chrome-b5f593c253',
    'account-f8d81e309793',
    'ub-chrome-748b00e533',
    'ub-chrome-f8f5d94158',
})
_CHROME_RESOURCE_COMPONENTS = {
    'admccjkmockfdflocgggjfgdacdodkdf': '/opt/google/chrome/resources/glic_extension',
    'fignfifoniblkonapihmkfakmlgkbkcf': '/opt/google/chrome/resources/network_speech_synthesis/mv3',
    'mhjfbmdgcfjbbpaeojofohoefgiehjai': '/opt/google/chrome/resources/pdf',
    'nkeimhogjdpnpccoofpliimaahmaaome': '/opt/google/chrome/resources/hangout_services',
}
_CHROME_PAYMENTS_COMPONENT = 'nmmhkkegccagdldgiimedpiccmgmieda'
_CHROME_DOCS_OFFLINE = 'ghbmnnjooekpmoecnnnilnnbdlolhkhi'
_CHROME_DICTATION_CONNECTOR = 'kbglekiebdohdafflpmiejhbfdmjdbbe'
_CHROME_STORE_UPDATE_URL = 'https://clients2.google.com/service/update2/crx'
# Chromium's ManifestLocation values for browser components.
_CHROME_COMPONENT = 5
_CHROME_EXTERNAL_PREF_DOWNLOAD = 6
_CHROME_EXTERNAL_COMPONENT = 10
CHROME_NAMES = {'chrome', 'chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable'}
DISALLOWED_FLAGS = (
    '--load-extension', '--disable-extensions-except', '--no-proxy-server',
    '--proxy-pac-url', '--proxy-auto-detect', '--proxy-bypass-list',
    '--host-resolver-rules',
)
LANGUAGE = re.compile(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*')
TIMEZONE = re.compile(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*')
FINGERPRINT_DIGEST = re.compile(r'[0-9a-f]{64}')
FINGERPRINT_SIGNALS = """() => {
    const canvas = document.createElement('canvas');
    canvas.width = 192;
    canvas.height = 48;
    const draw = canvas.getContext('2d');
    if (draw) {
        draw.fillStyle = '#156081';
        draw.fillRect(0, 0, 192, 48);
        draw.fillStyle = '#f3ab50';
        draw.font = '19px sans-serif';
        draw.fillText('Flow profile', 8, 29);
    }
    const gl = document.createElement('canvas').getContext('webgl');
    const renderer = gl && gl.getExtension('WEBGL_debug_renderer_info');
    return {
        webdriver: navigator.webdriver,
        ua: navigator.userAgent,
        screen: [screen.width, screen.height, screen.availWidth, screen.availHeight,
                 screen.colorDepth, screen.pixelDepth, devicePixelRatio],
        canvas: draw ? canvas.toDataURL() : null,
        webgl: gl ? [gl.getParameter(gl.VENDOR), gl.getParameter(gl.RENDERER),
                     gl.getParameter(gl.VERSION), gl.getParameter(gl.SHADING_LANGUAGE_VERSION),
                     renderer && gl.getParameter(renderer.UNMASKED_VENDOR_WEBGL),
                     renderer && gl.getParameter(renderer.UNMASKED_RENDERER_WEBGL),
                     (gl.getSupportedExtensions() || []).sort()] : null,
    };
}"""


def dispatch_check(root, profile, *, online=False):
    """Check the local dispatch policy and optionally verify the live browser."""
    policy = Path(root) / 'console-data/dispatch-locales.json'
    try:
        if policy.is_symlink():
            raise ValueError('unexpected symlink')
        values = json.loads(policy.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return False, ['dispatch_locale_missing']
    except (OSError, ValueError):
        return False, ['dispatch_locale_invalid']
    if not isinstance(values, dict):
        return False, ['dispatch_locale_invalid']
    if profile not in values:
        return False, ['dispatch_locale_missing']
    expected = values[profile]
    if not isinstance(expected, dict) or set(expected) != {'languages', 'timezone'}:
        return False, ['dispatch_locale_invalid']
    languages, timezone = expected['languages'], expected['timezone']
    if (not isinstance(languages, list) or not languages
            or not all(isinstance(value, str) and LANGUAGE.fullmatch(value) for value in languages)
            or not isinstance(timezone, str) or not TIMEZONE.fullmatch(timezone)):
        return False, ['dispatch_locale_invalid']
    try:
        options = {'allow_chrome_components': True} if not online and profile in _OFFLINE_PILOT_PROFILES else {}
        local = check_profile(root, profile, expected_languages=languages, expected_timezone=timezone, **options)
    except (OSError, RuntimeError, ValueError):
        return False, ['profile_preflight_unavailable']
    if not online or not local[0]:
        return local
    return _check_fingerprint_baseline(root, profile)


def pilot_dispatch_reasons(root, profile):
    """Apply the new offline dispatch policy only to approved profiles."""
    if profile not in _OFFLINE_PILOT_PROFILES:
        return []
    return dispatch_check(root, profile)[1]


def _check_fingerprint_baseline(root, profile):
    policy = Path(root) / 'console-data/fingerprint-baseline.json'
    try:
        if policy.is_symlink():
            raise ValueError('unexpected symlink')
        values = json.loads(policy.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return False, ['dispatch_fingerprint_baseline_missing']
    except (OSError, ValueError):
        return False, ['dispatch_fingerprint_baseline_invalid']
    if not isinstance(values, dict):
        return False, ['dispatch_fingerprint_baseline_invalid']
    if profile not in values:
        return False, ['dispatch_fingerprint_baseline_missing']
    expected = values[profile]
    if not isinstance(expected, str) or not FINGERPRINT_DIGEST.fullmatch(expected):
        return False, ['dispatch_fingerprint_baseline_invalid']

    observed, reason = collect_browser_fingerprint(root, profile)
    if reason:
        return False, [reason]
    if observed != expected:
        return False, ['dispatch_fingerprint_mismatch']
    return True, []


async def _read_browser_signals(port, flow_url):
    from playwright.async_api import Error, async_playwright

    try:
        async with asyncio.timeout(5), async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(
                f'http://127.0.0.1:{port}', timeout=4000, no_defaults=True)
            pages = browser.contexts[0].pages if browser.contexts else []
            page = next((tab for tab in pages if tab.url.startswith(flow_url)), None)
            if page is None:
                page = next((tab for tab in pages if tab.url == 'about:blank'), None)
            if page is None:
                raise RuntimeError('no eligible page')
            original_url = page.url
            sample = await page.evaluate(FINGERPRINT_SIGNALS)
            if page.url != original_url:
                raise RuntimeError('page changed during sampling')
            return sample
    except Error as exc:
        raise RuntimeError('unable to read browser signals') from exc


def collect_browser_fingerprint(root, profile):
    """Return (SHA-256, error code) from an existing, locally managed browser."""
    if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile):
        return None, 'invalid_profile'
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return None, 'dispatch_browser_unavailable'

    try:
        from studiowright import chrome

        port = chrome.debug_port(profile)
        target = (Path(root) / 'data/profiles' / profile).resolve()
        port_arg = f'--remote-debugging-port={port}'
        listeners = [args for _, args in _chrome_processes(Path('/proc')) if port_arg in args]
        if len(listeners) != 1 or not browser_profile_matches(listeners[0], target):
            return None, 'dispatch_browser_unavailable'
        sample = asyncio.run(_read_browser_signals(port, chrome.FLOW_URL))
    except (ImportError, OSError, RuntimeError, ValueError):
        return None, 'dispatch_browser_unavailable'

    if not isinstance(sample, dict):
        return None, 'dispatch_fingerprint_unavailable'
    if sample.get('webdriver') is not False:
        return None, 'dispatch_webdriver_detected'
    fingerprint = {key: sample.get(key) for key in ('ua', 'screen', 'canvas', 'webgl')}
    if not all(fingerprint.values()):
        return None, 'dispatch_fingerprint_unavailable'
    serialized = json.dumps(fingerprint, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(serialized).hexdigest(), None


def _chrome_processes(proc_root, target=None):
    for cmdline in proc_root.glob('[0-9]*/cmdline'):
        try:
            args = [arg.decode(errors='replace') for arg in cmdline.read_bytes().split(b'\0') if arg]
            if len(args) == 1:
                args = shlex.split(args[0])
            if (args and Path(args[0]).name in CHROME_NAMES
                    and not any(arg.startswith('--type=') for arg in args)
                    and (target is None or browser_profile_matches(args, target))):
                yield cmdline.parent.name, args
        except (OSError, ValueError):
            # A process can disappear during the scan.
            continue


def _system_timezone():
    zoneinfo = Path('/usr/share/zoneinfo')
    try:
        name = Path('/etc/localtime').resolve(strict=True).relative_to(zoneinfo).as_posix()
        return name.removeprefix('posix/').removeprefix('right/')
    except (OSError, ValueError):
        # Ubuntu can also be configured with a copied /etc/localtime file.
        name = Path('/etc/timezone').read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*', name) or not (zoneinfo / name).is_file():
            raise ValueError('invalid system timezone')
        return name


def _manifest_key(manifest, ident):
    if not isinstance(manifest, dict) or not isinstance(manifest.get('key'), str):
        return None
    key = manifest['key']
    try:
        digest = hashlib.sha256(base64.b64decode(key, validate=True)).hexdigest()[:32]
    except ValueError:
        return None
    return key if digest.translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop')) == ident else None


def _component_manifest_on_disk(folder, *, require_verified_contents=False):
    path = folder / 'manifest.json'
    try:
        if (any(p.is_symlink() for p in (folder.parent, folder, path))
                or not path.is_file() or path.stat().st_size > 65536):
            return {}
        if require_verified_contents:
            verified = folder / '_metadata/verified_contents.json'
            if (verified.parent.is_symlink() or verified.is_symlink() or not verified.is_file()
                    or not verified.stat().st_size):
                return {}
        disk_manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return disk_manifest if isinstance(disk_manifest, dict) else {}


def _allowed_chrome_component(default, ident, entry, on_disk):
    profile = default.parent.name
    resource = _CHROME_RESOURCE_COMPONENTS.get(ident)
    docs = profile == 'account-f8d81e309793' and ident == _CHROME_DOCS_OFFLINE
    dictation = profile == 'ub-chrome-f8f5d94158' and ident == _CHROME_DICTATION_CONNECTOR
    if (resource is None and ident != _CHROME_PAYMENTS_COMPONENT and not docs and not dictation
            or not isinstance(entry, dict)):
        return False
    manifest = entry.get('manifest')
    key = _manifest_key(manifest, ident)
    if not key:
        return False
    if resource is not None:
        return (entry.get('location') == _CHROME_COMPONENT and entry.get('from_webstore') is False
                and entry.get('path') == resource and not on_disk)

    path = entry.get('path')
    if dictation:
        connector_root = default.parent / 'DictationConnector'
        if (entry.get('location') != _CHROME_COMPONENT or entry.get('from_webstore') is not False or on_disk
                or not isinstance(path, str)
                or not re.fullmatch(re.escape(str(connector_root)) + r'/[0-9]+(?:\.[0-9]+){1,3}', path)):
            return False
        disk_manifest = _component_manifest_on_disk(Path(path), require_verified_contents=True)
        return _manifest_key(disk_manifest, ident) == key

    location = _CHROME_EXTERNAL_PREF_DOWNLOAD if docs else _CHROME_EXTERNAL_COMPONENT
    if (entry.get('location') != location or entry.get('from_webstore') is not True
            or (docs and entry.get('was_installed_by_default') is not True)
            or not isinstance(path, str) or not re.fullmatch(ident + r'/[0-9]+(?:\.[0-9]+){0,3}_[0-9]+', path)
            or not on_disk or manifest.get('update_url') != _CHROME_STORE_UPDATE_URL):
        return False
    extensions = default / 'Extensions'
    if extensions.is_symlink():
        return False
    disk_manifest = _component_manifest_on_disk(extensions / path, require_verified_contents=docs)
    return (_manifest_key(disk_manifest, ident) == key
            and disk_manifest.get('update_url') == _CHROME_STORE_UPDATE_URL)


def _extension_settings(preferences):
    extensions = preferences.get('extensions', {}) if isinstance(preferences, dict) else None
    return extensions.get('settings', {}) if isinstance(extensions, dict) else None


def _unexpected_extensions(default, settings, on_disk):
    secure_path = default / 'Secure Preferences'
    secure = {}
    if secure_path.is_symlink():
        raise ValueError('unexpected secure preferences symlink')
    if secure_path.exists():
        if not secure_path.is_file() or secure_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('invalid secure preferences file')
        secure = _extension_settings(json.loads(secure_path.read_text(encoding='utf-8')))
        if not isinstance(secure, dict):
            raise ValueError('invalid secure extension settings')
    unexpected = on_disk.difference(settings, secure)
    for source in (settings, secure):
        for ident, entry in source.items():
            if not EXTENSION_ID.fullmatch(ident):
                continue
            installed = ident in on_disk
            # Chrome can keep an update timestamp after the extension is gone.
            if (not installed and isinstance(entry, dict) and entry.keys() == {'lastpingday'}
                    and isinstance(entry['lastpingday'], str)
                    and re.fullmatch(r'[0-9]{1,20}', entry['lastpingday'])):
                continue
            if not _allowed_chrome_component(default, ident, entry, installed):
                unexpected.add(ident)
    return unexpected


def check_profile(root, profile, *, expected_languages=None, expected_timezone=None, proc_root=Path('/proc'),
                  allow_chrome_components=False):
    """Return (ok, reasons), without opening Chrome or making network requests.

    `root` is the Flow install directory. Optional expectations come from the
    caller's local policy. An absent Chrome process is normal before launch.
    """
    if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile):
        return False, ['invalid_profile']
    root = Path(root)
    base = (root / 'data/profiles').resolve()
    target = base / profile
    default = target / 'Default'
    if target.is_symlink() or default.is_symlink() or not default.is_dir():
        return False, ['invalid_or_missing_profile']

    reasons = []
    extension_ids = set()
    extensions = default / 'Extensions'
    try:
        if extensions.is_symlink():
            raise ValueError('symlink')
        if extensions.exists():
            extension_ids.update(item.name for item in extensions.iterdir()
                                 if EXTENSION_ID.fullmatch(item.name))
    except (OSError, ValueError):
        reasons.append('extension_directory_unreadable')

    prefs_path = default / 'Preferences'
    preferences = {}
    try:
        if prefs_path.is_symlink() or not prefs_path.is_file() or prefs_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('invalid preferences file')
        preferences = json.loads(prefs_path.read_text(encoding='utf-8'))
        if not isinstance(preferences, dict):
            raise ValueError('invalid preferences object')
    except (OSError, ValueError):
        reasons.append('preferences_unreadable')
        preferences = {}
    settings = _extension_settings(preferences)
    if not isinstance(settings, dict):
        reasons.append('extension_settings_invalid')
    elif allow_chrome_components and profile in _OFFLINE_PILOT_PROFILES:
        try:
            extension_ids = _unexpected_extensions(default, settings, extension_ids)
        except (OSError, ValueError):
            reasons.append('extension_settings_invalid')
            extension_ids.update(name for name in settings if EXTENSION_ID.fullmatch(name))
    else:
        extension_ids.update(name for name in settings if EXTENSION_ID.fullmatch(name))
    reasons.extend(f'extension_present:{name}' for name in sorted(extension_ids))

    intl = preferences.get('intl', {})
    if not isinstance(intl, dict):
        intl = {}
    languages = intl.get('accept_languages') or intl.get('selected_languages')
    actual_languages = tuple(code.strip() for code in languages.split(',') if code.strip()) if isinstance(languages, str) else ()
    if not actual_languages:
        reasons.append('profile_languages_missing')
    elif expected_languages is not None and actual_languages != tuple(expected_languages):
        reasons.append('profile_languages_mismatch')
    try:
        timezone = _system_timezone()
    except (OSError, ValueError):
        reasons.append('system_timezone_unavailable')
    else:
        if expected_timezone is not None and timezone != expected_timezone:
            reasons.append('system_timezone_mismatch')

    port = None
    try:
        port = mapped_ports(root / 'bin/exit-map.txt').get(profile)
        if port is None:
            reasons.append('proxy_mapping_missing')
    except (OSError, ValueError, AttributeError):
        reasons.append('proxy_mapping_unreadable_or_invalid')

    proc_root = Path(proc_root)
    if not proc_root.is_dir():
        reasons.append('chrome_process_inventory_unavailable')
    else:
        for pid, args in _chrome_processes(proc_root, target):
            flags = {arg.partition('=')[0] for arg in args}
            reasons.extend(f'chrome_unsafe_flag:{pid}:{flag}' for flag in DISALLOWED_FLAGS if flag in flags)
            proxies = [arg for arg in args if arg == '--proxy-server' or arg.startswith('--proxy-server=')]
            if not proxies:
                reasons.append(f'chrome_proxy_missing:{pid}')
            elif port is None or proxies != [f'--proxy-server=socks5://127.0.0.1:{port}']:
                reasons.append(f'chrome_proxy_mismatch:{pid}')

    return not reasons, reasons
