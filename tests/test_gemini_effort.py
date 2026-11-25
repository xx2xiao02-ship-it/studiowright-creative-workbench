import json,os,sys,tempfile,time,unittest,uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from gemini_effort import resolve,DEFAULT,DEFAULT_TEXT_MODEL,NEON_FAMILY,HYDROGEN_FAMILY,THOUGHT_LEVELS,text_model_options
from official_runtime import OfficialRuntime
from official_pool import OfficialPool
from live_pool import RealPool
from dispatch_limit import DispatchBusy
import test_gemini_stability as fixtures
from test_flow_regressions import fixture_directory

class Effort(unittest.TestCase):
    def test_supported_and_fallback(self):
        for level in ('low','medium','high'):
            self.assertEqual(resolve(effort=level),'gemini-3.8-flash-'+level)
        for invalid in (None,'','max','HIGH',False,123,{},[]):
            self.assertEqual(resolve(effort=invalid),DEFAULT)

    def test_model_and_effort_resolve_and_only_configured_advanced_options_are_advertised(self):
        for family in (NEON_FAMILY,HYDROGEN_FAMILY):
            self.assertEqual(resolve(family),family+'-high')
            for effort in THOUGHT_LEVELS:
                self.assertEqual(resolve(family,effort),family+'-'+effort)
            with self.assertRaises(ValueError):
                resolve(family+'-high','low')
        with self.assertRaises(ValueError):
            resolve(NEON_FAMILY+'-typo','typo')
        self.assertEqual(OfficialRuntime._text_models({'neon_text_enabled':True}),{NEON_FAMILY+'-high'})
        configured=OfficialRuntime._text_models({'neon_text_enabled':True,'advanced_text_models':[HYDROGEN_FAMILY+'-xhigh','invalid']})
        self.assertEqual([row['id'] for row in text_model_options(configured)],[
            'gemini-3.8-flash-low',DEFAULT,'gemini-3.8-flash-high',HYDROGEN_FAMILY+'-xhigh',
        ])

    def test_advanced_models_require_explicit_supported_account_and_are_text_only(self):
        pool=OfficialPool.__new__(OfficialPool)
        pool.pool=S(conversation_id=Mock(),allowed=Mock())
        neon=NEON_FAMILY+'-high'
        pool.runtime=S(inventory=lambda:{'ready':True},text_models=lambda:{'enabled':{neon}})
        with self.assertRaisesRegex(ValueError,'仅用于文字'):
            pool.generate('enabled','test','request',[],model=neon,readiness_probe=True)
        with self.assertRaisesRegex(ValueError,'未开通'):
            pool.generate('other','test','request',[],model=neon)
        with self.assertRaisesRegex(ValueError,'未开通'):
            pool.generate('enabled','test','request',[],model=HYDROGEN_FAMILY)

    @unittest.skipUnless(os.name=='posix','Workbench account pool requires Linux')
    @patch('official_pool.reader_for')
    def test_advanced_auto_prefers_the_first_supported_account_until_quota_changes(self,reader_for):
        a,b,standard=('a'*64,'b'*64,'c'*64)
        keys=(a,b,standard)
        models={a:{NEON_FAMILY+'-high',HYDROGEN_FAMILY+'-low'},b:{NEON_FAMILY+'-high'}}
        quotas={}
        reader_for.return_value.cached_accounts.side_effect=lambda keys:{k:quotas.get(k,{}) for k in keys}
        with tempfile.TemporaryDirectory() as directory:
            pool=RealPool(directory,directory)
            official=pool.official
            official.runtime=S(inventory=lambda:dict.fromkeys(keys,True),text_models=lambda:models)
            items=[{'key':'flow-account-'+k} for k in keys]
            def send(model, rid=None):
                request=rid or str(uuid.uuid4())
                return official.generate('auto','test',request,items,model=model)
            rid=str(uuid.uuid4())
            self.assertEqual(send(NEON_FAMILY,rid)[0]['account_key'],a)
            self.assertFalse(send(NEON_FAMILY,rid)[1])
            self.assertEqual(send(DEFAULT)[0]['account_key'],a)
            self.assertEqual(send(NEON_FAMILY)[0]['account_key'],a)
            self.assertEqual(send(HYDROGEN_FAMILY+'-low')[0]['account_key'],a)
            quotas[a]={'status':'ok','checked_at':time.time(),'five_hour':{'remaining_percent':0}}
            self.assertEqual(send(NEON_FAMILY)[0]['account_key'],b)
            with self.assertRaises(DispatchBusy):
                send(HYDROGEN_FAMILY+'-low')
            claimed=official.claim()
            self.assertEqual((claimed['request_id'],claimed['account_key'],claimed['model']), (rid,b,NEON_FAMILY+'-high'))

    @unittest.skipUnless(os.name=='posix','Workbench account pool requires Linux')
    @patch('official_pool.reader_for')
    def test_advanced_auto_continuation_keeps_original_account(self,reader_for):
        first,second=('a'*64,'b'*64)
        reader_for.return_value.cached_accounts.return_value={}
        models={key:{NEON_FAMILY+'-high',NEON_FAMILY+'-low'} for key in (first,second)}
        with tempfile.TemporaryDirectory() as directory:
            pool=RealPool(directory,directory)
            official=pool.official
            official.runtime=S(inventory=lambda:{first:True,second:True},text_models=lambda:models)
            items=[{'key':'flow-account-'+k} for k in models]
            rid=str(uuid.uuid4())
            request,_=official.generate('auto','first',rid,items,model=NEON_FAMILY)
            conversation=request['conversation_id']
            native={'channel':'official_antigravity_cli','model':NEON_FAMILY+'-high','official_conversation_id':str(uuid.uuid4())}
            with pool.connection() as db:
                db.execute("UPDATE requests SET status='completed' WHERE request_id=?",(rid,))
                db.execute('UPDATE conversations SET native_json=? WHERE id=?',(json.dumps(native),conversation))
            def continue_chat():
                return official.generate('auto','follow up',str(uuid.uuid4()),items,conversation_id=conversation,
                                         model=NEON_FAMILY,effort='low')
            reader_for.return_value.cached_accounts.return_value={first:{'status':'verification_required'}}
            with self.assertRaises(DispatchBusy):
                continue_chat()
            reader_for.return_value.cached_accounts.return_value={}
            result,_=continue_chat()
            self.assertEqual(result['account_key'],first)
            self.assertEqual(result['model'],NEON_FAMILY+'-low')

    @unittest.skipUnless(os.name=='posix','Workbench account pool requires Linux')
    @patch('official_pool.reader_for')
    def test_default_hydrogen_is_quota_aware_and_replay_keeps_original_request(self,reader_for):
        standard, hydrogen = ('a'*64, 'b'*64)
        models = {hydrogen: {DEFAULT_TEXT_MODEL}}
        quotas = {}
        reader_for.return_value.cached_accounts.side_effect = lambda keys: {k:quotas.get(k,{}) for k in keys}
        with fixture_directory() as directory, closing(RealPool(directory,directory)) as pool:
            official=pool.official
            official.runtime=S(inventory=lambda:{standard:True,hydrogen:True},text_models=lambda:models)
            items=[{'key':'flow-account-'+key} for key in (standard,hydrogen)]
            rid=str(uuid.uuid4())
            result,_=official.generate('auto','default',rid,items)
            self.assertEqual((result['model'],result['account_key']),(DEFAULT_TEXT_MODEL,hydrogen))
            quotas[hydrogen]={'status':'ok','checked_at':time.time(),'weekly':{'remaining_percent':0}}
            self.assertFalse(official.generate('auto','default',rid,items)[1])
            with self.assertRaises(DispatchBusy):
                official.generate('auto','quota depleted',str(uuid.uuid4()),items)
            flash,_=official.generate('auto','explicit flash',str(uuid.uuid4()),items,model=DEFAULT)
            self.assertEqual(flash['model'],DEFAULT)

    @unittest.skipUnless(os.name=='posix','Workbench account pool requires Linux')
    @patch('official_pool.reader_for')
    def test_legacy_flash_replay_and_follow_up_keep_the_pinned_model(self,reader_for):
        key='a'*64
        reader_for.return_value.cached_accounts.return_value={}
        with fixture_directory() as directory, closing(RealPool(directory,directory)) as pool:
            official=pool.official
            official.runtime=S(inventory=lambda:{key:True},text_models=lambda:{key:{DEFAULT_TEXT_MODEL}})
            items=[{'key':'flow-account-'+key}]
            rid=str(uuid.uuid4())
            result,_=official.generate('auto','legacy prompt',rid,items,model=DEFAULT)
            replay,created=official.generate('auto','legacy prompt',rid,items)
            self.assertFalse(created)
            self.assertEqual(replay['model'],DEFAULT)
            conversation=result['conversation_id']
            saved={'channel':'official_antigravity_cli','model':DEFAULT,'official_conversation_id':str(uuid.uuid4())}
            with pool.connection() as db:
                db.execute("UPDATE requests SET status='completed' WHERE request_id=?",(rid,))
                db.execute('UPDATE conversations SET native_json=? WHERE id=?',(json.dumps(saved),conversation))
            follow_up,_=official.generate('auto','follow up',str(uuid.uuid4()),items,conversation_id=conversation)
            self.assertEqual((follow_up['model'],follow_up['account_key']),(DEFAULT,key))

    def test_capacity_retry_keeps_level(self):
        p=OfficialPool.__new__(OfficialPool);p.pool=S(state_dir=Path('/unused'))
        p.runtime=S(run=Mock(return_value=dict(status='failed',capacity_retry_safe=True)))
        p.capacity_retries=lambda job,result,call:call(2)
        p.run_text(dict(request_id='x',account_key='a',prompt='test',native_before=None,model='gemini-3.8-flash-high'))
        self.assertEqual(p.runtime.run.call_count,2)
        for c in p.runtime.run.call_args_list:self.assertEqual(c.kwargs['model'],'gemini-3.8-flash-high')

    @patch('official_pool.reader_for')
    def test_opt_in_flash_fallback_at_admission_and_before_first_send(self,reader_for):
        first, second, standard = ('a'*64, 'b'*64, 'c'*64)
        neon = NEON_FAMILY + '-xhigh'
        quotas = {}
        blocked = dict(status='ok', checked_at=time.time(), five_hour={'remaining_percent':0})
        reader_for.return_value.cached_accounts.side_effect = lambda keys: {k:quotas.get(k,{}) for k in keys}
        with fixture_directory() as directory, closing(RealPool(directory,directory)) as pool:
            official = pool.official
            official.runtime = S(inventory=lambda:dict.fromkeys((first,second,standard),True),
                                 text_models=lambda:{first:{neon},second:{neon}})
            items = [{'key':'flow-account-'+k} for k in (first,second,standard)]
            def submit(rid, fallback='gemini-3.8-flash-low'):
                return official.generate('auto','test',rid,items,model=neon,fallback_model=fallback)
            queued_id = str(uuid.uuid4())
            request, _ = submit(queued_id)
            self.assertEqual(request['account_key'],first)
            self.assertFalse(request['fallback']['used'])
            quotas[first] = blocked
            claimed = official.claim()
            self.assertEqual((claimed['account_key'],claimed['model']),(second,neon))
            quotas[second] = blocked
            immediate_id = str(uuid.uuid4())
            request, created = submit(immediate_id)
            self.assertTrue(created)
            self.assertEqual((request['account_key'],request['model'],request['requested_model']),
                             (standard,'gemini-3.8-flash-low',neon))
            self.assertEqual(request['fallback']['reason'],'advanced_pool_unavailable')
            self.assertFalse(submit(immediate_id)[1])
            self.assertEqual(official.claim()['request_id'], immediate_id)
            quotas.clear()
            fallback_id = str(uuid.uuid4())
            submit(fallback_id)
            quotas.update({first:blocked,second:blocked})
            self.assertEqual(official.claim()['account_key'],standard)
            result = pool.get(fallback_id)
            self.assertEqual(result['fallback'],{'model':'gemini-3.8-flash-low','used':True,'reason':'quota_exhausted'})
            self.assertEqual(len(result['attempts']),1)
            quotas[standard] = blocked
            with self.assertRaises(DispatchBusy):
                submit(str(uuid.uuid4()))
            with self.assertRaises(DispatchBusy):
                submit(str(uuid.uuid4()), None)

    @unittest.skipUnless(os.name=='posix','CLI pipes require Linux')
    @patch('official_runtime.official_network_env',return_value={})
    def test_cli_receipt_and_selection(self,_):
        body="""import sys,json
model=sys.argv[sys.argv.index('--model')+1]
print(json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':'11111111-1111-4111-8111-111111111111','response':model}}),flush=True)
"""
        for level in ('low','medium','high'):
            with tempfile.TemporaryDirectory() as d:
                runtime,root=fixtures.LocalTransport().setup_runtime(d,body)
                model=resolve(effort=level);r=runtime.run('a','fixture',None,root/'audit',model=model)
                self.assertEqual(r['response'],model)
                self.assertEqual(json.loads((root/'audit/submission.json').read_text())['model'],model)

    @unittest.skipUnless(os.name=='posix','CLI pipes require Linux')
    @patch('official_runtime.official_network_env',return_value={})
    def test_advanced_model_uses_ssh_auth_context_and_requires_configured_level(self,_):
        body="""import os, sys, json
assert os.environ['SSH_TTY']
model=sys.argv[sys.argv.index('--model')+1]
print(json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':'11111111-1111-4111-8111-111111111111','response':model}}),flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            runtime,root=fixtures.LocalTransport().setup_runtime(directory,body)
            account=runtime.account('fixture')
            model=HYDROGEN_FAMILY+'-xhigh'
            runtime.account=lambda _:{**account,'advanced_text_models':{NEON_FAMILY+'-high'}}
            blocked=runtime.run('fixture','test',None,root/'blocked',model=model)
            self.assertEqual(blocked['reason'],'model_account_unavailable')
            self.assertFalse((root/'blocked').exists())
            runtime.account=lambda _:{**account,'advanced_text_models':{model}}
            result=runtime.run('fixture','test',None,root/'allowed',model=model)
            self.assertEqual(result['response'],model)
