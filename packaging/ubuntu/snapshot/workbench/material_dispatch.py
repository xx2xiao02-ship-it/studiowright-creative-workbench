"""One shared, durable material analysis lane with a model chosen before dispatch."""
import hashlib
import json
import tempfile
import time
import uuid
from pathlib import Path

from material_backfill import has_labels
from material_vision import analyze, read_media, PROMPT, SHOT_PROMPT, clean_shot_notes
from official_pool import busy_official, model_candidates, text_default_for
from official_runtime import MODEL


def initialize(pool):
    with pool.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS material_vision_version(id INTEGER PRIMARY KEY CHECK(id=1), activated REAL NOT NULL)')
        now=time.time()
        if db.execute('INSERT OR IGNORE INTO material_vision_version VALUES(1,?)',(now,)).rowcount:
            # Restart discovery at the new rollout; backfill is explicit and scoped.
            db.execute('UPDATE auto_material_settings SET since=? WHERE id=1',(now,))
        db.execute("UPDATE material_shot_jobs SET status='unknown',note='分析中断，结果需核对，不会重复提交。' WHERE status='running'")
        db.execute('COMMIT')


def enqueue_shot_notes(pool, ident, queue_id):
    from live_pool import Conflict
    pool.material_id(ident)
    pool.conversation_id(queue_id)
    with pool.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT asset_id FROM material_shot_jobs WHERE queue_id=?', (queue_id,)).fetchone()
        if previous and previous['asset_id'] != ident:
            raise Conflict('请求编号已用于其他素材。')
        if pool.maintenance() or db.execute(
            "SELECT 1 FROM material_analyses WHERE asset_id=? AND status IN ('preparing','saving','unknown')", (ident,)
        ).fetchone():
            raise Conflict('素材正在分析、结果待核对或工作台维护中。')
        if not db.execute('SELECT 1 FROM material_shot_notes WHERE asset_id=?', (ident,)).fetchone():
            db.execute("""INSERT INTO material_shot_jobs(asset_id,queue_id,status,created,note)
                          VALUES(?,?,'waiting',?,'等待 Gemini 分析镜头。') ON CONFLICT(asset_id) DO NOTHING""",
                       (ident,queue_id,time.time()))
        db.execute('COMMIT')


def shot_pending(pool):
    with pool.connection() as db:
        return db.execute("SELECT 1 FROM material_shot_jobs WHERE status='waiting' LIMIT 1").fetchone() is not None


def claim(pool, auto):
    official=pool.official
    available=official.runtime.inventory()
    if not any(available.values()):return None
    quota=official.account_quotas(available)
    models=official.runtime.text_models()
    table='auto_material_jobs' if auto else 'material_backfill_jobs'
    with pool.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            settings=db.execute('SELECT enabled,paused_reason FROM auto_material_settings WHERE id=1').fetchone()
            if pool.maintenance() or (auto and (not settings['enabled'] or settings['paused_reason'])):
                return None
            if (db.execute("SELECT 1 FROM material_analyses WHERE status IN ('preparing','saving') LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM material_shot_jobs WHERE status='running' LIMIT 1").fetchone()):
                return None
            shot = None if auto else db.execute("SELECT asset_id FROM material_shot_jobs WHERE status='waiting' ORDER BY created,asset_id LIMIT 1").fetchone()
            if shot:
                table='material_shot_jobs'
            candidate=shot or db.execute("SELECT asset_id FROM auto_material_jobs WHERE status='waiting' ORDER BY added,asset_id LIMIT 1" if auto else
                                 "SELECT j.asset_id,r.scope FROM material_backfill_jobs j JOIN material_backfill_runs r ON r.id=j.run_id WHERE j.status='waiting' AND r.paused_reason IS NULL ORDER BY j.added,j.asset_id LIMIT 1").fetchone()
            if not candidate:return None
            ident=candidate['asset_id']; now=time.time()
            if shot and db.execute('SELECT 1 FROM material_shot_notes WHERE asset_id=?',(ident,)).fetchone():
                db.execute("UPDATE material_shot_jobs SET status='skipped',note='已有 Gemini 镜头说明。' WHERE asset_id=?",(ident,))
                return None
            if not shot and db.execute('SELECT 1 FROM material_analyses WHERE asset_id=? LIMIT 1',(ident,)).fetchone():
                db.execute(f"UPDATE {table} SET status='skipped',updated=?,note='已有分析记录，未重复提交' WHERE asset_id=?",(now,ident))
                return None
            if not official.has_concurrency(db):
                return None
            # A material job only uses an idle eligible account; normal dialogue has priority.
            if db.execute("SELECT 1 FROM requests r JOIN official_jobs j USING(request_id) WHERE r.status='queued' LIMIT 1").fetchone():
                return None
            eligible=sorted(k for k in available if official.reason(db,k,available) is None and not busy_official(db,k))
            model=text_default_for(models)
            candidates=model_candidates(eligible,model,models,quota,now)
            if not candidates and model!=MODEL:
                model=MODEL
                candidates=model_candidates(eligible,model,models,quota,now)
            if not candidates:return None
            key=official.priority_order(candidates)[0]
            request_id=str(uuid.uuid4())
            db.execute("INSERT INTO requests(request_id,account_key,prompt_digest,status,created,note,selection) VALUES(?,?,?,'dispatching',?,?,?)",
                       (request_id,key,hashlib.sha256(((SHOT_PROMPT if shot else PROMPT)+ident).encode()).hexdigest(),now,'正在分析素材；固定账号，不会自动重试。','auto'))
            # Reuse the official pool's busy and unknown guards without creating a chat.
            db.execute('INSERT INTO official_jobs(request_id,conversation_id,model,started,queue_wait_ms) VALUES(?,?,?,?,0)',(request_id,request_id,model,now))
            if shot:
                db.execute("UPDATE material_shot_jobs SET status='running',request_id=?,note='Gemini 正在分析镜头。' WHERE asset_id=?",(request_id,ident))
            else:
                db.execute("INSERT INTO material_analyses(request_id,asset_id,status,created) VALUES(?,?,'preparing',?)",(request_id,ident,now))
                db.execute(f"UPDATE {table} SET status='running',request_id=?,updated=?,note='正在分析素材' WHERE asset_id=?",(request_id,now,ident))
            return {'ident':ident,'request_id':request_id,'account':key,'table':table,'model':model,
                    'imported':table == 'material_backfill_jobs' and candidate['scope'] == 'windows_import'}
        finally:
            if db.in_transaction:
                # Commit only a normal return, and roll back a partially written claim.
                import sys
                db.execute('ROLLBACK' if sys.exc_info()[0] else 'COMMIT')


def finish(pool, sources, job, result, video):
    ident, rid, table=job['ident'],job['request_id'],job['table']
    status=result['status'];reason=result.get('reason');now=time.time()
    notes={'completed':'官方素材分析已收到。','failed':'素材分析未完成。',
           'unknown':'素材分析结果未确认；不会自动重试或换号。','skipped':'已有标签，未重复分析。'}
    note=notes[status]
    if reason:note+=f' ({reason})'
    shot = table == 'material_shot_jobs'
    details = {}
    if shot and status == 'completed':
        import re
        try:
            text = (result.get('response') or '').strip()
            blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)```',text,flags=re.I)
            details = clean_shot_notes(json.loads(blocks[0] if len(blocks)==1 else text),video)
        except (ValueError, TypeError):
            pass
    with pool.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE requests SET status=?,finished=?,note=?,response=? WHERE request_id=?',
                   ('failed' if status=='skipped' else status,now,note,result.get('response'),rid))
        db.execute('UPDATE official_jobs SET reason=? WHERE request_id=?',(reason,rid))
        if reason in ('verification_required','quota_or_rate_limit'):
            db.execute('INSERT INTO official_account_blocks VALUES(?,?,?) ON CONFLICT(account_key) DO UPDATE SET reason=excluded.reason,created=excluded.created',(job['account'],reason,now))
        if shot:
            if details:
                db.execute("INSERT OR IGNORE INTO material_shot_notes(asset_id,notes_json,source) VALUES(?,?,'gemini')",
                           (ident,json.dumps(details,ensure_ascii=False)))
                note='Gemini 镜头说明已保存。'
            elif status == 'completed':
                status, note = 'failed', 'Gemini 没有返回可保存的镜头说明。'
            db.execute("UPDATE material_shot_jobs SET status=?,note=? WHERE request_id=? AND status='running'",(status,note,rid))
        elif status=='skipped':
            db.execute("UPDATE material_analyses SET status='skipped',finished=?,note=? WHERE request_id=?",(now,note,rid))
            db.execute(f"UPDATE {table} SET status='skipped',updated=?,note=? WHERE request_id=?",(now,note,rid))
        db.execute('COMMIT')
    if shot or status=='skipped':return
    extra=pool.finish_material_analysis(ident,rid)
    if (extra.get('analysis') or {}).get('status')=='saving':
        if not has_labels(extra.get('suggested')):
            with pool.connection() as db:
                db.execute("UPDATE material_analyses SET status='invalid',note='未收到可用标签。' WHERE request_id=?",(rid,))
        else:
            try:
                sources.apply_gemini_tags(ident,extra['suggested'])
                prefix='Gemini 按时间分析 4 张视频抽帧' if video else 'Gemini 分析图片'
                pool.material_analysis_note(rid,prefix+'；已登记空白标签，已有标签保留。')
            except (OSError, ValueError):
                pool.material_analysis_note(rid,'已收到标签；保存结果未确认，不重复分析。',status='unknown')


def run_once(sources,pool,backfill=None):
    if pool.maintenance():return
    job=None; result=None; video=False
    job=claim(pool,backfill is None)
    if not job:return
    ident=job['ident'];rid=job['request_id']
    shot=job['table']=='material_shot_jobs'
    receipt=pool.state_dir/'material-vision-audit'/f'{rid}.json'
    try:
        with sources.library('GET',f'/api/library/{ident}') as response:
            if response.status!=200:raise ValueError('asset_unavailable')
            item=json.load(response)
        video=item.get('media_type')=='video'
        extra=pool.material_extra(ident)
        imported = job['imported']
        if imported and not any(origin.get('source') == 'windows_local' for origin in item['origins']):
            raise ValueError('unexpected_source')
        labels = (item.get('observed_tags'), extra.get('suggested')) if imported else (
            item.get('tags'), item.get('structured_tags'), extra.get('manual'), extra.get('suggested'))
        if not shot and any(has_labels(x) for x in labels):
            result={'status':'skipped'}
        else:
            with tempfile.TemporaryDirectory(prefix='sw-material-') as directory:
                parts=read_media(sources,ident,Path(directory),imported=imported)
                if shot:
                    parts[0]={'type':'text','text':SHOT_PROMPT}
                result=analyze(pool.official.runtime,job['account'],parts,receipt,model=job['model'])
    except Exception:
        result={'status':'unknown' if receipt.exists() else 'failed','reason':'material_preparation_or_call_failed'}
    finish(pool,sources,job,result,video)
    if not shot and result['status']!='skipped':
        (backfill.finish if backfill else pool.finish_auto_material)(rid)
