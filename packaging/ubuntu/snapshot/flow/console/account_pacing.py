"""Read Flow generation restrictions and support explicit, audited manual recovery."""

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from studiowright.store import OPERATOR_PAUSE_SCHEMA


def account_status(accounts, database, now=None):
    now = time.time() if now is None else now
    rows = [dict(account) for account in accounts]
    for row in rows:
        row.update(generation_state="unknown", cooldown_until=None, cooldown_remaining=None,
                   risk_paused=None, risk_review_required=None, operator_paused=None, risk_consecutive=None)
    try:
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            cooldowns = dict(db.execute("SELECT profile,cooldown_until FROM pacing"))
            try:
                risk = {profile: (paused, consecutive) for profile, paused, consecutive in
                        db.execute("SELECT profile,paused,consecutive FROM flow_risk")}
                has_operator_pauses = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='flow_operator_pauses'").fetchone()
                operator_pauses = {profile for profile, in db.execute(
                    "SELECT profile FROM flow_operator_pauses")} if has_operator_pauses else set()
                risk_known = True
            except sqlite3.Error:
                risk, operator_pauses, risk_known = {}, set(), False
    except (sqlite3.Error, OSError):
        # An unreadable source must never be presented as a confirmed absence of cooldown.
        return rows
    for row in rows:
        try:
            until = float(cooldowns.get(row["profile"]) or 0)
            paused, consecutive = risk.get(row['profile'], (0, 0)) if risk_known else (None, None)
            if not risk_known:
                paused, consecutive = None, None
            elif type(paused) is not int or paused not in (0, 1) or type(consecutive) is not int or consecutive < 0:
                continue
        except (ValueError, TypeError):
            continue
        if not math.isfinite(until):
            continue
        remaining = max(0, math.ceil(until - now))
        operator_paused = row['profile'] in operator_pauses
        row.update(
            generation_state="cooling_down" if remaining else "not_cooling_down",
            cooldown_until=until or None,
            cooldown_remaining=remaining,
            risk_paused=(bool(paused) or operator_paused) if risk_known else None,
            risk_review_required=bool(paused) if risk_known else None,
            operator_paused=operator_paused if risk_known else None, risk_consecutive=consecutive,
        )
    return rows


def resume_after_review(database, profile, now=None):
    """Clear only the human-review pause; retain any unexpired cooldown."""
    path = Path(database)
    if path.is_symlink() or not path.is_file():
        raise ValueError('账号限制状态暂不可读取。')
    now = time.time() if now is None else now
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        result = db.execute("UPDATE flow_risk SET paused=0,consecutive=0,resumed_at=? WHERE profile=? AND paused=1", (now, profile))
        if result.rowcount != 1:
            raise ValueError('该账号当前没有需要人工恢复的暂停。')
        db.execute("INSERT INTO flow_risk_events(profile,action,created) VALUES(?,'manual_resume',?)", (profile, now))


def set_operator_pause(database, profile, paused, now=None):
    """Update the operator hold and record each actual change."""
    path = Path(database)
    if path.is_symlink() or not path.is_file():
        raise ValueError('账号限制状态暂不可读取。')
    now = time.time() if now is None else now
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(OPERATOR_PAUSE_SCHEMA)
        if paused:
            changed = db.execute("INSERT OR IGNORE INTO flow_operator_pauses VALUES(?,?)", (profile, now)).rowcount
        else:
            changed = db.execute("DELETE FROM flow_operator_pauses WHERE profile=?", (profile,)).rowcount
        if changed:
            action = 'operator_pause' if paused else 'operator_resume'
            db.execute("INSERT INTO flow_risk_events(profile,action,created) VALUES(?,?,?)", (profile, action, now))
