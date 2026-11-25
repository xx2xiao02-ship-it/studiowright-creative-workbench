"""Workbench using the existing Flow accounts and source-specific execution."""
from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal
import hashlib
import http.client
import http.cookiejar
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
from dispatch_limit import DispatchBusy
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from music_recovery import DATABASE as MUSIC_DATABASE, MUSIC_HOME


ROOT = Path(__file__).resolve().parent
FLOW_READINESS_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)
MAX_RESPONSE = 16 * 1024 * 1024
APPLY_LIBRARY_TAGS = r'''
import json,os,pathlib,re,sqlite3,subprocess,sys
payload=json.load(sys.stdin);ident=payload.get('asset_id');values=payload.get('tags')
if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{64}',ident) or not isinstance(values,dict): raise ValueError('invalid')
pid=subprocess.check_output(['systemctl','--user','show','studiowright-console.service','--property=MainPID','--value'],text=True,timeout=5).strip()
if not pid.isdigit() or pid=='0': raise ValueError('service_unavailable')
env=dict(p.split(b'=',1) for p in pathlib.Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in p)
home=pathlib.Path.home().resolve()
root=pathlib.Path(os.fsdecode(env.get(b'SW_ROOT',os.fsencode(home/'.local/share/studiowright-evaluation')))).resolve()
program=pathlib.Path(os.readlink('/proc/'+pid+'/cwd')).resolve();sys.path.insert(0,str(program))
from material_library import Library
from material_tags import DIMENSIONS,clean_dimensions
tags=clean_dimensions({k:v for k,v in values.items() if k in DIMENSIONS and v})
libraryroot=(root/'material-library').resolve();database=libraryroot/'library.db'
if database.is_symlink() or not database.is_file(): raise ValueError('library_unavailable')
library=Library.__new__(Library);library.root=libraryroot;library.db=database
with library.connect() as c:
 c.execute('BEGIN IMMEDIATE')
 if not c.execute('SELECT 1 FROM assets WHERE id=?',(ident,)).fetchone(): raise ValueError('asset_missing')
 existing={r[0] for r in c.execute('SELECT DISTINCT dimension FROM dimension_tags WHERE asset_id=?',(ident,))}
 manual={r[0] for r in c.execute('SELECT dimension FROM manual_dimensions WHERE asset_id=?',(ident,))}
 chosen={k:v for k,v in tags.items() if k not in existing and k not in manual}
 library.write_dimensions(c,ident,chosen,'visual_analysis')
 c.commit()
print(json.dumps({'added_dimensions':len(chosen)}))
'''
ALLOWED_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/api-docs.js": ("api-docs.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


@lru_cache(maxsize=8)
def _asset_digest(path, fingerprint):
    return hashlib.sha256(path.read_bytes()).digest()


def frontend_revision(index=None):
    revision = hashlib.sha256()
    for name, _ in ALLOWED_STATIC.values():
        path = ROOT / name
        if name == "index.html" and index is not None:
            revision.update(hashlib.sha256(index).digest())
        else:
            st = path.stat()
            revision.update(_asset_digest(path, (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)))
    return revision.hexdigest()


def clean_text(value, limit=300):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]


def discover_new_materials(sources, real_pool, since):
    """Scan recent library pages, retaining only items added after activation."""
    for page in range(1, 101):
        with sources.library('GET', '/api/library?page=' + str(page) + '&limit=100') as upstream:
            if upstream.status != 200:
                raise ValueError('素材库暂时不可读取。')
            data = upstream.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError('素材列表过大。')
        data = json.loads(data.decode('utf-8'))
        items, pages = data.get('items'), data.get('pages')
        if not isinstance(items, list) or type(pages) is not int or pages < 1:
            raise ValueError('素材列表不完整。')
        real_pool.discover_auto_materials(items)
        older = any(type(x.get('added')) in (int, float) and x['added'] < since for x in items if isinstance(x, dict))
        if not items or older or page >= pages:
            break


def run_auto_material_once(sources, real_pool):
    from material_dispatch import run_once
    state = real_pool.auto_material_state()
    if real_pool.maintenance() or not state['enabled'] or state['paused_reason'] or state['since'] is None:
        return
    discover_new_materials(sources, real_pool, state['since'])
    if state['counts'].get('waiting') or real_pool.auto_material_state()['counts'].get('waiting'):
        run_once(sources, real_pool)


def read_all_materials(sources):
    """Read a complete library snapshot; fail rather than queue a partial list."""
    result = []
    expected_total = expected_pages = None
    seen = set()
    for page in range(1, 101):
        with sources.library('GET', '/api/library?page=' + str(page) + '&limit=100') as upstream:
            if upstream.status != 200:
                raise ValueError('素材库暂时不可读取。')
            raw = upstream.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError('素材列表过大。')
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict):
            raise ValueError('素材列表不完整。')
        items, pages, total = data.get('items'), data.get('pages'), data.get('total')
        if (not isinstance(items, list) or type(pages) is not int or not 1 <= pages <= 100 or page > pages
                or type(total) is not int or total < 0):
            raise ValueError('素材列表不完整。')
        if expected_total is None:
            expected_total, expected_pages = total, pages
        if total != expected_total or pages != expected_pages:
            raise ValueError('素材列表已变化，请刷新后重试。')
        if page < pages and not items:
            raise ValueError('素材列表不完整。')
        for item in items:
            ident = item.get('id') if isinstance(item, dict) else None
            if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{64}', ident) or ident in seen:
                raise ValueError('素材列表不完整或已变化。')
            seen.add(ident)
        result.extend(items)
        if page == pages:
            if len(result) != expected_total:
                raise ValueError('素材列表不完整或已变化。')
            return result
    raise ValueError('素材列表超过处理上限。')


def run_backfill_once(sources, real_pool, backfill):
    from material_dispatch import run_once
    run_once(sources, real_pool, backfill)


def identity_text(value):
    # Never trim or truncate source identities: that could merge distinct projects.
    if not isinstance(value, str) or not value or len(value) > 256 or re.search(r"[\x00-\x1f\x7f]", value):
        return ""
    return value


def numeric_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def project_key(profile, project):
    """Keep provider and complete source identity distinct, even with repeated names."""
    encoded = json.dumps(["flow", profile, project], ensure_ascii=False, separators=(",", ":"))
    return "flow-project-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def project_rows(state):
    rows = []
    for row in state.get("projects", []):
        if not isinstance(row, dict):
            continue
        profile = identity_text(row.get("profile"))
        ident = identity_text(row.get("id"))
        if not profile or not ident:
            continue
        rows.append({
            "key": project_key(profile, ident),
            "source": "Flow",
            "source_id": ident,
            "source_profile": profile,
            "title": clean_text(row.get("title"), 180) or "未命名项目",
            "checked": numeric_time(row.get("checked")),
        })
    return rows


def open_flow_project_names(state, projects):
    """Overlay only current titles from the exact already-open account/project tab."""
    if state.get('test') is True or not isinstance(state.get('accounts'), list):
        return
    helper = ROOT / 'flow_open_project_names.py'
    payload = {'accounts': state['accounts'], 'projects': state['projects']}
    names = []
    try:
        if not helper.is_file() or helper.is_symlink():
            raise ValueError('name_reader_unavailable')
        pid = subprocess.check_output(['systemctl', '--user', 'show', 'studiowright-console.service',
                                       '--property=MainPID', '--value'], text=True, timeout=3).strip()
        if not pid.isdigit() or pid == '0':
            raise ValueError('flow_unavailable')
        python = Path(os.fsdecode(Path('/proc', pid, 'cmdline').read_bytes().split(b'\0', 1)[0]))
        if not python.is_absolute() or not python.is_file():
            raise ValueError('flow_python_unavailable')
        result = subprocess.run([str(python), '-B', str(helper)],
                                input=json.dumps(payload, ensure_ascii=True).encode(),
                                capture_output=True, timeout=15, check=True)
        names = json.loads(result.stdout)
        if not isinstance(names, list):
            names = []
    except (OSError, ValueError, subprocess.SubprocessError):
        names = []
    try:
        from project_name_cache import merge
        merge(projects, names)
    except (OSError, ValueError, ImportError, sqlite3.Error):
        pass


def model_rows(state):
    source = state.get('models', {})
    result = {'image': [], 'video': []}
    if not isinstance(source, dict):
        return result
    for kind in result:
        entries = source.get(kind, [])
        if not isinstance(entries, list):
            continue
        seen = set()
        for row in entries:
            if not isinstance(row, dict):
                continue
            ident = identity_text(row.get('id'))
            if not ident or ident in seen:
                continue
            seen.add(ident)
            result[kind].append({'id': ident, 'label': clean_text(row.get('label'), 120) or ident})
    return result


def task_failure_reason(row):
    """Display a stored failure without inspecting the browser or running a task."""
    if row.get('status') != 'failed':
        return ''
    error = clean_text(row.get('error'), 4000)
    if not error:
        return '后台未提供具体失败原因。'
    known = {
        'pasted_text_mismatch': '提示词粘贴后校验不一致，任务在发送前停止。',
        'desktop_keyboard_not_ready': 'Ubuntu 桌面键盘控制未就绪，任务在粘贴和发送前停止。',
        'desktop_keyboard_authorization_timeout': '等待 Ubuntu 桌面键盘控制授权超时，未粘贴或发送。',
        'desktop_binding_unconfirmed': '无法确认账号对应的浏览器窗口，任务在发送前停止。',
        'desktop_focus_unconfirmed': '未能确认浏览器窗口已激活，任务在发送前停止。',
        'input_focus_unconfirmed': '未能确认输入框已获得焦点，任务在发送前停止。',
        'flow_window_registration_stale': '浏览器窗口登记信息已变化，任务在发送前停止。',
        'task_window_process_unconfirmed': '无法确认账号浏览器进程，未输入或发送任务。',
        'task_window_process_changed': '账号浏览器已重新启动，需要重新核对窗口绑定。',
        'task_window_account_mismatch': '任务窗口与目标账号不一致，未输入或发送任务。',
        'task_window_project_unconfirmed': '任务网页与指定项目不一致，未输入或发送任务。',
        'task_window_identity_unconfirmed': '窗口绑定信息不完整，未输入或发送任务。',
        'task_window_identity_changed': '核对期间窗口或网页发生变化，未输入或发送任务。',
        'task_window_binding_conflict': '任务窗口绑定冲突，已保留所有窗口和草稿，未发送任务。',
        'task_window_registry_unavailable': '窗口绑定记录暂不可读取，未发送任务。',
        'existing_flow_draft': '网页中有未发送的草稿，已保留草稿并停止本次输入。',
        'existing_flow_attachments': '网页中已有参考素材，已保留并停止本次输入。',
        'pointer_not_over_input': '未能确认鼠标位于输入框，任务在发送前停止。',
        'input_moved_after_scroll': '滚动后输入框位置发生变化，任务在发送前停止。',
        'flow_insufficient_credits': 'Flow 提示当前账号的可用积分不足。',
        'previous_task_result_unconfirmed': '该账号上次任务的结果仍待核对，未开始新生成。',
        'multiple_account_task_windows': '发现多个标记为任务用途的窗口，已保留现场，未猜测或关闭窗口。',
        'task_window_requires_login': '任务窗口需要完成登录或验证，已保留窗口，未重新生成或换号。',
        'submission_outcome_unknown': '发送结果尚未确认，已保留原窗口和任务，禁止重复生成。',
        'window_activation_unconfirmed': '自动恢复窗口焦点后仍未确认激活，未发送生成请求。',
        'composer_not_empty': '输入框已有内容，已保留草稿，未重复粘贴。',
    }
    if error in known:
        return known[error]
    if error.startswith('pre_submit_failed:'):
        reason=error.split(':',1)[1]
        return '输入准备自救后仍未完成：' + known.get(reason, reason) + '；本次未重新生成。'
    # Keep unfamiliar errors useful while concealing addresses and credential values.
    error = re.sub(r'https?://[^\s<>"\)]+', '[服务地址已省略]', error)
    error = re.sub(r'(?i)\b(?:bearer)\s+\S+', 'Bearer [已隐藏]', error)
    error = re.sub(r"""(?ix)(["']?(?:cookie|csrf|authorization|token|access_token|refresh_token|sapisid|sid|api_key)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)""", r'\1[已隐藏]', error)
    return error[:600]


def task_rows(state, projects):
    titles = {row["key"]: row["title"] for row in projects}
    rows = []
    for row in state.get("tasks", []):
        if not isinstance(row, dict):
            continue
        ident = identity_text(row.get("id"))
        if not ident:
            continue
        profile = identity_text(row.get("effective_profile") or row.get("profile"))
        project = identity_text(row.get("effective_project") or row.get("project"))
        key = project_key(profile, project) if profile and project else None
        downloads = []
        raw_downloads = row.get('downloads', [])
        if isinstance(raw_downloads, list):
            for item in raw_downloads:
                if not isinstance(item, dict):
                    continue
                path = item.get('url')
                if isinstance(path, str) and re.fullmatch(r'/api/tasks/' + re.escape(ident) + r'/files/[0-9]+', path):
                    downloads.append({'path': path, 'name': clean_text(item.get('name'), 180) or '下载文件'})
        note = ''
        result = row.get('result')
        source_jobs = result.get('source_jobs', []) if isinstance(result, dict) else []
        if (row.get('status') == 'failed' and isinstance(source_jobs, list)
                and any(isinstance(job, dict) and job.get('stage') == 'generated'
                        and isinstance(job.get('media_ids'), list) and job['media_ids']
                        and not job.get('files') for job in source_jobs)):
            note = '已返回素材编号，文件未下载；请到原后台核对。'
        recoverable = (row.get('status') == 'failed' and row.get('kind') == 'image'
                       and isinstance(source_jobs, list) and sum(1 for job in source_jobs if isinstance(job, dict)
                           and job.get('stage') == 'generated' and isinstance(job.get('media_ids'), list)
                           and len(job['media_ids']) == 1 and not job.get('files')) == 1
                       and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident)))
        raw_retry = row.get('retry')
        retry = None
        if isinstance(raw_retry, dict) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(raw_retry.get('root_id', ''))):
            ordinal = raw_retry.get('ordinal')
            if type(ordinal) is int and 0 <= ordinal <= 3 and raw_retry.get('reason') in ('original', 'same_account', 'switch_account'):
                retry = {'root_id': raw_retry['root_id'], 'ordinal': ordinal,
                         'reason': raw_retry['reason'], 'state': raw_retry.get('state')}
                retry['attempts'] = [
                    {'id':identity_text(a.get('id')), 'ordinal':a['ordinal'], 'profile':identity_text(a.get('profile')),
                     'status':clean_text(a.get('status'),80), 'started':numeric_time(a.get('started')),
                     'finished':numeric_time(a.get('finished')), 'failure_reason':task_failure_reason(a)}
                    for a in (raw_retry.get('attempts') or [])[:4] if isinstance(a,dict)
                    and type(a.get('ordinal')) is int and 0 <= a['ordinal'] <= 3]
                if not note and retry['state'] in ('needs_review','interrupted'):
                    note = ('Flow 拒绝生成并提示异常活动，已暂停；未换号重发。'
                            if 'PUBLIC_ERROR_UNUSUAL_ACTIVITY' in str(row.get('error') or '')
                            else '原结果需核对，已停止重试。')
                if not note and retry['state'] == 'succeeded' and len(retry['attempts']) > 1:
                    note = '换号重试已完成，仍用原任务编号查询和下载。'
                if not note and retry['state'] == 'pending_switch':
                    note = '正在等待另一个可用账号及其 Flow 项目。'
                elif not note and retry['state'] == 'succeeded' and row.get('status') == 'failed':
                    note = '关联的后续重试已成功。'
                elif not note and retry['state'] == 'active' and row.get('status') == 'failed':
                    note = '已安排后续重试，请查看关联任务。'
                elif not note and row.get('status') == 'failed':
                    note = {'needs_review':'结果需核对，停止重试。','exhausted':'已达到 3 次换号重试上限。',
                            'references_unavailable':'原参考素材不可用，停止重试。','no_account':'等待超时，没有符合条件的其他账号。'}.get(retry['state'])
        if not note and not retry and row.get('kind') in ('image', 'video') and row.get('status') == 'failed':
            note = '这条任务未启用自动换号重试，没有重试记录。'
        attempts = retry['attempts'] if retry and retry['root_id'] == ident else []
        latest = max(attempts, key=lambda attempt: attempt['ordinal'], default=None)
        params = row.get('params')
        manual_retry_ordinal = (latest['ordinal'] if latest and row.get('kind') == 'image'
            and row.get('status') == 'failed' and isinstance(params, dict) and params.get('count') == 1
            and retry['state'] in ('manual_failed', 'no_account') and latest['status'] == 'failed'
            and latest['finished'] and latest['ordinal'] < 3 else None)
        rows.append({
            "key": "flow-task-" + hashlib.sha256(ident.encode("utf-8")).hexdigest(),
            "source": "Flow",
            "source_id": ident,
            "source_profile": profile,
            "project_key": key,
            "project_title": titles.get(key, "未登记项目" if key else "未关联项目"),
            "kind": clean_text(row.get("kind"), 80),
            "status": clean_text(row.get("status"), 80),
            "created": numeric_time(row.get("created")),
            "finished": numeric_time(row.get("finished")),
            "downloads": downloads,
            **({"style_id": row["params"]["style_id"]} if isinstance(row.get("params"), dict) and "style_id" in row["params"] else {}),
            "self_recovery": [
                {"action": clean_text(e.get('action'),80), "created": numeric_time(e.get('created'))}
                for e in (result.get('self_recovery',[]) if isinstance(result,dict) and isinstance(result.get('self_recovery'),list) else [])[-40:]
                if isinstance(e,dict)
            ],
            "note": note,
            "failure_reason": task_failure_reason(row),
            "recoverable": recoverable,
            "manual_retry_ordinal": manual_retry_ordinal,
            "retry": retry,
        })
    return rows


def account_rows(state):
    """Project existing Flow account status; never copy credentials or guess Gemini bindings."""
    accounts = state.get("accounts")
    if not isinstance(accounts, list) or state.get("test") is True:
        return None
    insights = state.get("account_insights")
    insight_rows = insights.get("accounts", []) if isinstance(insights, dict) else []
    if not isinstance(insight_rows, list):
        insight_rows = []
    by_profile = {}
    duplicates = set()
    for row in insight_rows:
        if not isinstance(row, dict):
            continue
        profile = identity_text(row.get("profile"))
        if not profile:
            continue
        if profile in by_profile:
            duplicates.add(profile)
        else:
            by_profile[profile] = row
    profiles = set()
    rows = []
    for row in accounts:
        if not isinstance(row, dict):
            continue
        profile = identity_text(row.get("profile"))
        # Auxiliary export browser is not a dispatchable account.
        if not profile or profile == "music-export" or profile in profiles:
            continue
        profiles.add(profile)
        info = by_profile.get(profile, {}) if profile not in duplicates else {}
        email = identity_text(row.get("email"))
        if not re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}", email):
            email = ""
        raw_health = info.get("health")
        health = raw_health if isinstance(raw_health, str) and raw_health in {"ready", "cooling", "attention", "empty", "paused"} else "unknown"
        preflight = row.get('dispatch_preflight_reasons')
        preflight = [reason for reason in preflight if isinstance(reason, str)] if isinstance(preflight, list) else []
        incident = row.get('flow_page_incident')
        if not (isinstance(incident, dict) and incident.get('profile') == profile
                and isinstance(incident.get('id'), str) and re.fullmatch(r'[0-9a-f]{32}', incident['id'])):
            incident = None
        if incident:
            health = 'paused'
        elif preflight and health not in ('paused', 'cooling'):
            health = 'attention'
        balance = info.get("balance")
        if isinstance(balance, bool) or not isinstance(balance, (int, float)) or not math.isfinite(balance) or balance < 0:
            balance = None
        network = row.get('exit_info')
        port = network.get('port') if isinstance(network, dict) else None
        exit_info = ({'port': port, 'public_ip': clean_text(network.get('public_ip'), 64) or None,
                      'ipv6': clean_text(network.get('ipv6'), 64) or None}
                     if type(port) is int and 1 <= port <= 65535 else None)
        rows.append({"key": "flow-account-" + hashlib.sha256(profile.encode("utf-8")).hexdigest(),
                     "exit_info": exit_info,
                     "profile": profile, "label": clean_text(row.get("label"), 120), "email": email,
                     "flow_login": "verified" if row.get("state") == "verified" else "unknown",
                     "flow_removed": row.get("flow_removed") == 1,
                     "flow_health": health, "flow_balance": balance,
                     "flow_preflight_reasons": preflight,
                     "flow_page_incident": ({'id': incident['id'], 'phase': incident.get('phase'),
                                             'has_screenshot': incident.get('has_screenshot') == 1,
                                             'destroyed': incident.get('screenshot_destroyed') == 1} if incident else None),
                     "flow_next_credit_date": info.get("next_credit_date"),
                     "flow_balance_error": clean_text(info.get("balance_error"), 200),
                     "flow_balance_fresh": info.get("balance_fresh") is True,
                     "flow_balance_observed": numeric_time(info.get("balance_observed")),
                     "flow_cooldown_seconds": max(0, numeric_time(row.get("cooldown_remaining")) or 0),
                     "flow_cooling": row.get('generation_state') == 'cooling_down',
                     "flow_pacing_known": row.get('generation_state') in ('cooling_down', 'not_cooling_down'),
                     "flow_risk_known": type(row.get('risk_paused')) is bool,
                     "flow_risk_paused": row.get('risk_paused') is True,
                     "flow_risk_consecutive": row.get('risk_consecutive') if type(row.get('risk_consecutive')) is int and row.get('risk_consecutive') >= 0 else None,
                     "gemini": "unlinked"})
    return rows


def flow_insights_summary(state):
    """Keep the original console's coverage and never turn missing balances into zero."""
    source = state.get('account_insights') if isinstance(state, dict) else None
    if not isinstance(source, dict):
        return None
    def number(value):
        return value if type(value) in (int,float) and math.isfinite(value) and value >= 0 else None
    def usage(name):
        item=source.get(name)
        if not isinstance(item,dict):
            return None
        return {'recorded_usage':number(item.get('recorded_usage')),
                'completed':number(item.get('completed')),'unpriced':number(item.get('unpriced')),
                'unresolved':number(item.get('unresolved')),
                'coverage':item.get('coverage') if item.get('coverage') in ('not_recorded','partial','recorded') else 'unknown'}
    health=source.get('health_counts') if isinstance(source.get('health_counts'),dict) else {}
    return {'healthy_accounts':number(source.get('healthy_accounts')),
            'total_accounts':number(source.get('total_accounts')),
            'health_counts':{k:number(health.get(k)) for k in ('ready','cooling','attention','empty','paused')},
            'pool_balance':number(source.get('pool_balance')),'available_today':number(source.get('available_today')),
            'balance_known_accounts':number(source.get('balance_known_accounts')),
            'balance_fresh_accounts':number(source.get('balance_fresh_accounts')),
            'balance_oldest_observed':number(source.get('balance_oldest_observed')),
            'today':usage('today'),'yesterday':usage('yesterday'),
            'today_date':clean_text(source.get('today_date'),20),'updated':number(source.get('updated'))}


def counts_only(payload):
    counts = payload.get("counts", {}) if isinstance(payload, dict) else {}
    media = counts.get("media_type", {}) if isinstance(counts, dict) else {}

    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return {
        "total": count(counts.get("total")),
        "images": count(media.get("image", 0)) if isinstance(media, dict) else None,
        "videos": count(media.get("video", 0)) if isinstance(media, dict) else None,
    }


def mp4_metadata(path):
    """Read MP4 headers only; never send media to an external service."""
    def boxes(stream, start, end):
        offset, count = start, 0
        while offset + 8 <= end and count < 10000:
            stream.seek(offset)
            header = stream.read(8)
            size, kind = struct.unpack('>I4s', header)
            header_size = 8
            if size == 1:
                extended = stream.read(8)
                if len(extended) != 8: break
                size, header_size = struct.unpack('>Q', extended)[0], 16
            elif size == 0:
                size = end - offset
            if size < header_size or offset + size > end:
                break
            yield kind, offset + header_size, offset + size
            offset += size
            count += 1

    result = {'duration': None, 'width': None, 'height': None}
    with Path(path).open('rb') as stream:
        end = stream.seek(0, 2)
        moov = next(((a,b) for k,a,b in boxes(stream, 0, end) if k == b'moov'), None)
        if not moov:
            return result
        for kind, start, stop in boxes(stream, *moov):
            if kind == b'mvhd':
                stream.seek(start)
                body = stream.read(min(40, stop-start))
                if len(body) >= 20:
                    if body[0] == 1 and len(body) >= 32:
                        scale, duration = struct.unpack('>IQ', body[20:32])
                    elif body[0] == 0:
                        scale, duration = struct.unpack('>II', body[12:20])
                    else:
                        continue
                    if scale and duration not in (0xffffffff, 0xffffffffffffffff):
                        result['duration'] = duration / scale
            elif kind == b'trak':
                track_header, video = None, False
                for subkind, substart, subend in boxes(stream, start, stop):
                    if subkind == b'tkhd' and subend - substart >= 8:
                        track_header = (subend - 8, subend)
                    elif subkind == b'mdia':
                        for nested, nstart, nend in boxes(stream, substart, subend):
                            if nested == b'hdlr' and nend - nstart >= 12:
                                stream.seek(nstart + 8)
                                video = stream.read(4) == b'vide'
                if video and track_header:
                    stream.seek(track_header[0])
                    width, height = struct.unpack('>II', stream.read(8))
                    width, height = width >> 16, height >> 16
                    if 0 < width <= 65535 and 0 < height <= 65535:
                        result['width'], result['height'] = width, height
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even a compromised local service cannot redirect this adapter elsewhere.
        return None


def check_library_storage_target(current, requested, required_bytes):
    """Validate a proposed destination without creating or changing anything there."""
    if (not isinstance(requested, str) or not requested or len(requested) > 1024
            or re.search(r'[\x00-\x1f\x7f]', requested)):
        raise ValueError('请输入有效的 Ubuntu 绝对路径。')
    candidate = Path(requested)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('请输入不包含 .. 的 Ubuntu 绝对路径。')
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError('请选择已存在的普通目录。')
        target = candidate.resolve(strict=True)
        original = Path(current).resolve(strict=True)
        if target == original:
            raise ValueError('这就是当前保存位置，不需要切换。')
        if target in original.parents or original in target.parents:
            raise ValueError('新目录不能是当前素材库或其上级、下级目录。')
        if not os.access(target, os.W_OK | os.X_OK):
            raise ValueError('工作台没有权限写入这个目录。')
        with os.scandir(target) as entries:
            if next(entries, None) is not None:
                raise ValueError('请选择空目录，以免与已有文件混在一起。')
        available = shutil.disk_usage(target).free
    except (OSError, RuntimeError):
        raise ValueError('无法读取该目录，请检查路径和磁盘连接。') from None
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError('暂时无法确认现有素材大小。')
    # This is a preliminary check, not permission to migrate; recheck before any switch.
    minimum = required_bytes + max(512 * 1024**2, required_bytes // 10)
    if available < minimum:
        raise ValueError('新位置可用空间不足，暂不能作为计划位置。')
    return {'target': str(target), 'available_bytes': available,
            'minimum_bytes': minimum, 'active': False}


def readiness_repair_accounts(payload, checks):
    """Only idle, enabled, unready accounts may be changed by the circle."""
    busy = set(payload.get('busy', []))
    return [a for a in payload['accounts']
            if a['repairable'] and a['profile'] not in busy and not a['review_task']
            and checks.get(a['profile']) not in ('ok', 'busy', 'login', 'verification_required')]


class Sources:
    """Only fixed localhost services and a small GET allowlist are reachable."""

    PATHS = {"flow": {"/health", "/api/state", "/api/library/facets"}, "gemini": {"/health"}}

    def __init__(self, flow_port=18888, gemini_port=8095, timeout=4, gemini_source="pooldesk"):
        self.ports = {"flow": int(flow_port), "gemini": int(gemini_port)}
        if any(not 1 <= port <= 65535 for port in self.ports.values()):
            raise ValueError("端口范围无效")
        if gemini_source not in {"pooldesk", "ubuntu-trial"}:
            raise ValueError("Gemini 服务类型无效")
        self.gemini_source = gemini_source
        self.timeout = timeout
        # Reuse the original console's local session in memory only.
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def read(self, service, path, *, retry_transient=False):
        if service == "gemini":
            return None, 'Gemini 使用官方 CLI 通道，由工作台直接调用；不探测独立后台。'
        if service not in self.PATHS or path not in self.PATHS[service]:
            raise ValueError("路径不在只读清单内")
        url = f"http://127.0.0.1:{self.ports[service]}{path}"
        request = Request(url, method="GET", headers={"Accept": "application/json"})
        started = time.monotonic()
        value, message, failure = self._read_once(request, self.timeout)
        if not retry_transient or failure is None:
            return value, message

        elapsed = time.monotonic() - started
        if failure in {'timeout', 'connection', 'http_500', 'http_502', 'http_503', 'http_504'}:
            LOGGER.warning('Flow account state read failed (%s, %.2fs); retrying once before submission.',
                           failure, elapsed)
            time.sleep(0.25)
            started = time.monotonic()
            value, message, failure = self._read_once(request, max(self.timeout, 6))
            if failure is None:
                return value, message
            elapsed = time.monotonic() - started
        LOGGER.warning('Flow account state unavailable before submission (%s, %.2fs).', failure, elapsed)
        return value, message

    def _read_once(self, request, timeout):
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                return None, "原后台返回的数据过大，暂时无法读取。", 'response_too_large'
            value = json.loads(body.decode("utf-8"))
            if not isinstance(value, dict):
                return None, "原后台返回的数据格式不正确。", 'invalid_response'
            return value, None, None
        except HTTPError as exc:
            with exc:
                if exc.code in (401, 403):
                    return None, "原后台需要登录或授权，请从原入口处理。", f'http_{exc.code}'
                return None, "原后台暂时无法读取，请稍后重试。", f'http_{exc.code}'
        except (OSError, http.client.HTTPException) as exc:
            cause = exc.reason if isinstance(exc, URLError) else exc
            failure = 'timeout' if isinstance(cause, TimeoutError) else 'connection'
            return None, "连接暂不可用，请检查对应后台。", failure
        except ValueError:
            return None, "原后台返回的数据格式不正确。", 'invalid_response'

    def library(self, method, target, data=None, range_header=None):
        """Forward only documented library operations to the existing loopback service."""
        parts = urlsplit(target)
        path = parts.path
        item_path = re.fullmatch(r'/api/library/[0-9a-f]{64}(?:/(?:file|thumbnail|tags))?', path)
        allowed_query = {'q', 'kind', 'tag', 'account', 'page', 'limit', 'offset', 'subject', 'scene',
                         'action', 'style', 'camera', 'color', 'keywords', 'model', 'project_id', 'account_id',
                         'created_from', 'created_to', 'index_status', 'storage_status', 'download', 'style_id', 'music_style', 'music_mood', 'music_instrument', 'music_use', 'music_voice',
                         'audio_category', 'duration_min', 'duration_max', 'shot_size', 'viewpoint',
                         'composition', 'movement'}
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=30)
        if (parts.scheme or parts.netloc or parts.fragment or len(parts.query) > 3000
                or any(k not in allowed_query or len(v) > 500 for k, v in query)
                or len({k for k, _ in query}) != len(query)
                or not (path in ('/api/library', '/api/library/facets') or item_path)
                or method not in ('GET', 'POST') or (method == 'POST' and not path.endswith('/tags'))
                or (method == 'GET' and path.endswith('/tags'))
                or (range_header and not re.fullmatch(r'bytes=\d*-\d*', range_header))):
            raise ValueError('素材请求格式不正确。')
        if method == 'POST' and (query or not isinstance(data, dict) or set(data) - {'tags', 'structured_tags'}):
            raise ValueError('标签内容不正确。')
        suffix = '?' + urlencode(query) if query else ''
        headers = {'Accept': 'application/json'}
        encoded = None
        if method == 'POST':
            headers.update({'Content-Type': 'application/json', 'Origin': f"http://127.0.0.1:{self.ports['flow']}", 'X-Console-Request': '1'})
            encoded = json.dumps(data, ensure_ascii=False).encode('utf-8')
        if range_header:
            headers['Range'] = range_header
        request = Request(f"http://127.0.0.1:{self.ports['flow']}{path}{suffix}", data=encoded, headers=headers, method=method)
        try:
            return self.opener.open(request, timeout=max(self.timeout, 15))
        except HTTPError as exc:
            return exc

    def flow_task(self, ident):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident):
            raise ValueError('任务编号不正确。')
        try:
            request = Request(f"http://127.0.0.1:{self.ports['flow']}/api/tasks/{ident}",
                              headers={'Accept': 'application/json'})
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                return 503, {'error':'任务状态暂不可读取。'}
            task = json.loads(raw)
            if not isinstance(task,dict):
                return 503, {'error':'任务状态暂不可读取。'}
            canonical = task.get('task_id')
            retry = task.get('retry')
            alias = (task.get('requested_task_id') == ident and isinstance(canonical, str)
                     and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', canonical)
                     and isinstance(retry, dict) and retry.get('root_id') == canonical
                     and retry.get('ordinal') == 0)
            if canonical != ident and not alias:
                return 503, {'error':'任务状态暂不可读取。'}
            return 200, task
        except HTTPError as exc:
            if exc.code == 404:
                return 404, {'error':'任务不存在。'}
        except (OSError, ValueError):
            pass
        return 503, {'error':'任务状态暂不可读取。'}

    def flow_retry(self, ident, ordinal):
        if not re.fullmatch(r'[a-f0-9]{16}', ident) or type(ordinal) is not int or not 0 <= ordinal < 3:
            raise ValueError('任务编号或尝试序号不正确。')
        origin = f"http://127.0.0.1:{self.ports['flow']}"
        request = Request(f'{origin}/api/tasks/{ident}/retry', method='POST',
                          data=json.dumps({'expected_ordinal': ordinal, 'confirm_generation': True}).encode(),
                          headers={'Content-Type':'application/json', 'X-Console-Request':'1', 'Origin':origin})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(8193))
                if response.status == 202 and isinstance(result, dict) and result.get('task_id') == ident:
                    return 202, result
        except HTTPError as exc:
            if exc.code in (400, 404, 409):
                try:
                    error = clean_text(json.loads(exc.read(8193)).get('error'), 180)
                except (ValueError, AttributeError, OSError):
                    error = ''
                return exc.code, {'error': error or '原任务目前无法重试。'}
        except (OSError, ValueError):
            pass
        return 503, {'error':'重试结果待核对，请刷新原任务记录。'}

    def remove_flow_account(self, key):
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
            raise ValueError('请选择已登记的 Flow 账号。')
        state, _ = self.read('flow', '/api/state')
        accounts = account_rows(state) if state is not None else None
        if accounts is None:
            return 503, {'error': 'Flow 账号名单暂不可读取。'}
        matches = [row['profile'] for row in accounts if row['key'] == 'flow-account-' + key and row['email']]
        if len(matches) != 1:
            raise ValueError('Flow 账号无法唯一匹配，请刷新。')
        origin = f"http://127.0.0.1:{self.ports['flow']}"
        request = Request(origin+'/api/accounts/flow-remove', method='POST',
                          data=json.dumps({'profile':matches[0],'confirmed':True}).encode(),
                          headers={'Content-Type':'application/json','Origin':origin,'X-Console-Request':'1'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(8193))
                if isinstance(result, dict) and result.get('removed') is True:
                    return 200, {'removed':True, 'message':clean_text(result.get('message'), 240) or '已从 Flow 号池移除。'}
        except HTTPError as exc:
            if exc.code == 409:
                try:
                    error = clean_text(json.loads(exc.read(8193)).get('error'), 180)
                except (ValueError, AttributeError, OSError):
                    error = ''
                return 409, {'error': error or '账号仍有未完成的操作，请结束后再移除。'}
        except (OSError, ValueError):
            pass
        return 503, {'error':'Flow 账号移除状态未确认，请刷新后核对。'}

    def flow_risk_resume(self, profile, incident=None):
        if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile):
            raise ValueError('账号标识不正确。')
        action = 'page-incident-review' if incident is not None else 'risk-resume'
        url = f"http://127.0.0.1:{self.ports['flow']}/api/accounts/{action}"
        data = {'profile': profile}
        if incident is not None:
            data['incident'] = incident
        body = json.dumps(data).encode('utf-8')
        request = Request(url, data=body, method='POST', headers={'Content-Type':'application/json',
                          'Origin': f"http://127.0.0.1:{self.ports['flow']}", 'X-Console-Request':'1'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(8193))
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError('Flow 恢复状态未确认，请刷新后核对。')
            return result
        except HTTPError as exc:
            if exc.code == 409:
                raise ValueError('这条警告已核查或已变化，请刷新。') from None
            if exc.code == 400:
                raise ValueError('该账号当前没有需要人工恢复的暂停。') from None
            raise ValueError('Flow 恢复暂不可用，请稍后核对。') from None

    def refresh_flow_balances_once(self, profiles=None):
        """Ask the existing Flow service once to read its existing browser panels."""
        port = self.ports['flow']
        data = {} if profiles is None else {'profiles': profiles}
        request = Request(f'http://127.0.0.1:{port}/api/accounts/credits/refresh', data=json.dumps(data).encode(), method='POST',
                          headers={'Content-Type':'application/json', 'X-Console-Request':'1',
                                   'Origin':f'http://127.0.0.1:{port}'})
        with self.opener.open(request, timeout=self.timeout) as response:
            result = json.loads(response.read(8193))
        return isinstance(result, dict) and (result.get('queued') is True or result.get('refreshing') is True)

    def flow_activation_check(self, profile, request_key):
        """Reuse the existing idempotent, non-generation Flow sync task."""
        if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile):
            raise ValueError('账号标识不正确')
        if not isinstance(request_key, str) or not re.fullmatch(r'activate-[0-9a-f]{32}-[0-9a-f]{32}', request_key):
            raise ValueError('检查编号不正确')
        port = self.ports['flow']
        body = json.dumps({'profile': profile, 'kind': 'sync', 'request_key': request_key, 'auto_retry': False}).encode()
        request = Request(f'http://127.0.0.1:{port}/api/tasks', data=body, method='POST',
                          headers={'Content-Type':'application/json', 'X-Console-Request':'1', 'Origin':f'http://127.0.0.1:{port}'})
        with self.opener.open(request, timeout=self.timeout) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('检查提交结果待核对')
        result = json.loads(raw)
        ident = result.get('id') if isinstance(result, dict) else None
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{16}', ident):
            raise ValueError('检查任务编号待核对')
        return ident

    def flow_retry_project(self, profile, project):
        if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile):
            raise ValueError('账号标识不正确。')
        if project is not None and (not isinstance(project, str) or not re.fullmatch(r'[0-9a-f-]{36}', project)):
            raise ValueError('项目标识不正确。')
        request = Request(f"http://127.0.0.1:{self.ports['flow']}/api/retry-project",
                          data=json.dumps({'profile': profile, 'project': project}).encode(), method='POST',
                          headers={'Content-Type':'application/json', 'X-Console-Request':'1',
                                   'Origin':f"http://127.0.0.1:{self.ports['flow']}"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(8193))
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError('备用项目保存状态未确认，请刷新核对。')
            return result
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise ValueError('备用项目未确认保存，请刷新后核对。') from None

    def library_storage(self):
        """Read the current original library location from the running Flow process."""
        pid = subprocess.check_output(
            ['systemctl', '--user', 'show', 'studiowright-console.service', '--property=MainPID', '--value'],
            text=True, timeout=5).strip()
        if not pid.isdigit() or pid == '0':
            raise ValueError('Flow 素材库当前不可读取。')
        environment = dict(p.split(b'=', 1) for p in Path('/proc', pid, 'environ').read_bytes().split(b'\0') if b'=' in p)
        root = Path(os.fsdecode(environment.get(b'SW_ROOT', os.fsencode(Path.home()/'.local/share/studiowright-evaluation'))))
        library = (root/'material-library').resolve(strict=True)
        database = library/'library.db'
        if database.is_symlink() or not database.is_file():
            raise ValueError('Flow 素材库当前不可读取。')
        usage = shutil.disk_usage(library)
        with self.library('GET', '/api/library?limit=1') as response:
            if response.status != 200:
                raise ValueError('Flow 素材库当前不可读取。')
            result = json.load(response)
        asset_bytes = result.get('stats', {}).get('bytes')
        if type(asset_bytes) is not int or asset_bytes < 0:
            raise ValueError('暂时无法确认现有素材大小。')
        return {'current': str(library), 'available_bytes': usage.free,
                'total_bytes': usage.total, 'asset_bytes': asset_bytes}

    def library_technical(self, ident):
        if not re.fullmatch(r'[0-9a-f]{64}', ident):
            raise ValueError('素材编号不正确。')
        with self.library('GET', '/api/library/' + ident) as response:
            if response.status != 200:
                raise ValueError('素材不存在。')
            item = json.load(response)
        result = {'width': item.get('width'), 'height': item.get('height'), 'duration': item.get('duration')}
        if item.get('media_type') == 'video' and item.get('mime') == 'video/mp4' and (not result['width'] or not result['height'] or result['duration'] is None):
            library = Path(self.library_storage()['current']).resolve()
            objects = (library / 'objects').resolve()
            path = (objects / ident[:2] / (ident + '.mp4')).resolve()
            if path.is_relative_to(objects) and path.is_file():
                try:
                    result.update({k:v for k,v in mp4_metadata(path).items() if v is not None})
                except (OSError, ValueError, struct.error):
                    pass
        return result

    def apply_gemini_tags(self, ident, tags):
        if not re.fullmatch(r'[0-9a-f]{64}', ident) or not isinstance(tags, dict):
            raise ValueError('标签内容不正确。')
        python = Path.home() / '.local/share/studiowright-evaluation/venv/bin/python'
        if not python.is_file():
            raise ValueError('原素材库环境暂不可用。')
        payload = json.dumps({'asset_id': ident, 'tags': tags}, ensure_ascii=False).encode('utf-8')
        if len(payload) > 100000:
            raise ValueError('标签内容过长。')
        process = subprocess.run([str(python), '-B', '-c', APPLY_LIBRARY_TAGS], input=payload,
                                 capture_output=True, timeout=25)
        if process.returncode or len(process.stdout) > 1000:
            raise ValueError('原素材库标签保存状态未确认。')
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or type(result.get('added_dimensions')) is not int:
            raise ValueError('原素材库标签保存状态未确认。')
        return result['added_dimensions']

    def status(self):
        flow, flow_error = self.read("flow", "/health")
        gemini, gemini_error = self.read("gemini", "/health")
        gemini_name = "Gemini · 官方 CLI 通道"
        gemini_description = '官方 Antigravity CLI 通道；发送入口在对话页与 Gemini 页快捷发送。'
        return {
            "checked_at": time.time(),
            "flow": {"connected": bool(flow and flow.get("ok") is True), "message": flow_error},
            "gemini": {"connected": bool(gemini and gemini.get("status") == "ok"), "message": gemini_error,
                       "name": gemini_name, "description": gemini_description},
        }

    def task_feed(self):
        # Poll only stored Flow records. No browser title scan, library facets or Gemini statistics.
        state, error = self.read("flow", "/api/state")
        if not state or not isinstance(state.get('tasks'), list) or not isinstance(state.get('projects'), list):
            return None, error or '任务记录暂不可读取。'
        projects = project_rows(state)
        accounts = account_rows(state)
        tasks = task_rows(state, projects)
        flow_task_notes(tasks, accounts, state.get('coordination'))
        return {"checked_at": time.time(), "flow_state": {"available": True, "skipped_tasks": len(state['tasks']) - len(tasks)},
                "tasks": tasks, "flow_coordination": state.get('coordination'), "real_accounts": {"items": accounts or []}}, None

    def flow_readiness(self, repair=False, manual_review=None, automatic=False, account_key=None, prepare_project=None):
        """Read current pages; only the explicit POST may repair or release a hold."""
        from flow_readiness_recovery import generation_evidence, operator_dispatch_paused
        state, _ = self.read("flow", "/api/state")
        if not state or state.get('test') is True:
            raise ValueError('flow_not_connected')
        accounts = account_rows(state)
        if not accounts:
            raise ValueError('flow_accounts_unavailable')
        projects = project_rows(state)
        coordination = state.get('coordination') or {}
        windows = {w['profile']:w for w in coordination.get('windows', [])}
        busy = {t.get('source_profile') for t in task_rows(state, projects)
                if t.get('status') in ('running','dispatching')}
        busy.update(w.get('profile') for w in coordination.get('windows', [])
                    if w.get('state') in ('preparing','submitting','submitted','generated','downloading'))
        payload = {'accounts': [{'profile': a['profile'], 'key': a['key'], 'email': a['email'],
                                'projects': [p['source_id'] for p in projects if p['source_profile']==a['profile']],
                                'review_task': windows.get(a['profile'],{}).get('task_id') if windows.get(a['profile'],{}).get('state')=='result_unknown' else None,
                                'repairable': (a['flow_login']=='verified' and not a['flow_page_incident']
                                               and a['flow_risk_known'] and not a['flow_risk_paused']
                                               and a['flow_pacing_known'] and not a['flow_cooling'])}
                               for a in accounts if a.get('email') and not a.get('flow_removed')],
                   'bindings': coordination.get('window_bindings', []), 'busy': sorted(p for p in busy if p)}
        if account_key is not None:
            payload['accounts'] = [account for account in payload['accounts'] if account['key'] == account_key]
            if len(payload['accounts']) != 1:
                raise ValueError('flow_account_unavailable')
        helper = ROOT/'flow_check_readiness.py'
        if helper.is_symlink() or not helper.is_file():
            raise ValueError('readiness_helper_unavailable')
        pid = subprocess.check_output(['systemctl','--user','show','studiowright-console.service',
                                       '-p','MainPID','--value'], text=True, timeout=3).strip()
        if not pid.isdigit() or pid == '0':
            raise ValueError('flow_not_connected')
        python = Path(os.fsdecode(Path('/proc',pid,'cmdline').read_bytes().split(b'\0',1)[0]))
        if not python.is_absolute() or not python.is_file():
            raise ValueError('flow_runtime_unavailable')
        repairs = {}
        if repair:
            if automatic:
                payload['safe_only'] = True
            recovery = ROOT/'flow_readiness_recovery.py'
            if recovery.is_symlink() or not recovery.is_file():
                raise ValueError('recovery_helper_unavailable')
            if not automatic and not manual_review and operator_dispatch_paused():
                payload.update(operator_dispatch_paused=True, safe_only=True)
            if manual_review:
                key, task_id = manual_review
                payload['accounts'] = [a for a in payload['accounts'] if a['key']==key and a['review_task']==task_id]
                if len(payload['accounts'])!=1:
                    return {'released':False}
                payload['manual_review'] = True
            recovery_payload = dict(payload)
            if not manual_review:
                before = subprocess.run([str(python),'-B',str(helper)], input=json.dumps(payload).encode(),
                                        capture_output=True, check=True, timeout=10)
                before_checks = json.loads(before.stdout)
                if not isinstance(before_checks, dict):
                    raise ValueError('invalid_readiness_result')
                recovery_payload['accounts'] = readiness_repair_accounts(payload, before_checks)
                if prepare_project is not None and not automatic and not payload.get('operator_dispatch_paused'):
                    from live_pool import Conflict
                    for account in recovery_payload['accounts']:
                        profile = account['profile']
                        if account['projects'] or before_checks.get(profile) != 'binding':
                            continue
                        try:
                            account['projects'] = [prepare_project(account)]
                            repairs[profile] = {'actions': ['prepare_project']}
                        except Conflict as exc:
                            repairs[profile] = {'actions': [], 'note': str(exc)}
                if automatic:
                    now = time.monotonic()
                    self._automatic_recovery_after = {
                        profile: until for profile, until in getattr(self, '_automatic_recovery_after', {}).items()
                        if until > now
                    }
                    recovery_payload['accounts'] = [
                        a for a in recovery_payload['accounts']
                        if a['profile'] not in self._automatic_recovery_after
                    ]
                recovery_payload['safe_only'] = True
            if recovery_payload['accounts']:
                result = subprocess.run([str(python),'-B',str(recovery)],input=json.dumps(recovery_payload).encode(),
                                        capture_output=True,check=True,timeout=175)
                for profile, result in json.loads(result.stdout).items():
                    initial = repairs.get(profile, {})
                    repairs[profile] = {**initial, **result,
                                        'actions': initial.get('actions', []) + result.get('actions', [])}
                if automatic:
                    self._automatic_recovery_after.update({
                        profile: time.monotonic() + 300 for profile, item in repairs.items()
                        if item.get('page') != 'busy'
                    })
            if manual_review:
                profile = payload['accounts'][0]['profile']
                released = 'release_review' in repairs.get(profile, {}).get('actions', [])
                if released:
                    from flow_warning_archive import purge_reviewed_screenshots
                    try:
                        purge_reviewed_screenshots(profile, task_id)
                    except (OSError, ValueError, sqlite3.Error):
                        pass  # Desktop sync or the next archive read retries the cleanup.
                return {'released':released}
            state, _ = self.read('flow','/api/state')
            if not state or state.get('test') is True:
                raise ValueError('flow_not_connected')
            coordination = state.get('coordination') or {}
            windows = {w['profile']:w for w in coordination.get('windows', [])}
            payload['bindings'] = coordination.get('window_bindings', [])
            projects = project_rows(state)
            for account in payload['accounts']:
                account['projects'] = [p['source_id'] for p in projects if p['source_profile']==account['profile']]
            payload['busy'] = sorted({t['source_profile'] for t in task_rows(state, projects)
                if t.get('source_profile') and t.get('status') in ('running','dispatching')} |
                {p for p,w in windows.items() if w.get('state') in ('preparing','submitting','submitted','generated','downloading')})
        result = subprocess.run([str(python),'-B',str(helper)], input=json.dumps(payload).encode(),
                                capture_output=True, check=True, timeout=10)
        checks = json.loads(result.stdout)
        allowed = {'ok','busy','binding','unique','project','flow_landing','login','verification_required',
                   'load_failed','draft','overlay','editor','unconfirmed'}
        if not isinstance(checks,dict) or any(v not in allowed for v in checks.values()):
            raise ValueError('invalid_readiness_result')
        for profile, item in repairs.items():
            if checks.get(profile) in ('binding','project','editor','load_failed','unconfirmed') and item.get('page') in ('draft','login','verification_required','flow_landing','overlay'):
                checks[profile] = item['page']
        if repair:
            eligible = {account['profile'] for account in payload['accounts']
                        if account['repairable'] and not account['review_task']}
            eligible.difference_update(payload['busy'])
            if automatic:
                balance_refresh_actions = {'reload', 'close_duplicate', 'recheck_window'}
                eligible.intersection_update(profile for profile, item in repairs.items()
                                             if balance_refresh_actions.intersection(item.get('actions', ())))
            stale = [a['profile'] for a in account_rows(state) if a['profile'] in eligible
                     and checks.get(a['profile']) == 'ok' and not a['flow_balance_fresh']
                     and a['flow_login'] == 'verified' and not a['flow_page_incident']
                     and a['flow_risk_known'] and not a['flow_risk_paused']
                     and a['flow_pacing_known'] and not a['flow_cooling']]
            if stale:
                try:
                    self.refresh_flow_balances_once(stale)
                except (ValueError, OSError, URLError):
                    pass  # A successful page recheck is independent of balance availability.
        now = time.time()
        return {'accounts':checks,'checked_at':now,'repairs':repairs,
                'evidence':generation_evidence(state['tasks'],windows,now),
                'review':{p:w['task_id'] for p,w in windows.items() if w.get('state')=='result_unknown' and w.get('task_id')}}

    def snapshot(self, *, submission=False):
        if submission:
            state, state_error = self.read("flow", "/api/state", retry_transient=True)
            # Submission uses account, project and task data. Library facets are only for display.
            facets, facets_error = None, None
        else:
            state, state_error = self.read("flow", "/api/state")
            facets, facets_error = self.read("flow", "/api/library/facets")
        if state is not None and (not isinstance(state.get("projects"), list) or not isinstance(state.get("tasks"), list)):
            if submission:
                LOGGER.warning('Flow account state returned invalid project or task data before submission.')
            state, state_error = None, "原后台项目或任务数据格式不正确。"
        projects = project_rows(state) if state is not None else []
        if state is not None:
            open_flow_project_names(state, projects)
        tasks = task_rows(state, projects) if state is not None else []
        accounts = account_rows(state) if state is not None else None
        if submission and state is not None and accounts is None:
            LOGGER.warning('Flow account state returned invalid account data before submission.')
        valid_projects = {(p['source_profile'], p['source_id']): p['key'] for p in projects}
        retry_projects = {}
        for item in state.get('retry_projects', []) if isinstance(state, dict) else []:
            if isinstance(item, dict) and (item.get('profile'), item.get('project')) in valid_projects:
                retry_projects[item['profile']] = valid_projects[(item['profile'], item['project'])]
        coordination = state.get('coordination') if isinstance(state, dict) else None
        flow_task_notes(tasks, accounts, coordination)
        screenshots = {}
        if coordination:
            from flow_warning_archive import current_window_screenshots
            try:
                screenshots = current_window_screenshots(coordination)
            except (OSError, ValueError, sqlite3.Error):
                pass
        return {
            "checked_at": time.time(),
            "flow_state": {"available": state is not None, "message": state_error,
                           "simulated": bool(state and state.get("test") is True),
                           "skipped_projects": len(state["projects"]) - len(projects) if state else 0,
                           "skipped_tasks": len(state["tasks"]) - len(tasks) if state else 0},
            "library": {"available": facets is not None, "message": facets_error,
                        "counts": counts_only(facets) if facets is not None else None},
            "projects": projects,
            "retry_projects": retry_projects,
            "tasks": tasks,
            "flow_insights": flow_insights_summary(state) if state is not None else None,
            "flow_coordination": coordination,
            "flow_warning_screenshots": screenshots,
            "flow_models": model_rows(state) if state is not None else {'image': [], 'video': []},
            "real_accounts": {"available": accounts is not None, "items": accounts or [],
                              "message": state_error if state is None else
                              ("当前 Flow 数据无法作为真实账号使用。" if accounts is None else None)},
        }


def flow_task_notes(tasks, accounts, coordination=None):
    by_profile = {a['profile']: a for a in accounts or []}
    for task in tasks:
        account = by_profile.get(task['source_profile'])
        if (task['status'] == 'queued' and task['kind'] in ('image', 'video')
                and isinstance(coordination, dict) and coordination.get('desktop_keyboard_ready') is False):
            task['note'] = '等待 Ubuntu 桌面键盘就绪，任务尚未派发。'
            continue
        if (task['status'] == 'queued' and task['kind'] in ('image', 'video') and account
                and (task.get('retry') or {}).get('state') != 'pending_switch'):
            if account.get('flow_page_incident') or account['flow_risk_paused']:
                task['note'] = '等待人工核查并恢复该账号的 Flow。'
            elif account['flow_cooling']:
                task['note'] = '账号冷却中，任务等待派发。'
            elif account.get('flow_preflight_reasons'):
                task['note'] = '账号派发自检未通过。请查看账号卡片。'


class Coordinator:
    """Reach only the workbench's own coordination component on loopback."""

    def __init__(self, port=18921, timeout=4):
        self.port = int(port)
        if not 1 <= self.port <= 65535:
            raise ValueError("调度组件端口无效")
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, data=None, key=None):
        if (method, path) not in {("GET", "/api/health"), ("GET", "/api/workflows"), ("POST", "/api/workflows"),
                                  ("GET", "/api/demo-accounts"), ("POST", "/api/demo-accounts/link"),
                                  ("POST", "/api/demo-accounts/unlink"), ("POST", "/api/demo-accounts/enabled"),
                                  ("POST", "/api/demo-accounts/account-enabled"), ("POST", "/api/demo-accounts/login"),
                                  ("POST", "/api/demo-accounts/keepalive"), ("POST", "/api/demo-accounts/quota"),
                                  ("POST", "/api/demo-accounts/quota-scope"), ("POST", "/api/demo-accounts/browser-sharing"), ("POST", "/api/demo-accounts/reconcile")}:
            raise ValueError("不支持的操作")
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Accept": "application/json"}
        encoded = None
        if method == "POST":
            headers.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}",
                            "X-Coordinator-Request": "1"})
            if key is not None:
                headers["Idempotency-Key"] = key
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            with self.opener.open(Request(url, data=encoded, headers=headers, method=method), timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                status = response.status
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(4097)
            if status not in {400, 403, 404, 409, 500, 503}:
                return 502, {"error": "工作台任务组件返回异常。"}
        except (URLError, TimeoutError, OSError):
            return 503, {"error": "工作台任务组件暂不可用。"}
        try:
            if len(raw) > MAX_RESPONSE:
                raise ValueError()
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError()
            return status, result
        except (ValueError, UnicodeError):
            return 502, {"error": "工作台任务组件返回的数据不正确。"}


SCHEDULER_TASK_KINDS = frozenset(('sync', 'login', 'session_import', 'check', 'doctor', 'keepalive', 'quote'))


def split_public_tasks(result):
    """Keep account and preflight operations out of clients' generation list."""
    tasks, scheduler_tasks = [], []
    for task in result.get('tasks', []):
        (scheduler_tasks if task.get('kind') in SCHEDULER_TASK_KINDS else tasks).append(task)
    return {**result, 'tasks': tasks, 'scheduler_tasks': scheduler_tasks}


def request_host_shutdown():
    """Power off the host from the desktop session, falling back to logind.

    The GNOME session manager allows the logged-in user to shut down without a
    password prompt; logind is only reached when that call fails.
    """
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
    try:
        done = subprocess.run(
            ['gdbus', 'call', '--session', '--dest', 'org.gnome.SessionManager',
             '--object-path', '/org/gnome/SessionManager',
             '--method', 'org.gnome.SessionManager.Shutdown'],
            env=env, capture_output=True, timeout=10)
        if done.returncode == 0:
            return
        print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} session shutdown failed: {done.stderr.decode(errors="replace").strip()}', flush=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} session shutdown unavailable: {exc}', flush=True)
    try:
        subprocess.run(['systemctl', 'poweroff', '-i'], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} logind shutdown unavailable: {exc}', flush=True)


# FlowMusic2API runs on this host's loopback. Host and port are compile-time
# constants, every path below is a literal, and user input travels only inside
# the JSON body — there is no URL surface for callers to influence.
MUSIC_API_HOST = '127.0.0.1'
MUSIC_API_PORT = 8000
MUSIC_API_CREDS = MUSIC_HOME / '.env.sw-credentials'
MUSIC_MODELS = {'lyria', 'lyria-fast', 'lyria-pro', 'lyria-pro-fast'}
MUSIC_GENERATION_PATH = '/v1/audio/generations'
MUSIC_PRIORITY_PATH = MUSIC_GENERATION_PATH + '/priority'


def music_call(method, path, *, body=None, token=None, timeout=30):
    """One request to the local FlowMusic2API service over a fixed connection."""
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    connection = http.client.HTTPConnection(MUSIC_API_HOST, MUSIC_API_PORT, timeout=timeout)
    try:
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = connection.getresponse()
        payload = json.load(response)
        if response.status >= 400:
            raise ValueError(str(payload.get('error') or payload)[:200])
        return payload
    finally:
        connection.close()


def music_credentials():
    user = password = key = None
    try:
        for line in MUSIC_API_CREDS.read_text(encoding='utf-8').splitlines():
            name, _, value = line.strip().partition('=')
            if name == 'FLOWMUSIC_ADMIN_USER': user = value
            elif name == 'FLOWMUSIC_ADMIN_PASSWORD': password = value
            elif name == 'FLOWMUSIC_DEFAULT_API_KEY': key = value
    except OSError: pass
    return user, password, key


def music_admin_token():
    user, password, _ = music_credentials()
    if not user or not password:
        raise ValueError('音乐服务凭据不可读取。')
    result = music_call('POST', '/api/admin/login', body={'username': user, 'password': password})
    return result.get('token') or ''


def music_credit_summary(accounts, path, observed_at):
    """Store the latest observed balance change for the same set of accounts."""
    balances = {}
    for row in accounts:
        email = row.get('email')
        credits = row.get('credits')
        if not isinstance(email, str) or not email.strip() or type(credits) not in (int, float) or not math.isfinite(credits) or credits < 0:
            return {'total': None, 'last_change': None}
        email = email.strip().lower()
        if email in balances:
            return {'total': None, 'last_change': None}
        balances[email] = Decimal(str(credits))
    total = sum(balances.values(), Decimal(0))
    result = {'total': float(total), 'last_change': None}
    membership = hashlib.sha256(json.dumps(sorted(balances)).encode()).hexdigest()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS music_credit_observations (
                id INTEGER PRIMARY KEY, membership TEXT, balance TEXT, change TEXT, observed_at INTEGER)""")
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT membership,balance,change,observed_at FROM music_credit_observations WHERE id=1').fetchone()
            if previous and observed_at <= previous[3]:
                change = previous[2] if previous[0] == membership else None
                if previous[0] == membership:
                    result['total'] = float(previous[1])
            else:
                change = None
                if previous and previous[0] == membership:
                    difference = total - Decimal(previous[1])
                    change = str(difference) if difference else previous[2]
                db.execute('INSERT OR REPLACE INTO music_credit_observations VALUES(1,?,?,?,?)',
                           (membership, str(total), change, observed_at))
    except (OSError, sqlite3.Error):
        return result
    return {**result, 'last_change': None if change is None else float(change)}


def music_service_status():
    _, _, key = music_credentials()
    status = {'schema_version': 1, 'available': bool(key), 'accounts': []}
    if not key:
        status['reason'] = '音乐服务未配置（缺少 FlowMusic2API 凭据）。'
        return status
    try:
        observed_at = time.time_ns()
        rows = music_call('GET', '/api/tokens', token=music_admin_token())
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('音乐账号列表格式无效。')
        now_utc = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
        status['accounts'] = [{'email': row.get('email'), 'credits': row.get('credits'),
                               'today_music': row.get('today_music_count'), 'tier': row.get('user_paygate_tier'),
                               # The token list hides the raw AT; readiness = active and the
                               # auto-refreshed access token is not yet past its expiry.
                               'ready': bool(row.get('is_active')) and str(row.get('at_expires') or '') > now_utc}
                              for row in rows if isinstance(row, dict)]
        path = Path.home()/'.local/state/creative-unified-workbench/music-credits.sqlite3'
        status['credits'] = music_credit_summary(status['accounts'], path, observed_at)
    except (OSError, ValueError, KeyError) as exc:
        status['available'] = False
        status['reason'] = f'音乐服务暂不可读取：{exc}'
    return status


def music_account_order(official, items):
    names = {}
    for item in items:
        key = item.get('key','').removeprefix('flow-account-')
        email = item.get('email')
        if re.fullmatch(r'[0-9a-f]{64}',key) and isinstance(email,str) and email.strip():
            names[key] = email.strip().lower()
    return list(dict.fromkeys(names[key] for key in official.priority_order(list(names))))


def music_order_supported():
    """Check the local router before choosing the endpoint for the only POST."""
    connection = http.client.HTTPConnection(MUSIC_API_HOST, MUSIC_API_PORT, timeout=3)
    try:
        connection.request('GET', MUSIC_PRIORITY_PATH)
        response = connection.getresponse()
        allowed = {method.strip().upper() for method in (response.getheader('Allow') or '').split(',')}
        if response.status == 405 and 'POST' in allowed:
            return True
        if response.status == 404:
            return False
    except (OSError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    raise ValueError('无法核对音乐服务的派发接口，本次尚未提交。')


def music_generate(prompt, model, *, account_order=None):
    if not isinstance(prompt, str):
        raise ValueError('请填写音乐描述。')
    if model is not None and not isinstance(model, str):
        raise ValueError('音乐模型不正确。')
    prompt = prompt.strip()
    if not prompt:
        raise ValueError('请填写音乐描述。')
    if len(prompt) > 2000:
        raise ValueError('音乐描述最多 2000 个字。')
    model = model or 'lyria-fast'
    if model not in MUSIC_MODELS:
        raise ValueError('音乐模型不正确。')
    _, _, key = music_credentials()
    if not key:
        raise ValueError('音乐服务未配置（缺少 FlowMusic2API 凭据）。')
    # The upstream call blocks until the song finishes (usually 1-3 minutes).
    path = MUSIC_GENERATION_PATH
    body = {'model': model, 'prompt': prompt}
    if account_order is not None and music_order_supported():
        path = MUSIC_PRIORITY_PATH
        body['account_order'] = account_order
    result = music_call('POST', path, body=body,
                        token=key, timeout=660)
    clips = []
    for clip in result.get('data') or []:
        if not isinstance(clip, dict):
            continue
        clips.append({'title': clip.get('title') or '未命名',
                      'audio_url': (clip.get('audio') or {}).get('url', ''),
                      'wav_url': (clip.get('wav') or {}).get('url', ''),
                      'cover_url': (clip.get('cover') or {}).get('url', ''),
                      'job_id': (result.get('flowmusic') or {}).get('job_id', ''),
                      'clip_id': clip.get('id') or clip.get('clip_id') or '',
                      'prompt': prompt})
    if not clips:
        raise ValueError('音乐服务未返回音频，稍后再试。')
    return {'schema_version': 1, 'model': model, 'clips': clips}


def music_library_ingest(clips):
    """Best-effort: hand the finished clips to the Flow console material library."""
    connection = http.client.HTTPConnection('127.0.0.1', 18888, timeout=180)
    try:
        connection.request('POST', '/api/library/music',
                           body=json.dumps({'clips': clips}).encode(),
                           headers={'Content-Type': 'application/json',
                                    'Origin': 'http://127.0.0.1:18888',
                                    'X-Console-Request': '1'})
        response = connection.getresponse()
        payload = json.load(response)
        if response.status >= 400:
            raise ValueError(str(payload.get('error') or payload)[:200])
        return payload
    finally:
        connection.close()


def make_handler(sources, deployment="local", coordinator=None, real_pool=None, activation=None, api_keys=None, backfill=None, project_actions=None):
    from task_feed import TaskFeed
    from music_jobs import MusicJobs, recent_music_jobs
    from music_readiness import MusicReadiness
    from contextlib import closing
    def music_busy():
        if real_pool and real_pool.maintenance():
            return True
        with closing(sqlite3.connect(MUSIC_DATABASE.as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('SELECT 1 FROM request_logs WHERE status_code=102 LIMIT 1').fetchone():
                return True
        jobs = Path.home()/'.local/state/creative-unified-workbench/music-jobs.sqlite3'
        if jobs.exists():
            with closing(sqlite3.connect('file:' + str(jobs) + '?mode=ro', uri=True)) as db:
                if db.execute("SELECT 1 FROM jobs WHERE status IN ('generating','ingesting','recovering') LIMIT 1").fetchone():
                    return True
        return False
    music_readiness = MusicReadiness(music_call, music_admin_token, music_busy)
    from music_recovery import recover as recover_music
    music_holder = []
    music_lock = threading.Lock()
    def generate_music_for_pool(prompt, model):
        order = []
        if real_pool:
            accounts = sources.snapshot().get('real_accounts',{})
            if accounts.get('available') is True:
                order = music_account_order(real_pool.official, accounts.get('items',[]))
            elif real_pool.official.saved_account_order():
                raise ValueError('暂时无法核对已保存的音乐账号顺序，尚未提交。')
        return music_generate(prompt,model,account_order=order)

    def get_music_jobs():
        with music_lock:
            if not music_holder:
                music_holder.append(MusicJobs(real_pool.state_dir if real_pool else Path.home()/'.local/state/creative-unified-workbench', generate_music_for_pool, music_library_ingest, real_pool.maintenance if real_pool else lambda:False, recover=recover_music))
            return music_holder[0]
    frontend_connections = threading.BoundedSemaphore(16)
    coordinator = coordinator or Coordinator()
    def task_snapshot():
        result, error = sources.task_feed()
        if result is not None and real_pool:
            real_pool.annotate_tasks(result["tasks"])
        return split_public_tasks(result) if result is not None else None, error
    databases = (Path.home() / '.local/share/studiowright-evaluation/console-data/console.db', real_pool.path) if real_pool else ()
    task_stream = TaskFeed(task_snapshot, databases)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalWorkbench/1"

        def log_message(self, _format, *_args):
            # Never log source records, URLs, request bodies, or credentials.
            pass

        def respond(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def valid(self, mutation=False, content_type="application/json"):
            host = self.headers.get("Host", "").lower()
            valid_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            if host not in valid_hosts or (origin and origin != "http://" + host):
                return False
            if mutation and (origin != "http://" + host or self.headers.get("X-Workbench-Request") != "1" or
                             self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != content_type):
                return False
            return True

        def json_response(self, status, result):
            body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return self.respond(status, body, "application/json; charset=utf-8")

        def stream_tasks(self):
            if not task_stream.subscribe():
                return self.json_response(503, {"error": "任务实时连接已满。"})
            self.close_connection = True
            try:
                self.connection.settimeout(8)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-transform")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(b'retry: 3000\n\n')
                self.wfile.flush()
                version = -1
                while True:
                    version, event = task_stream.next_event(version)
                    self.wfile.write(event)
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                task_stream.unsubscribe()

        def stream_frontend(self):
            if not frontend_connections.acquire(blocking=False):
                return self.json_response(503, {"error": "界面更新连接已满。"})
            self.close_connection = True
            try:
                self.connection.settimeout(8)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-transform")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                previous = None
                while True:
                    revision = frontend_revision()
                    event = ("event: frontend\ndata: " + revision + "\n\n").encode("ascii") if revision != previous else b"event: heartbeat\ndata: 1\n\n"
                    self.wfile.write(event)
                    self.wfile.flush()
                    previous = revision
                    time.sleep(5)
            except OSError:
                pass
            finally:
                frontend_connections.release()

        def do_GET(self):
            if not self.valid():
                return self.respond(403, b"Forbidden", "text/plain; charset=utf-8")
            if self.path == '/api/real-pool/request-policy':
                return self.json_response(200, {'retry_policy':['never'], 'scope':'request', 'version':1})
            if self.path == '/api/recent-tasks/events':
                return self.stream_tasks()
            if self.path == '/api/frontend/events':
                return self.stream_frontend()
            warning = re.fullmatch(r'/api/flow-warning-screenshots/([0-9a-f]{12}|[0-9a-f]{32})\.png', self.path)
            if warning and deployment == 'ubuntu':
                from flow_warning_archive import FLOW_ROOT, read_page_png, read_png
                try:
                    ident = warning.group(1)
                    body = (read_page_png if len(ident) == 32 else read_png)(FLOW_ROOT, ident)
                except FileNotFoundError:
                    return self.json_response(404, {'error': '未找到这张警告截图。'})
                except (OSError, ValueError, sqlite3.Error):
                    return self.json_response(503, {'error': '警告截图暂不可读取。'})
                return self.respond(200, body, 'image/png')
            if self.path == '/api/flow-readiness' and deployment == 'ubuntu':
                try:
                    return self.json_response(200,sources.flow_readiness())
                except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
                    return self.json_response(503,{'error':'Flow 实际任务窗口暂不可核对。'})
            if self.path == '/api/official-reauth/activate':
                if not real_pool:
                    return self.json_response(503, {'error':'账号治理暂不可用。'})
                return self.json_response(200, real_pool.official.reauth.activation.status())
            if self.path == '/api/official-reauth':
                if not real_pool:
                    return self.json_response(503, {'error':'账号治理暂不可用。'})
                return self.json_response(200,real_pool.official.reauth.dashboard())
            if self.path == '/api/official-quota/verify':
                from official_quota import reader_for
                from gemini_quota_verification import status
                if not real_pool:
                    return self.json_response(503, {'error':'额度服务暂不可用。'})
                return self.json_response(200,status(reader_for(real_pool.official.runtime)))
            if self.path == '/api/official-quota':
                from official_quota import reader_for
                return self.json_response(200, reader_for(real_pool.official.runtime).snapshot(refresh=False) if real_pool else {'schema_version':1,'accounts':{},'total_accounts':0,'known_accounts':0,'windows':{}})
            if self.path == '/api/channel-status':
                from channel_status import read_status
                data = read_status(real_pool.path, real_pool.official.public_status()) if real_pool else {'schema_version': 1, 'available': False}
                if real_pool and data.get('available'):
                    removed = real_pool.removed_gemini_accounts()
                    order = real_pool.official.saved_account_order()
                    data['shared_account_order'] = order
                    data['account_order'] = [key for key in order if key not in removed]
                return self.json_response(200 if data.get('available') else 503, data)
            if self.path == '/api/music/records':
                state_dir = real_pool.state_dir if real_pool else Path.home()/'.local/state/creative-unified-workbench'
                try:
                    return self.json_response(200, {'items': recent_music_jobs(state_dir)})
                except (OSError, sqlite3.Error, ValueError):
                    return self.json_response(503, {'error': '音乐生成记录暂时无法读取。'})
            if self.path == '/api/music/jobs/latest':
                return self.json_response(200, {'job':get_music_jobs().latest()})
            if self.path.startswith('/api/music/jobs/'):
                job=get_music_jobs().get(self.path.rsplit('/',1)[-1])
                return self.json_response(200 if job else 404, {'job':job})
            if self.path == '/api/music/readiness':
                return self.json_response(200, music_readiness.status())
            if self.path == '/api/music/status':
                return self.json_response(200, music_service_status())
            if project_actions and self.path == '/api/project-actions':
                return self.json_response(200, project_actions.status())
            if self.path in ('/api/dispatch/concurrency', '/api/gemini/concurrency'):
                if not real_pool:
                    return self.json_response(503, {'error': '并发设置尚未接入。'})
                try:
                    result = (real_pool.official.concurrency_status() if self.path == '/api/gemini/concurrency'
                              else real_pool.dispatch_limit.public_status())
                except sqlite3.Error:
                    return self.json_response(503, {'error':'并发设置暂不可读取。'})
                return self.json_response(200 if result['available'] else 503, result)
            if api_keys and self.path == '/api/access-keys':
                try:
                    return self.json_response(200, {'items': api_keys.list()})
                except Exception:
                    return self.json_response(503, {'error': '密钥管理暂不可用。'})
            if activation and self.path == '/api/account-activation/schedule':
                return self.json_response(200, activation.schedule_status())
            if activation and self.path == '/api/account-activation/poll':
                return self.json_response(200, activation.poll_status())
            if activation and self.path == '/api/account-activation':
                return self.json_response(200, activation.status())
            if activation and re.fullmatch(r'/api/account-activation\?service=(flow|gemini)', self.path):
                return self.json_response(200, activation.status(self.path.split('=', 1)[1]))
            if self.path in ALLOWED_STATIC:
                name, content_type = ALLOWED_STATIC[self.path]
                body = (ROOT / name).read_bytes()
                if name == "index.html":
                    meta = f'<meta name="workbench-revision" content="{frontend_revision(index=body)}">'.encode("ascii")
                    body = body.replace(b"</head>", meta + b"\n</head>", 1)
                return self.respond(200, body, content_type)
            if self.path == '/api/storage/library':
                try:
                    result = sources.library_storage()
                    result['plan'] = real_pool.library_storage_plan() if real_pool else None
                    return self.json_response(200, result)
                except (ValueError, OSError, subprocess.SubprocessError):
                    return self.json_response(503, {'error': '素材库存放位置暂时不可读取。'})
            if real_pool and self.path == '/api/auto-material-analysis':
                return self.json_response(200, real_pool.auto_material_state())
            if backfill and self.path == '/api/material-backfill':
                return self.json_response(200, backfill.state())
            if backfill and self.path == '/api/material-backfill/preview':
                try:
                    return self.json_response(200, backfill.preview(read_all_materials(sources)))
                except (ValueError, OSError, UnicodeError, URLError):
                    return self.json_response(503, {'error': '素材库暂时不可读取，请稍后再试。'})
            if urlsplit(self.path).path.startswith('/api/library'):
                try:
                    with sources.library('GET', self.path, range_header=self.headers.get('Range')) as upstream:
                        status = upstream.status
                        kind = upstream.headers.get_content_type()
                        length = upstream.headers.get('Content-Length', '')
                        is_media = bool(re.fullmatch(r'/api/library/[0-9a-f]{64}/(?:file|thumbnail)', urlsplit(self.path).path))
                        if is_media and status in (200, 206) and kind.startswith(('image/', 'video/', 'audio/')) and length.isdigit() and int(length) <= 2 * 1024**3:
                            self.send_response(status)
                            self.send_header('Content-Type', kind)
                            self.send_header('Content-Length', length)
                            self.send_header('Cache-Control', 'no-store')
                            self.send_header('X-Content-Type-Options', 'nosniff')
                            self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
                            for name in ('Content-Range', 'Accept-Ranges', 'Content-Disposition'):
                                value = upstream.headers.get(name)
                                if value and '\r' not in value and '\n' not in value:
                                    self.send_header(name, value)
                            self.end_headers()
                            remaining = int(length)
                            while remaining:
                                chunk = upstream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                            return
                        body = upstream.read(MAX_RESPONSE + 1)
                        if len(body) > MAX_RESPONSE:
                            return self.json_response(502, {'error': '素材库返回的数据过大。'})
                        path = urlsplit(self.path).path
                        if real_pool and status == 200 and kind == 'application/json' and re.fullmatch(r'/api/library(?:/[0-9a-f]{64})?', path):
                            try:
                                payload = json.loads(body)
                                rows = payload.get('items', []) if path == '/api/library' else [payload]
                                items = [row for row in rows if isinstance(row, dict)
                                         and re.fullmatch(r'[0-9a-f]{64}', str(row.get('id', '')))]
                                fields = real_pool.material_card_fields(row['id'] for row in items)
                                if fields:
                                    for row in items:
                                        if fields.get(row['id']):
                                            row['shot_tags'] = fields[row['id']]
                                    return self.json_response(status, payload)
                            except (ValueError, TypeError, AttributeError, sqlite3.Error):
                                pass  # The original library remains readable without analysis data.
                        return self.respond(status, body, 'application/json; charset=utf-8')
                except (ValueError, OSError, URLError):
                    return self.json_response(503, {'error': '素材库暂时不可读取。'})
            if real_pool and self.path == '/api/conversations':
                return self.json_response(200, real_pool.conversations())
            if real_pool and self.path == '/api/conversations/hidden':
                return self.json_response(200, real_pool.conversations(hidden=True))
            if real_pool and self.path == '/api/simulated-conversations':
                try:
                    from simulated_conversations import listing
                    return self.json_response(200, listing(real_pool.state_dir))
                except (ValueError, OSError, UnicodeError, ImportError):
                    return self.json_response(503, {'error': '模拟记录暂不可读取。'})
            if real_pool and re.fullmatch(r'/api/simulated-conversations/[0-9a-f-]{36}', self.path):
                try:
                    from simulated_conversations import detail
                    item = detail(real_pool.state_dir, self.path.rsplit('/', 1)[-1])
                    return self.json_response(200 if item else 404, item or {'error': '没有找到模拟记录。'})
                except (ValueError, OSError, UnicodeError, ImportError):
                    return self.json_response(503, {'error': '模拟记录暂不可读取。'})
            if real_pool and re.fullmatch(r'/api/gemini-images/[0-9a-f-]{36}/[0-3]', self.path):
                parts = self.path.split('/')
                item = real_pool.gemini_image(parts[-2],int(parts[-1]))
                return self.respond(200,item[0],item[1]) if item else self.json_response(404,{'error':'图片文件不可读取。'})
            if real_pool and urlsplit(self.path).path == '/api/material-extra-search':
                try:
                    query = parse_qsl(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=2)
                    if len({k for k, _ in query}) != len(query) or any(k not in ('q','page') for k, _ in query):
                        raise ValueError()
                    fields = dict(query)
                    return self.json_response(200, real_pool.search_material_extra(fields.get('q',''), int(fields.get('page','1'))))
                except ValueError:
                    return self.json_response(400, {'error': '搜索条件不正确。'})
            if real_pool and re.fullmatch(r'/api/conversations/[0-9a-f-]{36}', self.path):
                try:
                    item = real_pool.conversation(self.path.rsplit('/', 1)[-1])
                    return self.json_response(200 if item else 404, item or {'error': '对话不存在。'})
                except ValueError:
                    return self.json_response(400, {'error': '对话编号不正确。'})
            if real_pool and urlsplit(self.path).path == '/api/material-analysis-history':
                try:
                    query = parse_qsl(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=3)
                    if len(query) != len(dict(query)) or set(dict(query)) - {'page', 'limit', 'asset_id'}:
                        raise ValueError()
                    fields = dict(query)
                    return self.json_response(200, real_pool.material_analysis_history(int(fields.get('page', '1')), int(fields.get('limit', '20')), fields.get('asset_id')))
                except (ValueError, TypeError):
                    return self.json_response(400, {'error': '分析记录查询条件不正确。'})
            if real_pool and re.fullmatch(r'/api/material-extras/[0-9a-f]{64}', self.path):
                ident = self.path.rsplit('/', 1)[-1]
                try:
                    result = real_pool.material_extra(ident)
                    result['technical'] = sources.library_technical(ident)
                    return self.json_response(200, result)
                except (ValueError, OSError, URLError):
                    return self.json_response(503, {'error': '素材信息暂不可读取。'})
            if self.path == "/api/health":
                result = {"application": "unified-workbench", "version": 1, "deployment": deployment,
                          "source_version": os.getenv('SW_SOURCE_VERSION'), "source_commit": os.getenv('SW_SOURCE_COMMIT'),
                          "request_policy": {"retry_policy":["never"], "scope":"request", "version":1}}
            elif self.path == "/api/status":
                result = sources.status()
                if real_pool:
                    official = getattr(real_pool, 'official', None)
                    try:
                        ready = bool(official and any(official.runtime.inventory().values()))
                    except (OSError, ValueError, TypeError, AttributeError):
                        ready = False
                    result["gemini"] = {"connected": ready,
                                        "message": None if ready else '官方 CLI 通道尚未就绪；请到账号页核对授权。',
                                        "name": "Gemini · 官方 CLI 通道",
                                        "description": '官方 Antigravity CLI 通道；发送入口在对话页与 Gemini 页快捷发送。'}
            elif self.path == "/api/recent-tasks":
                result, error = task_snapshot()
                if result is None:
                    return self.json_response(503, {'error': error})
            elif self.path == "/api/snapshot":
                result = sources.snapshot()
                if real_pool:
                    real_pool.annotate(result["real_accounts"])
                    result['gemini_stats'] = real_pool.dialogue_statistics()
                    result['keepalive_monitor'] = dict(real_pool.monitor_heartbeat)
                    if "tasks" in result:
                        real_pool.annotate_tasks(result["tasks"])
                result = split_public_tasks(result)
            elif self.path in {"/api/scheduler/health", "/api/scheduler/workflows", "/api/scheduler/demo-accounts"}:
                status, result = coordinator.request("GET", self.path.replace("/api/scheduler/", "/api/", 1))
                return self.json_response(status, result)
            elif real_pool and re.fullmatch(r"/api/real-pool/activity/[0-9a-f-]{36}", self.path):
                with real_pool.connection() as db:
                    row = db.execute("""SELECT r.request_id,r.status,r.response,r.note,t.prompt,j.model
                                        FROM requests r LEFT JOIN conversation_turns t USING(request_id)
                                        LEFT JOIN official_jobs j USING(request_id) WHERE r.request_id=?""",
                                     (self.path.rsplit('/',1)[-1],)).fetchone()
                if row is None:
                    return self.json_response(404, {"error":"没有找到该请求。"})
                return self.json_response(200,dict(row))
            elif real_pool and self.path in ('/api/real-pool/activity','/api/real-pool/activity?include_checks=0'):
                result = real_pool.request_history(include_checks=not self.path.endswith('?include_checks=0'))
            elif real_pool and re.fullmatch(r"/api/real-pool/requests/[0-9a-f-]{36}", self.path):
                result = real_pool.get(self.path.rsplit('/', 1)[-1])
                if result is None:
                    return self.json_response(404, {"error": "没有找到该请求。"})
            elif re.fullmatch(r"/api/tasks/[A-Za-z0-9_-]{1,128}", self.path):
                status, task = sources.flow_task(self.path.rsplit('/',1)[-1])
                return self.json_response(status, task)
            elif real_pool and re.fullmatch(r"/api/real-pool/flow-requests/[0-9a-f-]{36}", self.path):
                result = real_pool.get_flow(self.path.rsplit('/', 1)[-1])
                if result is None:
                    return self.json_response(404, {"error": "没有找到该请求。"})
                if result.get('task_id'):
                    status, task = sources.flow_task(result['task_id'])
                    if status == 200:
                        result['task'] = task
            elif real_pool and re.fullmatch(r"/api/real-pool/recoveries/[A-Za-z0-9_-]{1,128}/file", self.path):
                recovered = real_pool.recovered_file(self.path.split('/')[-2])
                if not recovered:
                    return self.json_response(404, {"error": "没有找到找回的图片。"})
                return self.respond(200, recovered[0], recovered[1])
            else:
                return self.respond(404, b"Not found", "text/plain; charset=utf-8")
            return self.json_response(200, result)

        def do_POST(self):
            view = re.fullmatch(r'/api/flow-warning-screenshots/([0-9a-f]{12}|[0-9a-f]{32})/view', self.path)
            if view and deployment == 'ubuntu':
                if not self.valid() or self.headers.get('Origin') != 'http://' + self.headers.get('Host', '').lower():
                    return self.json_response(403, {'error': '请从工作台打开警告原图。'})
                required_body = b'view=original'
                if (self.headers.get('Transfer-Encoding')
                        or self.headers.get_all('Content-Length', []) != [str(len(required_body))]
                        or self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded'):
                    return self.json_response(400, {'error': '请求格式不正确。'})
                try:
                    self.connection.settimeout(4)
                    if self.rfile.read(len(required_body)) != required_body:
                        return self.json_response(400, {'error': '请求格式不正确。'})
                except OSError:
                    return self.json_response(400, {'error': '请求读取未完成。'})
                from flow_warning_archive import FLOW_ROOT, view_screenshot_once
                try:
                    body = view_screenshot_once(FLOW_ROOT, view.group(1))
                except FileNotFoundError:
                    return self.json_response(404, {'error': '截图已阅后销毁或已不可用。'})
                except (OSError, ValueError, sqlite3.Error):
                    return self.json_response(503, {'error': '警告原图暂不可读取。'})
                return self.respond(200, body, 'image/png')
            if self.path in ('/api/flow-readiness','/api/flow-readiness/review','/api/flow-readiness/auto') and deployment == 'ubuntu':
                if not self.valid(mutation=True):
                    return self.json_response(403,{'error':'请从工作台发起 Flow 就绪检查。'})
                length = self.headers.get_all('Content-Length',[])
                if (self.headers.get('Transfer-Encoding') or len(length)!=1 or not re.fullmatch(r'[0-9]{1,3}',length[0])
                        or not 0<int(length[0])<=300):
                    return self.json_response(400,{'error':'请求格式不正确。'})
                try:
                    self.connection.settimeout(4)
                    raw = json.loads(self.rfile.read(int(length[0])))
                    manual = self.path.endswith('/review')
                    if manual:
                        if (not isinstance(raw,dict) or set(raw)!={'account','task','confirmed'} or raw['confirmed'] is not True
                                or not isinstance(raw['account'],str) or not re.fullmatch(r'flow-account-[0-9a-f]{64}',raw['account'])
                                or not isinstance(raw['task'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',raw['task'])):
                            raise ValueError()
                    elif raw != {} and not (self.path == '/api/flow-readiness' and isinstance(raw, dict)
                            and set(raw) == {'account'} and isinstance(raw['account'], str)
                            and re.fullmatch(r'flow-account-[0-9a-f]{64}', raw['account'])):
                        raise ValueError()
                    self.connection.settimeout(None)
                except (OSError,ValueError,TypeError):
                    return self.json_response(400,{'error':'请求格式不正确。'})
                from flow_readiness_recovery import operator_dispatch_paused
                maintenance_blocked = real_pool and real_pool.maintenance() and not (
                    self.path == '/api/flow-readiness' and operator_dispatch_paused())
                if not real_pool or maintenance_blocked or not FLOW_READINESS_LOCK.acquire(blocking=False):
                    return self.json_response(409,{'error':'工作台正在维护或已有一轮就绪检查，请稍后再试。'})
                try:
                    result = sources.flow_readiness(repair=True,manual_review=(raw['account'],raw['task']) if manual else None,
                        automatic=self.path.endswith('/auto'), account_key=None if manual else raw.get('account'),
                        prepare_project=real_pool.prepare_readiness_project if self.path == '/api/flow-readiness' else None)
                    if manual and not result.get('released'):
                        return self.json_response(409,{'error':'原任务或账号状态已变化，未放行；请重新检查。'})
                    return self.json_response(200,result)
                except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,subprocess.SubprocessError):
                    return self.json_response(503,{'error':'本轮 Flow 就绪治理未完成，请重新检查当前状态。'})
                finally:
                    FLOW_READINESS_LOCK.release()
            if self.path == '/api/music/readiness':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起检查。'})
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 32 or json.loads(self.rfile.read(length)) != {}:
                        raise ValueError()
                except (ValueError, TypeError):
                    return self.json_response(400, {'error': '请求格式不正确。'})
                return self.json_response(202, music_readiness.start())
            if self.path == '/api/music/jobs':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台提交。'})
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0 < length <= 16000:raise ValueError('请求大小不正确。')
                    body=json.loads(self.rfile.read(length))
                    if not isinstance(body,dict) or set(body)-{'request_id','prompt','model','tags','style_id'}:raise ValueError('请求格式不正确。')
                    header_id=self.headers.get('Idempotency-Key')
                    if header_id and header_id!=body.get('request_id'):raise ValueError('请求编号与 Idempotency-Key 不一致。')
                    job=get_music_jobs().submit(body.get('request_id'),body.get('prompt'),body.get('model') or 'lyria-fast', {k:body[k] for k in ('tags','style_id') if k in body})
                    return self.json_response(202, {'job':job})
                except (ValueError,TypeError) as exc:
                    return self.json_response(400, {'error':str(exc)})
            if self.path == '/api/music/generate':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台发起音乐生成。'})
                try:
                    length = int(self.headers.get('Content-Length','0') or 0)
                    raw = json.loads(self.rfile.read(length) if length else b'{}')
                except (OSError,ValueError,TypeError):
                    return self.json_response(400, {'error':'请求格式不正确。'})
                prompt = raw.get('prompt') if isinstance(raw,dict) else None
                model = raw.get('model') if isinstance(raw,dict) else None
                try:
                    from music_metadata import validate_metadata, attach_metadata
                    metadata=validate_metadata({k:raw[k] for k in ('tags','style_id') if k in raw})
                    result = attach_metadata(generate_music_for_pool(prompt, model),prompt,model or 'lyria-fast','',metadata)
                except ValueError as exc:
                    return self.json_response(400, {'error':str(exc)})
                except (OSError,TimeoutError) as exc:
                    return self.json_response(504, {'error':f'音乐生成未完成：{exc}'})
                # 自动送进素材池：失败不影响生成结果本身，仅附带提示。
                try:
                    ingest = music_library_ingest(result['clips'])
                    result['library'] = ingest.get('ingested', [])
                    result['ingested'] = sum(1 for item in result['library'] if item.get('ok'))
                except (OSError, ValueError, KeyError) as exc:
                    result['ingested'] = 0
                    result['ingest_error'] = f'素材池登记未完成：{exc}'
                return self.json_response(200, result)
            if self.path == '/api/host-shutdown':
                # Only the Ubuntu deployment may power off its own host, and only
                # after the panel's explicit two-step confirmation.
                if deployment != 'ubuntu' or not self.valid(mutation=True):
                    return self.json_response(403, {'error':'仅限 Ubuntu 主机的工作台发起关机。'})
                try:
                    length = int(self.headers.get('Content-Length','0') or 0)
                    raw = json.loads(self.rfile.read(length) if length else b'{}')
                except (OSError,ValueError,TypeError):
                    return self.json_response(400, {'error':'请求格式不正确。'})
                if not isinstance(raw, dict) or raw.get('confirm') is not True:
                    return self.json_response(400, {'error':'缺少关机确认。'})
                print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} host shutdown requested from panel', flush=True)
                threading.Timer(1.5, request_host_shutdown).start()
                return self.json_response(202, {'status':'shutdown_scheduled'})
            if self.path == '/api/flow/accounts/remove' and deployment == 'ubuntu':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台移除 Flow 账号。'})
                length = self.headers.get('Content-Length','')
                if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[])) != 1
                        or not re.fullmatch(r'[0-9]{1,3}',length) or not 2 <= int(length) <= 256):
                    return self.json_response(400, {'error':'移除账号请求格式不正确。'})
                try:
                    self.connection.settimeout(4)
                    data = json.loads(self.rfile.read(int(length)))
                    if not isinstance(data,dict) or set(data)!={'account','confirmed'} or data['confirmed'] is not True:
                        raise ValueError('请确认要从 Flow 号池移除的账号。')
                    code, result = sources.remove_flow_account(data['account'])
                    return self.json_response(code,result)
                except ValueError as exc:
                    return self.json_response(400, {'error':str(exc)})
                except (OSError, sqlite3.Error):
                    return self.json_response(503, {'error':'暂时无法移除该 Flow 账号。'})
            if self.path == '/api/gemini/accounts/remove':
                from live_pool import Conflict
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台删除 Gemini 账号。'})
                length = self.headers.get('Content-Length','')
                if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[])) != 1
                        or not length.isdigit() or not 2 <= int(length) <= 256):
                    return self.json_response(400, {'error':'删除账号请求格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data,dict) or set(data)!={'account','confirmed'} or data['confirmed'] is not True:
                        raise ValueError('请确认要删除的 Gemini 账号。')
                    account_data = sources.snapshot().get('real_accounts',{})
                    if account_data.get('available') is not True:
                        return self.json_response(503, {'error':'账号名单暂不可读取。'})
                    result = real_pool.remove_gemini_account(data['account'], account_data.get('items',[]))
                    return self.json_response(200,result)
                except Conflict as exc:
                    return self.json_response(409, {'error':str(exc)})
                except ValueError as exc:
                    return self.json_response(400, {'error':str(exc)})
                except (OSError,sqlite3.Error):
                    return self.json_response(503, {'error':'暂时无法删除该 Gemini 账号。'})
            if self.path == '/api/account-order':
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台发起账号顺序调整。'})
                try:
                    length = int(self.headers.get('Content-Length','0') or 0)
                    raw = json.loads(self.rfile.read(length) if length else b'{}')
                except (OSError,ValueError,TypeError):
                    return self.json_response(400, {'error':'请求格式不正确。'})
                order = raw.get('order') if isinstance(raw,dict) else None
                try:
                    accounts = sources.snapshot().get('real_accounts',{})
                    if accounts.get('available') is not True:
                        return self.json_response(503, {'error':'账号名单暂不可读取。'})
                    visible = {item.get('key','').removeprefix('flow-account-') for item in accounts.get('items',[])}
                    saved = real_pool.official.save_account_order(order,visible_accounts=visible)
                except ValueError as exc:
                    return self.json_response(400, {'error':str(exc)})
                except (OSError,TypeError,AttributeError):
                    return self.json_response(503, {'error':'账号顺序暂时无法保存。'})
                return self.json_response(200, {'schema_version':1,'account_order':saved})
            if self.path == '/api/official-reauth/activate':
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台发起授权。'})
                if self.headers.get('Transfer-Encoding') or self.headers.get_all('Content-Length',[]) != ['2'] or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {'error':'请求格式不正确。'})
                return self.json_response(202, real_pool.official.reauth.activation.start())
            reauth = re.fullmatch(r'/api/official-reauth/([0-9a-f-]{36})', self.path)
            if reauth:
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台发起重新授权。'})
                if self.headers.get('Transfer-Encoding') or self.headers.get_all('Content-Length',[]) != ['2'] or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {'error':'请求格式不正确。'})
                try:
                    return self.json_response(202,real_pool.official.reauth.start(reauth[1]))
                except ValueError as exc:
                    return self.json_response(409,{'error':str(exc)})
                except (OSError,sqlite3.Error):
                    return self.json_response(503,{'error':'重新授权暂不可用，请稍后再试。'})
            from flow_references import MAX_BYTES as reference_limit, UPLOAD_ROUTE as reference_route
            match = reference_route.fullmatch(self.path)
            if match:
                if not real_pool or real_pool.maintenance():
                    return self.json_response(503, {'error': '工作台暂不可用。'})
                if not self.valid(mutation=True, content_type='application/octet-stream'):
                    return self.json_response(403, {'error': '请使用统一 API 提交参考图。'})
                length = self.headers.get('Content-Length', '')
                if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1
                        or not re.fullmatch(r'[0-9]{1,8}', length) or not 0 < int(length) <= reference_limit):
                    return self.json_response(400, {'error': '参考图文件大小无效，单张最多 8 MiB。'})
                try:
                    self.connection.settimeout(30)
                    data = self.rfile.read(int(length))
                    if len(data) != int(length):
                        raise ValueError('参考图上传不完整。')
                    snapshot = {} if match.group(1) == 'auto' else sources.snapshot()
                    result, created = real_pool.upload_flow_reference(
                        snapshot, *match.groups(), data, request_id=self.headers.get('Idempotency-Key', ''))
                    return self.json_response(201 if created else 200, result)
                except ValueError as exc:
                    return self.json_response(400, {'error': str(exc)})
                except (ImportError, OSError, sqlite3.Error):
                    return self.json_response(503, {'error': '参考图暂存服务暂不可用。'})
            if self.path == '/api/official-quota/verify':
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台发起验证。'})
                if self.headers.get('Transfer-Encoding') or self.headers.get_all('Content-Length',[]) != ['2'] or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {'error':'请求格式不正确。'})
                from official_quota import reader_for
                from gemini_quota_verification import start
                return self.json_response(202,start(reader_for(real_pool.official.runtime)))
            if self.path in ('/api/official-quota/refresh','/api/gemini-readiness','/api/gemini-readiness?scope=recoverable'):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台刷新额度。'})
                if self.headers.get('Transfer-Encoding') or self.headers.get_all('Content-Length',[]) != ['2'] or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {'error':'请求格式不正确。'})
                from official_quota import snapshot,reader_for
                if self.path!='/api/official-quota/refresh':
                    if not real_pool:
                        return self.json_response(503, {'error':'Gemini 检查暂不可用。'})
                    from official_runtime import check_official_network
                    if not check_official_network():
                        return self.json_response(503, {'error':'Ubuntu 暂时连不上 Google 的授权或 Gemini 服务。本轮没有发送验活消息，请检查 Ubuntu 的代理连接。','code':'gemini_network_unavailable'})
                    official=real_pool.official
                    quota=reader_for(official.runtime).snapshot(refresh=False)
                    snapshot_accounts=sources.snapshot().get('real_accounts',{})
                    probes=official.queue_readiness_probes(snapshot_accounts.get('items',[]),quota['accounts'],
                        blocked_only=self.path.endswith('?scope=recoverable')) if snapshot_accounts.get('available') is True else []
                    return self.json_response(200, {**quota,'readiness_probes':probes})
                return self.json_response(200, reader_for(real_pool.official.runtime).snapshot(force=True, recover=True) if real_pool else {'schema_version':1,'accounts':{},'total_accounts':0,'known_accounts':0,'windows':{}})
            if self.path in {'/api/real-pool/recover-account', '/api/real-pool/check'}:
                return self.json_response(410, {"error":'旧 Gemini 网页功能已停用。当前官方通道提供文字对话；这项旧操作未执行。',"code":"legacy_gemini_removed"})
            if self.path == '/api/channel-status':
                from channel_status import read_status
                data = read_status(real_pool.path) if real_pool else {'schema_version': 1, 'available': False}
                return self.json_response(200 if data.get('available') else 503, data)
            if project_actions and self.path == '/api/project-actions':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从工作台发起操作。'})
                length=self.headers.get('Content-Length','')
                if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1
                        or not length.isdigit() or not 0<int(length)<=1024):
                    return self.json_response(400, {'error':'请求格式不正确。'})
                try:
                    data=json.loads(self.rfile.read(int(length)).decode('utf8'))
                    if not isinstance(data,dict) or set(data)!={'project_key','kind'}:raise ValueError('请求格式不正确。')
                    result=project_actions.start(self.headers.get('Idempotency-Key',''),data['project_key'],data['kind'])
                    return self.json_response(202,result)
                except (ValueError,UnicodeError) as exc:
                    return self.json_response(409,{'error':str(exc) or '操作未开始。'})
                except (OSError,sqlite3.Error):
                    return self.json_response(503,{'error':'项目操作暂不可用。'})
            if self.path in ('/api/dispatch/concurrency', '/api/gemini/concurrency'):
                if not real_pool:
                    return self.json_response(503, {'error': '并发设置尚未接入。'})
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台设置并发。'})
                length = self.headers.get('Content-Length', '')
                if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1
                        or not length.isdigit() or not 0 < int(length) <= 1024):
                    return self.json_response(400, {'error': '并发设置格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data, dict) or set(data) != {'maximum'}:
                        raise ValueError('并发设置格式不正确。')
                    result = (real_pool.official.set_concurrency(data['maximum']) if self.path == '/api/gemini/concurrency'
                              else real_pool.dispatch_limit.set_limit(data['maximum']))
                    return self.json_response(200, result)
                except DispatchBusy as exc:
                    return self.json_response(409, {'error': str(exc)})
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '并发设置格式不正确。'})
                except (OSError, sqlite3.Error):
                    return self.json_response(503, {'error': '并发设置未能保存，请稍后操作。'})
            if api_keys and (self.path == '/api/access-keys' or re.fullmatch(r'/api/access-keys/[0-9a-f]{32}/(?:revoke|copy)', self.path)):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                if real_pool and real_pool.maintenance():
                    return self.json_response(503, {'error': '工作台正在更新，请稍后操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1 or not length.isdigit() or not 0 < int(length) <= 4096:
                    return self.json_response(400, {'error': '密钥请求格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data, dict):
                        raise ValueError('密钥请求格式不正确。')
                    if self.path == '/api/access-keys':
                        if set(data) != {'name'}:
                            raise ValueError('密钥请求格式不正确。')
                        return self.json_response(201, api_keys.create(data['name']))
                    if data:
                        raise ValueError('密钥请求格式不正确。')
                    if self.path.endswith('/copy'):
                        return self.json_response(200, {'key': api_keys.reveal(self.path.split('/')[-2])})
                    return self.json_response(200, {'revoked': api_keys.revoke(self.path.split('/')[-2])})
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '密钥请求格式不正确。'})
                except Exception:
                    return self.json_response(503, {'error': '密钥管理暂不可用，请先刷新密钥列表。'})
            if activation and self.path == '/api/account-activation/poll':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1 or not length.isdigit() or not 0 < int(length) <= 128:
                    return self.json_response(400, {'error': '轮询设置格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data, dict) or set(data) != {'enabled', 'interval_minutes'}:
                        raise ValueError('轮询设置格式不正确。')
                    return self.json_response(200, activation.set_poll(data['enabled'], data['interval_minutes']))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '轮询设置格式不正确。'})
                except Exception:
                    return self.json_response(503, {'error': '轮询设置暂不可用，请刷新核对。'})
            if activation and self.path == '/api/account-activation/schedule':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1 or not length.isdigit() or not 0 < int(length) <= 128:
                    return self.json_response(400, {'error': '定时设置格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data, dict) or set(data) != {'enabled', 'time'}:
                        raise ValueError('定时设置格式不正确。')
                    return self.json_response(200, activation.set_schedule(data['enabled'], data['time']))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '定时设置格式不正确。'})
                except Exception:
                    return self.json_response(503, {'error': '定时设置暂不可用，请刷新核对。'})
            if activation and self.path == '/api/account-activation':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 2 <= int(length) <= 80:
                    return self.json_response(400, {'error': '请求内容格式不正确。'})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(data, dict) or (data and (set(data) != {'service'} or data['service'] not in ('flow','gemini'))):
                        raise ValueError()
                except (ValueError, UnicodeError, TypeError):
                    return self.json_response(400, {'error': '请选择 Flow 或 Gemini 激活。'})
                try:
                    return self.json_response(202, activation.start(service=data['service']) if data else activation.start())
                except ValueError as exc:
                    return self.json_response(409, {'error': str(exc)})
                except Exception:
                    return self.json_response(503, {'error': '账号状态暂不可读取，未开始激活。'})
            if real_pool and self.path == '/api/auto-material-analysis':
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 100:
                    return self.json_response(400, {'error': '自动分析设置不正确。'})
                try:
                    body = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(body, dict) or set(body) != {'enabled'}:
                        raise ValueError('自动分析设置不正确。')
                    return self.json_response(200, real_pool.set_auto_material_enabled(body['enabled']))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '自动分析设置不正确。'})
            if backfill and self.path in ('/api/material-backfill', '/api/material-backfill/resume'):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 100:
                    return self.json_response(400, {'error': '补打请求格式不正确。'})
                try:
                    body = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if self.path.endswith('/resume'):
                        if body != {}:
                            raise ValueError('补打请求格式不正确。')
                        result = backfill.resume()
                    else:
                        if not isinstance(body, dict) or set(body) != {'scope'}:
                            raise ValueError('补打请求格式不正确。')
                        result = backfill.start(body['scope'], self.headers.get('Idempotency-Key', ''), read_all_materials(sources))
                    return self.json_response(200, result)
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(409, {'error': str(exc) or '补打请求格式不正确。'})
                except (OSError, URLError):
                    return self.json_response(503, {'error': '素材库暂不可读取，未开始补打。'})
            if self.path == '/api/storage/library/plan':
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 5000:
                    return self.json_response(400, {'error': '路径格式不正确。'})
                try:
                    body = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(body, dict) or set(body) != {'target'}:
                        raise ValueError('路径格式不正确。')
                    info = sources.library_storage()
                    checked = check_library_storage_target(info['current'], body['target'], info['asset_bytes'])
                    checked['plan'] = real_pool.save_library_storage_plan(info['current'], checked['target'])
                    return self.json_response(200, checked)
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '路径格式不正确。'})
                except (OSError, subprocess.SubprocessError, URLError):
                    return self.json_response(503, {'error': '暂时无法检查该位置。'})
            if real_pool and backfill and re.fullmatch(r'/api/(?:material-analysis|material-shot-notes)/[0-9a-f]{64}', self.path):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length') != '2' or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {'error': '请求内容格式不正确。'})
                ident, request_id = self.path.rsplit('/', 1)[-1], self.headers.get('Idempotency-Key', '')
                try:
                    if not real_pool.official.ready:
                        return self.json_response(503, {'error': '官方分析账号尚未就绪，未提交。'})
                    with sources.library('GET', '/api/library/' + ident) as response:
                        item = json.load(response)
                    if item.get('id') != ident:
                        raise ValueError('素材暂不可读取。')
                    if self.path.startswith('/api/material-shot-notes/'):
                        from material_backfill import SUPPORTED
                        from material_dispatch import enqueue_shot_notes
                        if item.get('mime') not in SUPPORTED or item.get('media_type') not in ('image','video'):
                            raise ValueError('该文件类型暂不支持镜头分析。')
                        enqueue_shot_notes(real_pool, ident, request_id)
                        backfill.wake.set()
                    else:
                        backfill.enqueue_project_assets(request_id, [item])
                except ValueError as exc:
                    return self.json_response(400, {'error': str(exc)})
                except (OSError, URLError):
                    return self.json_response(503, {'error': '素材库暂不可读取，未提交。'})
                return self.json_response(202, real_pool.material_extra(ident))
            if real_pool and re.fullmatch(r'/api/material-extras/[0-9a-f]{64}', self.path):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 20000:
                    return self.json_response(400, {'error': '请求内容格式不正确。'})
                try:
                    ident = self.path.rsplit('/', 1)[-1]
                    sources.library_technical(ident)  # Refuse records for nonexistent assets.
                    values = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    return self.json_response(200, real_pool.save_material_extra(ident, values))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '标签格式不正确。'})
                except (OSError, URLError):
                    return self.json_response(503, {'error': '素材库暂时不可读取。'})
            if (self.path == '/api/conversations' or re.fullmatch(r'/api/conversations/[0-9a-f-]{36}/(?:rename|visibility)', self.path)
                    or re.fullmatch(r'/api/library/[0-9a-f]{64}/tags', self.path)):
                if not self.valid(mutation=True):
                    return self.json_response(403, {'error': '请从工作台发起操作。'})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 100000:
                    return self.json_response(400, {'error': '请求格式不正确。'})
                try:
                    body = json.loads(self.rfile.read(int(length)).decode('utf-8'))
                    if not isinstance(body, dict):
                        raise ValueError('请求内容格式不正确。')
                    if self.path.startswith('/api/library/'):
                        with sources.library('POST', self.path, body) as upstream:
                            raw = upstream.read(MAX_RESPONSE + 1)
                            if len(raw) > MAX_RESPONSE:
                                raise ValueError('素材库返回数据过大。')
                            return self.respond(upstream.status, raw, 'application/json; charset=utf-8')
                    if not real_pool or real_pool.maintenance():
                        return self.json_response(503, {'error': '工作台正在更新。'})
                    if self.path.endswith('/visibility'):
                        if set(body) != {'hidden'}:
                            raise ValueError('对话状态不正确。')
                        return self.json_response(200, real_pool.hide_conversation(self.path.split('/')[-2], body['hidden']))
                    if set(body) != {'title'}:
                        raise ValueError('对话名称格式不正确。')
                    if self.path == '/api/conversations':
                        return self.json_response(201, real_pool.create_conversation(body['title']))
                    return self.json_response(200, real_pool.rename_conversation(self.path.split('/')[-2], body['title']))
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {'error': str(exc) or '请求内容格式不正确。'})
                except (OSError, URLError):
                    return self.json_response(503, {'error': '暂时无法处理，请稍后再试。'})
            upload = re.fullmatch(r"/api/real-pool/uploads/([0-9a-f-]{36})/([0-3])/(jpg|jpeg|png|webp|mp4|mov|webm|pdf)", self.path)
            if upload:
                if not real_pool or not self.valid(mutation=True, content_type="application/octet-stream"):
                    return self.json_response(403, {"error": "请从工作台上传附件。"})
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 100 * 1024 * 1024:
                    return self.json_response(400, {"error": "附件大小不符合要求。"})
                try:
                    return self.json_response(201, real_pool.upload(upload.group(1), int(upload.group(2)), upload.group(3), self.rfile, int(length)))
                except ValueError as exc:
                    return self.json_response(400, {"error": str(exc)})
                except Exception:
                    return self.json_response(500, {"error": "附件未上传完成，本次没有发送给 Gemini。"})
            retry_route = re.fullmatch(r'/api/flow/tasks/([a-f0-9]{16})/retry', self.path)
            if retry_route:
                if deployment != 'ubuntu' or not self.valid(mutation=True):
                    return self.json_response(403, {'error':'请从算力中心发起重试。'})
                length = self.headers.get_all('Content-Length', [])
                try:
                    if (self.headers.get('Transfer-Encoding') or len(length) != 1
                            or not re.fullmatch(r'[1-9][0-9]?', length[0])):
                        raise ValueError()
                    self.connection.settimeout(4)
                    body = json.loads(self.rfile.read(int(length[0])))
                    if not isinstance(body, dict) or set(body) != {'expected_ordinal','confirm_generation'} or body['confirm_generation'] is not True:
                        raise ValueError()
                    status, result = sources.flow_retry(retry_route.group(1), body['expected_ordinal'])
                except (ValueError, OSError):
                    return self.json_response(400, {'error':'请核对原任务并确认重试。'})
                finally:
                    self.connection.settimeout(None)
                return self.json_response(status, result)
            if re.fullmatch(r"/api/real-pool/recoveries/[A-Za-z0-9_-]{1,128}", self.path):
                if not real_pool or not self.valid(mutation=True):
                    return self.json_response(403, {"error": "请从工作台发起操作。"})
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length') != '2' or self.rfile.read(2) != b'{}':
                    return self.json_response(400, {"error": "请求内容格式不正确。"})
                try:
                    return self.json_response(200, real_pool.recover_image(self.path.rsplit('/', 1)[-1]))
                except ValueError as exc:
                    return self.json_response(400, {"error": str(exc)})
                except Exception:
                    return self.json_response(500, {"error": "暂时无法找回，请核对原任务。"})
            if self.path in {"/api/real-pool/chat", "/api/real-pool/image-analysis", "/api/real-pool/image-analysis-validate", "/api/real-pool/flow", "/api/real-pool/flow-validate", "/api/real-pool/enabled", "/api/real-pool/keepalive-enabled", "/api/real-pool/dispatch-policy", "/api/real-pool/flow-risk-resume", "/api/real-pool/flow-retry-project"}:
                if not real_pool:
                    return self.json_response(503, {"error": "真实账号接入暂不可用。"})
                if getattr(real_pool, 'maintenance', lambda: False)():
                    return self.json_response(503, {"error": "工作台正在更新，请稍后操作。"})
                if not self.valid(mutation=True):
                    return self.json_response(403, {"error": "请从工作台发起操作。"})
                length = self.headers.get("Content-Length", "")
                if self.headers.get("Transfer-Encoding") or not length.isdigit() or int(length) > 100000:
                    return self.json_response(400, {"error": "请求格式不正确。"})
                try:
                    data = json.loads(self.rfile.read(int(length)).decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("请求内容格式不正确。")
                    if 'method' in data:
                        # HTTP POST is shared. The operation, not the caller, selects the pipeline.
                        expected = {
                            '/api/real-pool/chat': 'gemini.chat',
                            '/api/real-pool/image-analysis': 'gemini.analyze_image',
                            '/api/real-pool/image-analysis-validate': 'gemini.analyze_image',
                            '/api/real-pool/flow': 'flow.generate',
                            '/api/real-pool/flow-validate': 'flow.generate',
                        }.get(self.path)
                        if expected is None or data.pop('method') != expected:
                            return self.json_response(400, {'error': '请求操作与接口不匹配，未提交。'})
                    snapshot = sources.snapshot(submission=True)
                    account_data = snapshot.get("real_accounts", {})
                    if not account_data.get("available"):
                        return self.json_response(503, {"error": "现有账号池暂不可读取。"})
                    if getattr(real_pool, 'maintenance', lambda: False)():
                        return self.json_response(503, {"error": "工作台正在更新，请稍后操作。"})
                    if self.path in {'/api/real-pool/image-analysis', '/api/real-pool/image-analysis-validate'}:
                        import gemini_image_input as vision
                        if not {'account', 'prompt', 'attachments'} <= data.keys() or data.keys() - {'account', 'prompt', 'attachments', 'retry_policy', 'effort', 'fallback_model'}:
                            raise ValueError('识图需要账号、问题和图片回执。')
                        if data.get('retry_policy') not in (None, 'never'):
                            raise ValueError('Invalid retry policy')
                        retry_kwargs={k:data[k] for k in ('retry_policy','effort','fallback_model') if k in data}
                        from gemini_effort import resolve, resolve_flash_fallback
                        resolve(data.get('model'),data.get('effort'))
                        request_id = real_pool.conversation_id(self.headers.get('Idempotency-Key', ''))
                        account, prompt = data['account'], data['prompt']
                        if not isinstance(account, str):
                            raise ValueError('请选择账号或自动轮询。')
                        if resolve_flash_fallback(data.get('fallback_model')) and account != 'auto':
                            raise ValueError('Flash 保底仅用于自动选账号的新请求。')
                        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
                            raise ValueError('请输入 1 到 20000 字的问题。')
                        attachments = vision.normalized_attachments(data['attachments'])
                        if self.path.endswith('-validate'):
                            if account != 'auto':
                                real_pool.allowed(account, account_data['items'])
                            attachments, paths = vision.files_for(real_pool, request_id, attachments)
                            vision.prompt_parts(prompt, attachments, paths)
                            return self.json_response(200, {'valid': True, 'submitted': False, 'image_count': len(attachments)})
                        record, created = real_pool.official.generate(
                            account, prompt, request_id, account_data['items'], attachments, image_analysis=True, **retry_kwargs)
                        return self.json_response(201 if created else 200, record)
                    if self.path.endswith('/flow-retry-project'):
                        if set(data) != {'account', 'project'} or not isinstance(data['account'], str) or not re.fullmatch(r'[0-9a-f]{64}', data['account']):
                            raise ValueError('请选择已登记账号。')
                        matches = [a for a in account_data['items'] if a.get('key') == 'flow-account-' + data['account']]
                        if len(matches) != 1:
                            raise ValueError('账号无法唯一匹配，请刷新。')
                        profile, selected = matches[0]['profile'], data['project']
                        if selected is None:
                            return self.json_response(200, sources.flow_retry_project(profile, None))
                        projects = [p for p in snapshot.get('projects', []) if p['key'] == selected and p['source_profile'] == profile]
                        if len(projects) != 1:
                            raise ValueError('请选择此账号已登记的项目。')
                        return self.json_response(200, sources.flow_retry_project(profile, projects[0]['source_id']))
                    if self.path.endswith('/flow-risk-resume'):
                        if set(data) not in ({'account'}, {'account', 'incident'}) or not isinstance(data['account'], str) or not re.fullmatch(r'[0-9a-f]{64}', data['account']):
                            raise ValueError('请选择已登记账号。')
                        matches = [a for a in account_data['items'] if a.get('key') == 'flow-account-' + data['account']]
                        if len(matches) != 1:
                            raise ValueError('账号无法唯一匹配，请刷新。')
                        if 'incident' in data:
                            current = matches[0].get('flow_page_incident')
                            if not current or data['incident'] != current['id']:
                                raise ValueError('这条警告已核查或已变化，请刷新。')
                            return self.json_response(200, sources.flow_risk_resume(matches[0]['profile'], current['id']))
                        if matches[0].get('flow_risk_paused') is not True:
                            raise ValueError('该账号当前没有需要人工恢复的暂停。')
                        return self.json_response(200, sources.flow_risk_resume(matches[0]['profile']))
                    if self.path.endswith('/dispatch-policy'):
                        if set(data) != {'five_hour_limit', 'weekly_limit'}:
                            raise ValueError('请求内容格式不正确。')
                        return self.json_response(200, real_pool.set_dispatch_policy(data['five_hour_limit'], data['weekly_limit']))
                    if self.path.endswith(('/flow', '/flow-validate')):
                        kwargs = {'validate_only': True} if self.path.endswith('/flow-validate') else {}
                        record, created = real_pool.submit_flow(data, self.headers.get('Idempotency-Key', ''), snapshot, **kwargs)
                        return self.json_response(201 if created else 200, record)
                    if self.path.endswith('/keepalive-enabled'):
                        if set(data) != {'account', 'enabled'}:
                            raise ValueError('请求内容格式不正确。')
                        return self.json_response(200, real_pool.set_keepalive_enabled(data['account'], data['enabled'], account_data['items']))
                    if self.path.endswith('/enabled'):
                        if set(data) != {'account', 'enabled'}:
                            raise ValueError('请求内容格式不正确。')
                        return self.json_response(200, real_pool.set_gemini_enabled(data['account'], data['enabled'], account_data['items']))
                    if set(data) - {"account", "prompt", "attachments", "conversation_id", "model", "retry_policy", "effort", "fallback_model"} or not {'account', 'prompt'} <= set(data):
                        raise ValueError("请求内容格式不正确。")
                    if data.get('retry_policy') not in (None, 'never'):
                        raise ValueError('Invalid retry policy')
                    retry_kwargs={k:data[k] for k in ('retry_policy','effort','fallback_model') if k in data}
                    from gemini_effort import resolve
                    resolve(data.get('model'),data.get('effort'))
                    if 'attachments' in data and not isinstance(data['attachments'], list):
                        raise ValueError("附件信息不正确。")
                    if data.get('conversation_id'):
                        record, created = real_pool.generate(data['account'], data['prompt'], self.headers.get('Idempotency-Key', ''), account_data['items'], data.get('attachments', []), data['conversation_id'], data.get('model'), **retry_kwargs)
                    elif data.get('model') is not None or data.get('fallback_model') is not None:
                        raise ValueError('模型选择需要在对话中使用。')
                    elif data.get('attachments'):
                        record, created = real_pool.generate(data['account'], data['prompt'], self.headers.get('Idempotency-Key', ''), account_data['items'], data['attachments'], **retry_kwargs)
                    else:
                        record, created = real_pool.generate(data['account'], data['prompt'], self.headers.get('Idempotency-Key', ''), account_data['items'], **retry_kwargs)
                    return self.json_response(201 if created else 200, record)
                except DispatchBusy as exc:
                    return self.json_response(409, {'error': str(exc)})
                except (ValueError, UnicodeError) as exc:
                    return self.json_response(400, {"error": str(exc) or "请求内容格式不正确。"})
                except Exception:
                    return self.json_response(500, {"error": "工作台暂不能处理，请查看本次请求记录。"})
            paths = {"/api/scheduler/workflows", "/api/scheduler/demo-accounts/link",
                     "/api/scheduler/demo-accounts/unlink", "/api/scheduler/demo-accounts/enabled",
                     "/api/scheduler/demo-accounts/account-enabled", "/api/scheduler/demo-accounts/login",
                     "/api/scheduler/demo-accounts/keepalive", "/api/scheduler/demo-accounts/quota",
                     "/api/scheduler/demo-accounts/quota-scope", "/api/scheduler/demo-accounts/browser-sharing", "/api/scheduler/demo-accounts/reconcile"}
            if self.path not in paths:
                return self.send_error(501, "Unsupported method")
            if not self.valid(mutation=True):
                return self.json_response(403, {"error": "请从工作台发起操作。"})
            length = self.headers.get("Content-Length", "")
            key = self.headers.get("Idempotency-Key", "")
            if (self.headers.get("Transfer-Encoding") or not length.isdigit() or int(length) > 4096 or
                    (self.path == "/api/scheduler/workflows" and not re.fullmatch(r"demo-[A-Za-z0-9_-]{8,95}", key))):
                return self.json_response(400, {"error": "模拟请求格式不正确。"})
            try:
                data = json.loads(self.rfile.read(int(length)).decode("utf-8"))
                flow_ids = {"demo-flow-shared", "demo-flow-separate"}
                gemini_ids = {"demo-gemini-shared", "demo-gemini-separate"}
                if not isinstance(data, dict):
                    raise ValueError()
                if self.path == "/api/scheduler/workflows":
                    if (set(data) - {"scenario", "mock_flow_source", "mock_gemini_source", "mock_pool_auto"} or data.get("scenario") not in {
                            "success", "flow_only", "gemini_only", "flow_failed", "flow_unknown", "save_failed", "analysis_failed", "analysis_unknown", "gemini_unknown"
                        } or ("mock_flow_source" in data and data["mock_flow_source"] not in flow_ids) or
                            ("mock_gemini_source" in data and data["mock_gemini_source"] not in gemini_ids) or
                            ("mock_pool_auto" in data and type(data["mock_pool_auto"]) is not bool) or
                            (data.get("mock_pool_auto") is True and ("mock_flow_source" in data or "mock_gemini_source" in data))):
                        raise ValueError()
                elif self.path.endswith("/link"):
                    if (set(data) != {"flow_source", "gemini_source", "confirm"} or data["flow_source"] not in flow_ids or
                            data["gemini_source"] not in gemini_ids or data["confirm"] is not True): raise ValueError()
                elif self.path.endswith("/unlink"):
                    if (set(data) != {"link_id", "confirm"} or not isinstance(data["link_id"], str) or
                            not re.fullmatch(r"demo-link-(shared|[0-9a-f]{32})", data["link_id"]) or data["confirm"] is not True): raise ValueError()
                elif self.path.endswith('/account-enabled') or self.path.endswith('/browser-sharing'):
                    if (set(data) != {'account_id', 'enabled'} or not isinstance(data['account_id'], str) or
                            not re.fullmatch(r'demo-account-demo-(flow|gemini)-(shared|separate)', data['account_id']) or
                            type(data['enabled']) is not bool): raise ValueError()
                elif self.path.endswith('/login'):
                    if (set(data) != {'source_id', 'state'} or data['source_id'] not in flow_ids | gemini_ids or
                            data['state'] not in ('ready', 'expired', 'unknown')): raise ValueError()
                elif self.path.endswith('/quota'):
                    if (set(data) != {'source_id', 'remaining'} or data['source_id'] not in flow_ids | gemini_ids or
                            (data['remaining'] is not None and (type(data['remaining']) is not int or not 0 <= data['remaining'] <= 10000))): raise ValueError()
                elif self.path.endswith('/quota-scope'):
                    if (set(data) != {'account_id', 'scope', 'remaining'} or not isinstance(data['account_id'], str) or
                            not re.fullmatch(r'demo-account-demo-(flow|gemini)-(shared|separate)', data['account_id']) or
                            data['scope'] not in ('per_service', 'shared') or
                            (data['remaining'] is not None and (type(data['remaining']) is not int or not 0 <= data['remaining'] <= 10000))): raise ValueError()
                elif self.path.endswith('/reconcile'):
                    if (set(data) != {'step_id', 'outcome'} or not isinstance(data['step_id'], str) or
                            not re.fullmatch(r'[0-9a-f-]{36}', data['step_id']) or data['outcome'] not in ('spent', 'not_spent')): raise ValueError()
                elif (set(data) != {"source_id", "enabled"} or data["source_id"] not in flow_ids | gemini_ids or
                      type(data["enabled"]) is not bool): raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                return self.json_response(400, {"error": "只允许操作模拟数据。"})
            if self.path == "/api/scheduler/workflows":
                spec = {"mode": "simulation", "title": "模拟流程", **data}
                status, result = coordinator.request("POST", "/api/workflows", spec, key)
            else:
                status, result = coordinator.request("POST", self.path.replace("/api/scheduler/", "/api/", 1), data)
            return self.json_response(status, result)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="创作工作台")
    parser.add_argument("--port", type=int, default=18910)
    parser.add_argument("--flow-port", type=int, default=18888)
    parser.add_argument("--gemini-port", type=int, default=8095)
    parser.add_argument("--coordinator-port", type=int, default=18921)
    parser.add_argument("--deployment", choices=("local", "ubuntu"), default="local")
    args = parser.parse_args()
    real_pool = None
    api_keys = None
    backfill = None
    if args.deployment == "ubuntu":
        os.umask(0o077)
        from live_pool import RealPool
        from unified_keys import UnifiedKeys
        real_pool = RealPool(Path.home() / '.local/state/creative-unified-workbench', ROOT)
        from material_backfill import MaterialBackfill
        backfill = MaterialBackfill(real_pool)
        from material_dispatch import initialize as initialize_material_analysis
        initialize_material_analysis(real_pool)
        api_keys = UnifiedKeys(Path.home() / '.local/state/creative-unified-workbench/api-keys.sqlite3')
    sources = Sources(args.flow_port, args.gemini_port, gemini_source="ubuntu-trial" if args.deployment == "ubuntu" else "pooldesk")
    activation = None
    coordinator = Coordinator(args.coordinator_port)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(sources, args.deployment, coordinator, real_pool, api_keys=api_keys))
    if real_pool:
        from activation import Activation
        real_pool.official.start()
        activation = Activation(Path.home() / '.local/state/creative-unified-workbench', ROOT, sources, real_pool)
        from project_actions import ProjectActions
        project_actions = ProjectActions(real_pool,sources,backfill,ROOT,project_rows,read_all_materials)
        server.RequestHandlerClass = make_handler(sources, args.deployment, coordinator, real_pool, activation, api_keys, backfill, project_actions)
    stop_monitor = threading.Event()
    if real_pool:
        def monitor():
            # Let startup and any deployment finish before touching an account.
            if stop_monitor.wait(120):
                return
            while not stop_monitor.is_set():
                try:
                    real_pool.purge_stale_uploads()
                    if sources.snapshot().get('real_accounts', {}).get('available'):
                        real_pool.monitor_heartbeat.update(last_completed=time.time(), last_error=None)
                    else:
                        real_pool.monitor_heartbeat['last_error'] = 'accounts_unavailable'
                except Exception as error:
                    real_pool.monitor_heartbeat['last_error'] = type(error).__name__
                stop_monitor.wait(60)
        threading.Thread(target=monitor, name='account-monitor', daemon=True).start()
        def recover_new_images():
            if stop_monitor.wait(120):
                return
            while not stop_monitor.is_set():
                try:
                    if not real_pool.maintenance():
                        real_pool.auto_recover_tasks(sources.snapshot().get('tasks', []))
                except Exception:
                    pass
                stop_monitor.wait(60)
        threading.Thread(target=recover_new_images, name='flow-recovery', daemon=True).start()
        def recover_idle_flow_pages():
            # Repairs run independently of the open UI. The shared lock,
            # five-minute cooldown and live account guards bound every pass.
            while not stop_monitor.wait(60):
                if args.deployment != 'ubuntu' or real_pool.maintenance():
                    continue
                if not FLOW_READINESS_LOCK.acquire(blocking=False):
                    continue
                try:
                    result = sources.flow_readiness(repair=True, automatic=True)
                    repairs = result.get('repairs', {})
                    real_pool.monitor_heartbeat['flow_auto_recovery'] = {
                        'status':'completed', 'checked_at':time.time(),
                        'repaired':sum(bool(r.get('actions')) for r in repairs.values())}
                except Exception as error:
                    real_pool.monitor_heartbeat['flow_auto_recovery'] = {
                        'status':'failed', 'checked_at':time.time(), 'reason':type(error).__name__}
                finally:
                    FLOW_READINESS_LOCK.release()
        threading.Thread(target=recover_idle_flow_pages, name='flow-page-auto-recovery', daemon=True).start()
        def analyze_new_materials():
            if stop_monitor.wait(15):
                return
            while not stop_monitor.is_set():
                delay = 30
                try:
                    run_auto_material_once(sources, real_pool)
                    if real_pool.auto_material_state()['counts'].get('waiting'):
                        delay = 4
                except Exception:
                    pass  # Keep source material and the persisted job state intact.
                stop_monitor.wait(delay)
        threading.Thread(target=analyze_new_materials, name='material-auto-analysis', daemon=True).start()
        def analyze_backfill():
            # Only a persisted task explicitly started by the user can be claimed.
            from material_dispatch import shot_pending
            if not (backfill.has_pending() or shot_pending(real_pool)):
                backfill.wake.wait(120)
            while not stop_monitor.is_set():
                backfill.wake.clear()
                try:
                    if not real_pool.maintenance() and (backfill.has_pending() or shot_pending(real_pool)):
                        run_backfill_once(sources, real_pool, backfill)
                except Exception:
                    pass
                backfill.wake.wait(10)
        threading.Thread(target=analyze_backfill, name='material-backfill', daemon=True).start()
    print(f"创作工作台：http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor.set()
        if backfill:
            backfill.wake.set()
        if activation:
            activation.close()
        server.server_close()
        if real_pool:
            real_pool.close()


if __name__ == "__main__":
    main()
