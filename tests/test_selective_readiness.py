"""One-click repair preserves healthy, disabled, busy and unresolved accounts."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from server import Sources, readiness_repair_accounts
from official_pool import OfficialPool
from gemini_authorization import GeminiAuthorization

class SelectiveReadiness(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
        CREATE TABLE requests(request_id TEXT,account_key TEXT,status TEXT,finished REAL);
        CREATE TABLE official_jobs(request_id TEXT,reason TEXT,readiness_probe INTEGER);
        CREATE TABLE official_account_blocks(account_key TEXT,reason TEXT,created REAL);
        CREATE TABLE account_preferences(account_key TEXT,gemini_enabled INTEGER);
        CREATE TABLE official_reauth(request_id TEXT,account_key TEXT,state TEXT);
        CREATE TABLE conversation_turns(request_id TEXT,conversation_id TEXT);
        CREATE TABLE conversations(id TEXT,native_json TEXT);
        INSERT INTO account_preferences VALUES('paused',0);
        INSERT INTO official_account_blocks VALUES('recover','provider_error',1);
        INSERT INTO official_account_blocks VALUES('busy','provider_error',1);
        INSERT INTO official_account_blocks VALUES('quota','activation_verification_required',1);
        INSERT INTO requests VALUES('running','busy','dispatching',NULL);
        INSERT INTO official_jobs VALUES('running',NULL,0);
        INSERT INTO requests VALUES('old','healthy','completed',1);
        INSERT INTO official_jobs VALUES('old',NULL,0);
        """)
        self.pool=OfficialPool.__new__(OfficialPool)
        self.pool.pool=S(connection=lambda:self.db,maintenance=lambda:False)
        self.pool.runtime=S(inventory=lambda:dict.fromkeys(['healthy','quota','paused','busy','recover'],True))
        self.pool.pool.official=self.pool
        self.pool.generate=Mock(return_value=({'request_id':'probe'},True))

    def test_only_recoverable_unready_account_is_probed(self):
        with patch.object(OfficialPool,'ready',new_callable=lambda:property(lambda self:True)):
            result=self.pool.queue_readiness_probes([], {})
        self.assertEqual([r['account_key'] for r in result],['recover'])
        self.assertEqual(self.pool.generate.call_count,1)
        self.assertEqual(self.db.execute("SELECT finished FROM requests WHERE request_id='old'").fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT gemini_enabled FROM account_preferences').fetchone()[0],0)

    def test_maintenance_does_not_submit_probe(self):
        self.pool.pool.maintenance=lambda:True
        with patch.object(OfficialPool,'ready',new_callable=lambda:property(lambda self:True)):
            self.assertEqual(self.pool.queue_readiness_probes([],{}),[])
        self.pool.generate.assert_not_called()

    def test_authorization_rechecks_healthy_disabled_and_busy(self):
        auth=GeminiAuthorization(S(pool=self.pool.pool,runtime=self.pool.runtime))
        for key in ['healthy','quota','paused','busy']:
            self.assertFalse(auth.allowed(key,login_only=True),key)
        self.assertTrue(auth.allowed('recover',login_only=True))
        self.db.execute("DELETE FROM official_account_blocks WHERE account_key='recover'")
        self.assertFalse(auth.allowed('recover',login_only=True))

    def test_submission_rechecks_ready_or_busy_before_creating_job(self):
        from live_pool import Conflict
        self.db.execute('CREATE TABLE flow_requests(request_id TEXT)')
        self.db.execute('ALTER TABLE requests ADD COLUMN prompt_digest TEXT')
        self.db.commit()
        self.pool.pool.conversation_id=Mock()
        self.pool.pool.allowed=Mock()
        for key in ['healthy','busy']:
            with patch.object(OfficialPool,'ready',new_callable=lambda:property(lambda self:True)):
                with self.assertRaises(Conflict):
                    OfficialPool.generate(self.pool,key,'check','new-probe',[],readiness_probe=True)
            self.assertIsNone(self.db.execute("SELECT 1 FROM requests WHERE request_id='new-probe'").fetchone())

    def test_flow_targets_exclude_ready_disabled_busy_and_review(self):
        accounts=[dict(profile=k,repairable=k!='paused',review_task='old' if k=='review' else None)
                  for k in ['healthy','paused','busy','pagebusy','review','broken','google_check','login']]
        payload=dict(accounts=accounts,busy=['busy'])
        checks=dict(healthy='ok',paused='binding',busy='binding',pagebusy='busy',review='draft',broken='binding',
                    google_check='verification_required',login='login')
        self.assertEqual([a['profile'] for a in readiness_repair_accounts(payload,checks)],['pagebusy','broken'])
        self.assertEqual(len(payload['accounts']),8)

    def test_auto_cleanup_refreshes_stale_credits_only_for_repaired_idle_account(self):
        common=dict(email='fixture@example.invalid',flow_login='verified',
                    flow_page_incident=None,flow_risk_known=True,flow_risk_paused=False,
                    flow_pacing_known=True,flow_cooling=False,flow_balance_fresh=False)
        accounts=[dict(common,profile=profile,key='flow-account-'+profile)
                  for profile in ('cleaned','returned','partial','untouched')]
        source=Sources()
        source.read=Mock(return_value=({'tasks':[]},None))
        source.refresh_flow_balances_once=Mock()
        response=lambda data:S(stdout=json.dumps(data).encode())
        with patch('server.account_rows',return_value=accounts), \
             patch('server.project_rows',return_value=[]), \
             patch('server.task_rows',return_value=[]), \
             patch('server.Path',side_effect=[S(read_bytes=lambda:sys.executable.encode()),Path(sys.executable)]), \
             patch('server.subprocess.check_output',return_value='123'), \
             patch('server.subprocess.run',side_effect=[
                 response({'cleaned':'unique','returned':'flow_landing','partial':'binding','untouched':'ok'}),
                 response({'cleaned':{'page':'ok','actions':['close_duplicate']},
                           'returned':{'page':'ok','actions':['return_to_project']},
                           'partial':{'page':'binding','actions':['open_browser']}}),
                 response({'cleaned':'ok','returned':'ok','partial':'ok','untouched':'ok'}),
             ]) as run:
            result=source.flow_readiness(repair=True,automatic=True)
        self.assertFalse(json.loads(run.call_args_list[1].kwargs['input'])['reset_draft'])
        self.assertEqual(result['accounts'],{'cleaned':'ok','returned':'ok','partial':'ok','untouched':'ok'})
        source.refresh_flow_balances_once.assert_called_once_with(['cleaned','returned'])

    def test_manual_readiness_prepares_a_project_for_the_selected_account(self):
        common = dict(email='fixture@example.invalid', flow_login='verified', flow_page_incident=None,
                      flow_risk_known=True, flow_risk_paused=False, flow_pacing_known=True,
                      flow_cooling=False, flow_balance_fresh=True)
        accounts = [dict(common, profile=profile, key='flow-account-'+profile)
                    for profile in ('new', 'other')]
        project_id = 'c843623e-00b9-4ce1-907d-f95eaa30317f'
        source = Sources()
        source.read = Mock(return_value=({'tasks': []}, None))
        prepare_project = Mock(return_value=project_id)
        projects = [dict(source_profile='new', source_id=project_id)]
        response = lambda data: S(stdout=json.dumps(data).encode())
        with patch('server.account_rows', return_value=accounts), \
             patch('server.project_rows', side_effect=[[], projects]), \
             patch('server.task_rows', return_value=[]), \
             patch('server.Path', side_effect=[S(read_bytes=lambda: sys.executable.encode()), Path(sys.executable)]), \
             patch('server.subprocess.check_output', return_value='123'), \
             patch('flow_readiness_recovery.operator_dispatch_paused', return_value=False), \
             patch('server.subprocess.run', side_effect=[
                 response({'new': 'binding'}),
                 response({'new': {'page': 'ok', 'actions': ['rebind']}}),
                 response({'new': 'ok'}),
             ]) as run:
            result = source.flow_readiness(repair=True, account_key='flow-account-new',
                                           prepare_project=prepare_project)
        self.assertEqual(prepare_project.call_count, 1)
        binding_input = json.loads(run.call_args_list[1].kwargs['input'])
        self.assertEqual([(account['profile'], account['projects']) for account in binding_input['accounts']],
                         [('new', [project_id])])
        self.assertTrue(binding_input['accounts'][0]['project_prepared'])
        self.assertTrue(binding_input['reset_draft'])
        self.assertEqual(result['accounts'], {'new': 'ok'})
        self.assertEqual(result['repairs']['new']['actions'], ['prepare_project', 'rebind'])

if __name__=='__main__':unittest.main()
