"""Read existing upstream receipts only; this module never generates music."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

MUSIC_HOME = Path(os.environ.get('STUDIOWRIGHT_MUSIC_HOME') or Path.home() / 'tools/FlowMusic2API').expanduser().resolve()
DATABASE = MUSIC_HOME / 'data/flowmusic2api.db'

def recover(row, database=DATABASE):
    if not database.is_file():return None
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        logs=db.execute("SELECT id,request_body,response_body,status_code,created_at FROM request_logs WHERE operation='music.generate' ORDER BY id DESC").fetchall()
    matches=[]
    for log in logs:
        try:
            request=json.loads(log['request_body'])
            created=datetime.fromisoformat(log['created_at'].replace('Z','+00:00'))
            if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
            if not row['created']-2 <= created.timestamp() <= row['created']+30:continue
            if request.get('prompt')!=row['prompt'] or request.get('model')!=row['model']:continue
            matches.append(log)
        except (ValueError,TypeError,AttributeError):continue
    # Never attach another request merely because it used similar words.
    if len(matches)!=1:return None
    log=matches[0]
    if log['status_code']==102:return {'status':'recovering','log_id':log['id']}
    try:body=json.loads(log['response_body'])
    except (ValueError,TypeError):return None
    clips=[]
    if log['status_code']==200 and body.get('job_id'):
        for clip in body.get('clips',[]):
            if not isinstance(clip,dict) or not clip.get('id'):return None
            audio=(clip.get('audio') or {}).get('url','');wav=(clip.get('wav') or {}).get('url','')
            if not audio and not wav:return None
            clips.append({'clip_id':clip['id'],'title':clip.get('title') or '未命名音乐',
                          'audio_url':audio,'wav_url':wav,'cover_url':(clip.get('image') or {}).get('url',''),
                          'job_id':body['job_id'],'prompt':row['prompt']})
    if not clips:return None
    return {'status':'completed','log_id':log['id'],'result':{'schema_version':1,'model':row['model'],'clips':clips}}
