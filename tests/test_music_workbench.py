import ast
from contextlib import closing
from decimal import Decimal
import hashlib
import http.client
import json
import math
import sqlite3
from pathlib import Path
import re
import time
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
from uuid import uuid4

SOURCE=Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench/server.py'

def functions():
    tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
    selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('music_generate','music_service_status','music_account_order','music_credit_summary','music_order_supported')]
    scope={'time':time,'re':re,'Decimal':Decimal,'hashlib':hashlib,'json':json,'math':math,'sqlite3':sqlite3,'closing':closing,'MUSIC_MODELS':{'lyria-fast'},'music_credentials':lambda:('user','password','test-key'),
           'music_admin_token':lambda:'test-admin','music_call':Mock(),
           'MUSIC_GENERATION_PATH':'/v1/audio/generations', 'MUSIC_PRIORITY_PATH':'/v1/audio/generations/priority',
           'MUSIC_API_HOST':'127.0.0.1', 'MUSIC_API_PORT':8000,
           'http':SimpleNamespace(client=SimpleNamespace(HTTPConnection=Mock(), HTTPException=http.client.HTTPException))}
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(SOURCE),'exec'),scope)
    return scope

class MusicWorkbench(unittest.TestCase):
    def test_invalid_input_is_rejected_before_provider_call(self):
        s=functions()
        for prompt,model in [(None,None),(42,None),([],None),('test',{}),('test',[]),('test','bad'),(' '*2,None),('x'*2001,None)]:
            with self.subTest(prompt=type(prompt).__name__,model=model):
                with self.assertRaises(ValueError):s['music_generate'](prompt,model)
        s['music_call'].assert_not_called()

    def test_failed_status_is_unavailable(self):
        s=functions();s['music_call'].side_effect=OSError('offline')
        result=s['music_service_status']()
        self.assertFalse(result['available'])
        self.assertEqual(result['accounts'],[])

    def test_credit_changes_keep_a_baseline_and_ignore_membership_and_stale_reads(self):
        path=SOURCE.parents[4]/'tests'/('music-test-'+uuid4().hex+'.sqlite3')
        summarize=functions()['music_credit_summary']
        def observed(a, b, timestamp, second='b'):
            return summarize([{'email':'a@example.test','credits':a},
                              {'email':second+'@example.test','credits':b}],path,timestamp)
        try:
            self.assertEqual(observed(20, 30, 1), {'total':50, 'last_change':None})
            self.assertEqual(observed(20, 25, 3), {'total':45, 'last_change':-5})
            self.assertEqual(observed(20, 29, 2), {'total':45, 'last_change':-5})
            self.assertEqual(observed(20, 25, 4)['last_change'], -5)
            self.assertEqual(observed(20, None, 5), {'total':None, 'last_change':None})
            self.assertEqual(observed(20, 40, 6)['last_change'], 15)
            self.assertEqual(observed(20, 20, 7, second='c')['last_change'], None)
        finally:
            path.unlink(missing_ok=True)

    def test_completed_clip_keeps_job_and_download_urls(self):
        s=functions();s['music_call'].return_value={'data':[{'id':'clip','title':'Test','audio':{'url':'https://example.test/a'},'wav':{'url':'https://example.test/w'}}],'flowmusic':{'job_id':'job'}}
        result=s['music_generate'](' test ','lyria-fast')
        self.assertEqual(result['clips'][0]['job_id'],'job')
        self.assertEqual(result['clips'][0]['clip_id'],'clip')
        self.assertEqual(result['clips'][0]['prompt'],'test')
        self.assertEqual(result['clips'][0]['wav_url'],'https://example.test/w')
        s['music_call'].assert_called_once()

    def test_shared_order_uses_the_priority_service_route_and_keeps_music_only_members(self):
        s=functions()
        connection=s['http'].client.HTTPConnection.return_value
        connection.getresponse.return_value=SimpleNamespace(status=405,getheader=lambda name:'POST')
        a,b='a'*64,'b'*64
        items=[{'key':'flow-account-'+a,'email':'A@example.test'},
               {'key':'flow-account-'+b,'email':'b@example.test','gemini_removed':True}]
        order=s['music_account_order'](SimpleNamespace(priority_order=lambda keys:list(reversed(keys))),items)
        s['music_call'].return_value={'data':[{'id':'clip','audio':{'url':'https://example.test/a'}}]}
        s['music_generate']('prompt','lyria-fast',account_order=order)
        s['music_call'].assert_called_once_with('POST','/v1/audio/generations/priority',
            body={'model':'lyria-fast','prompt':'prompt','account_order':['b@example.test','a@example.test']},
            token='test-key',timeout=660)
        connection.request.assert_called_once_with('GET','/v1/audio/generations/priority')
        connection.close.assert_called_once()

    def test_legacy_music_route_still_submits_exactly_once(self):
        s=functions()
        connection=s['http'].client.HTTPConnection.return_value
        connection.getresponse.return_value=SimpleNamespace(status=404,getheader=lambda name:None)
        s['music_call'].return_value={'data':[{'id':'clip','audio':{'url':'https://example.test/a'}}]}
        s['music_generate']('prompt','lyria-fast',account_order=['a@example.test'])
        s['music_call'].assert_called_once_with('POST','/v1/audio/generations',
            body={'model':'lyria-fast','prompt':'prompt'},token='test-key',timeout=660)

    def test_unknown_route_state_does_not_submit(self):
        s=functions()
        connection=s['http'].client.HTTPConnection.return_value
        connection.getresponse.side_effect=TimeoutError
        with self.assertRaisesRegex(ValueError,'尚未提交'):
            s['music_generate']('prompt','lyria-fast',account_order=[])
        s['music_call'].assert_not_called()

    def test_failed_priority_post_never_retries_the_old_route(self):
        s=functions()
        connection=s['http'].client.HTTPConnection.return_value
        connection.getresponse.return_value=SimpleNamespace(status=405,getheader=lambda name:'POST')
        s['music_call'].side_effect=TimeoutError
        with self.assertRaises(TimeoutError):
            s['music_generate']('prompt','lyria-fast',account_order=[])
        s['music_call'].assert_called_once()
