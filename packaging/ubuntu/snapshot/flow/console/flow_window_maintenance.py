"""Idle-only window housekeeping. No prompt, clipboard, submit or generation API."""
import asyncio
from contextlib import closing
import hashlib
from pathlib import Path
import random
import sqlite3
import sys
import time

H=Path.home();F=H/'.local/share/studiowright-evaluation';S=H/'.local/state/creative-unified-workbench'
shared=str(H/'.local/share/creative-unified-workbench')
if shared not in sys.path:sys.path.append(shared)
from flow_window_policy import Lifecycle,INTERVAL,safe_page,ensure_unique
from account_pacing import account_status

def gate(account,*,manual=False,now=None):
    now=time.time() if now is None else now;profile=account['profile']
    if account.get('flow_removed'):return 'account_removed'
    if (S/'maintenance').exists():return 'maintenance'
    saved=next((r for r in Lifecycle().status() if r['profile']==profile),None)
    if not manual and saved and now<(saved.get('next_due') or 0):return 'not_due'
    if account.get('state')!='verified':return 'account_not_verified'
    with closing(sqlite3.connect(F/'console-data/console.db')) as db:
        # Any queued user task has priority over idle interaction.
        if db.execute("SELECT count(*) FROM flow_task_execution_rows WHERE status IN ('queued','running')").fetchone()[0]:return 'tasks_have_priority'
        finished=db.execute('SELECT max(finished) FROM tasks WHERE profile=?',(profile,)).fetchone()[0] or 0
    with closing(sqlite3.connect(F/'data/studiowright.db')) as db:
        if db.execute("SELECT count(*) FROM jobs WHERE profile=? AND status IN ('queued','running')",(profile,)).fetchone()[0]:return 'account_busy'
    restriction=account_status([account],F/'data/studiowright.db',now)[0]
    if restriction['generation_state']=='cooling_down':return 'cooling_read_only'
    if restriction['risk_paused'] is not False:return 'account_paused'
    from task_coordinator import Coordinator
    state=Coordinator().window(profile)
    if state and state['state']!='idle':return 'window_not_idle'
    rows={r['profile']:r for r in Lifecycle().status()};row=rows.get(profile)
    if not row:return 'window_not_checked'
    if not row['unique_ok']:return 'window_not_unique'
    due=max(row.get('next_due') or 0,finished+INTERVAL)
    if not manual and now<due:return 'not_due'
    return None

async def safe_pointer(page,target,driver,*,checkpoint,rng):
    from input_backend import open_pointer
    from pointer_sequence import focus_path
    pointer=open_pointer(target,gui_environment=driver.env)
    await pointer.ensure_ready(timeout=2)
    await checkpoint()
    await page.evaluate('''()=>{window.__swIdleMove=null;window.__swIdleHandler=e=>{
      if(e.isTrusted)window.__swIdleMove={x:e.clientX,y:e.clientY};};
      window.addEventListener('pointermove',window.__swIdleHandler,true);}''')
    try:
        m=await page.evaluate('()=>({innerWidth,innerHeight,outerWidth,outerHeight,sw:screen.width,sh:screen.height})')
        frame=await driver.window_geometry(target)
        if frame['width']<m['innerWidth'] or frame['height']<m['innerHeight']:raise ValueError('window_geometry_unavailable')
        point=await page.evaluate('''()=>{
          const points=[];for(const fx of [.25,.5,.75,.9])for(const fy of [.16,.3,.55,.8]){
            const x=innerWidth*fx,y=innerHeight*fy,e=document.elementFromPoint(x,y);
            if(e&&!e.closest('button,a,input,textarea,select,[contenteditable=true],[role],canvas,video')&&
               (['BODY','MAIN','SECTION'].includes(e.tagName)||(e.tagName==='DIV'&&!e.children.length&&!e.textContent.trim())))points.push({x,y});
          }return points[0]||null;}''')
        if not point:raise ValueError('keepalive_safe_area_unconfirmed')
        x=point['x'];y=point['y']
        sx=frame['x']+max(0,(frame['width']-m['innerWidth'])/2)+x
        sy=frame['y']+max(0,frame['height']-m['innerHeight'])+y
        if not(0<=sx<m['sw'] and 0<=sy<m['sh']):raise ValueError('window_geometry_unavailable')
        await pointer.move(100000,100000);px,py=m['sw']-1,m['sh']-1
        for nx,ny in focus_path(sx,sy,m['sw'],m['sh'],rng):
            await checkpoint();await pointer.move(nx-px,ny-py);px,py=nx,ny;await asyncio.sleep(.025)
        await checkpoint()
        hit=await page.evaluate('''()=>{const p=window.__swIdleMove;const e=p&&document.elementFromPoint(p.x,p.y);
          if(!e||e.closest('button,a,input,textarea,select,[contenteditable=true],[role=button],[role=menuitem],[role=option],canvas,video'))return false;
          return ['DIV','MAIN','SECTION','H1','H2','P','SPAN','BODY'].includes(e.tagName);}''')
        if not hit:raise ValueError('keepalive_safe_area_unconfirmed')
        direction=rng.choice([-1,1]);await pointer.scroll(direction);await asyncio.sleep(.08)
        await checkpoint();await pointer.scroll(-direction);await asyncio.sleep(.08)
        # Click only if the trusted pointer is still over a non-interactive area.
        clickable=await page.evaluate('''()=>{const p=window.__swIdleMove;const e=p&&document.elementFromPoint(p.x,p.y);
          return !!e&&!e.closest('button,a,input,textarea,select,[contenteditable=true],[role],canvas,video');}''')
        clicked=False
        if clickable:
            await checkpoint();await pointer.click();clicked=True
        return {'mouse_movement':True,'scroll':[direction,-direction],'safe_blank_click':clicked}
    finally:
        await page.evaluate('()=>{window.removeEventListener("pointermove",window.__swIdleHandler,true);delete window.__swIdleHandler;delete window.__swIdleMove;}')

async def run_one(account,*,manual=False,console=None,navigation=None):
    from filelock import FileLock,Timeout
    from studiowright import paths
    from playwright.async_api import async_playwright
    from foreground_lease import ForegroundLease
    from flow_task_window import registration,find_flow_page
    from flow_browser_state import focus_call,gui_env
    from wayland_desktop_driver import WaylandDesktopDriver
    from browser_rendering import RenderingSession
    from adapter import READ_STATE
    profile=account['profile'];lifecycle=Lifecycle();reason=gate(account,manual=manual)
    if reason:
        if reason!='not_due':lifecycle.attempt(profile,reason)
        return {'profile':profile,'outcome':reason or 'skipped','success':False}
    if console and console.remote.for_profile(profile):
        lifecycle.attempt(profile,'manual_login_open');return {'profile':profile,'outcome':'manual_login_open','success':False}
    lock=FileLock(str(paths.locks_dir()/(profile+'.lock')),thread_local=False)
    try:lock.acquire(timeout=0)
    except Timeout:return {'profile':profile,'outcome':'account_busy','success':False}
    lease=ForegroundLease();original=None;page=None;session=None;driver=None;target=None;actions={}
    rng=random.SystemRandom()
    try:
        if not await lease.try_acquire():return {'profile':profile,'outcome':'foreground_busy','success':False}
        reason=gate(account,manual=manual)
        if reason:raise ValueError(reason)
        key=hashlib.sha256(profile.encode()).hexdigest();reg=registration(key)
        async with async_playwright() as pw:
            browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:'+str(reg['port']))
            if len(browser.contexts)!=1:raise ValueError('browser_context_ambiguous')
            session=RenderingSession(browser,browser.contexts[0],reg['port']);session._profile=profile
            try:
                page=await ensure_unique(session,profile);original=page.url
                if '/project/' not in original:raise ValueError('keepalive_project_not_ready')
                _,target=await find_flow_page(context=session.context,account_key=key,desired_url=original,layout_call=focus_call,preferred_page=page)
                driver=WaylandDesktopDriver(gui_env())
                if not await driver.activate_and_confirm(target):raise ValueError('desktop_focus_unconfirmed')
                async def checkpoint():
                    reason=gate(account,manual=manual)
                    if reason:raise ValueError(reason)
                    await safe_page(session,page)
                    identity=await page.evaluate(READ_STATE)
                    if not identity.get('signed_in') or identity.get('emails')!=[account['email'].lower()]:raise ValueError('keepalive_account_mismatch')
                    if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):raise ValueError('desktop_focus_unconfirmed')
                await checkpoint()
                actions=await safe_pointer(page,target,driver,checkpoint=checkpoint,rng=rng)
                visit=rng.randrange(2)==1 if navigation is None else bool(navigation)
                if visit:
                    await checkpoint();await page.goto('https://flow.google.com/',wait_until='domcontentloaded',timeout=15000)
                    await checkpoint();await page.goto(original,wait_until='domcontentloaded',timeout=15000)
                    # DOM ready can precede the actual project editor. Observe
                    # readiness without clicking, editing or dismissing dialogs.
                    from studiowright.flow import selectors
                    await page.locator(selectors.COMPOSER).first.wait_for(state='visible',timeout=15000)
                    await checkpoint();actions['project_home_roundtrip']=True
                    actions['project_editor_ready']=True
                lifecycle.event(profile,'keepalive_actions',**actions)
            finally:
                # Return only through known, still authenticated Flow pages. Never
                # navigate away from a verification page or overwrite a new draft.
                if page and original and page.url!=original:
                    from flow_window_policy import trusted_flow
                    if trusted_flow(page.url):
                        await safe_page(session,page)
                        await page.goto(original,wait_until='domcontentloaded',timeout=15000)
                        from studiowright.flow import selectors
                        await page.locator(selectors.COMPOSER).first.wait_for(state='visible',timeout=15000)
        lifecycle.attempt(profile,'completed',True)
        return {'profile':profile,'outcome':'completed','success':True,'actions':actions}
    except Exception as exc:
        reason=str(exc);reason=reason if reason.replace('_','').isalnum() and len(reason)<100 else type(exc).__name__
        lifecycle.attempt(profile,reason)
        return {'profile':profile,'outcome':reason,'success':False}
    finally:
        if lease.active:await lease.release()
        lock.release()

async def worker(console):
    lifecycle=Lifecycle()
    while True:
        await asyncio.sleep(30)
        for account in console.store.accounts():
            try:await run_one(account,console=console)
            except Exception:
                lifecycle.attempt(account['profile'],'maintenance_check_failed')
