"""Deadline, priority and no-retry checks; provider transports use local fake CLIs."""
import ast
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
import threading
import unittest
from uuid import uuid4
from types import SimpleNamespace as S
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'
sys.path.insert(0, str(ROOT))
from gemini_execution import CallBudget, execution_limits
from official_pool import OfficialPool, DEFAULT_MAX_CONCURRENCY, IMAGE_SESSION_START_TIMEOUT
from dispatch_limit import DispatchLimit
from official_runtime import OfficialRuntime, MODEL, BAD_REPLY
import material_vision
import gemini_image_input as vision


class Budgets(unittest.TestCase):
    def test_text_progress_extends_idle_but_never_total(self):
        now = [0]
        budget = CallBudget(10, 3, clock=lambda: now[0])
        for t in (2, 4, 6, 8):
            now[0] = t
            self.assertIsNone(budget.expired())
            budget.progress()
        now[0] = 10
        self.assertEqual(budget.expired(), 'total')
        now[0] = 0
        budget = CallBudget(10, 3, clock=lambda: now[0])
        now[0] = 3
        self.assertEqual(budget.expired(), 'idle')

    def test_configuration_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as d:
            runtime = S(config=Path(d) / 'official-runtime.json')
            self.assertEqual(execution_limits(runtime)['text_total_seconds'], 600)
            cfg = Path(d) / 'gemini-execution.json'
            cfg.write_text(json.dumps({'image_total_seconds': 900}))
            self.assertEqual(execution_limits(runtime)['image_total_seconds'], 900)
            for value in (True, 0, 3601, '600'):
                cfg.write_text(json.dumps({'idle_seconds': value}))
                with self.assertRaises(ValueError): execution_limits(runtime)


class ProbeSlots(unittest.TestCase):
    def test_single_probe_reserves_business_capacity_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            limit = DispatchLimit(Path(d) / 'dispatch')
            limit.set_limit(3)
            probe = limit.try_acquire_probe()
            self.assertIsNotNone(probe)
            self.assertIsNone(limit.try_acquire_probe())
            a = limit.try_acquire()
            b = limit.try_acquire()
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            self.assertIsNone(limit.try_acquire())
            probe.close(); probe.close()
            # Two business calls already occupy the two nonreserved slots.
            self.assertIsNone(limit.try_acquire_probe())
            a.close(); b.close()
            probe = limit.try_acquire_probe()
            self.assertIsNotNone(probe)
            probe.close()
            limit.set_limit(1)
            probe = limit.try_acquire_probe()
            self.assertIsNotNone(probe)
            self.assertIsNone(limit.try_acquire())
            probe.close()


class ProbePriority(unittest.TestCase):
    def make_pool(self, dbpath):
        with sqlite3.connect(dbpath) as db:
            db.executescript('''
              CREATE TABLE official_dispatch_settings(id INTEGER PRIMARY KEY,maximum INTEGER NOT NULL);
              CREATE TABLE requests(request_id TEXT PRIMARY KEY, account_key TEXT,created REAL,finished REAL,status TEXT,note TEXT);
              CREATE TABLE official_jobs(request_id TEXT PRIMARY KEY,reason TEXT,conversation_id TEXT,native_before TEXT,readiness_probe INTEGER,no_retry INTEGER,started REAL,queue_wait_ms REAL,model TEXT DEFAULT 'gemini-3.8-flash-medium');
              CREATE TABLE official_model_fallbacks(request_id TEXT PRIMARY KEY, requested_model TEXT, fallback_model TEXT, permitted_accounts TEXT, reason TEXT);
              CREATE TABLE conversation_turns(request_id TEXT, prompt TEXT, attachments TEXT, conversation_id TEXT);
              CREATE TABLE conversations(id TEXT PRIMARY KEY,hidden INTEGER,native_json TEXT);
              CREATE TABLE official_reauth(request_id TEXT,state TEXT,block_reason TEXT,block_created REAL,account_key TEXT);
              CREATE TABLE official_attempts(request_id TEXT,ordinal INTEGER,status TEXT,started REAL);
              INSERT INTO requests VALUES('probe','a',1,NULL,'queued',NULL);
              INSERT INTO requests VALUES('business','b',2,NULL,'queued',NULL);
              INSERT INTO official_jobs(request_id,reason,conversation_id,native_before,readiness_probe,no_retry,started,queue_wait_ms) VALUES('probe',NULL,'p',NULL,1,1,NULL,NULL);
              INSERT INTO official_jobs(request_id,reason,conversation_id,native_before,readiness_probe,no_retry,started,queue_wait_ms) VALUES('business',NULL,'b',NULL,0,1,NULL,NULL);
              INSERT INTO conversation_turns VALUES('probe','ready','[]','p');
              INSERT INTO conversation_turns VALUES('business','question','[]','b');
              INSERT INTO conversations VALUES('p',0,NULL);
              INSERT INTO conversations VALUES('b',0,NULL);
            ''')
        def connect():
            db = sqlite3.connect(dbpath, isolation_level=None)
            db.row_factory = sqlite3.Row
            return closing(db)
        pool = OfficialPool.__new__(OfficialPool)
        pool.pool = S(connection=connect, maintenance=lambda: False)
        pool.wake = threading.Event()
        pool.runtime = S(inventory=lambda: {'a': True, 'b': True})
        pool.account_quotas = lambda _: {}
        pool.reason = lambda *args, **kwargs: None
        pool.readiness_block = lambda *args: None
        return pool

    def test_business_overtakes_probe_and_probe_waits_for_active_business(self):
        with tempfile.TemporaryDirectory() as d:
            dbpath = Path(d) / 'queue.db'
            pool = self.make_pool(dbpath)
            self.assertIsNone(pool.claim(probe_only=True))
            claimed = pool.claim(probe_only=False)
            self.assertEqual(claimed['request_id'], 'business')
            self.assertIsNone(pool.claim(probe_only=True))
            with sqlite3.connect(dbpath) as db:
                db.execute("UPDATE requests SET status='completed',finished=3 WHERE request_id='business'")
            self.assertEqual(pool.claim(probe_only=True)['request_id'], 'probe')

    def test_recent_success_prevents_new_probe(self):
        with tempfile.TemporaryDirectory() as d:
            dbpath = Path(d) / 'queue.db'
            pool = self.make_pool(dbpath)
            import time
            with sqlite3.connect(dbpath) as db:
                db.execute("UPDATE requests SET status='completed',finished=? WHERE request_id='probe'", (time.time(),))
                db.execute("DELETE FROM requests WHERE request_id='business'")
            pool.runtime.inventory = lambda: {'a': True}
            pool.generate = Mock()
            self.assertEqual(pool.queue_readiness_probes([], {}), [])
            pool.generate.assert_not_called()

    def test_fifty_concurrent_claims_use_saved_limit_and_are_independent_of_flow(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            pool=self.make_pool(root/'queue.db')
            self.assertEqual(pool.concurrency_status()['maximum'],50)
            for bad in (0,51,True,1.5,'50'):
                with self.assertRaises(ValueError):
                    pool.set_concurrency(bad)
            limit=DispatchLimit(root/'dispatch');limit.set_limit(2)
            slots=[limit.try_acquire(),limit.try_acquire()]
            try:
                with sqlite3.connect(root/'queue.db') as db:
                    db.execute("DELETE FROM requests")
                    db.execute("DELETE FROM official_jobs")
                    db.execute("DELETE FROM conversation_turns")
                    for i in range(55):
                        rid='job-'+str(i)
                        db.execute("INSERT INTO requests VALUES(?,'a',?,NULL,'queued',NULL)",(rid,i))
                        db.execute("INSERT INTO official_jobs(request_id,reason,conversation_id,native_before,readiness_probe,no_retry,started,queue_wait_ms) VALUES(?,NULL,?,NULL,0,1,NULL,NULL)",(rid,rid))
                        db.execute("INSERT INTO conversation_turns VALUES(?,'question','[]',?)",(rid,rid))
                        db.execute("INSERT INTO conversations VALUES(?,0,NULL)",(rid,))
                with ThreadPoolExecutor(max_workers=55) as workers:
                    claims=list(workers.map(lambda _:pool.claim(),range(55)))
                self.assertEqual(sum(r is not None for r in claims),DEFAULT_MAX_CONCURRENCY)
                self.assertEqual(len({r['request_id'] for r in claims if r}),DEFAULT_MAX_CONCURRENCY)
                self.assertIsNone(pool.claim())
                status = pool.set_concurrency(2)
                self.assertEqual((status['active'],status['queued'],status['maximum']), (50,5,2))
                self.assertEqual(pool.concurrency_status()['maximum'],2)
                self.assertIsNone(pool.claim())
                with pool.pool.connection() as db:
                    self.assertFalse(pool.has_concurrency(db))
                self.assertEqual(limit.status()['active'],2)
                pool.set_concurrency(50)
                with sqlite3.connect(root/'queue.db') as db:
                    db.execute("UPDATE requests SET status='completed' WHERE request_id=?",(next(r['request_id'] for r in claims if r),))
                self.assertIsNotNone(pool.claim())
            finally:
                for slot in slots:slot.close()

    def test_same_conversation_never_runs_twice(self):
        with tempfile.TemporaryDirectory() as d:
            pool=self.make_pool(Path(d)/'queue.db')
            with sqlite3.connect(Path(d)/'queue.db') as db:
                db.execute("UPDATE official_jobs SET conversation_id='b',readiness_probe=0")
            self.assertIsNotNone(pool.claim())
            self.assertIsNone(pool.claim())

    def test_worker_does_not_consult_flow_limiter(self):
        pool=OfficialPool.__new__(OfficialPool)
        pool.stop=threading.Event()
        pool.wake=threading.Event()
        from contextlib import nullcontext
        db=Mock()
        db.execute.return_value.fetchone.return_value=(1,)
        pool.pool=S(connection=lambda:nullcontext(db))  # No Flow dispatch_limit.
        job=dict(attachments='[]')
        def claim():
            pool.stop.set()
            return job
        pool.claim=claim
        pool.run_text=Mock(return_value={'status':'completed'})
        pool.finish=Mock()
        pool.worker()
        pool.run_text.assert_called_once_with(job)
        pool.finish.assert_called_once_with(job,{'status':'completed'})


class ImageNoRetry(unittest.TestCase):
    def test_never_does_not_even_reserve_an_extra_attempt(self):
        pool = OfficialPool.__new__(OfficialPool)
        pool.runtime = object()
        pool.pool = S(state_dir=Path('/unused'))
        pool.reserve_retry = Mock(return_value=True)
        job = dict(request_id='test',account_key='a',prompt='describe',attachments='[]',no_retry=1)
        for reason,submitted in ((BAD_REPLY,True),(IMAGE_SESSION_START_TIMEOUT,False)):
            result = dict(status='failed',submitted=submitted,reason=reason)
            with self.subTest(reason=reason), \
                 patch.object(vision,'normalized_attachments',return_value=[]), \
                 patch.object(vision,'files_for',return_value=([],[])), \
                 patch.object(vision,'prompt_parts',return_value=[]), \
                 patch.object(material_vision,'analyze',return_value=result) as analyze:
                self.assertEqual(pool.run_image(job), result)
                analyze.assert_called_once()
                pool.reserve_retry.assert_not_called()


@unittest.skipUnless(os.name == 'posix', 'Pipe selector integration requires Linux')
class LocalTransport(unittest.TestCase):
    def setup_runtime(self, directory, body):
        root = Path(directory)
        binary = root / 'fake-cli'
        binary.write_text('#!/usr/bin/python3\n' + textwrap.dedent(body), encoding='utf-8')
        binary.chmod(0o700)
        (root / 'workspace').mkdir()
        runtime = OfficialRuntime(root / 'official-runtime.json')
        runtime.account = lambda _: dict(root=root,binary=binary,email='fixture@example.test')
        (root / 'gemini-execution.json').write_text(json.dumps(dict(
            text_total_seconds=4,image_total_seconds=4,idle_seconds=1)))
        return runtime, root

    @patch('official_runtime.official_network_env', return_value={})
    def test_text_stream_keeps_waiting_and_no_progress_times_out_once(self, _):
        body = '''
            import json,sys,time
            assert sys.argv[sys.argv.index('--print-timeout')+1]=='0'
            prompt=sys.argv[sys.argv.index('--print')+1]
            def emit(d):print(json.dumps(d),flush=True)
            if prompt=='idle':
                time.sleep(6)
            else:
                for char in 'ABC':
                    time.sleep(.6)
                    emit({'event':'step_update','step_update':{'step_type':'agent_response','text_delta':char}})
                emit({'event':'result','result':{'status':'SUCCESS','conversation_id':'11111111-1111-4111-8111-111111111111','response':'ABC'}})
        '''
        with tempfile.TemporaryDirectory() as d:
            runtime, root = self.setup_runtime(d, body)
            result = runtime.run('a','stream',None,root/'stream-audit')
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['response'], 'ABC')
            self.assertGreater(result['total_ms'], 1000)
            result = runtime.run('a','idle',None,root/'idle-audit')
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['timeout_kind'], 'idle')
            self.assertLess(result['total_ms'], 4000)
            self.assertTrue((root/'idle-audit/result.json').is_file())

    @patch('material_vision.official_network_env', return_value={})
    def test_acp_stream_is_not_cut_off_while_text_arrives(self, _):
        body = '''
            import json,sys,time
            def emit(d):print(json.dumps(d),flush=True)
            for line in sys.stdin:
                req=json.loads(line);i=req['id']
                if i==1:r={'agentCapabilities':{'promptCapabilities':{'image':True}}}
                elif i==2:r={'sessionId':'fixture','models':{'currentModelId':'gemini-3.8-flash-medium'}}
                elif i==4:
                    if req['params']['prompt']==['idle']:
                        emit({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'A'}}}})
                        time.sleep(6)
                    for char in 'ABC':
                        time.sleep(.6)
                        emit({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':char}}}})
                    r={'stopReason':'end_turn'}
                else:r={}
                emit({'jsonrpc':'2.0','id':i,'result':r})
        '''
        with tempfile.TemporaryDirectory() as d:
            runtime, root = self.setup_runtime(d, body)
            result = material_vision.analyze(runtime,'a',[],root/'receipt.json',keep_partial=True)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['response'], 'ABC')
            self.assertGreater(result['total_ms'], 1000)
            result = material_vision.analyze(runtime,'a',['idle'],root/'idle.json',keep_partial=True)
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['reason'], 'protocol_idle_timeout_4')
            self.assertEqual(result['response'], 'A')
            self.assertLess(result['total_ms'], 4000)

    @patch('material_vision.official_network_env', return_value={})
    @patch('official_pool.reader_for')
    def test_image_session_timeout_retries_on_the_same_request_before_sending(self, reader, _):
        from live_pool import RealPool

        body = '''
            import json,sys,time
            from pathlib import Path
            sessions=Path(__file__).with_suffix('.sessions')
            prompts=Path(__file__).with_suffix('.prompts')
            def emit(d):print(json.dumps(d),flush=True)
            for line in sys.stdin:
                req=json.loads(line)
                method=req['method']
                if method=='initialize':
                    result={'agentCapabilities':{'promptCapabilities':{'image':True}}}
                elif method=='session/new':
                    first=not sessions.exists()
                    with sessions.open('a') as record:record.write('new\\n')
                    if first:time.sleep(30)
                    result={'sessionId':'fixture','models':{'currentModelId':'gemini-4-hydrogen-medium'}}
                elif method=='session/prompt':
                    with prompts.open('a') as record:record.write('sent\\n')
                    emit({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk',
                        'content':{'type':'text','text':'red square'}}}})
                    result={'stopReason':'end_turn'}
                else:raise AssertionError(method)
                emit({'jsonrpc':'2.0','id':req['id'],'result':result})
        '''
        key='a'*64
        request_id=str(uuid4())
        attachments=[{'index':0,'extension':'png','sha256':'f'*64}]
        reader.return_value.cached_accounts.return_value={}
        with tempfile.TemporaryDirectory() as d:
            runtime, root = self.setup_runtime(d, body)
            runtime.inventory=lambda:{key:True}
            runtime.text_models=lambda:{key:{'gemini-4-hydrogen-medium'}}
            with closing(RealPool(root,root)) as live, \
                 patch.object(vision,'files_for',return_value=(attachments,[])), \
                 patch.object(vision,'prompt_parts',return_value=[]):
                live.official.runtime=runtime
                live.official.generate('auto','describe',request_id,[{'key':'flow-account-'+key}],
                                       attachments,image_analysis=True)
                job=live.official.claim()
                self.assertEqual((job['request_id'],job['account_key']),(request_id,key))
                self.assertEqual(job['model'],'gemini-4-hydrogen-medium')
                result=live.official.run_image(job)
                live.official.finish(job,result)
                self.assertEqual((result['status'],result['response']),('completed','red square'))
                with live.connection() as db:
                    attempts=db.execute("""SELECT ordinal,status,reason,submitted FROM official_attempts
                        WHERE request_id=? ORDER BY ordinal""",(request_id,)).fetchall()
                self.assertEqual([tuple(row) for row in attempts],[
                    (1,'failed',IMAGE_SESSION_START_TIMEOUT,0),(2,'completed',None,1),
                ])
                self.assertEqual((root/'fake-cli.sessions').read_text(),'new\nnew\n')
                self.assertEqual((root/'fake-cli.prompts').read_text(),'sent\n')
                receipts=root/'official-image-audit'
                self.assertFalse((receipts/(request_id+'.json')).exists())
                self.assertTrue((receipts/(request_id+'.attempt-2.json')).is_file())


if __name__ == '__main__':
    unittest.main()
