"""Kernel-level HID input devices for the desktop (mouse and keyboard).

The workbench's terminal input path used to depend on two things that are not
real hardware: the XDG remote-desktop portal's virtual pointer and CDP text
injection. This module creates ``uinput`` devices instead, so the compositor
delivers ordinary hardware pointer and key events, with no portal consent
dialog and no synthetic DOM events.

Two rules hold throughout:

* Standard library only. No ``evdev``, no third-party wheel, and the daemon
  must run under the system interpreter.
* Fail closed. The runtime directory is ownership- and permission-checked, and
  every request is rejected unless the helper reported itself ready. A missing
  ``/dev/uinput`` is a hard failure that callers may only recover from by
  explicitly falling back to the portal backend.
"""
import asyncio
import contextlib
import json
import math
import os
import random
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # importable for unit tests off Linux; devices still refuse
    fcntl = None

INPUT_HID_VERSION = "uinput-hid-20261122"

# Linux input event constants (linux/input-event-codes.h).
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
SYN_REPORT = 0x00
REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08
BTN_LEFT = 0x110
BTN_MIDDLE = 0x112
BTN_RIGHT = 0x111
KEY_A = 0x1e
KEY_LEFTSHIFT = 0x2a
KEY_LEFTRIGHT = 0x6d
KEY_LEFTCTRL = 0x1d
KEY_ENTER = 0x1c
KEY_TAB = 0x0f
KEY_V = 0x2f
KEY_BACKSPACE = 0x0e
BUS_USB = 0x03
BUS_BLUETOOTH = 0x05

# ioctl encoding for linux/uinput.h.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, kind, number, size):
    return ((direction << _IOC_DIRSHIFT) | (size << _IOC_SIZESHIFT) |
            (kind << _IOC_TYPESHIFT) | (number << _IOC_NRSHIFT))


def _io(kind, number):
    return _ioc(_IOC_NONE, kind, number, 0)


def _iow(kind, number, size):
    return _ioc(_IOC_WRITE, kind, number, size)


UI_SET_EVBIT = _iow(ord('U'), 100, 4)
UI_SET_KEYBIT = _iow(ord('U'), 101, 4)
UI_SET_RELBIT = _iow(ord('U'), 102, 4)
UI_DEV_CREATE = _io(ord('U'), 1)
UI_DEV_DESTROY = _io(ord('U'), 2)

EVENT_STRUCT = '=qqHHi'  # input_event: timeval(sec,usec), type, code, value
EVENT_SIZE = struct.calcsize(EVENT_STRUCT)


class HidInputError(Exception):
    """Raised when HID input cannot be provided or a request is refused."""


class HidUnavailable(HidInputError):
    """The kernel or the runtime directory does not allow HID input."""


# ---------------------------------------------------------------- device identity

DEFAULT_POINTER_DEVICE = {
    "name": "Wireless Mouse",
    "bustype": BUS_USB,
    "vendor": 0x046D,
    "product": 0xC077,
    "version": 1,
}
DEFAULT_KEYBOARD_DEVICE = {
    "name": "Wireless Keyboard",
    "bustype": BUS_USB,
    "vendor": 0x046D,
    "product": 0xC078,
    "version": 1,
}
CONFIG_DIR = Path.home() / '.config/creative-unified-workbench'
CONFIG_FILE = CONFIG_DIR / 'input-backend.json'
RUNTIME_ROOT = Path.home() / '.local/state/creative-unified-workbench/desktop-hid'


def _clamped_int(value, low, high, default):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


def _device_spec(raw, default):
    raw = raw if isinstance(raw, dict) else {}
    name = raw.get('name', default['name'])
    if not isinstance(name, str) or not name.strip():
        name = default['name']
    name = name.strip()[:79]
    return {
        'name': name.strip()[:79],
        'bustype': _clamped_int(raw.get('bustype'), 0, 0x1f, default['bustype']),
        'vendor': _clamped_int(raw.get('vendor'), 0, 0xffff, default['vendor']),
        'product': _clamped_int(raw.get('product'), 0, 0xffff, default['product']),
        'version': _clamped_int(raw.get('version'), 0, 0xffff, default['version']),
    }


def normalise_config(raw):
    """Return a validated backend configuration or the conservative defaults."""
    raw = raw if isinstance(raw, dict) else {}
    pointer = raw.get('pointer', 'auto')
    keyboard = raw.get('keyboard', 'auto')
    policy = ('hid', 'portal', 'auto')
    pointer = pointer if pointer in policy else 'auto'
    keyboard = keyboard if keyboard in policy else 'auto'
    # One task must not mix hardware and portal provenance: an explicit hid
    # choice on either role turns the other role's explicit portal into auto,
    # so both end up on the same backend family.
    if 'hid' in (pointer, keyboard):
        if pointer == 'portal':
            pointer = 'auto'
        if keyboard == 'portal':
            keyboard = 'auto'
    return {
        'pointer': pointer,
        'keyboard': keyboard,
        'pointer_device': _device_spec(raw.get('pointer_device'), DEFAULT_POINTER_DEVICE),
        'keyboard_device': _device_spec(raw.get('keyboard_device'), DEFAULT_KEYBOARD_DEVICE),
        'report_interval_min': min(.02, max(.002, _number(raw.get('report_interval_min'), .004))),
        'report_interval_max': min(.025, max(.003, _number(raw.get('report_interval_max'), .009))),
        'type_interval_min': min(.3, max(.02, _number(raw.get('type_interval_min'), .07))),
        'type_interval_max': min(.5, max(.03, _number(raw.get('type_interval_max'), .16))),
        'identity_source': raw.get('identity_source') if raw.get('identity_source') in ('config', 'paired', 'auto') else 'config',
        'identity_hint': raw.get('identity_hint') if isinstance(raw.get('identity_hint'), str) else '',
    }


def _number(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value):
        return default
    return float(value)


def load_config(path=None):
    """Read the backend configuration; never treat a bad file as an error."""
    target = Path(path) if path else CONFIG_FILE
    try:
        if target.is_symlink() or not target.is_file():
            return normalise_config({})
        return normalise_config(json.loads(target.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return normalise_config({})


# ---------------------------------------------------------------- paired hardware identity

SYSFS_INPUT = Path('/sys/class/input')


def _hex_file(path):
    try:
        return int(Path(path).read_text().strip(), 16)
    except (OSError, ValueError):
        return None


def _bitmap(text):
    """Parse a sysfs capability bitmap (hex words, most significant first)."""
    value = 0
    for word in (text or '').split():
        try:
            value = (value << 64) | int(word, 16)
        except ValueError:
            value = (value << 64)
    return value


def paired_devices(root=None):
    """Describe the real input devices the kernel already knows about.

    This only reads sysfs identity files; it never opens an event node, so no
    keystroke or movement made by the user can end up in this process.
    """
    root = Path(root or SYSFS_INPUT)
    devices = []
    for event in sorted(root.glob('event*')):
        device = event / 'device'
        try:
            name = (device / 'name').read_text().strip()
        except OSError:
            continue
        bustype = _hex_file(device / 'id/bustype')
        vendor = _hex_file(device / 'id/vendor')
        product = _hex_file(device / 'id/product')
        version = _hex_file(device / 'id/version')
        if None in (bustype, vendor, product):
            continue
        try:
            uniq = (device / 'uniq').read_text().strip()
            phys = (device / 'phys').read_text().strip()
            rel_text = (device / 'capabilities/rel').read_text()
        except OSError:
            uniq, phys, rel_text = '', '', ''
        try:
            key_text = (device / 'capabilities/key').read_text()
        except OSError:
            key_text = ''
        # A mouse is the device with both relative axes; a keyboard is the one
        # that can report KEY_A. Logitech's Unifying receiver exposes large key
        # bitmaps on both nodes and a horizontal wheel on the keyboard node,
        # so the old "any relative axis means pointer" test mislabelled the
        # keyboard and made the identity lookup ambiguous.
        rel_bits = _bitmap(rel_text)
        pointer_capable = bool(rel_bits & (1 << REL_X)) and bool(rel_bits & (1 << REL_Y))
        if key_text.strip():
            keyboard_capable = bool((_bitmap(key_text) >> KEY_A) & 1)
        else:
            keyboard_capable = not pointer_capable
        kind = 'pointer' if pointer_capable else 'keyboard'
        devices.append({
            'event': event.name, 'name': name, 'bustype': bustype,
            'vendor': vendor, 'product': product, 'version': version or 1,
            'uniq': uniq, 'phys': phys,
            'kind': kind,
            'pointer': pointer_capable, 'keyboard': keyboard_capable,
        })
    return devices


def _role_candidates(candidates, kind):
    """Keep the devices that can serve ``kind``; prefer exclusive roles."""
    if kind == 'pointer':
        pool = [device for device in candidates
                if device.get('pointer', device['kind'] == 'pointer')]
        role_only = [device for device in pool
                     if not device.get('keyboard', device['kind'] != 'keyboard')]
    elif kind == 'keyboard':
        pool = [device for device in candidates
                if device.get('keyboard', device['kind'] == 'keyboard')]
        role_only = [device for device in pool
                     if not device.get('pointer', device['kind'] == 'pointer')]
    else:
        return candidates
    return role_only or pool


def resolve_identity(devices, hint='', kind='any'):
    """Pick the real hardware device a virtual twin should imitate.

    Only USB or Bluetooth devices with a non-empty ``phys`` qualify, which
    excludes the helper's own uinput devices (they carry no physical path).
    """
    hint = (hint or '').strip().casefold()
    candidates = [device for device in devices
                  if device['bustype'] in (BUS_USB, BUS_BLUETOOTH) and device['phys']]
    if hint:
        candidates = [device for device in candidates if hint in device['name'].casefold()]
    candidates = _role_candidates(candidates, kind)
    if len(candidates) != 1:
        return None
    return candidates[0]


def apply_auto_identity(config, devices=None):
    """Copy the identity of the one real keyboard and mouse on this machine."""
    devices = paired_devices() if devices is None else devices
    updated = dict(config)
    resolved = {}
    for key, kind in (('pointer_device', 'pointer'), ('keyboard_device', 'keyboard')):
        match = resolve_identity(devices, config.get('identity_hint', ''), kind)
        if match is None:
            resolved[kind] = None
            continue
        resolved[kind] = match['event']
        updated[key] = {
            'name': match['name'][:79], 'bustype': match['bustype'],
            'vendor': match['vendor'], 'product': match['product'],
            'version': match['version'],
        }
    updated['paired_identity'] = resolved
    return updated


def choose_identity(devices, hint='', kind='any'):
    """Pick the Bluetooth device whose identity the virtual device should copy."""
    hint = (hint or '').strip().casefold()
    candidates = [device for device in devices if device['bustype'] == BUS_BLUETOOTH]
    if hint:
        candidates = [device for device in candidates if hint in device['name'].casefold()]
    candidates = _role_candidates(candidates, kind)
    if len(candidates) != 1:
        return None  # ambiguous or missing: never guess a device identity
    return candidates[0]


def apply_paired_identity(config, devices=None):
    """Replace configured identities with the paired keyboard and mouse."""
    devices = paired_devices() if devices is None else devices
    updated = dict(config)
    pointer = choose_identity(devices, config.get('identity_hint', ''), 'pointer')
    keyboard = choose_identity(devices, config.get('identity_hint', ''), 'keyboard')
    for key, match in (('pointer_device', pointer), ('keyboard_device', keyboard)):
        if match is None:
            continue
        updated[key] = {
            'name': match['name'][:79], 'bustype': match['bustype'],
            'vendor': match['vendor'], 'product': match['product'],
            'version': match['version'],
        }
    updated['paired_identity'] = {
        'pointer': None if pointer is None else pointer['event'],
        'keyboard': None if keyboard is None else keyboard['event'],
    }
    return updated


# ---------------------------------------------------------------- human motion

def human_steps(dx, dy, rng, max_report=6.0):
    """Split one relative move into plausible hardware reports.

    Speed is uneven, the middle of the move carries a small perpendicular
    tremor, and the last report lands exactly on the requested delta so a
    caller's absolute coordinate bookkeeping stays true.
    """
    distance = math.hypot(dx, dy)
    if distance < 1e-9:
        return []
    count = max(1, min(160, int(round(distance / max(2.0, max_report * .7)))))
    weights = []
    for index in range(count):
        t = (index + 1) / (count + 1)
        ease = .35 + .9 * math.sin(math.pi * t)
        weights.append(ease * rng.uniform(.75, 1.25))
    total = sum(weights)
    normal_x, normal_y = -dy / distance, dx / distance
    steps = []
    sent_x = sent_y = 0.0
    cumulative = 0.0
    for index, weight in enumerate(weights):
        cumulative += weight
        if index == count - 1:
            target_x, target_y = dx, dy
        else:
            fraction = cumulative / total
            tremor = rng.uniform(-1.2, 1.2) * math.sin(math.pi * fraction)
            target_x = dx * fraction + normal_x * tremor
            target_y = dy * fraction + normal_y * tremor
        step_x, step_y = int(round(target_x)) , int(round(target_y))
        steps.append((step_x - int(round(sent_x)), step_y - int(round(sent_y))))
        sent_x, sent_y = step_x, step_y
    return [step for step in steps if step != (0, 0)] or [(0, 0)]


_PLAIN_KEYS = {
    '\n': KEY_ENTER, '\t': KEY_TAB, ' ': 0x39, '\x08': KEY_BACKSPACE,
    '-': 0x0c, '=': 0x0d, '[': 0x1a, ']': 0x1b, '\\': 0x2b, ';': 0x27,
    "'": 0x28, '`': 0x29, ',': 0x33, '.': 0x34, '/': 0x35,
    '1': 0x02, '2': 0x03, '3': 0x04, '4': 0x05, '5': 0x06,
    '6': 0x07, '7': 0x08, '8': 0x09, '9': 0x0a, '0': 0x0b,
}
for _index, _char in enumerate('abcdefghijklmnopqrstuvwxyz'):
    _PLAIN_KEYS.setdefault(_char, {
        'a': 0x1e, 'b': 0x30, 'c': 0x2e, 'd': 0x20, 'e': 0x12, 'f': 0x21,
        'g': 0x22, 'h': 0x23, 'i': 0x17, 'j': 0x24, 'k': 0x25, 'l': 0x26,
        'm': 0x32, 'n': 0x31, 'o': 0x18, 'p': 0x19, 'q': 0x10, 'r': 0x13,
        's': 0x1f, 't': 0x14, 'u': 0x16, 'v': 0x2f, 'w': 0x11, 'x': 0x2d,
        'y': 0x15, 'z': 0x2c,
    }[_char])

_SHIFTED_KEYS = {
    '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7',
    '*': '8', '(': '9', ')': '0', '_': '-', '+': '=', '{': '[', '}': ']',
    '|': '\\', ':': ';', '"': "'", '~': '`', '<': ',', '>': '.', '?': '/',
}


def key_for_char(char):
    """Return ``(keycode, needs_shift)`` or raise for a non-ASCII character."""
    if 'A' <= char <= 'Z':
        return _PLAIN_KEYS[char.lower()], True
    if char in _PLAIN_KEYS:
        return _PLAIN_KEYS[char], False
    base = _SHIFTED_KEYS.get(char)
    if base is not None:
        return _PLAIN_KEYS[base], True
    raise HidInputError('unsupported_character')


def can_type(text, maximum=400):
    """True when the HID keyboard can reproduce the text without help.

    CJK and other non-ASCII text cannot be produced by a US hardware keymap,
    so callers must send it through the clipboard path instead.
    """
    if not isinstance(text, str) or not text or len(text) > maximum:
        return False
    try:
        for char in text:
            key_for_char(char)
    except HidInputError:
        return False
    return True


def keystrokes(text, rng=None, minimum=None, maximum=None):
    """Expand text into ``(keycode, shift)`` presses with human-paced gaps."""
    rng = rng or random.SystemRandom()
    minimum = .07 if minimum is None else minimum
    maximum = .16 if maximum is None else maximum
    if not isinstance(text, str) or not text:
        raise HidInputError('invalid_text')
    first = key_for_char(text[0])
    presses = [(first, 0.0)]
    for char in text[1:]:
        presses.append((key_for_char(char), rng.uniform(minimum, maximum)))
    return presses


def typing_delay(char, rng, minimum, maximum):
    """Longer pause after sentence breaks, matching ordinary typing."""
    delay = rng.uniform(minimum, maximum)
    if char in '.!?;:。！？；：':
        delay += rng.uniform(.12, .38)
    elif char in ',，、':
        delay += rng.uniform(.04, .16)
    elif char == ' ':
        delay += rng.uniform(0, .05)
    return delay


# ---------------------------------------------------------------- device layer

class _UinputDevice:
    def __init__(self, spec, *, rels=(), buttons=(), keys=()):
        self.spec = spec
        if fcntl is None:
            raise HidUnavailable('uinput_unavailable')
        try:
            self.fd = os.open('/dev/uinput', os.O_RDWR | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
        except OSError as exc:
            raise HidUnavailable('uinput_unavailable') from exc
        try:
            for rel in rels:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                fcntl.ioctl(self.fd, UI_SET_RELBIT, rel)
            for button in buttons:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, button)
            for key in keys:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
            setup = struct.pack('=HHHH80sI', spec['bustype'], spec['vendor'],
                                spec['product'], spec['version'],
                                spec['name'].encode('utf-8')[:79], 0)
            fcntl.ioctl(self.fd, _iow(ord('U'), 3, len(setup)), setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError as exc:
            os.close(self.fd)
            raise HidUnavailable('uinput_device_rejected') from exc

    def _emit(self, kind, code, value):
        now = time.time()
        seconds = int(now)
        microseconds = int((now - seconds) * 1e6)
        os.write(self.fd, struct.pack(EVENT_STRUCT, seconds, microseconds, kind, code, value))

    def sync(self):
        self._emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


class HidMouse(_UinputDevice):
    def __init__(self, spec):
        super().__init__(spec, rels=(REL_X, REL_Y, REL_WHEEL, REL_HWHEEL),
                         buttons=(BTN_LEFT, BTN_RIGHT, BTN_MIDDLE))
        self.position = None

    def move(self, dx, dy, config, rng):
        if not isinstance(dx, (int, float)) or not isinstance(dy, (int, float)):
            raise HidInputError('invalid_delta')
        if abs(dx) > 200000 or abs(dy) > 200000:
            raise HidInputError('delta_too_large')
        if abs(dx) >= 10000 or abs(dy) >= 10000:
            # Deliberate corner slam used to reset the pointer anchor: one
            # report, no human pacing, because a person does not glide there.
            if dx:
                self._emit(EV_REL, REL_X, int(dx))
            if dy:
                self._emit(EV_REL, REL_Y, int(dy))
            self.sync()
            return
        for step_x, step_y in human_steps(dx, dy, rng):
            if step_x:
                self._emit(EV_REL, REL_X, step_x)
            if step_y:
                self._emit(EV_REL, REL_Y, step_y)
            self.sync()
            time.sleep(rng.uniform(config['report_interval_min'], config['report_interval_max']))

    def scroll(self, steps, rng):
        if isinstance(steps, bool) or not isinstance(steps, int) or not -10 <= steps <= 10:
            raise HidInputError('invalid_scroll')
        if steps == 0:
            return
        direction = 1 if steps > 0 else -1
        for _ in range(abs(steps)):
            self._emit(EV_REL, REL_WHEEL, direction)
            self.sync()
            time.sleep(rng.uniform(.03, .08))

    def click(self, rng):
        self._emit(EV_KEY, BTN_LEFT, 1)
        self.sync()
        time.sleep(rng.uniform(.06, .115))
        self._emit(EV_KEY, BTN_LEFT, 0)
        self.sync()


class HidKeyboard(_UinputDevice):
    def __init__(self, spec):
        super().__init__(spec, keys=tuple(range(1, 249)))
        self.held = set()

    def _down(self, code):
        self._emit(EV_KEY, code, 1)
        self.held.add(code)
        self.sync()

    def _up(self, code):
        self._emit(EV_KEY, code, 0)
        self.held.discard(code)
        self.sync()

    def release_all(self):
        for code in tuple(self.held):
            self._emit(EV_KEY, code, 0)
        self.held.clear()
        self.sync()

    def type_text(self, text, config, rng):
        for index, char in enumerate(text):
            keycode, shift = key_for_char(char)
            if shift:
                self._down(KEY_LEFTSHIFT)
            self._down(keycode)
            time.sleep(rng.uniform(.018, .045))
            self._up(keycode)
            if shift:
                self._up(KEY_LEFTSHIFT)
            if index < len(text) - 1:
                time.sleep(typing_delay(char, rng, config['type_interval_min'],
                                        config['type_interval_max']))

    def chord(self, modifier, key, rng):
        self._down(modifier)
        time.sleep(rng.uniform(.02, .05))
        self._down(key)
        time.sleep(rng.uniform(.03, .07))
        self._up(key)
        self._up(modifier)

    def paste(self, rng):
        self.chord(KEY_LEFTCTRL, KEY_V, rng)


# ---------------------------------------------------------------- helper daemon

def _safe_runtime(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise HidUnavailable('unsafe_runtime_directory')
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HidUnavailable('unsafe_runtime_directory')
    return root


def _write_status(root, value):
    temp = root / ('.status-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, separators=(',', ':'))
    os.replace(temp, root / 'status.json')


def _serve(root, config, mouse, keyboard, rng):
    socket_path = root / 'hid.sock'
    if socket_path.exists() or socket_path.is_symlink():
        raise HidUnavailable('socket_already_exists')
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(4)
        _write_status(root, {'pid': os.getpid(), 'state': 'ready', 'version': INPUT_HID_VERSION,
                             'identity': config.get('paired_identity') or {},
                             'devices': {'pointer': config['pointer_device']['name'],
                                         'keyboard': config['keyboard_device']['name'],
                                         'bus': config['pointer_device']['bustype']}})
        while True:
            connection, _ = server.accept()
            with connection:
                try:
                    raw = connection.recv(65536)
                    request = json.loads(raw.decode('utf-8'))
                    if not isinstance(request, dict):
                        raise HidInputError('invalid_request')
                    action = request.get('action')
                    if action == 'move':
                        mouse.move(request.get('dx'), request.get('dy'), config, rng)
                    elif action == 'scroll':
                        mouse.scroll(request.get('steps'), rng)
                    elif action == 'click':
                        mouse.click(rng)
                    elif action == 'type':
                        keyboard.type_text(request.get('text'), config, rng)
                    elif action == 'paste':
                        keyboard.paste(rng)
                    elif action == 'status':
                        pass
                    else:
                        raise HidInputError('unknown_action')
                    connection.sendall(b'{"ok":true}\n')
                except Exception as exc:
                    reason = str(exc)
                    if not reason or len(reason) > 80:
                        reason = type(exc).__name__
                    with contextlib.suppress(OSError):
                        connection.sendall(json.dumps({'ok': False, 'error': reason}).encode() + b'\n')
    finally:
        server.close()
        with contextlib.suppress(OSError):
            socket_path.unlink()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('usage: uinput_hid.py <runtime-dir> [config.json]', file=sys.stderr)
        return 2
    root = Path(argv[0])
    config_path = argv[1] if len(argv) > 1 else None
    rng = random.SystemRandom()
    try:
        config = load_config(config_path)
        _safe_runtime(root)
        source = config.get('identity_source')
        if source == 'auto':
            config = apply_auto_identity(config)
        elif source == 'paired':
            config = apply_paired_identity(config)
            paired = config.get('paired_identity', {})
            if not paired.get('pointer') and not paired.get('keyboard'):
                raise HidUnavailable('paired_identity_unavailable')
        mouse = HidMouse(config['pointer_device'])
        keyboard = HidKeyboard(config['keyboard_device'])
    except Exception as exc:
        reason = str(exc) if str(exc) else type(exc).__name__
        with contextlib.suppress(Exception):
            _safe_runtime(root)
            _write_status(root, {'pid': os.getpid(), 'state': 'failed',
                                 'reason': reason[:80], 'version': INPUT_HID_VERSION})
        print(reason, file=sys.stderr)
        return 1
    try:
        _serve(root, config, mouse, keyboard, rng)
    except Exception as exc:
        print(str(exc) or type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        keyboard.release_all()
        mouse.close()
        keyboard.close()
    return 0


# ---------------------------------------------------------------- clients

def hid_supported():
    """True when this process may create uinput devices."""
    try:
        fd = os.open('/dev/uinput', os.O_RDWR | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
    except OSError:
        return False
    os.close(fd)
    return True


class _HidClient:
    """Spawns and talks to the single HID helper; never guesses on failure."""

    def __init__(self, target=None, runtime=None, gui_environment=None, config_path=None):
        self.root = Path(runtime or RUNTIME_ROOT)
        self.path = self.root / 'hid.sock'
        self.target = target
        self.env = dict(gui_environment or os.environ)
        self.config_path = Path(config_path) if config_path else CONFIG_FILE
        self._ready_pid = None

    def _helper_running(self, pid):
        if type(pid) is not int or pid <= 1:
            return False
        try:
            cmd = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
            return any(part.endswith(b'uinput_hid.py') for part in cmd)
        except OSError:
            return False

    def _read_status(self):
        status = self.root / 'status.json'
        if status.is_symlink():
            raise HidUnavailable('desktop_hid_status_unavailable')
        try:
            return json.loads(status.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    @contextlib.contextmanager
    def _spawn_lock(self):
        """Serialise helper startup so pointer and keyboard share one daemon."""
        if fcntl is None:
            yield
            return
        lock_path = self.root / 'spawn.lock'
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def ensure_ready(self, timeout=30):
        if not hid_supported():
            raise HidUnavailable('uinput_unavailable')
        module = Path(__file__).resolve()
        if module.is_symlink() or not module.is_file():
            raise HidUnavailable('desktop_hid_module_unavailable')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_runtime(self.root)
        with self._spawn_lock():
            current = self._read_status()
            pid = current.get('pid')
            if not self._helper_running(pid):
                if self.path.is_symlink():
                    raise HidUnavailable('desktop_hid_socket_unavailable')
                if self.path.exists():
                    info = self.path.stat()
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                        raise HidUnavailable('desktop_hid_socket_unavailable')
                    self.path.unlink()
                python = '/usr/bin/python3' if Path('/usr/bin/python3').exists() else sys.executable
                proc = subprocess.Popen([python, '-B', str(module), str(self.root), str(self.config_path)],
                                        env=self.env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
                pid = proc.pid
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self._read_status()
            if current.get('pid') == pid and current.get('state') == 'ready':
                if not self.path.exists() or self.path.is_symlink() or \
                        not stat.S_ISSOCK(self.path.stat().st_mode):
                    raise HidUnavailable('desktop_hid_socket_unavailable')
                self._ready_pid = pid
                return
            if current.get('pid') == pid and current.get('state') == 'failed':
                raise HidUnavailable(current.get('reason') or 'desktop_hid_authorization_failed')
            if not self._helper_running(pid):
                raise HidUnavailable('desktop_hid_helper_stopped')
            await asyncio.sleep(.15)
        raise HidUnavailable('desktop_hid_authorization_timeout')

    async def _send(self, action, timeout=3, **values):
        if self._ready_pid is None:
            await self.ensure_ready()
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.path)), timeout)
        try:
            writer.write((json.dumps({'action': action, **values}, allow_nan=False) + '\n').encode())
            await asyncio.wait_for(writer.drain(), timeout)
            response = json.loads(await asyncio.wait_for(reader.readline(), timeout))
            if response.get('ok') is not True:
                raise HidInputError(response.get('error') or 'desktop_hid_unavailable')
        except (OSError, asyncio.TimeoutError, ValueError):
            self._ready_pid = None  # the helper died or is unreachable: re-check next call
            raise
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class HidPointerClient(_HidClient):
    ready = staticmethod(hid_supported)

    async def move(self, dx, dy):
        await self._send('move', dx=dx, dy=dy)

    async def scroll(self, steps):
        await self._send('scroll', steps=steps)

    async def click(self):
        await self._send('click')


class HidKeyboardClient(_HidClient):
    ready = staticmethod(hid_supported)

    async def type(self, text, guard=None, chunk=24):
        """Type text on the device; ``guard`` re-confirms focus between chunks.

        The helper types a whole request without any window awareness, so a
        focus switch mid-prompt would send the remaining characters to whatever
        window now owns the keyboard. With a guard, the prompt is split into
        chunks and the caller re-confirms the target window before each one;
        the first failed check stops sending immediately.
        """
        if guard is None:
            await self._send('type', timeout=90, text=text)
            return
        for start in range(0, len(text), chunk):
            if not await guard():
                raise HidInputError('window_focus_lost')
            await self._send('type', timeout=90, text=text[start:start + chunk])

    async def paste(self):
        await self._send('paste', timeout=10)


if __name__ == '__main__':
    raise SystemExit(main())
