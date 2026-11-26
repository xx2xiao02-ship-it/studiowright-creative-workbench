const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'packaging', 'ubuntu', 'snapshot', 'workbench', 'app.js'), 'utf8');
function load(name, context) {
  context.viewedFlowScreenshots ??= new Set();
  const match = source.match(new RegExp('^(?:async )?function ' + name + '\\([^]*?^}', 'm'));
  assert.ok(match, name + ' should exist');
  vm.runInContext(match[0], context);
}

test('automatic Gemini chats only offer models with an eligible account', () => {
  const flash = {id:'gemini-3.8-flash-medium', family:'gemini-3.8-flash', effort:'medium'};
  const hydrogen = {id:'gemini-4-hydrogen-medium', family:'gemini-4-hydrogen', effort:'medium'};
  const neon = {id:'gemini-4-neon-high', family:'gemini-4-neon', effort:'high'};
  const accounts = [
    {key:'flow-account-ready', auto:true, gemini_model_options:[flash, hydrogen]},
    {key:'flow-account-duplicate', auto:true, gemini_model_options:[hydrogen]},
    {key:'flow-account-paused', auto:false, gemini_model_options:[neon]},
    {key:'flow-account-limited', auto:true, restriction:'quota_exhausted', gemini_model_options:[neon]}
  ];
  const context = vm.createContext({accounts, flashChatChoices:[flash],
    geminiAccountAvailability: item => ({auto:item.auto}),
    officialAccount: item => ({data:{advanced_quota_restriction:item.restriction}})});
  load('geminiChatModelChoices', context);
  const options = key => JSON.stringify(vm.runInContext(`geminiChatModelChoices('${key}', accounts).map(option => option.id)`, context));
  assert.equal(options('auto'), JSON.stringify([flash.id, hydrogen.id]));
  assert.equal(options('ready'), JSON.stringify([flash.id, hydrogen.id]));
  assert.equal(options('limited'), JSON.stringify([flash.id]));
});

test('overview distribution applies segment widths through the browser style API', () => {
  let segments = [];
  const target = {
    innerHTML:'',
    querySelectorAll(selector) {
      assert.equal(selector, '[data-chart-width]');
      segments = [...this.innerHTML.matchAll(/data-chart-width="([^"]+)"/g)].map(([, width]) => ({
        dataset:{chartWidth:width}, style:{}
      }));
      return segments;
    }
  };
  const parts = [
    {label:'已完成', color:'chart-green', value:3},
    {label:'失败', color:'chart-rust', value:1},
    {label:'其他状态', color:'chart-slate', value:0}
  ];
  const context = vm.createContext({target, parts});
  for (const name of ['esc', 'overviewDistribution']) load(name, context);
  const render = () => vm.runInContext("overviewDistribution(target, '任务分布', '', parts)", context);
  render();
  assert.doesNotMatch(target.innerHTML, /\sstyle=/);
  assert.deepEqual(segments.map(segment => segment.style.width), ['75.00%', '25.00%', '0.00%']);

  parts[0].value = null;
  render();
  assert.equal(segments.length, 0);
  assert.match(target.innerHTML, /暂时无法读取数据/);
});

test('Flow page warning shows its own reason and screenshot without a generation task', () => {
  const incident = {id:'0123456789abcdef0123456789abcdef', phase:'login', has_screenshot:true};
  const context = vm.createContext({incident});
  for (const name of ['esc', 'flowLoginNotice', 'flowAccountReason', 'flowAccountNoticeMarkup']) load(name, context);
  const code = 'flowAccountNoticeMarkup(flowAccountReason({flow_page_incident:incident}, {}, {}, null), incident)';
  const warning = vm.runInContext(code, context);
  assert.match(warning, /核对登录时检测到异常活动/);
  assert.match(warning, /\/api\/flow-warning-screenshots\/0123456789abcdef0123456789abcdef\.png/);
  assert.match(warning, /<form method="post" action="\/api\/flow-warning-screenshots\/0123456789abcdef0123456789abcdef\/view" target="_blank"/);
  assert.equal((warning.match(/<button\b/g) || []).length, 1);
  assert.doesNotMatch(warning, /已阅，销毁截图/);
  incident.has_screenshot = false;
  assert.match(vm.runInContext(code, context), /本次未保存截图/);
  incident.destroyed = true;
  const destroyed = vm.runInContext(code, context);
  assert.match(destroyed, /等待人工核查/);
  assert.doesNotMatch(destroyed, /<img|<form|截图/);
});

test('opening the original removes the thumbnail immediately and survives a stale refresh', async () => {
  const id = '123456789abc';
  const button = {disabled:false};
  let removed = 0;
  let drawn = 0;
  let status = 503;
  const canvas = {hidden:true, getContext:()=>({drawImage(){drawn++;}})};
  const message = {hidden:false};
  const viewer = {open:true, querySelector:selector=>selector === 'canvas' ? canvas : message};
  const localStorage = {setItem(key, value){ this[key]=value; }};
  const form = {dataset:{flowScreenshot:id}, action:'/api/flow-warning-screenshots/'+id+'/view',
    querySelector:()=>button, remove:()=>{removed++;}};
  const event = {target:{closest:()=>form}, preventDefault(){}};
  const context = vm.createContext({id,event,
    URLSearchParams, AbortSignal, localStorage, viewedFlowScreenshotsKey:'flowViewedScreenshots',
    showFlowScreenshotViewer:()=>viewer,
    createImageBitmap:async ()=>({width:192,height:100,close(){}}),
    accountActionsInFlight:new Set(),
    fetch:async (path, options)=>{
      assert.equal(path, form.action);
      assert.equal(options.body.toString(), 'view=original');
      return {status, ok:status===200, blob:async()=>({type:'image/png'})};
    }
  });
  for (const name of ['esc','markFlowScreenshotViewed','flowAccountNoticeMarkup','openFlowScreenshot']) load(name, context);
  await vm.runInContext('openFlowScreenshot(event)', context);
  assert.equal(removed, 0);
  const render = () => vm.runInContext("flowAccountNoticeMarkup({label:'Flow 失败原因', text:'生成失败'}, {id})", context);
  assert.match(render(), /<img/);
  status = 200;
  await vm.runInContext('openFlowScreenshot(event)', context);
  assert.equal(removed, 1);
  assert.equal(drawn, 1);
  assert.equal(canvas.hidden, false);
  assert.deepEqual(JSON.parse(localStorage.flowViewedScreenshots), [id]);
  assert.match(render(), /生成失败/);
  assert.doesNotMatch(render(), /<form|<img|截图/);
});

test('Flow preflight mismatch explains why the account is blocked without reusing an old screenshot', () => {
  const context = vm.createContext({});
  for (const name of ['esc', 'flowLoginNotice', 'flowAccountReason', 'flowAccountNoticeMarkup']) load(name, context);
  const message = vm.runInContext(`
    const reason = flowAccountReason({flow_preflight_reasons:['profile_languages_mismatch', 'system_timezone_mismatch']}, {}, {}, null);
    flowAccountNoticeMarkup(reason, {id:'123456789abc'});
  `, context);
  assert.match(message, /浏览器语言与账号设置不一致/);
  assert.match(message, /Ubuntu 时区与账号设置不一致/);
  assert.doesNotMatch(message, /warning-screenshots/);
});

test('Flow reports a verified Google challenge before stale balances and clears it when the editor is ready', () => {
  const account = {profile:'a',flow_login:'verified',flow_health:'attention',flow_balance_fresh:false,
    flow_balance_error:'未找到该账号登记的 Flow 固定任务页',flow_risk_known:true,flow_pacing_known:true};
  const snapshot = {flow_state:{available:true}, real_accounts:{available:true,items:[account]},
    flow_coordination:{desktop_keyboard_ready:true,window_lifecycle_available:true,window_bindings_available:true,
      windows:[{profile:'a',state:'idle'}],window_lifecycle:[{profile:'a',unique_ok:1}],
      window_bindings:[{profile:'a',binding_state:'verified'}]}};
  const checks = {accounts:{a:'verification_required'}};
  const context = vm.createContext({account,snapshot,checks,flowReadinessObservation:null,flowWindowIssue:()=>null});
  for (const name of ['flowLoginNotice','accountIssues','flowGovernance','auditFlowAccounts','flowAccountReason','flowOccupiedSlots']) load(name,context);
  const row = () => vm.runInContext('auditFlowAccounts(snapshot,{available:true,active:0,maximum:1},checks,1)[0]',context);
  assert.equal(row().issues[0].title,'需要用户登录');
  assert.equal(row().status,'blocked');
  assert.match(vm.runInContext("flowAccountReason(account,snapshot,{},'verification_required').text",context), /Ubuntu 浏览器完成 Google 验证/);
  assert.equal(vm.runInContext("flowAccountReason(account,snapshot,{},'login').label",context), '需要用户登录');
  assert.match(vm.runInContext("flowAccountReason(account,snapshot,{},'login').text",context), /重新登录 Google.*再点「Flow 一键就绪」/);
  assert.equal(vm.runInContext("flowAccountReason(account,snapshot,{},'flow_landing').label",context), '需要用户登录');
  assert.equal(vm.runInContext("flowLoginNotice('load_failed')",context),null);
  for (const loginPage of ['login', 'flow_landing']) {
    checks.accounts.a = loginPage;
    assert.equal(row().issues[0].title,'需要用户登录');
    assert.equal(row().status,'blocked');
  }
  checks.accounts.a = 'ok';
  assert.equal(row().issues.length,1);
  assert.equal(row().issues[0].title,'任务页已就绪，积分待核对');
  assert.equal(row().status,'blocked');
  account.flow_balance_fresh = true;
  account.flow_balance = 1050;
  account.flow_health = 'ready';
  assert.equal(row().status,'ready');
});

test('Flow card gives one direct next step for a missing binding or stale balance', () => {
  const account = {profile:'a', flow_login:'verified', flow_balance_fresh:false};
  const row = {status:'blocked', issues:[{key:'flow_binding',title:'任务窗口绑定待核对'},
    {key:'flow_balance',title:'未找到该账号登记的 Flow 固定任务页，保留上次余额'}]};
  const context = vm.createContext({account, row});
  for (const name of ['esc','flowLoginNotice','flowAccountReason','flowAccountNoticeMarkup']) load(name,context);
  for (const page of ['binding','project',null]) {
    context.page = page;
    const reason = vm.runInContext('flowAccountReason(account,{},row,page)',context);
    assert.equal(reason.showOnCard,true);
    assert.equal(reason.label,'请打开原 Flow 项目');
    assert.match(reason.text,/如果 Google 显示登录页.*重新登录.*点「Flow 一键就绪」/);
    assert.doesNotMatch(reason.text,/任务窗口绑定待核对|保留上次余额/);
  }
  const balance = vm.runInContext("flowAccountReason(account,{},row,'ok')",context);
  assert.equal(balance.label,'请重新读取积分');
  assert.doesNotMatch(balance.text,/登录/);
  account.flow_risk_paused=true;
  assert.notEqual(vm.runInContext("flowAccountReason(account,{},row,'binding').showOnCard",context),true);
});

test('overview only averages the fresh shared quotas of the default HY pool', () => {
  const now = 1800000000;
  const flow = {};
  const shared = {};
  const breakdown = {};
  const target = {
    querySelector: selector => selector === 'details[open]' ? null : {addEventListener() {}},
    querySelectorAll: () => [],
    contains: () => false,
    setAttribute() {}
  };
  const context = vm.createContext({
    $: id => ({geminiQuotaOverview:target, flowSuccessOverview:flow, geminiSharedQuota:shared, geminiQuotaBreakdown:breakdown})[id],
    document: {},
    state: {},
    hydrogenMedium: 'gemini-4-hydrogen-medium',
    Date: {now: () => now * 1000},
    officialQuotaReadFailed: false,
    officialQuotaRequest: null,
    officialQuotaData: {
      advanced_pool: {
        total_accounts: 3,
        accounts: {
          first: {status:'ok', checked_at:now-30, models:['gemini-4-hydrogen-medium','gemini-4-neon-high'],
            five_hour:{remaining_percent:100}, weekly:{remaining_percent:60}},
          second: {status:'ok', checked_at:now-60, models:['gemini-4-neon-high'],
            five_hour:{remaining_percent:75}},
          stale: {status:'ok', checked_at:now-601, models:['gemini-4-hydrogen-medium'],
            five_hour:{remaining_percent:0}, weekly:{remaining_percent:100}}
        }
      }
    }
  });
  for (const name of ['esc', 'savedQuotaValue', 'savedQuotaSummary', 'renderOfficialQuota']) load(name, context);
  vm.runInContext('renderOfficialQuota()', context);
  assert.match(flow.innerHTML, /FLOW · 图片 \/ 视频成功率/);
  assert.equal((shared.innerHTML.match(/class="quota-shared-card"/g) || []).length, 1);
  assert.match(shared.innerHTML, /HY 默认池额度[^]*?Gemini Models 共享/);
  assert.match(shared.innerHTML, /5 小时额度<\/span><strong>100%[^]*?aria-valuenow="100"[^]*?>1 \/ 2 个账号已读/);
  assert.match(shared.innerHTML, /每周额度<\/span><strong>60%[^]*?aria-valuenow="60"[^]*?>1 \/ 2 个账号已读/);
  assert.doesNotMatch(shared.innerHTML, /87\.50%|175%|120%/);
  assert.match(breakdown.innerHTML, /账号与普通 Gemini 额度明细/);
});

test('Gemini account avatars distinguish Neon and Carbon independently', () => {
  const context = vm.createContext({});
  load('esc', context);
  load('accountAvatarMarkup', context);
  const render = families => vm.runInContext(`accountAvatarMarkup('account', ${JSON.stringify(families.map(family => ({family})))})`, context);
  const neon = 'gemini-4-neon';
  const carbon = 'gemini-4-carbon';
  assert.match(render([neon]), /class="account-avatar has-gemini-neon"[^>]+aria-label="可用模型：Gemini 4 Neon"/);
  assert.match(render([carbon]), /class="account-avatar has-gemini-carbon"[^>]+aria-label="可用模型：Gemini 4 Carbon"/);
  assert.match(render([carbon, neon]), /class="account-avatar has-gemini-neon has-gemini-carbon"[^>]+aria-label="可用模型：Gemini 4 Neon、Gemini 4 Carbon"/);
  for (const families of [[], ['gemini-3.8-flash'], ['gemini-4-hydrogen']]) {
    assert.match(render(families), /class="account-avatar" aria-hidden="true"/);
  }
});

test('usage bars preserve zero, exact usage, and unknown values', () => {
  const context = vm.createContext({Intl});
  load('esc', context);
  load('accountUsageBar', context);
  const render = (value) => vm.runInContext(`accountUsageBar('5 小时', ${value}, false)`, context);
  assert.match(render('0'), /aria-valuenow="0"/);
  assert.match(render('47.5'), /data-usage-percent="47.5"/);
  assert.match(render('100'), /aria-valuenow="100"/);
  for (const bad of ['null', 'undefined', '-1', '101', 'NaN']) {
    const result = render(bad);
    assert.match(result, /用量未知/);
    assert.doesNotMatch(result, /data-usage-percent|aria-valuenow/);
  }
});

test('Flow refresh cooldown belongs to the account that was already rechecked', async () => {
  const first = {profile:'first', flow_login:'verified'};
  const second = {profile:'second', flow_login:'verified'};
  const context = vm.createContext({
    state:{snapshotFresh:true,snapshot:{real_accounts:{items:[first,second]}}},
    flowReadinessObservation:{}, rememberFlowReadiness:()=>{},
    autoFlowRecovery:null, autoFlowRecoveryProfiles:new Set(),
    autoFlowRecoveryAfter:new Map([['first',Date.now()+300000]]),
    postJson:async () => ({accounts:{first:'overlay',second:'ok'}}),
  });
  load('autoRecoverFlowPages',context);
  vm.runInContext("autoRecoverFlowPages({accounts:{first:'overlay',second:'overlay'}})",context);
  assert.deepEqual(Array.from(context.autoFlowRecoveryProfiles), ['second']);
  await context.autoFlowRecovery;
  vm.runInContext("autoRecoverFlowPages({accounts:{first:'login',second:'verification_required'}})",context);
  assert.equal(context.autoFlowRecovery,null);
});

test('compact Gemini quota keeps recovery dates in hover details', () => {
  const now = 1800000000;
  const context = vm.createContext({
    Date: {now: () => now * 1000}, dateTime: value => '日期' + value,
    officialQuotaReadFailed: false, governanceAudits: {},
    officialQuotaData: {accounts: {sample: {status: 'ok', checked_at: now - 60,
      five_hour: {remaining_percent: 72, reset_at: now + 300},
      weekly: {remaining_percent: 60, reset_at: now - 60}}}},
  });
  for (const name of ['esc', 'savedQuotaValue', 'accountOfficialQuotaMarkup']) load(name, context);
  const markup = () => vm.runInContext("accountOfficialQuotaMarkup('sample').html", context);
  assert.match(markup(), /title="5 小时额度 · 恢复 日期1800000300"/);
  assert.match(markup(), /title="每周额度 · 恢复时间待官方更新"/);
  assert.doesNotMatch(markup(), /account-quota-reset/);
  assert.doesNotMatch(markup(), /日期1799999940/);
  context.officialQuotaData.accounts.sample.checked_at = now - 601;
  assert.match(markup(), /title="5 小时额度 · 恢复 日期1800000300（上次读取）"/);
});

test('a missing Gemini quota is neutral, and a confirmed zero stays visible', () => {
  const now = 1800000000;
  const context = vm.createContext({
    Date: {now: () => now * 1000}, dateTime: value => '日期' + value, officialQuotaReadFailed: false,
    officialQuotaData: {accounts: {}},
  });
  for (const name of ['esc', 'savedQuotaValue', 'accountOfficialQuotaMarkup']) load(name, context);
  const show = row => {
    context.officialQuotaData.accounts.account = row;
    return vm.runInContext("accountOfficialQuotaMarkup('account')", context);
  };
  for (const status of ['not_read', 'unavailable', 'network_error', 'eligibility_failed']) {
    const {message, html} = show({status});
    assert.equal(message, '额度暂未获取');
    assert.doesNotMatch(html, /aria-valuenow=/);
  }
  const partial = show({status: 'ok', checked_at: now, five_hour: {remaining_percent: 0}});
  assert.equal(partial.message, '部分额度待读取');
  assert.match(partial.html, /aria-valuenow="0"/);
  assert.match(partial.html, /每周<\/span><strong>待读取/);
  const stale = show({status: 'ok', checked_at: now - 601, weekly: {remaining_percent: 100}});
  assert.match(stale.message, /上次读取.*部分额度待读取/);
  assert.match(stale.html, /5 小时<\/span><strong>待读取/);
  assert.match(stale.html, /每周<\/span><strong>100%/);
  assert.match(show({status: 'authorization_required'}).message, /重新授权/);
});

test('an open account page re-reads saved Gemini quota after its initial load', async () => {
  let now = 1800000000000;
  let quotaReads = 0;
  const noop = () => {};
  const context = vm.createContext({
    Date: {now: () => now}, viewReads: new Map(),
    officialQuotaData: {refreshing: false}, officialQuotaReadAt: now, OFFICIAL_QUOTA_SYNC_MS: 15000,
    refreshOfficialQuota: () => { quotaReads++; },
    refreshChannelStatus: noop, refreshFlowReadinessStatus: noop, refreshGovernanceCases: noop,
    refreshGeminiActivity: noop, refreshActivation: noop,
    activationScheduleSaving: true, activationPollSaving: true,
  });
  load('syncVisiblePage', context);
  const sync = async () => {
    vm.runInContext("syncVisiblePage('accounts', true)", context);
    await Promise.all(context.viewReads.values());
  };
  await sync();
  assert.equal(quotaReads, 0);
  now += context.OFFICIAL_QUOTA_SYNC_MS;
  await sync();
  assert.equal(quotaReads, 1);
});

test('quota refresh keeps the card tooltip in sync with partial and complete data', () => {
  const now = 1800000000;
  const service = {
    querySelector: () => ({textContent:'可调用'}),
    setAttribute(name, value) { this[name] = value; }
  };
  const panel = {dataset:{accountQuota:'account'}, closest:()=>service};
  const row = {status:'ok', checked_at:now, weekly:{remaining_percent:100}};
  const context = vm.createContext({
    Date:{now:()=>now * 1000}, document:{querySelectorAll:()=>[panel]},
    officialQuotaReadFailed:false, officialQuotaData:{accounts:{account:row}},
  });
  for (const name of ['esc','savedQuotaValue','accountOfficialQuotaMarkup','renderAccountQuotas']) load(name, context);
  vm.runInContext('renderAccountQuotas()', context);
  assert.match(service.title, /部分额度待读取/);
  assert.equal(service['aria-label'], service.title);
  row.five_hour = {remaining_percent:0};
  vm.runInContext('renderAccountQuotas()', context);
  assert.equal(service.title, 'Gemini：可调用');
  assert.equal(service['aria-label'], service.title);
  assert.match(panel.innerHTML, /aria-valuenow="0"/);
});

test('Gemini readiness requests quota recovery and still finishes when quota is unavailable', async () => {
  const snapshot = {real_accounts: {available: true, items: []}};
  const channel = {available: true};
  const requests = [];
  const quotaReads = [];
  const context = vm.createContext({
    governanceAudits: {}, state: {view: 'accounts'}, officialQuotaData: null, channelReadFailed: false,
    async getJson(path) { return path === '/api/snapshot' ? snapshot : channel; },
    async postJson(path) {
      requests.push(path);
      return path === '/api/gemini-readiness' ? {readiness_probes: []} : {state: 'completed', completed: 0};
    },
    async refreshOfficialQuota(force) { quotaReads.push(force); return null; },
    auditGeminiAccounts: () => [{status: 'ready'}],
    renderGovernance() {}, renderAccountQuotas() {}, refreshGovernanceCases: async () => {},
  });
  load('startGovernanceAudit', context);
  await vm.runInContext("startGovernanceAudit('gemini')", context);
  assert.deepEqual(quotaReads, [true]);
  assert.deepEqual(requests, ['/api/gemini-readiness', '/api/official-reauth/activate']);
  const audit = context.governanceAudits.gemini;
  assert.equal(audit.rows[0].status, 'ready');
  assert.equal(audit.error, '');
  assert.equal(audit.running, false);
  assert.ok(audit.checkedAt);
});

test('Gemini readiness panel opens and its recheck does not ask Flow to run', async () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'packaging', 'ubuntu', 'snapshot', 'workbench', 'index.html'), 'utf8');
  const panel = html.match(/<section id="governanceDialog"[^>]+>[\s\S]*?<\/section>/)?.[0];
  assert.ok(panel);
  for (const id of ['governanceTitle', 'governanceStatus', 'governanceItems', 'governanceCheck', 'governanceRecords']) {
    assert.ok(panel.includes(`id="${id}"`));
  }
  const calls = [];
  const governanceDialog = {hidden: true, scrollIntoView() { calls.push('scroll'); }};
  const context = vm.createContext({
    governanceService: 'gemini', governanceError: '', governanceDialog, governanceAudits: {},
    renderGovernance() { calls.push('render'); },
    async startGovernanceAudit(service) { calls.push(service); },
    async refreshGovernanceCases() {},
    async postJson(url) { calls.push(url); },
    button: {id: 'governanceCheck', dataset: {}},
  });
  load('openGovernance', context);
  load('handleGovernanceClick', context);
  await vm.runInContext("handleGovernanceClick({target: {closest: () => button}})", context);
  assert.equal(governanceDialog.hidden, false);
  assert.deepEqual(calls, ['scroll', 'render', 'gemini']);
});

test('Gemini readiness rechecks after login and keeps the authorization result visible', async () => {
  const snapshot = {real_accounts: {available: true, items: []}};
  const channel = {available: true, accounts: []};
  const requests = [];
  const renders = [];
  const context = vm.createContext({
    governanceAudits: {}, state: {view: 'accounts'}, officialQuotaData: null, channelReadFailed: false,
    setTimeout(callback) { callback(); },
    async getJson(path) {
      if (path === '/api/snapshot') return snapshot;
      if (path === '/api/official-reauth/activate') return {state: 'completed', completed: 1, total: 1};
      return channel;
    },
    async postJson(path) {
      requests.push(path);
      if (path === '/api/official-reauth/activate') return {state: 'waiting', email: 'a@example.test', completed: 0, total: 1};
      if (path === '/api/gemini-readiness?scope=recoverable') {
        channel.accounts = [{account_key: 'account', latest_probe: {request_id: 'followup', status: 'completed'}}];
        return {readiness_probes: [{account_key: 'account', request_id: 'followup'}]};
      }
      return {readiness_probes: []};
    },
    async refreshOfficialQuota() { return null; },
    auditGeminiAccounts: (_snapshot, _channel, _quota, expected) => [{request: expected.get('account')}],
    renderGovernance() {
      const audit = context.governanceAudits.gemini;
      renders.push([audit.authorization?.state, audit.checkingAfterAuthorization]);
    },
    renderAccountQuotas() {},
    async refreshGovernanceCases() {},
  });
  load('startGovernanceAudit', context);
  await vm.runInContext("startGovernanceAudit('gemini')", context);
  assert.deepEqual(requests, ['/api/gemini-readiness', '/api/official-reauth/activate', '/api/gemini-readiness?scope=recoverable']);
  assert.ok(renders.some(([state]) => state === 'waiting'));
  assert.ok(renders.some(([, checking]) => checking === true));
  const audit = context.governanceAudits.gemini;
  assert.equal(audit.authorization.completed, 1);
  assert.equal(audit.rows[0].request, 'followup');
  assert.equal(audit.running, false);
});

test('Gemini removal requires confirmation and then excludes the account from selection', async () => {
  const target = {key: 'flow-account-a', email: 'a@example.test', gemini_registered: true, gemini: 'verified'};
  const other = {key: 'flow-account-b', email: 'b@example.test', gemini: 'verified'};
  const state = {view: 'accounts', snapshot: {real_accounts: {available: true, items: [target, other]}}};
  const feedback = {textContent: ''};
  const calls = [];
  let confirmed = false;
  const context = vm.createContext({
    state, button: {dataset: {removeGemini: 'a'}, disabled: false}, accountActionsInFlight: new Set(),
    accountOrderOverride: ['a', 'b'],
    window: {confirm(message) { assert.match(message, /a@example\.test/); return confirmed; }},
    $() { return feedback; },
    async postJson(path, body) { calls.push({path, account: body.account, confirmed: body.confirmed}); return {removed: true, message: '已删除'}; },
    async refresh() {}, async refreshChannelStatus() {}, async refreshOfficialQuota() {}, renderAccounts() {},
    select: {value: 'a', dataset: {}, options: [{value: 'auto'}, {value: 'b'}]},
  });
  for (const name of ['esc', 'removeGeminiAccountFromPanel', 'geminiAccountAvailability', 'renderGeminiAccountOptions'])
    load(name, context);
  await vm.runInContext('removeGeminiAccountFromPanel(button)', context);
  assert.deepEqual(calls, []);
  confirmed = true;
  await vm.runInContext('removeGeminiAccountFromPanel(button)', context);
  assert.deepEqual(calls, [{path: '/api/gemini/accounts/remove', account: 'a', confirmed: true}]);
  assert.equal(feedback.textContent, '已删除');
  assert.equal(target.gemini_removed, true);
  vm.runInContext('renderGeminiAccountOptions(select,state.snapshot.real_accounts.items)', context);
  assert.doesNotMatch(context.select.innerHTML, /a@example/);
  assert.match(context.select.innerHTML, /b@example/);
  assert.equal(context.select.value, 'auto');
});

test('cooldown counts down and becomes awaiting confirmation without changing eligibility', () => {
  const time = {textContent: ''};
  const label = {textContent: ''};
  const status = {textContent: '冷却中', classList: {add(value) { this.added = value; }}};
  const clock = {
    dataset: {cooldownEnd: '20000'},
    querySelector(selector) { return selector === '.account-cooldown-time' ? time : label; },
    closest() { return {querySelector() { return status; }}; },
    setAttribute(_name, value) { this.accessibleLabel = value; },
  };
  let now = 14000;
  const context = vm.createContext({document: {querySelectorAll: () => [clock]}, Date: {now: () => now}});
  load('updateAccountCooldowns', context);
  vm.runInContext('updateAccountCooldowns()', context);
  assert.equal(time.textContent, '0:06');
  assert.equal(status.textContent, '冷却中');
  now = 20000;
  vm.runInContext('updateAccountCooldowns()', context);
  assert.equal(time.textContent, '待核对');
  assert.equal(label.textContent, '已到时间');
  assert.equal(status.textContent, '待核对');
  assert.match(clock.accessibleLabel, /刷新核对/);
});

test('account cards render fallback states without aborting the account list', () => {
  const render = source.slice(source.indexOf('function renderAccounts()'));
  const presentation = render.slice(render.indexOf('    const cooling ='), render.indexOf('    const cooldownSeconds ='));
  for (const status of ['ready', 'blocked', 'unknown', 'checking']) {
    const context = vm.createContext({
      item: {profile:'test', key:'flow-account-test', gemini:'verified'}, state:{snapshot:{}},
      flow:true,
      channelReadFailed:false, officialQuotaData:null, officialQuotaReadFailed:false,
      flowRow:{status,issues:[]}, geminiRow:{status}, issues:[],
      flowWindowIssue: () => null, readinessBadge: row => row.status,
      officialAccount: () => ({paused:false, data:null}),
      geminiAccountAvailability: () => ({manual: status === 'ready', auto: status === 'ready'}),
      geminiCardStatus: () => ({label: status === 'ready' ? '可调用' : '待确认', attention: status !== 'ready'}),
      Date,
    });
    const result = vm.runInContext(presentation + '\n({flowFallback,geminiFallback,flowStatusText,geminiStatusText});', context);
    assert.equal(result.flowStatusText, status);
    assert.equal(Boolean(result.flowFallback), status !== 'ready');
    assert.equal(Boolean(result.geminiFallback), status !== 'ready');
    assert.equal(result.geminiStatusText, status === 'ready' ? '可调用' : '待确认');
  }
});

test('dragging in Gemini keeps the shared order entries omitted by the account tab', async () => {
  const cards = ['b','a'].map(key=>({dataset:{dragKey:key}}));
  const accounts = ['a','hidden','b'].map(key=>({key:'flow-account-'+key}));
  const context = vm.createContext({
    accountDrag:{key:'b'}, accountOrderOverride:null,
    state:{snapshot:{real_accounts:{items:accounts}}},
    applyAccountOrder: items=>items,
    bareAccountKey:key=>key.replace('flow-account-',''),
    document:{querySelectorAll:()=>cards},
    renderAccounts(){},
    postJson:async (path, payload)=>{
      assert.equal(path,'/api/account-order');
      assert.deepEqual(Array.from(payload.order), ['b','hidden','a']);
    },
    refreshChannelStatus(){}
  });
  load('saveAccountOrderFromDom',context);
  vm.runInContext('saveAccountOrderFromDom()',context);
  assert.deepEqual(Array.from(context.accountOrderOverride), ['b','hidden','a']);
});

test('account reason identifies running work without inventing a failure', () => {
  const context = vm.createContext({});
  load('flowLoginNotice', context);
  load('flowAccountReason', context);
  context.account = {profile:'a'};
  context.snapshot = {flow_coordination:{windows:[{profile:'a',task_id:'current',stage:'preparing'}]},
    tasks:[{source_id:'current',source_profile:'a',kind:'image',status:'running',retry:{state:'active',attempts:[]}}]};
  const reason = vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked'},'busy')", context);
  assert.equal(reason.label, '当前占用原因');
  assert.match(reason.text, /current.*准备输入/);
  assert.match(reason.text, /尚未收到失败回执/);
});

test('the account card shows only the matching failure screenshot', () => {
  const context = vm.createContext({});
  load('flowLoginNotice', context);
  load('flowAccountReason', context);
  load('flowAccountNoticeMarkup', context);
  context.account = {profile:'a'};
  context.snapshot = {flow_coordination:{windows:[{profile:'a',task_id:'stopped',stage:'submitting'}]},
    tasks:[{source_id:'stopped',source_profile:'a',kind:'image',status:'interrupted',
      retry:{state:'needs_review',attempts:[{profile:'a',status:'failed',failure_reason:'等待图片回执超时，结果未确认'}]}}]};
  context.reason = vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked'},'busy')", context);
  assert.equal(context.reason.taskId, 'stopped');
  const markup = vm.runInContext("flowAccountNoticeMarkup(reason,{id:'123456789abc',task_id:'stopped'})", context);
  assert.match(markup, /action="\/api\/flow-warning-screenshots\/123456789abc\/view"/);
  assert.match(markup, /src="\/api\/flow-warning-screenshots\/123456789abc\.png"/);
  assert.match(markup, /<img .*src="\/api\/flow-warning-screenshots\/123456789abc\.png"/);
  assert.doesNotMatch(markup, /失败原因|等待图片回执超时/);
  assert.equal(vm.runInContext("flowAccountNoticeMarkup(reason,null)", context), '');
  assert.equal(vm.runInContext("flowAccountNoticeMarkup(reason,{id:'123456789abc',task_id:'other'})", context), '');
});

test('Flow cooldown shows the failed attempt even after the task succeeds on another account', () => {
  const context = vm.createContext({});
  load('esc', context);
  load('flowLoginNotice', context);
  load('flowAccountReason', context);
  load('flowAccountNoticeMarkup', context);
  context.account = {profile:'a', flow_cooling:true};
  context.snapshot = {
    flow_coordination:{windows:[{profile:'a', state:'idle', task_id:'original'}]},
    tasks:[{source_id:'original', source_profile:'b', kind:'video', status:'succeeded', retry:{
      state:'succeeded', attempts:[
        {id:'original', profile:'a', status:'failed', failure_reason:'PUBLIC_ERROR_UNUSUAL_ACTIVITY'},
        {id:'original', profile:'b', status:'succeeded'}
      ]
    }}]
  };
  context.reason = vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked'},'ok')", context);
  const missing = vm.runInContext('flowAccountNoticeMarkup(reason,null)', context);
  assert.match(missing, /任务 original.*异常活动.*本次未保存截图/);
  const captured = vm.runInContext("flowAccountNoticeMarkup(reason,{id:'123456789abc',task_id:'original'})", context);
  assert.match(captured, /123456789abc\.png/);
  assert.match(captured, /任务 original.*异常活动/);
  assert.doesNotMatch(captured, /本次未保存截图/);
  const unrelated = vm.runInContext("flowAccountNoticeMarkup(reason,{id:'123456789abc',task_id:'other'})", context);
  assert.doesNotMatch(unrelated, /123456789abc\.png/);
});

test('unlinked busy state is rechecked before reporting a persistent problem', () => {
  const context = vm.createContext({});
  load('flowLoginNotice', context);
  load('flowAccountReason', context);
  const reason = vm.runInContext("flowAccountReason({profile:'a'},{tasks:[]},{status:'blocked'},'busy')", context);
  assert.equal(reason.label, '正在复查');
  const after = vm.runInContext("flowAccountReason({profile:'a'},{tasks:[]},{busyRechecked:true},'busy')", context);
  assert.equal(after.label, '暂不可用原因');
  assert.match(after.text, /复查后仍无法确认/);
  const active = vm.runInContext("flowAccountReason({profile:'a'},{tasks:[]},{activeTask:'live'},'busy')", context);
  assert.equal(active.label, '当前占用原因');
  assert.match(active.text, /任务 live/);
});

test('a running account is queued rather than marked failed, while real blockers remain', () => {
  const account = {profile:'a'};
  const context = vm.createContext({
    flowReadinessObservation:{pageChecks:{accounts:{a:'busy'}}},
    flowWindowIssue:()=>null, flowRecoveryLabels:{},
    flowGovernance:()=>({shared:[],rows:[{account,issues:[]}]}),
  });
  load('flowLoginNotice', context);
  load('accountIssues', context);
  load('auditFlowAccounts', context);
  context.account = account;
  context.snapshot = {real_accounts:{items:[account]},tasks:[{kind:'image',source_profile:'a',status:'running'}],
    flow_coordination:{windows:[{profile:'a',state:'submitted'}]}};
  assert.equal(vm.runInContext('accountIssues(account,snapshot).length',context),0);
  const rows=vm.runInContext("auditFlowAccounts(snapshot,{available:true,active:1,maximum:3},{accounts:{a:'busy'}},0)",context);
  assert.equal(rows[0].status,'queue');
  context.snapshot.tasks=[];
  const fresh=vm.runInContext("auditFlowAccounts(snapshot,{available:true,active:1,maximum:3},{accounts:{a:'busy'},active_tasks:{a:'live'}},0)",context);
  assert.equal(fresh[0].status,'queue');
  context.flowGovernance=()=>({shared:[],rows:[{account,issues:[{title:'Google 要求验证'}]}]});
  assert.equal(vm.runInContext("auditFlowAccounts(snapshot,{available:true},{accounts:{a:'busy'}},0)[0].status",context),'blocked');
});

test('a finished no-image lookup is failed while an active attempt remains active', () => {
  const context=vm.createContext({statusBadge:value=>value});
  load('flowTaskStatusBadge',context);
  context.item={kind:'image',status:'interrupted',retry:{state:'needs_review',attempts:[{
    failure_reason:'no image reply within 240 s, and no new image is in the project'}]}};
  assert.equal(vm.runInContext('flowTaskStatusBadge(item)',context),'failed');
  context.item.status='running';context.item.retry.state='active';
  assert.equal(vm.runInContext('flowTaskStatusBadge(item)',context),'running');
});


test('ready account clears historical failure but preserves task history and current blockers', () => {
  const context=vm.createContext({});
  load('flowLoginNotice', context);
  load('flowAccountReason',context);
  context.account={profile:'a'};
  context.snapshot={flow_coordination:{windows:[{profile:'a',task_id:'old'}]},tasks:[{
    kind:'image',source_profile:'a',source_id:'old',status:'failed',failure_reason:'Google 提示异常活动'}]};
  const original=JSON.stringify(context.snapshot);
  assert.equal(vm.runInContext("flowAccountReason(account,snapshot,{status:'ready',issues:[]},'ready')",context),null);
  assert.equal(JSON.stringify(context.snapshot),original);
  assert.equal(vm.runInContext("flowAccountReason(account,snapshot,{status:'blocked',issues:[{title:'风控暂停'}]},'ready').taskId",context),'old');
});
