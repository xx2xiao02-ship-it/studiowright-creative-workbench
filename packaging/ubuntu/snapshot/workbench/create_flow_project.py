"""Create one project in an already-running, identity-checked Flow profile."""
import asyncio
import json
import re
import sys
import uuid
from urllib.parse import urlsplit

from filelock import FileLock, Timeout
from playwright.async_api import async_playwright
import shared_session_controller as controller

provider_call_started = False


async def readiness_project(context, email, title):
    from adapter import READ_STATE, flow_home_projects

    home = [page for page in context.pages
            if urlsplit(page.url)._replace(query='', fragment='').geturl() == 'https://flow.google.com/']
    if len(home) != 1:
        raise ValueError('Flow home unavailable')
    page = home[0]
    await page.get_by_role('button', name=re.compile(r'New project|新建项目|新建專案|建立專案', re.I)).first.wait_for(
        state='visible', timeout=10000)
    for attempt in range(6):
        state = await page.evaluate(READ_STATE)
        if not state['signed_in'] or state['emails'] != [email]:
            raise ValueError('identity unavailable')
        projects = flow_home_projects(state)
        if projects:
            matching = [project for project in projects if project['title'].casefold() == title.casefold()]
            if len(matching) != 1:
                raise ValueError('existing Flow project needs registration')
            return matching[0]['id']
        if attempt < 5:
            await asyncio.sleep(1)
    if any(urlsplit(tab.url).path.startswith('/project/') for tab in context.pages):
        raise ValueError('existing Flow project needs registration')
    return None


async def main(profile, readiness=False):
    global provider_call_started
    if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile):
        raise ValueError('invalid profile')
    chrome, paths = controller.flow_runtime()
    from adapter import READ_STATE
    from browser_rendering import RenderingStudio as Studio
    state = controller.state()
    accounts = controller.eligible_accounts(state['accounts'])
    matched = [a for a in accounts if a.get('profile') == profile]
    running = controller.running_accounts(state['accounts'], chrome, paths)
    if readiness and len(matched) == 1 and profile not in running:
        from activation_browser import main as open_flow
        try:
            await asyncio.to_thread(open_flow, controller.key(profile), flow_only_start=True)
        except ValueError as exc:
            if str(exc) != 'flow_browser_already_running':
                raise
        running = controller.running_accounts(state['accounts'], chrome, paths)
    if len(matched) != 1 or profile not in running:
        raise ValueError('account not ready')
    email = matched[0]['email'].strip().lower()
    if email.count('@') != 1 or not all(email.split('@')):
        raise ValueError('account email unavailable')
    title = 'FLOW+' + email.split('@', 1)[0]
    guard = FileLock(str(paths.locks_dir() / (profile + '.lock')))
    try:
        guard.acquire(timeout=0)
    except Timeout:
        raise ValueError('account busy') from None
    try:
        if readiness:
            current = controller.state()
            rows = [row for row in current['accounts'] if row.get('profile') == profile]
            if (len(rows) != 1 or rows[0].get('state') != 'verified' or rows[0].get('flow_removed')
                    or (rows[0].get('email') or '').lower() != email or rows[0].get('risk_paused') is not False
                    or rows[0].get('generation_state') != 'not_cooling_down'
                    or any(t.get('profile') == profile and t.get('status') in ('queued', 'running') for t in current['tasks'])
                    or any(s.get('profile') == profile for s in current.get('remote_logins', []))):
                raise ValueError('account not ready')
            if any(project.get('profile') == profile for project in current['projects']):
                raise ValueError('project already registered')
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(
                'http://127.0.0.1:' + str(running[profile]), timeout=4000, no_defaults=True)
            contexts = [c for c in browser.contexts if any(p.url.startswith('https://flow.google.com/') for p in c.pages)]
            if len(contexts) != 1:
                raise ValueError('identity unavailable')
            states = [await p.evaluate(READ_STATE) for p in contexts[0].pages if p.url.startswith('https://flow.google.com/')]
            if not any(isinstance(s, dict) and s.get('signed_in') and s.get('emails') == [email] for s in states):
                raise ValueError('identity unavailable')
            if readiness:
                existing = await readiness_project(contexts[0], email, title)
                if existing:
                    return {'project': existing}
            # Existing Studio owns the actual provider operation and records its own project metadata.
            studio = Studio(profile)
            studio.configure_task('project-' + uuid.uuid4().hex)
            async with studio:
                provider_call_started = True
                project = await studio.create_project(title)
            return {'project': project}
    finally:
        guard.release()


if __name__ == '__main__':
    try:
        value = json.load(sys.stdin)
        print(json.dumps(asyncio.run(main(value.get('profile'), value.get('readiness') is True))))
    except Exception:
        print(json.dumps({'result': 'unknown' if provider_call_started else 'not_started'}))
