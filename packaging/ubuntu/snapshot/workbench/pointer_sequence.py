"""Pointer route for maintenance and audit records for desktop input."""
import time
import json
import os
from pathlib import Path

INPUT_SEQUENCE_VERSION = "observed-pointer-correction-20261108"

def record_input_action(target, action, **extra):
    root = Path.home()/".local/state/creative-unified-workbench/desktop-pointer"
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise PointerPreparationError("input_audit_unavailable")
    path = root/"input-actions.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os,"O_NOFOLLOW",0), 0o600)
    event = {"time":time.time(),"version":INPUT_SEQUENCE_VERSION,"account_key":target.account_key,
             "window":target.window_id,"workspace":target.workspace,"action":action,**extra}
    with os.fdopen(fd,"a",encoding="utf-8") as stream:
        stream.write(json.dumps(event,separators=(",",":"))+"\n")

FOCUS_MOTION_STEPS = 10


def focus_path(sx, sy, width, height, rng):
    """Bounded curve from the screen corner to a verified maintenance target."""
    ax, ay = width - 1, height - 1
    dx, dy = sx - ax, sy - ay
    distance = max(1, (dx * dx + dy * dy) ** .5)
    bend = rng.uniform(-1, 1) * min(80, distance * .08)
    offset_x, offset_y = -dy / distance * bend, dx / distance * bend
    points = []
    for step in range(1, FOCUS_MOTION_STEPS + 1):
        t = step / FOCUS_MOTION_STEPS
        arc = 4 * t * (1 - t)
        x = min(width - 3, max(2, ax + dx * t + offset_x * arc))
        y = min(height - 3, max(2, ay + dy * t + offset_y * arc))
        points.append((x, y))
    points[-1] = (sx, sy)
    return points


class PointerPreparationError(Exception):
    pass
