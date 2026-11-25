"""Validate and probe the explicit per-profile SOCKS mapping before Chrome starts."""
import ipaddress
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


MAP_LIMIT = 16384
INVENTORY_LIMIT = 65536


def mapped_ports(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('unsafe_flow_proxy_map')
    if info.st_size > MAP_LIMIT:
        raise ValueError('invalid_flow_proxy_map')
    mappings = {}
    used = set()
    for line in path.read_text(encoding='utf-8').splitlines():
        fields = line.partition('#')[0].split()
        if not fields:
            continue
        if (len(fields) != 2 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', fields[0])
                or not re.fullmatch(r'[0-9]{1,5}', fields[1])):
            raise ValueError('invalid_flow_proxy_map')
        profile, raw_port = fields
        port = int(raw_port)
        if not 1 <= port <= 65535 or profile in mappings or port in used:
            raise ValueError('duplicate_or_invalid_flow_proxy_map')
        mappings[profile] = port
        used.add(port)
    return mappings


def _exit_inventory(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or info.st_size > INVENTORY_LIMIT):
        raise ValueError('invalid_flow_exit_inventory')
    records = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(records, dict):
        raise ValueError('invalid_flow_exit_inventory')
    exits = {}
    for raw_port, record in records.items():
        if (not re.fullmatch(r'[1-9][0-9]{0,4}', raw_port) or int(raw_port) > 65535
                or not isinstance(record, dict) or set(record) != {'public_ip', 'confirmed'}
                or not isinstance(record['public_ip'], str) or type(record['confirmed']) is not bool):
            raise ValueError('invalid_flow_exit_inventory')
        address = ipaddress.ip_address(record['public_ip'])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast or '%' in str(address):
            raise ValueError('invalid_flow_exit_inventory')
        exits[int(raw_port)] = (address, record['confirmed'])
    return exits


def reserve_confirmed_exit(root, profile):
    """Bind an imported profile once, or return None so its login can wait."""
    if not re.fullmatch(r'account-[0-9a-f]{12}', profile):
        return None
    mapping = Path(root) / 'bin/exit-map.txt'
    inventory = mapping.with_name('exit-inventory.json')
    try:
        with _locked_mapping(mapping):
            assignments = mapped_ports(mapping)
            if profile in assignments:
                return assignments[profile]
            exits = _exit_inventory(inventory)
            used_ports = set(assignments.values())
            if any(port not in exits or not exits[port][1] for port in used_ports):
                return None
            used_addresses = {exits[port][0] for port in used_ports}
            port = next((port for port, (address, confirmed) in sorted(exits.items())
                         if confirmed and port not in used_ports and address not in used_addresses), None)
            if port is None:
                return None
            original = mapping.read_bytes()
            separator = b'' if not original or original.endswith(b'\n') else b'\n'
            updated = original + separator + f'{profile} {port}\n'.encode('ascii')
            if len(updated) > MAP_LIMIT:
                return None
            _write_mapping(mapping, updated)
            return port
    except (ImportError, OSError, ValueError):
        return None


@contextmanager
def _locked_mapping(mapping):
    import fcntl

    lock = mapping.with_name('exit-map.txt.lock')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('unsafe_flow_proxy_map_lock')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _write_mapping(mapping, contents):
    staging = None
    try:
        with tempfile.NamedTemporaryFile(dir=mapping.parent, delete=False) as output:
            staging = Path(output.name)
            os.fchmod(output.fileno(), stat.S_IMODE(mapping.stat().st_mode))
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, mapping)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def _profiles_in_use(root, profiles, processes=Path('/proc')):
    paths = {os.fsencode((root / 'data/profiles' / profile).resolve()): profile for profile in profiles}
    active = set()
    for process in processes.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            command = (process / 'cmdline').read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        for match in re.finditer(rb'(?:^|[\0 ])--user-data-dir=([^\0 ]+)', command):
            profile = paths.get(match[1])
            if profile is not None:
                active.add(profile)
    return active


def release_removed_exits(root, removed):
    """Release bindings only after the retired browser and any launcher have exited."""
    root = Path(root)
    mapping = root / 'bin/exit-map.txt'
    if not removed or not mapping.exists():
        return set()
    with _locked_mapping(mapping):
        candidates = set(mapped_ports(mapping)) & removed
        if not candidates:
            return set()
        released = candidates - _profiles_in_use(root, candidates)
        if released:
            original = mapping.read_bytes().splitlines(keepends=True)
            removed_names = {(name.encode('ascii'),) for name in released}
            _write_mapping(mapping, b''.join(line for line in original
                           if tuple(line.partition(b'#')[0].split()[:1]) not in removed_names))
        return released


def exit_bindings(root):
    mapping = Path(root) / 'bin/exit-map.txt'
    try:
        assignments = mapped_ports(mapping)
    except (OSError, ValueError):
        return {}
    try:
        exits = _exit_inventory(mapping.with_name('exit-inventory.json'))
    except (OSError, ValueError):
        exits = {}
    bindings = {}
    for profile, port in assignments.items():
        address, confirmed = exits.get(port, (None, False))
        bindings[profile] = {'port': port, 'public_ip': str(address) if confirmed else None,
                             'ipv6': str(address) if confirmed and address.version == 6 else None}
    return bindings


def check_proxy(port):
    response = subprocess.run(
        ['curl', '--disable', '--silent', '--output', '/dev/null', '--write-out', '%{http_code}',
         '--noproxy', '', '--socks5-hostname', f'127.0.0.1:{port}', '--connect-timeout', '4',
         '--max-time', '8', 'https://www.gstatic.com/generate_204'],
        capture_output=True, text=True, timeout=9, check=False,
    )
    if response.returncode != 0 or response.stdout.strip() != '204':
        raise ValueError('flow_proxy_unreachable')


def proxy_argument(root, args):
    if any(arg.startswith(('--proxy-', '--no-proxy', '--host-resolver-rules')) for arg in args):
        raise ValueError('flow_proxy_override_forbidden')
    profiles = [arg.partition('=')[2] for arg in args if arg.startswith('--user-data-dir=')]
    if len(profiles) != 1 or '--user-data-dir' in args:
        raise ValueError('flow_proxy_profile_required')
    profile = Path(profiles[0]).resolve()
    if profile.parent != (root / 'data/profiles').resolve():
        raise ValueError('flow_proxy_profile_required')
    port = mapped_ports(root / 'bin/exit-map.txt').get(profile.name)
    if port is None:
        raise ValueError('flow_proxy_unbound')
    check_proxy(port)
    return f'--proxy-server=socks5://127.0.0.1:{port}'


if __name__ == '__main__':
    try:
        print(proxy_argument(Path(__file__).resolve().parents[1], sys.argv[1:]))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, subprocess.SubprocessError):
        print('flow_proxy_check_unavailable', file=sys.stderr)
        sys.exit(1)
