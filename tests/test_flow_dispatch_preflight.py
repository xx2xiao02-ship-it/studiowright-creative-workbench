import base64
import copy
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


SNAPSHOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
sys.path[:0] = [str(SNAPSHOT / 'flow/console'), str(SNAPSHOT / 'workbench'), str(SNAPSHOT / 'flow/source/src')]
from console_store import Store
from flow_warning_archive import read_page_png, view_screenshot_once
from login_readiness import resume_google_session
from page_activity_guard import WARNING_REGIONS, PageActivityWarning, stop_on_page_warning
from preflight import FINGERPRINT_SIGNALS, check_profile, collect_browser_fingerprint, dispatch_check
from studiowright import chrome


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jMuoAAAAASUVORK5CYII='
)


class LocalState(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / 'console-data')
        self.store.add_account('account-a', 'A')
        self.store.add_account('account-b', 'B')

    def test_account_locale_policy_passes_expected_values_and_rejects_missing_policy(self):
        profile = self.root / 'data/profiles/account-a/Default'
        profile.mkdir(parents=True)
        (profile / 'Preferences').write_text('{"intl":{"accept_languages":"en-US,en"}}', encoding='utf-8')
        self.assertEqual(dispatch_check(self.root, 'account-a'), (False, ['dispatch_locale_missing']))
        path = self.root / 'console-data/dispatch-locales.json'
        path.write_text(json.dumps({'account-a': {'languages': ['zh-CN'], 'timezone': 'Asia/Shanghai'}}),
                        encoding='utf-8')
        with patch('preflight.mapped_ports', return_value={'account-a': 3101}), \
                patch('preflight._system_timezone', return_value='Etc/UTC'), \
                patch('preflight.check_profile', side_effect=lambda *args, **kwargs:
                      check_profile(*args, proc_root=self.root, **kwargs)):
            ok, reasons = dispatch_check(self.root, 'account-a')
            path.write_text(json.dumps({'account-a': {'languages': ['en-US', 'en'], 'timezone': 'Etc/UTC'}}),
                            encoding='utf-8')
            self.assertEqual(dispatch_check(self.root, 'account-a'), (True, []))
        self.assertFalse(ok)
        self.assertIn('profile_languages_mismatch', reasons)
        self.assertIn('system_timezone_mismatch', reasons)
        self.assertEqual(dispatch_check(self.root, 'account-b'), (False, ['dispatch_locale_missing']))

    @staticmethod
    def warning_page(*, text='Our systems have detected unusual traffic from your computer network.',
                     body_text='', url='https://accounts.google.com/sorry'):
        region = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                                 inner_text=AsyncMock(return_value=text))
        regions = SimpleNamespace(count=AsyncMock(return_value=int(text is not None)),
                                  nth=Mock(return_value=region))
        body = SimpleNamespace(count=AsyncMock(return_value=1), inner_text=AsyncMock(return_value=body_text))
        return SimpleNamespace(url=url, goto=AsyncMock(), close=AsyncMock(),
                               screenshot=AsyncMock(return_value=PNG),
                               locator=Mock(side_effect={WARNING_REGIONS: regions, 'body': body}.__getitem__))

    async def test_body_fallback_checks_each_line_and_ignores_embedded_text(self):
        unrelated = self.warning_page(text=None, body_text='Help for unusual activity detected.\nOther content')
        await stop_on_page_warning(unrelated, self.root, 'account-a', 'browse')
        unrelated.screenshot.assert_not_awaited()
        page = self.warning_page(text='Page title', body_text='Page title\n  Unusual activity detected.  ')
        with self.assertRaises(PageActivityWarning):
            await stop_on_page_warning(page, self.root, 'account-a', 'browse')
        page.screenshot.assert_awaited_once()
        self.assertEqual(set(self.store.page_incidents()), {'account-a'})

    async def test_google_consent_is_allowed_and_preserves_the_url_restrictions(self):
        denied = (
            'http://consent.google.com/', 'https://consent.google.com:444/',
            'https://user@consent.google.com/', 'https://sub.consent.google.com/',
            'https://consent.google.com.invalid/',
        )
        for url in denied:
            with self.subTest(url=url):
                page = self.warning_page(url=url)
                await stop_on_page_warning(page, self.root, 'account-a', 'login')
                page.locator.assert_not_called()
        page = self.warning_page(url='https://consent.google.com:443/', body_text='Unusual activity detected.')
        with self.assertRaises(PageActivityWarning):
            await stop_on_page_warning(page, self.root, 'account-b', 'login')
        page.locator.assert_called_once_with(WARNING_REGIONS)
        page.screenshot.assert_awaited_once()
        self.assertEqual(set(self.store.page_incidents()), {'account-b'})

    async def test_login_saves_original_before_close_and_holds_only_the_affected_account(self):
        page = self.warning_page()

        async def capture(**_):
            self.assertEqual(self.store.page_incidents(), {})
            page.close.assert_not_awaited()
            return PNG

        async def close():
            hold = self.store.page_incidents()['account-a']
            self.assertEqual(read_page_png(self.root, hold['id']), PNG)

        page.screenshot.side_effect = capture
        page.close.side_effect = close
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with self.assertRaises(PageActivityWarning):
            await resume_google_session(context, 'account-a@example.test', None,
                check_warning=lambda current: stop_on_page_warning(current, self.root, 'account-a', 'login'))
        page.close.assert_awaited_once()
        holds = self.store.page_incidents()
        self.assertEqual(set(holds), {'account-a'})
        self.assertEqual(holds['account-a']['phase'], 'login')
        self.assertFalse(self.store.review_page_incident('account-b', holds['account-a']['id']))
        self.assertTrue(self.store.review_page_incident('account-a', holds['account-a']['id']))
        self.assertEqual(self.store.page_incidents(), {})
        self.assertIsNone(self.store.rows('SELECT screenshot FROM flow_page_incidents')[0]['screenshot'])

    def test_destroy_page_evidence_preserves_stop_and_prevents_capture_from_reappearing(self):
        self.store.record_page_incident('account-a', 'login', PNG)
        self.store.record_page_incident('account-b', 'browse', PNG)
        ident = self.store.page_incidents()['account-a']['id']
        self.assertEqual(read_page_png(self.root, ident), PNG)
        def open_original():
            try:
                return view_screenshot_once(self.root, ident)
            except FileNotFoundError:
                return None

        with ThreadPoolExecutor(max_workers=2) as clients:
            first = clients.submit(open_original)
            second = clients.submit(open_original)
            self.assertCountEqual([first.result(), second.result()], [PNG, None])
        self.store.record_page_incident('account-a', 'browse', PNG)
        holds = self.store.page_incidents()
        self.assertEqual(holds['account-a']['id'], ident)
        self.assertEqual(holds['account-a']['screenshot_destroyed'], 1)
        self.assertEqual(holds['account-a']['has_screenshot'], 0)
        with self.assertRaises(FileNotFoundError):
            read_page_png(self.root, ident)
        self.assertEqual(read_page_png(self.root, holds['account-b']['id']), PNG)

    def test_workbench_previews_without_burning_and_serves_the_original_once(self):
        spec = spec_from_file_location('workbench_screenshot_test', SNAPSHOT / 'workbench/server.py')
        workbench = module_from_spec(spec)
        spec.loader.exec_module(workbench)
        self.store.record_page_incident('account-a', 'login', PNG)
        ident = self.store.page_incidents()['account-a']['id']
        handler = workbench.make_handler(Mock(), deployment='ubuntu')
        with ThreadingHTTPServer(('127.0.0.1', 0), handler) as service:
            worker = threading.Thread(target=service.serve_forever, daemon=True)
            worker.start()
            try:
                with patch('flow_warning_archive.FLOW_ROOT', self.root), \
                        closing(HTTPConnection('127.0.0.1', service.server_port, timeout=4)) as client:
                    def request(method, path, body=None, headers=None):
                        client.request(method, path, body=body, headers=headers or {})
                        response = client.getresponse()
                        return response.status, response.read()

                    image = f'/api/flow-warning-screenshots/{ident}.png'
                    view = f'/api/flow-warning-screenshots/{ident}/view'
                    headers = {'Origin': f'http://127.0.0.1:{service.server_port}',
                               'Content-Type': 'application/x-www-form-urlencoded'}
                    self.assertEqual(request('GET', image), (200, PNG))
                    self.assertEqual(request('GET', view)[0], 404)
                    self.assertEqual(request('POST', view, 'view=original')[0], 403)
                    self.assertEqual(request('POST', view, 'view=preview!', headers)[0], 400)
                    self.assertEqual(request('GET', image), (200, PNG))
                    self.assertEqual(request('POST', view, 'view=original', headers), (200, PNG))
                    self.assertEqual(request('GET', image)[0], 404)
                    self.assertEqual(request('POST', view, 'view=original', headers)[0], 404)
            finally:
                service.shutdown()
                worker.join()
        self.assertEqual(self.store.page_incidents()['account-a']['id'], ident)

    async def test_capture_failure_still_blocks_and_an_unrelated_page_does_not(self):
        page = self.warning_page(text='检测到异常活动。', url='https://flow.google.com/project/example')
        page.screenshot.side_effect = OSError('unavailable')
        with self.assertRaises(PageActivityWarning):
            await stop_on_page_warning(page, self.root, 'account-a', 'browse')
        incident = self.store.page_incidents()['account-a']
        self.assertEqual(incident['has_screenshot'], 0)
        with self.assertRaises(FileNotFoundError):
            read_page_png(self.root, incident['id'])
        unrelated = self.warning_page(url='https://example.test/')
        await stop_on_page_warning(unrelated, self.root, 'account-b', 'browse')
        unrelated.locator.assert_not_called()

    async def test_execute_returns_claim_to_queue_before_touching_adapter(self):
        # Load explicitly since both Ubuntu components have a server.py.
        spec = spec_from_file_location('flow_console_dispatch_test', SNAPSHOT / 'flow/console/server.py')
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        console = module.Console.__new__(module.Console)
        console.root, console.store, console.test = self.root, self.store, False
        console.adapter = Mock()
        pilot = 'ub-chrome-975371911f'
        self.store.add_account(pilot, 'Pilot')
        console.busy, console.generation_busy, console.generation_projects = {pilot}, {}, set()
        console.desktop_keyboard_ready = Mock(return_value=True)
        database = self.root / 'data/studiowright.db'
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript('CREATE TABLE pacing(profile TEXT,cooldown_until REAL);'
                             'CREATE TABLE flow_risk(profile TEXT,paused INTEGER,consecutive INTEGER);')
        task = self.store.create_task(pilot, 'fixed-project', 'image', {}, 'dispatch-fixture')
        claim = self.store.claim(set(), {pilot})
        self.assertEqual(claim['id'], task['id'])
        await console.execute(claim)
        self.assertEqual(self.store.task(task['id'])['status'], 'queued')
        self.assertEqual(console.busy, set())
        self.assertEqual(console.adapter.mock_calls, [])

    async def test_console_keeps_other_accounts_out_of_pilot_checks_and_preserves_page_holds(self):
        spec = spec_from_file_location('flow_console_pilot_test', SNAPSHOT / 'flow/console/server.py')
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        console = module.Console.__new__(module.Console)
        console.root, console.store, console.test = self.root, self.store, False
        self.store.record_page_incident('account-b', 'login', PNG)
        pilots = ['ub-chrome-975371911f', 'account-2f8797411d41',
                  'ub-chrome-424097083b', 'ub-chrome-e7fc86b674',
                  'account-1', 'ub-chrome-2a0f1cbdca', 'ub-chrome-b5f593c253',
                  'account-f8d81e309793', 'ub-chrome-748b00e533', 'ub-chrome-f8f5d94158']
        with patch('preflight.dispatch_check', return_value=(False, ['dispatch_locale_missing'])) as check:
            expected = {profile: ['dispatch_locale_missing'] for profile in pilots}
            expected.update({'account-a': [], 'account-b': ['flow_page_warning']})
            self.assertEqual(console.dispatch_preflight(['account-a', 'account-b', *pilots]), expected)
        self.assertEqual([call.args for call in check.call_args_list], [(self.root, profile) for profile in pilots])


class ChromeComponentDispatch(unittest.TestCase):
    profile = 'ub-chrome-975371911f'
    additional_profiles = ('account-2f8797411d41', 'ub-chrome-424097083b', 'ub-chrome-e7fc86b674',
                           'account-1', 'ub-chrome-2a0f1cbdca', 'ub-chrome-b5f593c253',
                           'account-f8d81e309793', 'ub-chrome-748b00e533', 'ub-chrome-f8f5d94158')
    peer = 'another-profile'
    store_url = 'https://clients2.google.com/service/update2/crx'

    @staticmethod
    def manifest(label):
        key = label.encode('ascii')
        digest = hashlib.sha256(key).hexdigest()[:32]
        ident = digest.translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop'))
        return ident, {'key': base64.b64encode(key).decode('ascii')}

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        policy = self.root / 'console-data/dispatch-locales.json'
        policy.parent.mkdir()
        fields = {'languages': ['zh-CN', 'zh', 'en'], 'timezone': 'America/Chicago'}
        profiles = (self.profile, *self.additional_profiles, self.peer)
        policy.write_text(json.dumps(dict.fromkeys(profiles, fields)), encoding='utf-8')

        resources = {}
        self.records = {}
        for name in ('gemini', 'network-speech', 'pdf', 'hangouts'):
            ident, manifest = self.manifest(name)
            resources[ident] = '/opt/google/chrome/resources/' + name
            self.records[ident] = dict(location=5, from_webstore=False, path=resources[ident], manifest=manifest)
        self.builtin = next(iter(resources))
        self.payment, manifest = self.manifest('payments')
        manifest['update_url'] = self.store_url
        relative = self.payment + '/1.0.0.6_0'
        self.records[self.payment] = dict(location=10, from_webstore=True, path=relative, manifest=manifest)
        for profile in profiles:
            folder = self.default(profile) / 'Extensions' / relative
            folder.mkdir(parents=True)
            (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.write_prefs(profile, self.records)
        self.payment_manifest = self.default(self.profile) / 'Extensions' / relative / 'manifest.json'
        self.enterContext(patch('preflight._CHROME_RESOURCE_COMPONENTS', resources))
        self.enterContext(patch('preflight._CHROME_PAYMENTS_COMPONENT', self.payment))
        self.enterContext(patch('preflight._system_timezone', return_value='America/Chicago'))
        self.enterContext(patch('preflight.mapped_ports',
                                return_value={profile: 3101 + index for index, profile in enumerate(profiles)}))
        self.enterContext(patch('preflight._chrome_processes', return_value=()))

    def default(self, profile):
        return self.root / 'data/profiles' / profile / 'Default'

    def write_prefs(self, profile, records, *, secure=False):
        name = 'Secure Preferences' if secure else 'Preferences'
        value = {'extensions': {'settings': records}, 'intl': {'selected_languages': 'zh-CN,zh,en'}}
        (self.default(profile) / name).write_text(json.dumps(value), encoding='utf-8')

    def test_only_selected_accounts_offline_accept_the_five_verified_components(self):
        denied = False, [f'extension_present:{ident}' for ident in sorted(self.records)]
        self.assertEqual(dispatch_check(self.root, self.peer), denied)
        with patch('preflight.collect_browser_fingerprint') as fingerprint:
            for profile in (self.profile, *self.additional_profiles):
                with self.subTest(profile=profile):
                    self.assertEqual(dispatch_check(self.root, profile), (True, []))
                    self.assertEqual(dispatch_check(self.root, profile, online=True), denied)
            fingerprint.assert_not_called()

    def test_unlisted_extensions_in_either_preferences_file_or_on_disk_are_blocked(self):
        extra, manifest = self.manifest('unlisted-component')
        record = dict(location=5, from_webstore=False, path='/opt/google/chrome/resources/other', manifest=manifest)
        denied = False, [f'extension_present:{extra}']
        self.write_prefs(self.profile, dict(self.records, **{extra: record}))
        self.assertEqual(dispatch_check(self.root, self.profile), denied)
        self.write_prefs(self.profile, self.records)
        self.write_prefs(self.profile, {extra: record}, secure=True)
        self.assertEqual(dispatch_check(self.root, self.profile), denied)
        (self.default(self.profile) / 'Secure Preferences').unlink()
        (self.default(self.profile) / 'Extensions' / extra).mkdir()
        self.assertEqual(dispatch_check(self.root, self.profile), denied)

    def test_empty_regular_preferences_entry_is_ignored_only_without_installation_evidence(self):
        ident, manifest = self.manifest('empty-updater-entry')
        self.write_prefs(self.profile, dict(self.records, **{ident: {}}))
        self.assertEqual(dispatch_check(self.root, self.profile), (True, []))

        expected = (False, [f'extension_present:{ident}'])
        folder = self.default(self.profile) / 'Extensions' / ident
        folder.mkdir()
        self.assertEqual(dispatch_check(self.root, self.profile), expected)
        folder.rmdir()

        self.write_prefs(self.profile, {ident: {'manifest': manifest, 'location': 1}}, secure=True)
        self.assertEqual(dispatch_check(self.root, self.profile), expected)

    def test_extension_ping_history_is_ignored_only_without_any_installation_evidence(self):
        extra, manifest = self.manifest('extension-ping-history')
        history = {'lastpingday': '13400000000000000'}
        records = dict(self.records, **{extra: history})
        self.write_prefs(self.profile, records)
        self.assertEqual(dispatch_check(self.root, self.profile), (True, []))
        self.write_prefs(self.profile, {extra: history}, secure=True)
        self.assertEqual(dispatch_check(self.root, self.profile), (True, []))

        expected = (False, [f'extension_present:{extra}'])
        self.write_prefs(self.profile, {extra: {'lastpingday': 'invalid'}}, secure=True)
        self.assertEqual(dispatch_check(self.root, self.profile), expected)
        self.write_prefs(self.profile, {extra: history}, secure=True)
        folder = self.default(self.profile) / 'Extensions' / extra
        folder.mkdir()
        self.assertEqual(dispatch_check(self.root, self.profile), expected)
        folder.rmdir()

        installed = dict(history, manifest=manifest, location=1)
        self.write_prefs(self.profile, {extra: installed}, secure=True)
        self.assertEqual(dispatch_check(self.root, self.profile), expected)
        self.write_prefs(self.profile, {extra: history}, secure=True)
        self.write_prefs(self.profile, dict(self.records, **{extra: installed}))
        self.assertEqual(dispatch_check(self.root, self.profile), expected)

    def test_extra_chrome_components_require_their_approved_profile_and_installation(self):
        docs, dictation = 'account-f8d81e309793', 'ub-chrome-f8f5d94158'
        for profile, label in ((docs, 'docs_offline'), (dictation, 'dictation_connector')):
            with self.subTest(profile=profile):
                ident, manifest = self.manifest(label)
                if profile == docs:
                    manifest['update_url'] = self.store_url
                    root = self.default(profile) / 'Extensions' / ident / '1.112.1_0'
                    entry = dict(location=6, from_webstore=True, was_installed_by_default=True,
                                 path=f'{ident}/1.112.1_0')
                else:
                    root = self.default(profile).parent / 'DictationConnector/20260928.1'
                    entry = dict(location=5, from_webstore=False, path=str(root))
                metadata = root / '_metadata/verified_contents.json'
                metadata.parent.mkdir(parents=True)
                metadata.write_text('[]', encoding='utf-8')
                (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                record = dict(entry, manifest=manifest)
                self.write_prefs(profile, dict(self.records, **{ident: record}))
                with patch('preflight._CHROME_' + label.upper(), ident):
                    self.assertEqual(dispatch_check(self.root, profile), (True, []))
                    self.write_prefs(self.profile, dict(self.records, **{ident: record}))
                    self.assertEqual(dispatch_check(self.root, self.profile),
                                     (False, [f'extension_present:{ident}']))
                    metadata.unlink()
                    self.assertEqual(dispatch_check(self.root, profile), (False, [f'extension_present:{ident}']))
    def test_recognized_id_still_requires_the_verified_installation_source(self):
        changes = {
            self.builtin: {'location': 4, 'path': '/tmp/lookalike', 'manifest': {'key': 'wrong'}},
            self.payment: {'from_webstore': False, 'path': f'{self.payment}/../1.0.0.6_0'},
        }
        for ident, fields in changes.items():
            for field, value in fields.items():
                with self.subTest(ident=ident, field=field):
                    records = copy.deepcopy(self.records)
                    records[ident][field] = value
                    self.write_prefs(self.profile, records)
                    self.assertEqual(dispatch_check(self.root, self.profile), (False, [f'extension_present:{ident}']))
        self.write_prefs(self.profile, self.records)
        (self.default(self.profile) / 'Extensions' / self.builtin).mkdir()
        self.assertEqual(dispatch_check(self.root, self.profile), (False, [f'extension_present:{self.builtin}']))

    def test_component_records_that_disagree_with_secure_preferences_or_disk_are_blocked(self):
        self.write_prefs(self.profile, {self.builtin: dict(self.records[self.builtin], location=4)}, secure=True)
        self.assertEqual(dispatch_check(self.root, self.profile), (False, [f'extension_present:{self.builtin}']))
        (self.default(self.profile) / 'Secure Preferences').unlink()
        self.payment_manifest.write_text(json.dumps(dict(self.records[self.payment]['manifest'], key='wrong')),
                                         encoding='utf-8')
        self.assertEqual(dispatch_check(self.root, self.profile), (False, [f'extension_present:{self.payment}']))
        self.payment_manifest.unlink()
        self.assertEqual(dispatch_check(self.root, self.profile), (False, [f'extension_present:{self.payment}']))


class OnlineDispatch(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.profile = 'browser-fixture'
        data = self.root / 'console-data'
        data.mkdir()
        (data / 'dispatch-locales.json').write_text(json.dumps({self.profile: {
            'languages': ['en-US'], 'timezone': 'Etc/UTC',
        }}), encoding='utf-8')
        self.policy = data / 'fingerprint-baseline.json'
        self.local = self.enterContext(patch('preflight.check_profile', return_value=(True, [])))
        browser_profile = self.root.resolve() / 'data/profiles' / self.profile
        port = chrome.debug_port(self.profile)
        self.port_arg = f'--remote-debugging-port={port}'
        args = ['chrome', f'--user-data-dir={browser_profile}', self.port_arg]
        self.processes = self.enterContext(patch('preflight._chrome_processes', return_value=[('42', args)]))
        self.tab = SimpleNamespace(url='https://flow.google.com/project/fixture', evaluate=AsyncMock())
        self.sample = dict(webdriver=False, ua='Chrome Fixture', screen=[1920, 1080, 1920, 1040, 24, 24, 1],
                           canvas='data:image/png;base64,fixture', webgl=['Test vendor', 'Test renderer'])
        self.tab.evaluate.return_value = self.sample
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[self.tab])])
        self.connect = AsyncMock(return_value=browser)
        playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=self.connect))
        manager = self.enterContext(patch('playwright.async_api.async_playwright'))
        manager.return_value.__aenter__.return_value = playwright

    def approve_current_fingerprint(self):
        digest, reason = collect_browser_fingerprint(self.root, self.profile)
        self.assertIsNone(reason)
        self.policy.write_text(json.dumps({self.profile: digest}), encoding='utf-8')

    def test_offline_and_invalid_baselines_skip_cdp(self):
        self.assertEqual(dispatch_check(self.root, self.profile), (True, []))
        self.assertEqual(dispatch_check(self.root, self.profile, online=True),
                         (False, ['dispatch_fingerprint_baseline_missing']))
        self.policy.write_text(json.dumps({self.profile: 'bad'}), encoding='utf-8')
        self.assertEqual(dispatch_check(self.root, self.profile, online=True),
                         (False, ['dispatch_fingerprint_baseline_invalid']))
        self.local.return_value = (False, ['profile_languages_mismatch'])
        self.assertEqual(dispatch_check(self.root, self.profile, online=True), self.local.return_value)
        self.connect.assert_not_awaited()

    def test_cdp_requires_exact_fingerprint_match(self):
        self.approve_current_fingerprint()
        self.assertEqual(dispatch_check(self.root, self.profile, online=True), (True, []))
        self.connect.assert_awaited_with(f'http://127.0.0.1:{chrome.debug_port(self.profile)}',
                                        timeout=4000, no_defaults=True)
        self.tab.evaluate.assert_awaited_with(FINGERPRINT_SIGNALS)
        for field in ('ua', 'screen', 'canvas', 'webgl'):
            with self.subTest(field=field):
                self.tab.evaluate.return_value = dict(self.sample, **{field: ['changed']})
                self.assertEqual(dispatch_check(self.root, self.profile, online=True),
                                 (False, ['dispatch_fingerprint_mismatch']))

    def test_webdriver_and_unavailable_signals_fail_closed(self):
        self.approve_current_fingerprint()
        for value in (True, None, 0):
            with self.subTest(value=value):
                self.tab.evaluate.return_value = dict(self.sample, webdriver=value)
                self.assertEqual(dispatch_check(self.root, self.profile, online=True),
                                 (False, ['dispatch_webdriver_detected']))
        self.tab.evaluate.return_value = dict(self.sample, webgl=None)
        self.assertEqual(dispatch_check(self.root, self.profile, online=True),
                         (False, ['dispatch_fingerprint_unavailable']))
        self.connect.side_effect = OSError('fixture connection refused')
        self.assertEqual(dispatch_check(self.root, self.profile, online=True),
                         (False, ['dispatch_browser_unavailable']))

    def test_the_debug_port_must_belong_to_the_requested_profile(self):
        self.approve_current_fingerprint()
        self.connect.reset_mock()
        self.processes.return_value = [('55', ['chrome', '--user-data-dir=/other', self.port_arg])]
        self.assertEqual(dispatch_check(self.root, self.profile, online=True),
                         (False, ['dispatch_browser_unavailable']))
        self.connect.assert_not_awaited()
