"use strict";

const $ = (id) => document.getElementById(id);
const state = { snapshot: null, snapshotReceivedAt: 0, view: "overview", refreshSequence: 0, dispatchPaused: null };
state.serviceActivations = {};
state.connectionLost = false;

function showConnectionState(connected) {
  state.connectionLost = !connected;
  let banner = $('workbenchConnectionNotice');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'workbenchConnectionNotice';
    banner.setAttribute('role','alert');
    banner.setAttribute('aria-live','assertive');
    banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:100000;padding:14px 20px;background:#fff1df;color:#713b13;border-bottom:2px solid #bc7736;text-align:center;font-size:15px';
    const text = document.createElement('span');
    text.textContent = '与 Ubuntu 的连接已断开。桌面端正在自动重连；主机需保持开机。已保留页面内容，暂时无法提交或检查账号。';
    const retry = document.createElement('button');
    retry.type = 'button'; retry.dataset.connectionRetry = '1'; retry.textContent = '立即检查连接';
    retry.style.marginLeft = '16px';
    retry.addEventListener('click', () => { void refresh({background:true}); });
    banner.append(text,retry);
    document.body.append(banner);
  }
  banner.hidden = connected;
}

document.addEventListener('click', event => {
  if (!state.connectionLost) return;
  const target = event.target.closest('button,input[type=submit],a');
  if (!target || target.dataset.connectionRetry || target.hasAttribute('data-view') ||
      (target.tagName==='A' && !target.hasAttribute('download'))) return;
  event.preventDefault(); event.stopImmediatePropagation();
  showConnectionState(false);
}, true);
document.addEventListener('submit', event => {
  if (state.connectionLost) { event.preventDefault(); event.stopImmediatePropagation(); showConnectionState(false); }
}, true);
window.addEventListener('studiowright-connection', event => {
  if (event.detail?.connected === false) showConnectionState(false);
  else if (event.detail?.connected === true) { showConnectionState(true); void refresh({background:true}); }
});
let serviceActivationReadSequence = 0;
let snapshotRefreshTimer = null;
let lastTaskMarkup = null;
let recentTasks = null, recentTasksReading = null, recentTasksTimer = null;
let recentTaskStream = null, taskHeartbeat = null, taskStreamHealthy = false, taskPushRevision = 0;
let lastLibraryMarkup = null;
let lastProjectMarkup = null;
let snapshotRefreshInFlight = null;
let projectMaterialCounts = null;
let projectMaterialLoading = false;
let projectActions = new Map();
let projectActionsPending = new Map();
const retryProjectDrafts = new Map(), accountActionsInFlight = new Set();
let lastProjectCountsRead = 0;
const viewReads = new Map();
let officialQuotaData = null;
let officialQuotaRequest = null;
let officialQuotaReadFailed = false;
let officialQuotaReadAt = 0;
let projectActionsLoading = false;
const SNAPSHOT_REFRESH_MS = 5000;
const OFFICIAL_QUOTA_SYNC_MS = 15000;
let dispatchPolicyDirty = false;
let dispatchPolicySaving = false;
let activationUpdating = false;
let activationPosting = false;
let activationSignature = null;
const labels = {
  status: { queued: "排队中", running: "执行中", succeeded: "已完成", failed: "失败", interrupted: "已中断", cancelled: "已取消" },
  kind: { image: "图片生成", video: "视频生成", quote: "视频报价", sync: "账号同步", login: "账号登录", session_import: "登录信息导入", check: "状态检查", doctor: "环境检查", keepalive: "窗口保活" },
};
const schedulerTaskKinds = new Set(['sync', 'login', 'session_import', 'check', 'doctor', 'keepalive', 'quote']);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[character]);
}

function dateTime(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "时间未知";
  const date = new Date(seconds * 1000);
  if (!Number.isFinite(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 16 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}

function kindLabel(kind) { return labels.kind[kind] || (kind ? `其他：${kind}` : "未标注类型"); }
function statusLabel(status) { return labels.status[status] || (status ? `未知：${status}` : "状态未知"); }
function statusBadge(status) {
  const known = Object.prototype.hasOwnProperty.call(labels.status, status) ? status : "unknown";
  return `<span class="task-badge ${known}">${esc(statusLabel(status))}</span>`;
}

function empty(title, description = "") {
  return `<div class="empty"><strong>${esc(title)}</strong>${description ? `<p>${esc(description)}</p>` : ""}</div>`;
}

// Read-only official evidence. Login and the production sender are separate states.
state.channelStatus = null;
let channelRead = null;
let channelReadFailed = false;
const channelLabels = {succeeded:'最近调用成功', verification_required:'待额外核验', processing:'调用处理中', failed:'最近调用失败', unconfirmed:'调用结果未确认', untested:'未实测'};
function officialAccount(account) {
  const key = String(account.key || '').replace(/^flow-account-/, '');
  const data = !channelReadFailed && state.channelStatus?.available ? state.channelStatus.accounts.find(a => a.account_key === key) : null;
  const call = data?.latest_call;
  const stale = !!call && Date.now()/1000 - call.created > 86400;
  const paused = data?.paused === true || account.gemini_enabled === false;
  const outcome = call?.outcome || 'untested';
  const label = channelReadFailed || !state.channelStatus?.available ? '实测状态未读取'
    : call ? channelLabels[outcome] || '状态未知' : paused ? '未实测 · 自动派发暂停' : '未实测';
  return {data, call, outcome, paused, stale, label,
    attention: !!data && !paused && !stale && ['verification_required','failed'].includes(outcome)};
}
function officialSummary() {
  if (channelReadFailed || !state.channelStatus?.available) return '官方实测记录暂不可读取；不据此判断账号故障。';
  const accounts = (state.snapshot?.real_accounts?.items || []).filter(account=>!account.gemini_removed);
  const records = accounts.map(officialAccount);
  const count = kind => records.filter(r => r.outcome === kind).length;
  const {ready}=geminiReadiness(state.snapshot);
  return `Gemini 就绪 ${ready ?? '—'} / ${records.length} · 手动暂停 ${records.filter(r=>r.paused).length}。最近调用记录：成功 ${count('succeeded')} · 待核验 ${count('verification_required')} · 未实测 ${count('untested')}${['processing','failed','unconfirmed'].filter(k=>count(k)).map(k=>' · '+channelLabels[k]+' '+count(k)).join('')}`;
}
function officialDetails(record) {
  const call = record.call;
  return `<p>通道：官方 Antigravity CLI · ${state.channelStatus?.production_send_available ? '已接入算力中心账号池' : '暂不可用'}。</p><p>登录授权：${record.data?.authorization === 'saved_verified_identity' ? '本地已保存且账号身份一致；不保证下一次官方请求一定通过' : '本地授权未就绪'}。</p><p>额外核验：${call?.verification === 'not_required_for_this_call' ? '该次调用未受拦截' : call?.verification === 'page_error_500' ? '记录中的官方核验页返回 500，不能据此认定账号失效' : call?.verification === 'required' ? '该次调用要求额外核验' : '未单独读取'}</p><p>最近真实调用：${esc(channelLabels[record.outcome] || '未实测')}${call ? ` · ${esc(dateTime(call.created))}` : ''}。${record.stale ? '记录超过 24 小时，仅作历史参考。' : '仅证明该次结果，不保证下次可用。'}</p><p>来源：已保存的官方实测请求${state.channelStatus?.read_at ? ` · 读取于 ${esc(dateTime(state.channelStatus.read_at))}` : '（暂未读取）'}。</p>`;
}
async function refreshChannelStatus() {
  if (channelRead) return channelRead;
  channelRead = (async () => {
    try {
      const data = await getJson('/api/channel-status', {timeoutMs:8000});
      if (data.schema_version !== 1 || data.available !== true || !Array.isArray(data.accounts)) throw new Error('unknown');
      state.channelStatus = data; channelReadFailed = false;
      if (accountOrderOverride && JSON.stringify(data.shared_account_order || data.account_order) === JSON.stringify(accountOrderOverride)) accountOrderOverride = null;
    } catch (_) { channelReadFailed = true; }
    if (state.snapshot && state.view === 'overview') renderAccountInsights(state.snapshot, $('overviewInsights'));
    if (state.snapshot && state.view === 'accounts') renderAccounts();
    if (state.view === 'conversations') syncConversationControls();
  })().finally(() => {channelRead = null;});
  return channelRead;
}
function officialConversation(item) {
  return ['official_antigravity_cli_isolated','official_antigravity_cli'].includes(state.channelStatus?.conversations?.[item.id]);
}
function safeRecordedNote(value) {
  return String(value || '').replace(/https?:\/\/[^\s<>"']+/gi, '（记录中的链接已隐藏）')
    .replace(/Bearer\s+\S+/gi, 'Bearer [已隐藏]')
    .replace(/(?:access_token|refresh_token|authorization_code|code_verifier|cookie)\s*[:=]\s*[^\s,;]+/gi, '[授权信息已隐藏]');
}
function renderGeminiReply(record) {
  const retry=record.attempts?.find(a => a.ordinal > 1 && a.status === 'running');
  if (record.status === 'dispatching' && retry) return `<p class="subtle">正在用原账号进行第 ${retry.ordinal-1} 次重试。</p>`;
  if (['queued','dispatching','running'].includes(record.status)) return `<p class="subtle">${record.status==='queued' ? '排队中，等待并发空位。' : '官方调用处理中。'} 不会重复提交。</p>`;
  const text=record.response ? `<p>${esc(record.response).replace(/\n/g,'<br>')}</p>` : '';
  const images=(record.images || []).filter(url=>/^\/api\/gemini-images\/[0-9a-f-]{36}\/[0-3]$/.test(url)).map(url=>`<a href="${esc(url)}" target="_blank" rel="noopener"><img src="${esc(url)}" alt="历史生成图片" style="max-width:100%">查看或下载图片</a>`).join('');
  const status=record.status==='unknown' ? '<p class="subtle">结果未确认，记录保留，不会自动重试或换号。</p>' : record.status==='failed' ? `<p class="subtle">${esc(safeRecordedNote(record.note || '本轮未成功，已停止。'))}</p>` : '';
  return text+images+status;
}
function renderGeminiAttempts(attempts) {
  if (!Array.isArray(attempts) || !attempts.length) return '';
  const statuses = {running:'处理中', completed:'成功', failed:'失败', unknown:'结果待核对'};
  const reasons = {
    temporary_cli_launch_failure:'调用程序启动遇到临时故障',
    protocol_timeout_2:'建立 Gemini 会话超时，图片尚未发送',
    authorization_not_ready:'账号授权未就绪',
    authorization_required_unsent:'原账号需要重新授权',
    execution_interrupted:'调用程序执行中断',
    verification_required:'需要 Google 额外核验',
    quota_or_rate_limit:'额度或频率受限',
    provider_error:'Gemini 返回错误',
    timeout_unconfirmed:'等待回复超时',
    output_limit_unconfirmed:'回复超过读取上限',
    duplicate_result:'收到多份结果，需核对',
    retry_or_fallback_stopped:'检测到调用程序内部重试或回退，已停止',
    unexpected_tool_stopped:'检测到意外工具调用，已停止',
    worker_interrupted:'对话处理意外中断',
    persistence_or_worker_failure:'处理或保存结果时中断',
    workbench_restarted:'算力中心重启，结果需核对',
    confirmed_bad_reply:'回复已结束，仅返回通用拒答',
    confirmed_incomplete_reply:'回复已结束，但内容为空或被截断',
    authorization_still_required:'恢复后仍需本人授权，本条已用完补发次数',
    readiness_authorization_required:'云端验活未发送，账号需要授权',
    readiness_unexpected_reply:'云端验活回复与预期不符',
  };
  return attempts.map(attempt => {
    const label = attempt.ordinal > 1 ? `第 ${attempt.ordinal-1} 次重试（原账号）` : '首次尝试';
    const reason = attempt.reason ? reasons[attempt.reason] || '调用未完成' : attempt.status === 'failed' || attempt.status === 'unknown' ? '未收到可核对的结果' : '';
    const detail = [label, statuses[attempt.status] || '状态待核对', reason, attempt.submitted === 0 ? '未发送' : '', typeof attempt.total_ms === 'number' ? (attempt.total_ms / 1000).toFixed(2) + ' 秒' : ''].filter(Boolean).join(' · ');
    const previous=attempt!==attempts.at(-1) && attempt.response
      ? `<details><summary>查看这次回复</summary><p>${esc(attempt.response).replace(/\n/g,'<br>')}</p></details>` : '';
    return `<small class="response-timing">${esc(detail)}</small>`+previous;
  }).join('');
}
const geminiReauthLabels = {
    required:'原账号需要重新授权；已确认这条消息没有发送。',
    waiting:'请在 Ubuntu 弹出的窗口完成 Google 授权。成功后会核对原账号并补发一次。',
    queued:'已核对原账号，正在排队补发。',
    resending:'正在用原账号补发。',
    completed:'重新授权后已用原账号补发成功。',
    stopped:'补发已停止；请查看尝试记录。',
    expired:'授权窗口已过期，可重新打开。',
    wrong_account:'授权账号不匹配，原授权未更改；请重新选择原账号。',
    window_failed:'未能打开 Ubuntu 授权窗口，请确认 Ubuntu 桌面已登录。',
    account_changed:'账号或消息状态已变化，本次没有补发。',
};
function renderGeminiReauth(turn) {
  const flow = turn.reauthorization;
  if (!flow) return '';
  return `<div class="subtle"><p role="status">${esc(geminiReauthLabels[flow.state] || '')}</p>${flow.can_start ?
    `<button class="button button-light" type="button" data-reauth-request="${esc(turn.request_id)}">重新授权并补发一次</button>` : ''}</div>`;
}
function flowWindowIssue(profile, snapshot) {
  const windows = snapshot?.flow_coordination?.windows;
  const window = Array.isArray(windows) ? windows.find(item => item.profile === profile) : null;
  if (!window || !['result_unknown', 'needs_verification'].includes(window.state)) return null;
  const unknown = window.state === 'result_unknown';
  const task = (snapshot.tasks || []).find(item => item.source_profile === profile && item.source_id === window.task_id);
  const rejection = /异常活动|UNUSUAL_ACTIVITY/i.test(task?.failure_reason || '') ? ' · 上次提交被提示异常活动' : '';
  const source = window.task_id ? `原任务 ${window.task_id}${window.updated ? ' · ' + dateTime(window.updated) : ''}${rejection}。` : '';
  return {
    key: unknown ? 'flow_window_unknown' : 'flow_window_verification',
    title: unknown ? 'Flow 旧任务待核对' : '需要用户登录',
    impact: unknown ? '旧任务尚未解除窗口占用，新生成任务暂不会开始。' : '任务窗口需要重新登录或验证，新生成任务暂不会开始。',
    action: source,
    taskId: window.task_id,
  };
}

function flowLoginNotice(page) {
  if (page === 'verification_required')
    return {label:'需要用户登录', text:'请在这个账号的 Ubuntu 浏览器完成 Google 验证，回到原 Flow 项目，再点「Flow 一键就绪」。'};
  if (page === 'login' || page === 'flow_landing')
    return {label:'需要用户登录', text:'请在这个账号的 Ubuntu 浏览器重新登录 Google，回到原 Flow 项目，再点「Flow 一键就绪」。'};
  return null;
}

function flowAccountReason(account, snapshot, row, pageState) {
  if (account.flow_page_incident) {
    const phase = account.flow_page_incident.phase === 'login' ? '核对登录' : '浏览 Flow 页面';
    return {label:'Flow 停派原因', text:`${phase}时检测到异常活动提示，等待人工核查。`,
      screenshotMissing:!account.flow_page_incident.has_screenshot};
  }
  const page = pageState || row?.page;
  const signIn = flowLoginNotice(page);
  if (signIn) return {...signIn, showOnCard:true, noScreenshot:true};
  const checks = account.flow_preflight_reasons || [];
  if (checks.length && !account.flow_risk_paused && !account.flow_cooling) {
    const labels = {dispatch_locale_missing:'未配置此账号的语言和时区', dispatch_locale_invalid:'账号语言和时区配置无效',
      profile_languages_missing:'浏览器未记录语言', profile_languages_mismatch:'浏览器语言与账号设置不一致',
      system_timezone_unavailable:'无法确认 Ubuntu 时区', system_timezone_mismatch:'Ubuntu 时区与账号设置不一致',
      invalid_or_missing_profile:'浏览器资料不存在或不可用', preferences_unreadable:'无法读取浏览器设置',
      proxy_mapping_missing:'账号没有代理映射', proxy_mapping_unreadable_or_invalid:'代理映射无法读取或无效'};
    const reasons = checks.map(code => {
      const key = code.split(':', 1)[0];
      return labels[key] || (key.startsWith('extension_') ? '浏览器扩展检查未通过'
        : key.startsWith('chrome_') ? '浏览器启动参数检查未通过' : '无法确认浏览器自检状态');
    });
    return {label:'Flow 派发自检未通过', text:[...new Set(reasons)].join('；'), noScreenshot:true};
  }
  if (row?.recovering) return {label:'刷新复查中',text:'正在刷新 Flow 任务页，随后复查页面和积分。'};
  const window = (snapshot?.flow_coordination?.windows || []).find(w => w.profile === account.profile);
  const tasks = (snapshot?.tasks || []).filter(t => ['image','video'].includes(t.kind) &&
    (t.source_profile === account.profile || (t.retry?.attempts || []).some(a => a.profile === account.profile)));
  const linked = window?.task_id && tasks.find(t => t.source_id === window.task_id || t.retry?.root_id === window.task_id ||
    (t.retry?.attempts || []).some(a => a.id === window.task_id));
  if ((account.flow_cooling || account.flow_health === 'cooling') && window?.task_id && linked) {
    const rejected = (linked.retry?.attempts || []).find(attempt => attempt.id === window.task_id &&
      attempt.profile === account.profile && attempt.status === 'failed' &&
      /PUBLIC_ERROR_UNUSUAL_ACTIVITY/.test(attempt.failure_reason || ''));
    if (rejected) return {taskId:window.task_id,label:'Flow 冷却原因',
      text:`任务 ${window.task_id} 的本账号提交被 Flow 以「异常活动」拒绝。`};
  }
  const active = tasks.find(t => ['queued','running'].includes(t.status) && t.source_profile === account.profile);
  if (!active && row?.status === 'ready' && !row?.issues?.length && pageState !== 'busy') return null;
  const historical = window?.state === 'idle' && !account.flow_risk_paused && !account.flow_cooling;
  const task = active || (historical ? null : linked);
  const attempts = (task?.retry?.attempts || []).filter(a => !a.profile || a.profile === account.profile);
  const latest = attempts.at(-1);
  if (task && (['failed','interrupted'].includes(task.status) || latest?.status === 'failed') &&
      !['active','pending_switch'].includes(task.retry?.state)) {
    return {taskId:task.source_id};
  }
  if (active || (task && ['queued','running'].includes(task.status))) {
    const current = active || task;
    const stage = {preparing:'准备输入',submitting:'提交中，等待服务方回执',submitted:'等待生成结果',
      generated:'结果已生成，正在整理',downloading:'下载结果'}[window?.stage] || (current.status === 'queued' ? '排队中' : '处理中');
    return {label:'当前占用原因',text:`任务 ${current.source_id}：${stage}；尚未收到失败回执。`};
  }
  if (page === 'busy') return {label:'暂不可用原因',text:'后台标记页面被占用，但未查到关联的进行中任务，需核对窗口状态。'};
  if (!row?.review && !account.flow_risk_paused && !account.flow_cooling) {
    const needsProject = ['binding','unique','project'].includes(page)
      || (!page && row?.issues?.some(issue => issue.key === 'flow_binding'));
    if (needsProject) return {label:'请打开原 Flow 项目',
      text:'在这个账号的 Ubuntu 浏览器打开原 Flow 项目。如果 Google 显示登录页，请用这个账号重新登录。完成后点「Flow 一键就绪」。',
      showOnCard:true,noScreenshot:true};
    if (page === 'ok' && account.flow_balance_fresh === false) return {label:'请重新读取积分',
      text:'原任务页已就绪。点「Flow 一键就绪」重新读取积分。',
      showOnCard:true,noScreenshot:true};
  }
  if (row?.issues?.length) return {label:'暂不可用原因',text:row.issues.map(i => i.title).join('；')};
  if (row?.status === 'blocked') return {label:'暂不可用原因',text:'就绪检查未通过，但后台未提供具体原因，待核对。'};
  return null;
}

const viewedFlowScreenshots = new Set();
const viewedFlowScreenshotsKey = 'flowViewedScreenshots';
try {
  for (const id of JSON.parse(localStorage.getItem(viewedFlowScreenshotsKey) || '[]').slice(-256)) {
    if (/^(?:[a-f0-9]{12}|[a-f0-9]{32})$/.test(id)) viewedFlowScreenshots.add(id);
  }
} catch (_) { /* The server still tracks views if local storage is unavailable. */ }

function markFlowScreenshotViewed(id) {
  viewedFlowScreenshots.add(id);
  try {
    localStorage.setItem(viewedFlowScreenshotsKey, JSON.stringify([...viewedFlowScreenshots].slice(-256)));
  } catch (_) { /* The current page still hides the screenshot. */ }
}

function flowAccountNoticeMarkup(reason, screenshot) {
  const id = screenshot?.id;
  const notice = reason?.label ? `<strong>${esc(reason.label)}：</strong>${esc(reason.text)}` : '';
  const matches = !reason?.taskId || screenshot?.task_id === reason.taskId;
  if (matches && (screenshot?.destroyed === true || viewedFlowScreenshots.has(id))) {
    return notice ? `<p class="account-flow-reason">${notice}</p>` : '';
  }
  if (matches && !reason?.noScreenshot && screenshot?.has_screenshot !== false &&
      /^(?:[a-f0-9]{12}|[a-f0-9]{32})$/.test(id)) {
    const path = '/api/flow-warning-screenshots/' + id;
    return `${notice ? `<p class="account-flow-reason">${notice}</p>` : ''}<form method="post" action="${path}/view" target="_blank" rel="noopener" data-flow-screenshot="${id}"><button class="account-flow-warning" type="submit" name="view" value="original" title="打开原图，阅后即焚"><img src="${path}.png" width="192" loading="lazy" draggable="false" alt="打开 Flow 警告原图，阅后即焚"></button></form>`;
  }
  if (!notice) return '';
  return `<p class="account-flow-reason">${notice}${reason.taskId || reason.screenshotMissing ? ' 本次未保存截图。' : ''}</p>`;
}

function showFlowScreenshotViewer() {
  const dialog = document.createElement('dialog');
  dialog.className = 'library-dialog flow-screenshot-viewer';
  dialog.setAttribute('aria-label', 'Flow 警告原图');
  dialog.innerHTML = '<header class="library-dialog-header"><h2>Flow 警告原图</h2><button type="button" class="button button-light">关闭</button></header><p role="status">正在打开原图…</p><canvas role="img" aria-label="Flow 警告原图" hidden></canvas>';
  dialog.querySelector('button').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove(), {once:true});
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

async function openFlowScreenshot(event) {
  const form = event.target.closest('form[data-flow-screenshot]');
  if (!form) return;
  event.preventDefault();
  const id = form.dataset.flowScreenshot;
  const button = form.querySelector('button');
  if (button.disabled || viewedFlowScreenshots.has(id)) return;
  const viewer = showFlowScreenshotViewer();
  const status = viewer.querySelector('[role="status"]');
  button.disabled = true;
  accountActionsInFlight.add(button);
  try {
    const response = await fetch(form.action, {method:'POST', credentials:'same-origin', cache:'no-store',
      body:new URLSearchParams({view:'original'}), signal:AbortSignal.timeout(12000)});
    if (!response.ok && response.status !== 404) throw new Error('原图暂不可读取，请稍后再试。');
    markFlowScreenshotViewed(id);
    form.remove();
    if (response.status === 404) {
      status.textContent = '这张截图已不可读取。';
      return;
    }
    const image = await response.blob();
    if (image.type !== 'image/png') throw new Error('没有收到原图。');
    if (viewer.open) {
      const bitmap = await createImageBitmap(image);
      try {
        const canvas = viewer.querySelector('canvas');
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
        canvas.getContext('2d').drawImage(bitmap, 0, 0);
        canvas.hidden = false;
        status.hidden = true;
      } finally {
        bitmap.close();
      }
    }
  } catch (error) {
    status.textContent = error.message || '原图暂不可读取，请稍后再试。';
  } finally {
    accountActionsInFlight.delete(button);
    button.disabled = false;
  }
}

$('realAccountTable').addEventListener('submit', openFlowScreenshot);

function flowTaskStatusBadge(item) {
  if (['image','video'].includes(item.kind) && ['failed','interrupted'].includes(item.status) &&
      !['active','pending_switch'].includes(item.retry?.state) &&
      (item.retry?.attempts || []).some(a => /no image reply within \d+ s, and no new image is in the project/.test(a.failure_reason || '')))
    return statusBadge('failed');
  if (!['image', 'video'].includes(item.kind) || item.status !== 'failed') return statusBadge(item.status);
  const retry = item.retry;
  const failedRounds = new Set((retry?.attempts || [])
    .filter(a => a.ordinal >= 1 && a.ordinal <= 3 && a.status === 'failed' && a.finished)
    .map(a => a.ordinal));
  if (retry?.state === 'exhausted' && failedRounds.size === 3) return statusBadge('failed');
  if (retry?.state === 'succeeded') return statusBadge('succeeded');
  if (['active', 'pending_switch'].includes(retry?.state)) return statusBadge('queued');
  return statusBadge('interrupted');
}

function flowQueueReason(item, snapshot, account) {
  if (item.status !== 'queued' || !['image', 'video'].includes(item.kind)) return '';
  if (snapshot.flow_coordination?.desktop_keyboard_ready === false) {
    return item.note ? '' : '等待 Ubuntu 桌面键盘就绪';
  }
  if (item.retry?.state === 'pending_switch') return '等待另一个可用账号';
  const issue = flowWindowIssue(item.source_profile, snapshot);
  if (issue) return issue.title + (issue.taskId ? ' · 原任务 ' + issue.taskId : '');
  if (account?.flow_risk_paused === true) return '账号已暂停，等待核对';
  if (account?.flow_cooling === true) return '等待账号冷却结束';
  if (account && account.flow_login !== 'verified') return '等待核对账号登录';
  if ((snapshot.tasks || []).some(task => task.source_profile === item.source_profile &&
      task.status === 'running')) return '等待同账号任务完成';
  return '等待可用调度名额';
}

// Each issue reflects recorded state only; it does not change account eligibility.
function accountIssues(account, snapshot = state.snapshot, geminiRow = null,
  pageState = flowReadinessObservation?.pageChecks?.accounts?.[account.profile]) {
  const issues = [];
  const signIn = flowLoginNotice(pageState);
  const add = (key, title, impact, action) => issues.push({key, title, impact, action});
  if (account.flow_risk_paused === true || account.flow_health === 'paused')
    add('flow_paused', 'Flow 已暂停，需核查', '该账号的 Flow 暂不能用于生成；Gemini 状态单独判断。', '核对原账号状态后，再决定是否恢复 Flow。');
  else if (account.flow_cooling === true || account.flow_health === 'cooling')
    add('flow_cooling', 'Flow 冷却中', '该账号的 Flow 暂时等待；Gemini 状态单独判断。', '等待冷却结束后核对状态。');
  else if (account.flow_health === 'empty')
    add('flow_empty', 'Flow 积分不足', '该账号暂不能用于需要积分的 Flow 任务。', '核对积分余额。');
  else if (['attention', 'unknown'].includes(account.flow_health) &&
      !signIn && !(pageState === 'ok' && !account.flow_balance_fresh))
    add('flow_unknown', 'Flow 状态待核对', '目前无法确认该账号的 Flow 是否可用。', '查看账号详情并核对 Flow 状态。');

  const activeFlow = (snapshot?.tasks || []).some(t => t.source_profile === account.profile &&
    ['image','video'].includes(t.kind) && ['queued','running'].includes(t.status));
  if (signIn) add('flow_page_state', signIn.label, signIn.text, '');
  else if (pageState === 'unique')
    add('flow_page_state', '固定任务窗口待复查', '算力中心会保留已绑定的任务页，只关闭同项目确认空的副本。', '');
  else if (pageState === 'draft')
    add('flow_page_state', '固定任务窗口有草稿或附件', '算力中心会保留草稿。处理后再检查就绪。', '');
  else if (pageState && pageState !== 'ok' && !(pageState === 'busy' && activeFlow))
    add('flow_page_state', '页面未就绪：' + ({overlay:'页面有弹窗待关闭', binding:'窗口绑定待核对', project:'页面停在非标准项目', unconfirmed:'页面状态待确认', busy:'页面忙', draft:'有草稿待核对', load_failed:'固定任务页加载失败'}[pageState] || pageState), '点一键就绪，算力中心会把页面带回标准项目并复查。', '使用一键就绪。');
  const windowIssue = flowWindowIssue(account.profile, snapshot);
  if (windowIssue) issues.push(windowIssue);
  for (const issue of geminiRow?.issues || []) add('gemini_'+(issue.key || 'blocked'),issue.title,'Gemini 当前就绪检查。',issue.action);
  return issues;
}

function geminiAccountAvailability(account) {
  if (account.gemini_removed) return {manual:false, auto:false, reason:'已从 Gemini 号池删除'};
  if (account.gemini_channel === 'official_antigravity_cli') {
    const item = officialAccount(account).data;
    const permitted = !channelReadFailed && state.channelStatus?.production_send_available === true && item?.dispatch_eligible === true;
    const reasons = {dispatch_paused:'已手动暂停', unresolved_request:'官方通道有结果待核对', official_call_blocked:'官方授权或调用受限', authorization_not_ready:'官方授权未就绪'};
    return {manual:permitted, auto:permitted, reason:permitted ? (account.gemini_busy ? '可排队 · 官方调用' : '官方授权就绪') : reasons[item?.restriction] || '官方调用暂不可用'};
  }
  if (account.gemini_enabled === false) return {manual: false, auto: false, reason: '已暂停 Gemini'};
  if (account.gemini_busy) return {manual: false, auto: false, reason: '正在处理，请稍候'};
  if (account.gemini_unresolved > 0) return {manual: false, auto: false, reason: '有结果未确认，需先核对'};
  if (account.gemini_auto_state === 'browser_unknown') return {manual: false, auto: false, reason: '浏览器状态待核对'};
  if (account.gemini_auto_state === 'browser_unavailable') return {manual: false, auto: false, reason: '账号浏览器未就绪'};
  if (account.gemini !== 'verified') {
    const browserPaused = /浏览器未确认运行/.test(account.keepalive_note || '');
    return {manual: false, auto: false, reason: browserPaused ? '账号浏览器待核对' : '登录或保活待核对'};
  }
  const automatic = ['eligible', 'refresh_needed'].includes(account.gemini_auto_state);
  const reason = account.gemini_auto_state === 'refresh_needed' ? '可选择，发送前核对用量'
    : account.gemini_auto_state === 'usage_unknown' ? '可手选，自动调度用量待核对'
    : account.gemini_auto_state === 'limit_reached' ? '可手选，已达到自动调度上限' : '上次登录核对通过';
  return {manual: true, auto: automatic, reason};
}

function geminiSelectionReady(value) {
  const data = state.snapshot?.real_accounts;
  if (!data?.available || !Array.isArray(data.items)) return false;
  if (value === 'auto') return data.items.some((item) => geminiAccountAvailability(item).auto);
  const account = data.items.find((item) => item.key === 'flow-account-' + value);
  return !!account && geminiAccountAvailability(account).manual;
}

function renderGeminiAccountOptions(select, accounts) {
  const selected = select.value;
  const poolAccounts = accounts.filter(item=>!item.gemini_removed);
  const autoCount = poolAccounts.filter((item) => geminiAccountAvailability(item).auto).length;
  const markup = `<option value="auto"${autoCount ? '' : ' disabled'}>按号池优先顺序分配（${autoCount ? autoCount + ' 个可参与' : '暂无符合条件账号'}）</option>`
    + poolAccounts.map((item) => {
      const availability = geminiAccountAvailability(item);
      return `<option value="${esc(item.key.slice('flow-account-'.length))}"${availability.manual ? '' : ' disabled'}>${esc(item.email || item.label || item.profile)} · ${esc(availability.reason)}</option>`;
    }).join('');
  if (select.dataset.options === markup) return;
  select.dataset.options = markup;
  select.innerHTML = markup;
  select.value = [...select.options].some((option) => option.value === selected) ? selected : 'auto';
}

function syncQuickGeminiControls() {
  $('geminiSend').disabled = !geminiSelectionReady($('geminiAccount').value);
}

const workViews = new Set(["tasks", "conversations", "music"]);
let lastWorkView = "tasks";
const apiChapterButtons = Array.from(document.querySelectorAll('[data-api-target]'));

function markApiChapter(id) {
  for (const button of apiChapterButtons) {
    if (button.dataset.apiTarget === id) button.setAttribute('aria-current', 'location');
    else button.removeAttribute('aria-current');
  }
}

function focusApiSection(id) {
  const target = $(id);
  if (!target) return;
  markApiChapter(id);
  target.scrollIntoView({block: 'start'});
  target.focus({preventScroll: true});
}

function setView(view) {
  const apiTarget = apiChapterButtons.some(button => button.dataset.apiTarget === view) ? view : null;
  if (apiTarget) view = 'api';
  if (view === "work") view = lastWorkView;
  if (!["overview", "accounts", "projects", "tasks", "records", "music", "conversations", "library", "api"].includes(view)) view = "overview";
  if (view === 'tasks' && state.view !== 'tasks') $('taskWindowsPanel').open = false;
  state.view = view;
  for (const name of ["overview", "accounts", "projects", "tasks", "records", "music", "conversations", "library", "api"]) $(name + "View").hidden = name !== view;
  const display = { overview: "操作台", accounts: "账号", projects: "项目", tasks: "任务", records: "任务记录", music: "音乐生成", conversations: "对话", library: "素材库", api: "API 文档" };
  const inWork = workViews.has(view);
  if (inWork) lastWorkView = view;
  $("workBar").hidden = !inWork;
  $("breadcrumb").textContent = inWork ? "算力中心 / " + ({tasks:"Flow",conversations:"自定义对话",music:"音乐生成"}[view]) : display[view];
  $("workBar").querySelectorAll("[data-view]").forEach((link) => {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.querySelectorAll(".navigation [data-view]").forEach((button) => {
    const selected = button.dataset.view === (inWork ? "work" : view);
    button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  history.replaceState(null, "", view === "overview" ? "/" : `/#${apiTarget || view}`);
  window.scrollTo({ top: 0, behavior: "auto" });
  if (view === 'api' && !apiTarget) markApiChapter('api-start');
  if (apiTarget) requestAnimationFrame(() => {
    if (state.view === 'api' && location.hash === '#' + apiTarget) focusApiSection(apiTarget);
  });
  if (view === 'projects') lastProjectCountsRead = 0;
  if (state.snapshot) renderVisibleSnapshot(true);
  scheduleRecentTasks(true);
  syncVisiblePage(view, false);
  refresh({background: !!state.snapshotReceivedAt});
}

// The Windows app and browser display the same workbench. Each cycle only
// reads the open page; sending, checking credentials and retrying stay explicit.
let recordsTab = 'flow';
let geminiRecordLoad = null;
async function refreshGeminiRecords() {
  if (geminiRecordLoad) return geminiRecordLoad;
  const summary=$('geminiRecordSummary');
  const table=$('geminiRecordTable');
  geminiRecordLoad=(async()=>{
    try {
      const showChecks=$('geminiShowChecks')?.checked === true;
      const data=await getJson('/api/real-pool/activity'+(showChecks?'':'?include_checks=0'),{timeoutMs:8000});
      if (showChecks!==($('geminiShowChecks')?.checked === true)) return;
      if (!Array.isArray(data.items)) throw new Error('invalid records');
      const accounts=new Map((state.snapshot?.real_accounts?.items || []).map(
        a=>[(a.key || '').replace(/^flow-account-/,''),a.email || a.label]));
      const statuses={queued:'排队中',dispatching:'执行中',completed:'已完成',failed:'失败',unknown:'结果待核对',cancelled:'已取消'};
      summary.textContent=`最近 ${data.items.length} 条请求${showChecks?' · 含自查请求':''}`;
      const rows=data.items.map(r=>{
        const archived=r.status==='unknown' && String(r.note || '').includes('已归档');
        const elapsed=Number.isFinite(r.finished) && Number.isFinite(r.created) && r.finished>=r.created
          ? ((r.finished-r.created).toFixed(1)+' 秒') : '—';
        return `<tr><td>${esc(accounts.get(r.account_key) || '—')}</td><td>${esc(archived?'已归档 · 结果待核对':statuses[r.status] || '状态待核对')}</td><td>${esc(Number.isFinite(r.created)?dateTime(r.created):'—')}</td><td>${esc(elapsed)}</td><td><code>${esc(r.request_id || '—')}</code></td><td>${r.readiness_probe ? '<strong>自查请求</strong> · ' : ''}${esc(r.note || '—')}<details data-gemini-request="${esc(r.request_id || '')}"><summary>查看请求和返回</summary><div class="subtle">展开后读取详情。</div></details></td></tr>`;
      }).join('');
      const markup=rows ? `<table><thead><tr><th>账号</th><th>状态</th><th>提交时间</th><th>总耗时</th><th>请求编号</th><th>结果说明</th></tr></thead><tbody>${rows}</tbody></table>`
        : '<div class="empty">还没有 Gemini 请求记录。</div>';
      if (!table.querySelector('details[open]') && table.innerHTML!==markup) table.innerHTML=markup;
    } catch (_) {
      summary.textContent='Gemini 请求记录暂时无法读取。';
    }
  })().finally(()=>{geminiRecordLoad=null;});
  return geminiRecordLoad;
}
$('geminiRecordTable')?.addEventListener('toggle',async event=>{
  const panel=event.target;
  if (!panel.matches('details[data-gemini-request]') || !panel.open || panel.dataset.loading==='true') return;
  panel.dataset.loading='true';
  const body=panel.querySelector('div');
  body.textContent='正在读取…';
  try {
    const row=await getJson('/api/real-pool/activity/'+encodeURIComponent(panel.dataset.geminiRequest),{timeoutMs:8000});
    body.replaceChildren();
    for (const [label,value] of [['请求内容',row.prompt ?? '没有保存原请求内容。'],
      ['返回内容',row.response ?? '没有收到或保存回复。']]) {
      const heading=document.createElement('strong');
      const content=document.createElement('p');
      heading.textContent=label;
      content.style.whiteSpace='pre-wrap';
      content.style.overflowWrap='anywhere';
      content.textContent=value;
      body.append(heading,content);
    }
  } catch (_) {
    body.textContent='详情暂时无法读取，合上后可再展开重试。';
  } finally { delete panel.dataset.loading; }
},true);

$('geminiShowChecks')?.addEventListener('change',async ()=>{
  const table=$('geminiRecordTable');
  table.replaceChildren();
  $('geminiRecordSummary').textContent='正在读取…';
  if (geminiRecordLoad) await geminiRecordLoad;
  void refreshGeminiRecords();
});
let musicRecordLoad = null;
async function refreshMusicRecords() {
  if (musicRecordLoad) return musicRecordLoad;
  const summary = $('musicRecordSummary');
  const table = $('musicRecordTable');
  musicRecordLoad = (async () => {
    try {
      const {items} = await getJson('/api/music/records', {timeoutMs:8000});
      const statuses = {generating:'生成中', ingesting:'入库中', recovering:'正在找回结果',
        completed:'已完成', unknown:'结果待核对', failed:'失败'};
      const rows = items.map(row => {
        const downloads = row.assets.filter(asset => /^[0-9a-f]{64}$/.test(asset.id))
          .map((asset, index) => `<a class="text-link" href="/api/library/${asset.id}/file?download=1" download>${asset.kind === 'wav' ? 'WAV' : '音频'} ${index + 1}</a>`).join(' · ');
        const result = row.clips ? `${row.clips} 首 · 入库 ${row.assets.length} 件` : '—';
        const description = row.prompt.length > 80 ? row.prompt.slice(0, 80) + '…' : row.prompt;
        return `<tr><td title="${esc(row.prompt)}">${esc(description)}</td><td>${esc(row.model)}</td>
          <td>${esc(statuses[row.status] || '状态待核对')}</td><td>${esc(dateTime(row.created))}</td>
          <td><code>${esc(row.id)}</code></td><td>${result}${row.note ? '<br>' + esc(row.note) : ''}
          ${downloads ? '<br>' + downloads : ''}</td></tr>`;
      }).join('');
      const markup = rows ? `<table><thead><tr><th>描述</th><th>模型</th><th>状态</th><th>提交时间</th><th>请求编号</th><th>结果</th></tr></thead><tbody>${rows}</tbody></table>`
        : empty('还没有音乐生成记录');
      if (table.innerHTML !== markup) table.innerHTML = markup;
      summary.textContent = `最近 ${items.length} 条音乐请求`;
    } catch (_) {
      summary.textContent = '音乐生成记录暂时无法读取。';
    }
  })().finally(() => { musicRecordLoad = null; });
  return musicRecordLoad;
}

const recordsTabs = [
  ['flow','flowRecordsTab','taskRecords'],
  ['gemini','geminiRecordsTab','geminiRecords'],
  ['music','musicRecordsTab','musicRecords']
];

function selectRecordsTab(tab, focus=false) {
  recordsTab=tab;
  for (const [name,id,panel] of recordsTabs) {
    const selected=name===tab;
    const tabEl=$(id),panelEl=$(panel);
    if (!tabEl || !panelEl) continue;
    tabEl.setAttribute('aria-selected',String(selected));
    tabEl.tabIndex=selected?0:-1;
    panelEl.hidden=!selected;
    if (selected && focus) tabEl.focus();
  }
  if (tab==='gemini') void refreshGeminiRecords();
  if (tab==='music') void refreshMusicRecords();
}
for (const [index,[name,id]] of recordsTabs.entries()) {
  $(id)?.addEventListener('click',()=>selectRecordsTab(name));
  $(id)?.addEventListener('keydown',event=>{
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const last = recordsTabs.length - 1;
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? last
      : (index + (event.key === 'ArrowRight' ? 1 : last)) % recordsTabs.length;
    selectRecordsTab(recordsTabs[next][0],true);
  });
}

function syncVisiblePage(view, background) {
  if (view==='records' && recordsTab==='gemini') void refreshGeminiRecords();
  if (view==='records' && recordsTab==='music') void refreshMusicRecords();
  if (view==='music' || view==='overview') void refreshMusicStatus();
  const read = (key, fn) => {
    if (viewReads.has(key)) return;
    const pending = Promise.resolve().then(fn).catch(() => {}).finally(() => viewReads.delete(key));
    viewReads.set(key, pending);
  };
  if (['overview', 'accounts', 'conversations'].includes(view)) read('channel', refreshChannelStatus);
  if (view === 'overview' || view === 'accounts') {
    // Sync saved results from desktop checks, other tabs, and scheduled refreshes.
    if (!background || officialQuotaData?.refreshing || Date.now()-officialQuotaReadAt >= OFFICIAL_QUOTA_SYNC_MS)
      read('quota', refreshOfficialQuota);
    read('flowReadinessStatus', refreshFlowReadinessStatus);
    if (view==='overview' && Date.now()-musicStatusReadAt>=60000) read('musicStatus', refreshMusicStatus);
  }
  if (view === 'overview') {
    read('concurrency', refreshDispatchConcurrency);
    read('geminiConcurrency', refreshGeminiConcurrency);
    read('serviceActivation', refreshServiceActivations);
    read('governance', refreshGovernanceCases);
  }
  if (view === 'accounts') {
    read('governance', refreshGovernanceCases);
    read('accountActivity', () => refreshGeminiActivity({background}));
    read('activation', refreshActivation);
    if (!activationScheduleSaving) read('activationSchedule', refreshActivationSchedule);
    if (!activationPollSaving) read('activationPoll', refreshActivationPoll);
  }
  if (view === 'projects') {
    read('projectActions', loadProjectActions);
    if (Date.now() - lastProjectCountsRead >= 30000) read('projectMaterials', loadProjectMaterialCounts);
  }
  if (view === 'conversations') read('conversations', () => refreshConversationView());
  if (view === 'library') {
    read('library', () => refreshLibraryView({background}));
    if (!autoMaterialSaving) read('libraryAutoAnalysis', () => loadAutoMaterialState({background}));
    if (!backfillBusy) read('libraryBackfill', () => loadBackfillState({background}));
    if ($('materialActivityPanel').open) read('materialActivity', () => loadMaterialActivity(materialActivityPage, {background: true}));
    if ($('libraryDetailPanel').open) read('materialDetail', refreshOpenMaterial);
    if ($('libraryStorageDialog').open && !$('libraryStoragePlan').disabled)
      read('libraryStorage', () => showLibraryStorage({background: true}));
  }
  if (view === 'api') read('apiKeys', () => loadApiKeys({background}));
}

function snapshotRefreshDelay() {
  if (document.hidden) return 15000;
  if (state.view === 'projects' && [...projectActions.values()].some(x => x.status === 'running')) return 3000;
  return ['conversations', 'library', 'api'].includes(state.view) ? 10000 : SNAPSHOT_REFRESH_MS;
}

function scheduleSnapshotRefresh() {
  if (snapshotRefreshTimer) clearTimeout(snapshotRefreshTimer);
  snapshotRefreshTimer = null;
  if (!snapshotRefreshInFlight) {
    snapshotRefreshTimer = setTimeout(() => {
      snapshotRefreshTimer = null;
      refresh({background: true});
    }, snapshotRefreshDelay());
  }
}

function wakeSnapshotRefresh() {
  syncVisiblePage(state.view, true);
  refresh({background: true});
}

function followWorkbenchReleases() {
  const loaded = document.querySelector('meta[name="workbench-revision"]')?.content;
  if (!loaded || typeof EventSource !== 'function') return;
  const notice = $('frontendUpdateStatus');
  let pending = '';
  let reloading = false;
  function applyWhenSafe() {
    notice.hidden = !pending;
    if (!pending || reloading || document.hidden || !document.hasFocus() ||
        !['overview','accounts','projects','records'].includes(state.view) || document.querySelector('dialog[open]') ||
        accountActionsInFlight.size || dispatchPolicyDirty ||
        ['flowPrompt', 'chatPrompt', 'geminiPrompt', 'apiKeyName', 'musicPrompt', 'musicTags', 'musicStyleId'].some(id => $(id)?.value) ||
        ['chatFiles', 'geminiFiles'].some(id => $(id)?.files?.length)) return;
    reloading = true;
    window.location.reload();
  }
  function connect() {
    const source = new EventSource('/api/frontend/events');
    source.addEventListener('frontend', event => {
      if (!/^[a-f0-9]{64}$/.test(event.data)) return;
      pending = event.data === loaded ? '' : event.data;
      applyWhenSafe();
    });
    source.addEventListener('heartbeat', applyWhenSafe);
    source.onerror = () => {
      // EventSource retries interrupted streams, but not rejected connections.
      if (source.readyState === EventSource.CLOSED) setTimeout(connect, 15000);
    };
  }
  connect();
}

document.addEventListener('visibilitychange', () => {
  scheduleSnapshotRefresh();
  scheduleRecentTasks(!document.hidden);
  if (!document.hidden) wakeSnapshotRefresh();
});
window.addEventListener('focus', () => { wakeSnapshotRefresh(); scheduleRecentTasks(true); });
window.addEventListener('pageshow', () => scheduleRecentTasks(true));
window.addEventListener('online', () => scheduleRecentTasks(true));
window.addEventListener('hashchange', () => {
  const view = location.hash.slice(1) || 'overview';
  if (view !== state.view) setView(view);
});
window.addEventListener('pageshow', wakeSnapshotRefresh);
window.addEventListener('online', wakeSnapshotRefresh);
window.addEventListener('offline', () => { showConnectionState(false); $('lastUpdated').textContent = '连接中断 · 保留已读取的数据'; });

function renderOverview() {
  const snapshot = state.snapshot;
  renderOverviewCharts(snapshot);
  renderOfficialQuota();
  renderAccountInsights(snapshot, $('overviewInsights'));
  const flowAudit=governanceAudits.flow;
  if (snapshot?.flow_state?.available && !snapshot.flow_state.simulated && !flowAudit?.running &&
      (!flowAudit || Date.now()-(flowAudit.startedAt || 0)>60000)) void startGovernanceAudit('flow',false);
  const dataNotice = snapshot?.flow_state?.simulated ? "当前来源不是正式后台，不能作为真实账号或任务使用。" : "Flow 保留原后台；Gemini 使用官方调用。新对话按号池优先顺序分配，多轮固定账号。";
  $("sourceNotice").textContent = dataNotice + (snapshot?.flow_state?.skipped_projects || snapshot?.flow_state?.skipped_tasks ? " 有格式异常的记录未显示，请在原后台查看。" : "");
}

function overviewDistribution(target, title, caption, parts, displayedTotal = null) {
  const known = parts.every(part => Number.isInteger(part.value) && part.value >= 0);
  const total = known ? parts.reduce((sum, part) => sum + part.value, 0) : 0;
  const list = known ? parts.map(part => `<div><span><i class="chart-dot ${part.color}" aria-hidden="true"></i>${esc(part.label)}</span><strong>${part.value.toLocaleString('zh-CN')}</strong></div>`).join('') : '<p class="chart-unavailable">暂时无法读取数据</p>';
  const bar = total ? parts.map(part => `<i class="${part.color}" data-chart-width="${(part.value / total * 100).toFixed(2)}%"></i>`).join('') : '';
  const count = displayedTotal ?? (known ? total : null);
  target.innerHTML = `<div class="overview-chart-header"><h3>${esc(title)}</h3><strong>${count?.toLocaleString('zh-CN') ?? '—'}</strong></div><p>${esc(caption)}</p><div class="overview-chart-track" aria-hidden="true">${bar}</div><div class="overview-chart-legend">${list}</div>`;
  for (const segment of target.querySelectorAll('[data-chart-width]')) {
    segment.style.width = segment.dataset.chartWidth;
  }
}

function renderOverviewCharts(snapshot) {
  const available = snapshot?.flow_state?.available === true && Array.isArray(snapshot?.tasks);
  const tasks = available ? snapshot.tasks.filter(item => !schedulerTaskKinds.has(item.kind)) : [];
  overviewDistribution($('overviewTaskChart'), '任务分布', '最多显示最近 150 条创作任务', [
    {label:'已完成', color:'chart-green', value: available ? tasks.filter(x => x.status === 'succeeded').length : null},
    {label:'失败', color:'chart-rust', value: available ? tasks.filter(x => x.status === 'failed').length : null},
    {label:'其他状态', color:'chart-slate', value: available ? tasks.filter(x => !['succeeded','failed'].includes(x.status)).length : null},
  ]);
  const counts = snapshot?.library?.available ? snapshot.library.counts : null;
  const complete = counts && Number.isInteger(counts.total) && Number.isInteger(counts.images) && Number.isInteger(counts.videos) && counts.total >= counts.images + counts.videos;
  overviewDistribution($('overviewMaterialChart'), '素材分布', counts ? '素材库中已登记的文件' : (snapshot?.library?.message || '暂时无法读取素材库'), [
    {label:'图片', color:'chart-green', value: complete ? counts.images : null},
    {label:'视频', color:'chart-blue', value: complete ? counts.videos : null},
    {label:'其他', color:'chart-slate', value: complete ? counts.total - counts.images - counts.videos : null},
  ], Number.isInteger(counts?.total) ? counts.total : null);
}

async function loadProjectMaterialCounts() {
  if (projectMaterialLoading) return;
  projectMaterialLoading = true;
  lastProjectCountsRead = Date.now();
  try {
    const counts = new Map(), seen = new Set();
    let expectedTotal = null, expectedPages = null;
    for (let page = 1; page <= 100; page++) {
      const data = await getJson(`/api/library?page=${page}&limit=100`, {timeoutMs: 15000});
      if (!Array.isArray(data.items) || !Number.isInteger(data.total) || data.total < 0 ||
          !Number.isInteger(data.pages) || data.pages < 1 || page > data.pages ||
          (expectedTotal !== null && (data.total !== expectedTotal || data.pages !== expectedPages))) throw new Error('incomplete');
      if (expectedTotal === null) { expectedTotal = data.total; expectedPages = data.pages; }
      for (const item of data.items) {
        if (!item || !/^[0-9a-f]{64}$/.test(item.id) || seen.has(item.id) || !Array.isArray(item.origins)) throw new Error('incomplete');
        seen.add(item.id);
        const projects = new Set(item.origins.map(o => o?.project_id).filter(id => typeof id === 'string' && id));
        for (const id of projects) counts.set(id, (counts.get(id) || 0) + 1);
      }
      if (page === expectedPages) {
        if (seen.size !== expectedTotal) throw new Error('incomplete');
        projectMaterialCounts = counts;
        break;
      }
    }
  } catch (_) { /* Keep the last complete count. */ }
  finally { projectMaterialLoading = false; if (state.view === 'projects') renderProjects(); }
}

async function loadProjectActions() {
  if (projectActionsLoading) return;
  projectActionsLoading = true;
  try {
    const data = await getJson('/api/project-actions', {timeoutMs:10000});
    if (!Array.isArray(data.items)) throw new Error('invalid');
    const next = new Map();
    for (const item of data.items) if (item?.project_key && !next.has(item.project_key)) next.set(item.project_key,item);
    const finished = [...next.entries()].some(([key,value]) => value.status !== 'running' && projectActions.get(key)?.id === value.id && projectActions.get(key)?.status === 'running');
    for (const [key,requestId] of projectActionsPending) if (next.get(key)?.id === requestId) projectActionsPending.delete(key);
    projectActions = next;
    if (state.view === 'projects') renderProjects();
    if (finished) { refresh({background:true}); loadProjectMaterialCounts(); }
  } catch (_) { /* The next sync reads again. */ }
  finally { projectActionsLoading = false; }
}

async function startProjectAction(key,kind) {
  const project = (state.snapshot?.projects || []).find(p => p.key === key);
  if (!project || projectActionsPending.has(key) || projectActions.get(key)?.status === 'running') return;
  if (kind === 'delete' && !confirm(`确定删除云端项目“${project.title}”吗？项目内尚未保存的素材和编辑内容也会删除，素材库中已保存的文件会保留。如它是默认或备用项目，对应设置也会清除。`)) return;
  const requestId=crypto.randomUUID();
  projectActionsPending.set(key,requestId); renderProjects();
  try {
    await postJson('/api/project-actions',{project_key:key,kind},requestId);
    await loadProjectActions();
  } catch (error) {
    if (error.status) {
      projectActionsPending.delete(key);
      projectActions.set(key,{status:'failed',detail:{message:error.message}});
    } else {
      projectActions.set(key,{status:'unknown',detail:{message:'提交结果待核对，请勿重复操作。'}});
    }
    renderProjects();
  }
}

function renderProjects() {
  const snapshot = state.snapshot;
  if (!snapshot?.flow_state?.available) {
    lastProjectMarkup = null;
    $("projectListSummary").textContent = "暂时无法读取项目";
    $("projectTable").innerHTML = empty("项目暂不可读取", snapshot?.flow_state?.message || "请检查 Flow 后台连接。");
    return;
  }
  const query = $("projectSearch").value.trim().toLocaleLowerCase("zh-CN");
  const accounts = new Map((snapshot.real_accounts?.items || []).map((account) => [account.profile, account.email || account.label || ""]));
  const accountName = (item) => accounts.get(item.source_profile) || "账号名称未获取";
  const taskByProject = new Map();
  for (const task of snapshot.tasks || []) {
    if (!task.project_key) continue;
    const current = taskByProject.get(task.project_key) || { last: 0, active: false };
    const last = Math.max(Number(task.created) || 0, Number(task.finished) || 0);
    current.last = Math.max(current.last, last);
    current.active ||= ['queued', 'running'].includes(task.status);
    taskByProject.set(task.project_key, current);
  }
  const projects = snapshot.projects.filter((item) => [item.title, item.source_id, item.source_profile, accountName(item)].some((value) => value.toLocaleLowerCase("zh-CN").includes(query)));
  $("projectListSummary").textContent = (query ? `找到 ${projects.length} 项，共 ${snapshot.projects.length} 项` : `共 ${snapshot.projects.length} 项 · 保留原始编号与来源`) + (snapshot.flow_state.skipped_projects ? ` · ${snapshot.flow_state.skipped_projects} 项格式异常` : "");
  if (!projects.length) {
    lastProjectMarkup = null;
    $("projectTable").innerHTML = empty(query ? "没有找到符合条件的项目" : "还没有已登记项目", query ? "试试其他名称或编号。" : "请从原后台读取或登记项目。");
    return;
  }
  const markup = `<table><thead><tr><th>项目名称</th><th>所属账号</th><th>最近算力中心任务</th><th>素材入库记录</th><th>生命周期状态</th><th>操作</th></tr></thead><tbody>${projects.map((item) => {
    const task = taskByProject.get(item.key);
    const activity = task?.last ? dateTime(task.last) : '无算力中心任务记录';
    const action = projectActions.get(item.key);
    const detail = action?.detail || {};
    const progress = action?.kind === 'transfer' && Number.isFinite(detail.total) ? `已识别 ${detail.total} · 新增 ${detail.added || 0} · 已有 ${detail.skipped || 0} · 失败 ${detail.failed || 0}${Number.isFinite(detail.tag_queued) ? ` · 标签排队 ${detail.tag_queued}` : ''}` : '';
    const busy = projectActionsPending.has(item.key) || action?.status === 'running' || (action?.kind === 'delete' && action?.status === 'unknown');
    const status = task?.active ? '任务进行中' : '待核对';
    const material = projectMaterialCounts === null ? (projectMaterialLoading ? '读取中' : '暂不可读取') : `已登记 ${projectMaterialCounts.get(item.source_id) || 0} 份`;
    return `<tr><td class="table-title"><strong>${esc(item.title)}</strong><br><span class="subtle" title="完整项目编号">${esc(item.source_id)}</span>${item.title_source === 'saved_flow_name' ? `<br><span class="subtle">${item.title_origin === 'browser_history' ? '浏览器历史名称 · 最后访问于' : 'Flow 名称 · 核对于'} ${esc(dateTime(item.title_observed))}</span>` : item.title === "未命名项目" ? '<br><span class="subtle">原后台未提供可确认的名称</span>' : ''}</td><td>${esc(accountName(item))}</td><td>${esc(activity)}<br><span class="subtle">不含网页内其他操作</span></td><td title="登记数量不代表云端素材已全部入库">${esc(material)}<br><span class="subtle">完整性待核对</span></td><td><span class="source-chip" title="需要核对项目时间和全部云端素材是否已保存">${status}</span>${detail.message ? `<br><span class="subtle">${esc(detail.message)}</span>` : ''}${progress ? `<br><span class="subtle">${esc(progress)}</span>` : ''}${detail.tag_message ? `<br><span class="subtle">${esc(detail.tag_message)}</span>` : ''}</td><td><button class="button button-light" type="button" data-project-action="transfer" data-project-key="${esc(item.key)}" title="只处理未入库的素材；新增素材将排队使用 Gemini 打标签" ${busy?'disabled':''}>转移资产到素材库</button> <button class="button button-light" type="button" data-project-action="delete" data-project-key="${esc(item.key)}" ${busy||task?.active?'disabled':''}>删除项目</button></td></tr>`;
  }).join("")}</tbody></table>`;
  if (lastProjectMarkup !== markup) {
    $("projectTable").innerHTML = markup;
    lastProjectMarkup = markup;
  }
}

function taskFailureMarkup(item) {
  const labels = {task_window_binding_created:'建立账号、网页与桌面窗口的唯一绑定',task_window_binding_reused:'重新核对并复用固定窗口绑定',task_window_binding_repaired:'恢复变化后的窗口绑定',task_preparation_started:'开始准备',fixed_window_reused:'复用账号固定窗口',fixed_window_created:'建立账号固定窗口',
    preparation_recovery:'尝试恢复页面',preparation_recovered:'页面恢复成功',project_page_reload:'重新加载原项目页面',flow_page_activated:'已激活 Flow 项目页',desktop_overview_dismissed:'收起桌面总览，恢复浏览器前台',
    input_preparation_recovery:'恢复窗口焦点与输入准备',paste_started:'开始单次粘贴',input_preparation_stopped:'输入准备停止，未重复粘贴',
    original_image_result_lookup:'查询原图片结果',original_video_address_lookup:'补取原视频地址',
    original_video_grid_download:'从项目列表下载原片',original_download_retry:'重新下载原文件',original_download_recovered:'原文件下载恢复成功',refresh_original_download_address:'刷新原作品下载地址',window_retained:'保留固定窗口'};
  labels.login_page_recheck='重新核对原账号登录页面';
  labels.confirmed_rejection_released='核对原提交已被拒绝，释放旧任务占用';
  const events=(item.self_recovery || []).filter(e=>labels[e.action]);
  const reason = item.failure_reason || '';
  const explanation = /PUBLIC_ERROR_UNUSUAL_ACTIVITY/i.test(reason)
    ? 'Flow 拒绝本次生成：检测到异常活动。请在账号页查看当前限制。'
    : reason || '后台未提供具体失败原因。';
  const failure=item.status==='failed' ? `<span class="task-failure-reason">失败原因：${esc(explanation)}</span>` : '';
  return failure + (events.length ? `<details class="task-recovery-record"><summary>调度与自救记录（${events.length}）</summary><ul>${events.map(e=>`<li>${esc(e.created ? dateTime(e.created) : '')} ${esc(labels[e.action])}</li>`).join('')}</ul></details>` : '');
}

function renderTaskWindows() {
  const table = $('taskWindowsTable');
  const summary = $('taskWindowsSummary');
  if (!table || !summary) return;
  const snapshot = state.snapshot;
  const coordination = snapshot?.flow_coordination;
  if (!snapshot?.flow_state?.available || !coordination || coordination.window_bindings_available !== true) {
    summary.textContent = '窗口绑定暂不可读取';
    table.innerHTML = empty('窗口状态待核对', '读取失败不会解除账号限制或重新发送任务。');
    return;
  }
  const bindings = new Map((coordination.window_bindings || []).map(b => [b.profile, b]));
  const windows = new Map((coordination.windows || []).map(w => [w.profile, w]));
  const lifecycle = new Map((coordination.window_lifecycle || []).map(w => [w.profile, w]));
  const accounts = snapshot.real_accounts?.items || [];
  const labels = {idle:'空闲',preparing:'准备中',submitting:'发送核对中',submitted:'等待结果',generated:'取回成果',result_unknown:'原任务待核对',blocked_draft:'保留草稿',needs_verification:'需要用户登录'};
  const verified = accounts.filter(a => lifecycle.get(a.profile)?.unique_ok === 1).length;
  const actual = [...lifecycle.values()].reduce((sum,w) => sum + (w.flow_windows || 0),0);
  const inventoryText = coordination.window_lifecycle_available === true && lifecycle.size ? `上次盘点 ${actual} 扇 Flow 窗口 · ${verified} 个窗口唯一性达标` : '实际窗口尚未盘点或暂不可读取';
  summary.textContent = `${accounts.length} 个账号 · ${inventoryText} · 空闲30分钟保活`;
  const outcomes = {completed:'保活完成',cooling_read_only:'冷却中，仅检查状态',tasks_have_priority:'任务优先，已避让',account_busy:'账号正在使用',account_paused:'账号已暂停',window_not_idle:'原窗口状态待核对',window_not_unique:'窗口未唯一',manual_login_open:'人工登录中',foreground_busy:'前台正被使用',not_due:'尚未到期',maintenance:'维护期间暂缓',account_not_verified:'账号需核对',flow_window_protected:'页面有需保留内容',keepalive_safe_area_unconfirmed:'未确认安全交互区域',desktop_focus_unconfirmed:'前台焦点未确认'};
  table.innerHTML = accounts.length ? `<table><thead><tr><th>账号</th><th>实际窗口 / 页面</th><th>任务窗口与项目</th><th>任务状态</th><th>空闲保活</th></tr></thead><tbody>${accounts.map(a => {
    const binding = bindings.get(a.profile);
    const taskWindow = windows.get(a.profile);
    const life = lifecycle.get(a.profile);
    const project = binding && (snapshot.projects || []).find(p => p.source_profile === a.profile && binding.project_url === `https://flow.google.com/project/${p.source_id}`);
    const text = binding?.binding_state === 'verified' ? '上次绑定已核对' : binding ? '绑定需重新核对' : '尚未核对绑定';
    const count = life ? `${life.flow_windows} 扇 / ${life.flow_pages} 页 · ${life.unique_ok ? '唯一性达标' : '未达标'}` : '实际数量待盘点';
    const maintenance = life?.last_outcome ? (outcomes[life.last_outcome] || '保活未完成，已保留现场') : '等待首次空闲保活';
    return `<tr><td>${esc(a.email || a.label || a.profile)}</td><td>${esc(count)}${life?.protected ? '<br><span class="subtle">有内容需保护，未关闭</span>' : ''}${life?.observed ? `<br><span class="subtle">${esc(dateTime(life.observed))}</span>` : ''}</td><td>${esc(text)}${binding ? `<br><span class="subtle">固定窗口 ${esc(String(binding.window_id))}</span>` : ''}<br>${esc(project?.title || (binding ? '项目登记待核对' : '等待任务准备'))}</td><td>${esc(labels[taskWindow?.state] || '待准备')}${a.flow_cooling ? '<br><span class="subtle">账号冷却限制保留</span>' : ''}</td><td>${esc(maintenance)}${life?.last_keepalive ? `<br><span class="subtle">上次 ${esc(dateTime(life.last_keepalive))}</span>` : ''}${life?.next_due ? `<br><span class="subtle">下次不早于 ${esc(dateTime(life.next_due))}</span>` : ''}</td></tr>`;
  }).join('')}</tbody></table>` : empty('尚无账号窗口', '接入账号后，在发送前建立和核对窗口绑定。');
}

function retryAttemptsMarkup(item, byProfile) {
  const attempts = item.retry?.attempts;
  if (!Array.isArray(attempts) || !attempts.length) return '';
  return `<details class="task-recovery-record"><summary>原任务与重试记录（${attempts.length} 次尝试）</summary><ul>${attempts.map((a, index) => {
    const elapsed = a.started && a.finished ? ' · ' + (a.finished - a.started).toFixed(1) + ' 秒' : '';
    const reason = /PUBLIC_ERROR_UNUSUAL_ACTIVITY/i.test(a.failure_reason || '') ? 'Flow 检测到异常活动' : a.failure_reason;
    return `<li>${a.ordinal ? (a.profile === attempts[index - 1]?.profile ? '原账号重试 ' : '换号重试 ') + a.ordinal : '原尝试'} · ${esc(byProfile.get(a.profile) || a.profile || '账号待核对')} · ${esc(labels.status[a.status] || a.status)}${esc(elapsed)}${reason ? ' · 失败原因：' + esc(reason) : ''}</li>`;
  }).join('')}</ul></details>`;
}

function renderSchedulerTaskRecords(snapshot) {
  const table = $('accountSyncTable');
  const summary = $('accountSyncSummary');
  if (!table || !summary) return;
  const available = snapshot?.flow_state?.available && Array.isArray(snapshot.tasks);
  const rows = available ? (snapshot.scheduler_tasks || snapshot.tasks.filter(item => schedulerTaskKinds.has(item.kind))) : [];
  summary.textContent = available ? `共 ${rows.length} 条 · 自动更新` : '暂时无法读取账号检查记录';
  const accounts = new Map((snapshot?.real_accounts?.items || []).map(a => [a.profile, a.email || a.label || a.profile]));
  const markup = rows.length ? `<table><thead><tr><th>任务类型</th><th>账号</th><th>状态</th><th>提交时间</th><th>任务编号</th><th>结果</th></tr></thead><tbody>${rows.map(item =>
    `<tr class="task-kind-sync"><td>${esc(kindLabel(item.kind))}</td><td>${esc(accounts.get(item.source_profile) || item.source_profile)}</td><td>${statusBadge(item.status)}</td><td>${esc(dateTime(item.created))}</td><td><code title="${esc(item.source_id)}">${esc(item.source_id)}</code></td><td>${taskFailureMarkup(item) || (item.note ? esc(item.note) : '—')}</td></tr>`).join('')}</tbody></table>`
    : available ? empty('暂无账号检查记录', '账号同步与状态检查等记录会显示在这里。')
    : empty('账号检查记录暂不可读取', snapshot?.flow_state?.message || '请检查 Flow 后台连接。');
  if (table.innerHTML !== markup) table.innerHTML = markup;
}

function displayRecentTasks(data, live) {
  if (!data?.flow_state?.available || !Array.isArray(data.tasks)) throw new Error('任务记录暂不可读取');
  recentTasks = data;
  renderTasks();
  $('taskLiveStatus').textContent = (live ? '实时更新' : '自动更新 · 实时连接恢复中') +
    ' · ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
}

function closeRecentTaskStream() {
  recentTaskStream?.close();
  recentTaskStream = null;
  taskStreamHealthy = false;
  clearTimeout(taskHeartbeat);
}

function connectRecentTasks() {
  if (recentTaskStream || typeof EventSource !== 'function') return;
  const source = new EventSource('/api/recent-tasks/events');
  recentTaskStream = source;
  const heartbeat = () => {
    clearTimeout(taskHeartbeat);
    taskHeartbeat = setTimeout(() => {
      if (recentTaskStream !== source) return;
      closeRecentTaskStream();
      scheduleRecentTasks(true);
    }, 25000);
  };
  heartbeat();
  source.addEventListener('heartbeat', heartbeat);
  source.addEventListener('tasks', event => {
    if (recentTaskStream !== source || state.view !== 'records') return;
    heartbeat();
    try {
      displayRecentTasks(JSON.parse(event.data), true);
      ++taskPushRevision;
      taskStreamHealthy = true;
      clearTimeout(recentTasksTimer);
      recentTasksTimer = null;
    } catch (_) { taskStreamHealthy = false; scheduleRecentTasks(); }
  });
  source.addEventListener('unavailable', () => {
    if (recentTaskStream !== source) return;
    heartbeat();
    taskStreamHealthy = false;
    scheduleRecentTasks();
  });
  source.onerror = () => {
    if (recentTaskStream !== source) return;
    taskStreamHealthy = false;
    if (source.readyState === EventSource.CLOSED) closeRecentTaskStream();
    scheduleRecentTasks();
  };
}

function scheduleRecentTasks(immediate = false) {
  if (immediate || state.view !== 'records' || document.hidden) {
    clearTimeout(recentTasksTimer);
    recentTasksTimer = null;
  }
  if (state.view !== 'records' || document.hidden) {
    closeRecentTaskStream();
    return;
  }
  if (immediate) connectRecentTasks();
  if (taskStreamHealthy || recentTasksReading || recentTasksTimer) return;
  if (immediate) refreshRecentTasks();
  else recentTasksTimer = setTimeout(() => {
    recentTasksTimer = null;
    connectRecentTasks();
    refreshRecentTasks();
  }, 3500);
}

function refreshRecentTasks() {
  if (recentTasksReading || state.view !== 'records' || document.hidden) return recentTasksReading;
  const revision = taskPushRevision;
  recentTasksReading = getJson('/api/recent-tasks', {timeoutMs: 4500}).then(data => {
    if (revision === taskPushRevision && state.view === 'records') displayRecentTasks(data, taskStreamHealthy);
  }).catch(() => {
    if (!taskStreamHealthy && state.view === 'records') $('taskLiveStatus').textContent = '任务同步暂缓，保留上次记录，恢复后自动更新';
  }).finally(() => {
    recentTasksReading = null;
    scheduleRecentTasks();
  });
  return recentTasksReading;
}

function renderTasks() {
  const snapshot = recentTasks || state.snapshot;
  if ($('taskWindowsPanel')?.open) renderSchedulerTaskRecords(snapshot);
  if (!snapshot?.flow_state?.available) {
    $("taskListSummary").textContent = "暂时无法读取任务";
    $("taskTable").innerHTML = empty("任务暂不可读取", snapshot?.flow_state?.message || "请检查 Flow 后台连接。");
    return;
  }
  const query = $("taskSearch").value.trim().toLocaleLowerCase("zh-CN");
  const status = $("taskStatusFilter").value;
  const kindFilter = $("taskKindFilter");
  const selectedKind = schedulerTaskKinds.has(kindFilter.value.slice(5)) ? '' : kindFilter.value;
  const productionRows = snapshot.tasks.filter(item => !schedulerTaskKinds.has(item.kind));
  const kinds = [...new Set(productionRows.map((item) => String(item.kind || "")))];
  if (selectedKind && !kinds.includes(selectedKind.slice(5))) kinds.push(selectedKind.slice(5));
  kinds.sort((a, b) => kindLabel(a).localeCompare(kindLabel(b), "zh-CN"));
  const options = [['全部类型', ''], ...kinds.map(kind => [kindLabel(kind), `kind:${kind}`])];
  if (options.length !== kindFilter.options.length ||
      options.some(([label, value], i) => kindFilter.options[i].text !== label || kindFilter.options[i].value !== value)) {
    kindFilter.replaceChildren(...options.map(([label, value]) => new Option(label, value)));
    kindFilter.value = selectedKind;
  }
  const tasks = productionRows.filter((item) => (!status || item.status === status) && (!selectedKind || `kind:${item.kind || ""}` === selectedKind) && [item.project_title, item.source_id, item.retry?.root_id || "", kindLabel(item.kind), ...(item.retry?.attempts || []).map(a => a.id || "")].some((value) => value.toLocaleLowerCase("zh-CN").includes(query)));
  const filtered = query || status || selectedKind;
  const reset = $("taskFiltersReset");
  if (reset) reset.disabled = !filtered;
  $("taskListSummary").textContent = (filtered ? `找到 ${tasks.length} 条，共 ${productionRows.length} 条` : `共 ${productionRows.length} 条 · 后台检查见 Flow 页的「账号检查记录」`) + (snapshot.flow_state.skipped_tasks ? ` · ${snapshot.flow_state.skipped_tasks} 条格式异常` : "");
  if(snapshot.flow_coordination?.window_policy==='one_per_account') {
    const held=(snapshot.flow_coordination.windows || []).filter(w=>['submitting','submitted','generated','result_unknown'].includes(w.state)).length;
    $('taskListSummary').textContent += ` · 每账号固定窗口、同账号排队 · 单图和单视频可换号重试，最多 3 次${held ? ` · ${held} 个窗口处理中或待核对` : ''}`;
  }
  if (!tasks.length) {
    $("taskTable").innerHTML = empty(filtered ? "没有找到符合条件的任务" : "暂时没有任务记录", filtered ? "可调整搜索内容、任务类型或状态。" : "新任务仍通过原后台创建。");
    return;
  }
  const byProfile = new Map((snapshot.real_accounts?.items || []).map(a => [a.profile, a.email || a.label || a.profile]));
  const accounts = new Map((snapshot.real_accounts?.items || []).map(a => [a.profile, a]));
  const reasons = new Map(tasks.map(item => [item.source_id, flowQueueReason(item, snapshot, accounts.get(item.source_profile))]));
  const taskMarkup = `<table><thead><tr><th>任务类型</th><th>项目 / 账号</th><th>状态</th><th>提交时间</th><th>任务编号</th><th>结果</th><th>来源</th></tr></thead><tbody>${tasks.map((item) => `
    <tr class="task-kind-${["image", "video", "quote", "sync", "session_import", "check"].includes(item.kind) ? item.kind : "other"}"><td><strong>${esc(kindLabel(item.kind))}</strong>${typeof item.style_id === "string" ? `<br><span class="subtle">画风 ID：<code style="white-space:pre-wrap">${esc(item.style_id)}</code></span>` : ""}${item.retry ? `<br><span class="subtle">${item.retry.attempts?.length > 1 ? `已重试 ${item.retry.attempts.length - 1} 次` : "原任务"}</span>` : ''}</td><td class="table-title">${esc(item.project_title)}<br><span class="subtle">${esc(byProfile.get(item.source_profile) || item.source_profile)}</span></td><td>${flowTaskStatusBadge(item)}${reasons.get(item.source_id) ? '<br><span class="subtle">' + esc(reasons.get(item.source_id)) + '</span>' : ''}${item.note ? `<br><span class="subtle">${esc(item.note.replace('换号重试已完成','重试已完成'))}</span>` : ""}</td><td>${esc(dateTime(item.created))}</td><td><code title="${esc(item.source_id)}">${esc(shortId(item.source_id))}</code>${item.retry && item.retry.ordinal ? `<br><span class="subtle" title="${esc(item.retry.root_id)}">原任务 ${esc(shortId(item.retry.root_id))}</span>` : ''}</td><td>${taskFailureMarkup(item)}${retryAttemptsMarkup(item, byProfile)}${Number.isInteger(item.manual_retry_ordinal) ? `<button class="text-link" type="button" data-retry-flow="${esc(item.source_id)}" data-retry-ordinal="${item.manual_retry_ordinal}">原任务重试</button>` : ""}${(item.downloads || []).map((file, index) => `<a class="text-link" href="http://127.0.0.1:18888${esc(file.path)}" target="_blank" rel="noopener noreferrer">下载${index + 1}</a>`).join(" ")}${item.recovered_path ? `<a class="text-link" href="${esc(item.recovered_path)}" target="_blank" rel="noopener noreferrer">打开已找回图片</a>` : ""}${item.recoverable ? `<button class="text-link" type="button" data-recover-flow="${esc(item.source_id)}">找回已生成图片</button>` : ""}${item.status !== "failed" && !(item.downloads || []).length && !item.recovered_path && !item.recoverable ? "—" : ""}</td><td><span class="source-chip">Flow</span></td></tr>`).join("")}</tbody></table>`;
  // Keep opened task details and keyboard focus during an unchanged refresh.
  const table = $("taskTable");
  if (lastTaskMarkup !== taskMarkup || !table.querySelector('table')) {
    const expanded = new Set([...table.querySelectorAll('details[open]')]
      .map(detail => detail.closest('tr')?.querySelector('code[title]')?.title).filter(Boolean));
    table.innerHTML = taskMarkup;
    if (expanded.size) table.querySelectorAll('details.task-recovery-record').forEach(detail => {
      detail.open = expanded.has(detail.closest('tr')?.querySelector('code[title]')?.title);
    });
    lastTaskMarkup = taskMarkup;
  }
}

const flowRetriesInFlight = new Set();
$("taskTable").addEventListener("click", async (event) => {
  const retry = event.target.closest('[data-retry-flow]');
  if (retry) {
    const ident = retry.dataset.retryFlow;
    if (flowRetriesInFlight.has(ident) ||
        !window.confirm(`使用原账号和项目重试任务 ${ident}？任务编号保留，生成可能消耗积分。`)) return;
    flowRetriesInFlight.add(ident);
    retry.disabled = true;
    retry.textContent = '正在安排重试…';
    try {
      const result = await postJson(`/api/flow/tasks/${encodeURIComponent(ident)}/retry`,
        {expected_ordinal:Number(retry.dataset.retryOrdinal),confirm_generation:true});
      if (result.task_id !== ident) throw new Error('重试结果待核对，请刷新原任务记录。');
      retry.textContent = '已安排重试';
      await refresh();
    } catch (error) {
      retry.textContent = error.message || '重试未受理，请核对原任务。';
      retry.disabled = false;
    } finally {
      flowRetriesInFlight.delete(ident);
    }
    return;
  }
  const button = event.target.closest("[data-recover-flow]");
  if (!button) return;
  button.disabled = true;
  button.textContent = "正在找回…";
  try {
    const response = await fetch(`/api/real-pool/recoveries/${encodeURIComponent(button.dataset.recoverFlow)}`, {
      method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-Workbench-Request": "1"}, body: "{}"
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "暂时无法找回。 ");
    await refresh();
  } catch (error) {
    button.textContent = "找回失败，可再尝试";
    button.disabled = false;
  }
});

// Host shutdown: two clicks within six seconds, then the host powers off.
let hostShutdownArmed = null;
function disarmHostShutdown() {
  if (hostShutdownArmed) clearTimeout(hostShutdownArmed);
  hostShutdownArmed = null;
  const button = $("hostShutdown");
  button.textContent = "关闭主机";
  button.classList.remove("is-armed");
}
$("hostShutdown")?.addEventListener("click", async () => {
  const button = $("hostShutdown");
  const status = $("hostShutdownStatus");
  const note = (text) => { if (status) status.textContent = text; };
  if (hostShutdownArmed) {
    disarmHostShutdown();
    button.disabled = true;
    button.textContent = "正在关机…";
    note("关机指令发送中…");
    try {
      const response = await fetch("/api/host-shutdown", {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Workbench-Request": "1"},
        body: JSON.stringify({confirm: true})
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || "关机指令未受理。");
      button.textContent = "关机指令已发出";
      note("主机即将关闭；面板连接会随之下线，开机后刷新本页即可恢复。");
    } catch (error) {
      note(error.message || "关机指令未受理。");
      button.disabled = false;
    }
    return;
  }
  button.textContent = "再点一次确认关机";
  button.classList.add("is-armed");
  note("再次点击才会真正关机。");
  hostShutdownArmed = setTimeout(() => {
    hostShutdownArmed = null;
    button.textContent = "关闭主机";
    button.classList.remove("is-armed");
    note("");
  }, 6000);
});

function flowGovernance(snapshot, fresh = state.snapshotFresh, verifiedEmpty = new Set(), probeStartedAt = 0, pageStates = {}) {
  const accounts=(snapshot?.real_accounts?.items || []).filter(account=>!account.flow_removed);
  const c=snapshot?.flow_coordination;
  const known=fresh && snapshot?.flow_state?.available === true && snapshot?.real_accounts?.available === true;
  const shared=[];
  if (!known) shared.push({title:'Flow 状态暂不可读取',action:'检查算力中心连接，再重新读取。'});
  else if (c?.desktop_keyboard_ready !== true) shared.push({title:'Ubuntu 桌面键盘未就绪',action:'在 Ubuntu 保持桌面登录，若出现远程交互授权，请开启「允许远程交互」并点击「分享」，然后重新读取状态。'});
  const windows=new Map((c?.windows || []).map(w=>[w.profile,w]));
  const bindings=new Map((c?.window_bindings || []).map(w=>[w.profile,w]));
  const lifecycle=new Map((c?.window_lifecycle || []).map(w=>[w.profile,w]));
  const rows=known ? accounts.map(account => {
    const issues=accountIssues(account,snapshot,null,pageStates[account.profile]).filter(issue=>issue.key.startsWith('flow_'));
    const add=(key,title,action)=>issues.push({key,title,action});
    if (account.flow_login!=='verified' && !issues.some(i=>i.key==='flow_unknown'))
      add('flow_login','Flow 登录待核对','使用「检查授权与额度」核对；如提示登录，请在原账号的 Ubuntu 浏览器完成。');
    if (account.flow_risk_known!==true || account.flow_pacing_known!==true)
      add('flow_controls','暂停或冷却状态待核对','核对原账号状态。');
    if (account.flow_balance_fresh !== true || !Number.isFinite(account.flow_balance))
      add('flow_balance',verifiedEmpty.has(account.profile) ? '任务页已就绪，积分待核对' : account.flow_balance_error || '积分待核对',
        '等待后台重新读取积分，再复查就绪状态。');
    else if (account.flow_balance <= 0 && !issues.some(i=>i.key==='flow_empty'))
      add('flow_empty','Flow 积分不足','积分恢复后重新检查。');
    const window=windows.get(account.profile);
    if (!window || !['idle','preparing','submitting','submitted','generated','downloading','blocked_draft','result_unknown','needs_verification'].includes(window.state))
      add('flow_binding','任务窗口运行状态待核对','打开 Flow 页的「账号检查记录」核对固定窗口。');
    if (window?.state==='blocked_draft' && !(verifiedEmpty.has(account.profile) && window.updated <= probeStartedAt))
      add('flow_draft','固定任务窗口有草稿或附件','算力中心会保留草稿。处理后再检查就绪。');
    if (!issues.some(issue=>issue.key.startsWith('flow_window_') || issue.key==='flow_draft')) {
      const life=lifecycle.get(account.profile);
      if (c?.window_lifecycle_available!==true || life?.unique_ok!==1)
        add('flow_binding','任务窗口唯一性待核对','打开 Flow 页的「账号检查记录」核对原账号窗口，保留手动窗口和草稿。');
      if (c?.window_bindings_available!==true || bindings.get(account.profile)?.binding_state!=='verified')
        add('flow_binding','任务窗口绑定待核对','打开 Flow 页的「账号检查记录」查看账号、窗口和项目绑定。');
    }
    return {account,issues};
  }).filter(row=>row.issues.length) : [];
  const blocked = new Set(rows.map(row=>row.account.profile));
  const eligible = known && !shared.length ? accounts.filter(account=>!blocked.has(account.profile)) : [];
  return {rows,shared,eligible,ready:known ? eligible.length : null};
}

let flowReadinessObservation=null, flowReadinessReadAt=0, lastFlowReadiness=null;

function rememberFlowReadiness(pageChecks,dispatch) {
  if (!pageChecks?.accounts || !Number.isFinite(pageChecks.checked_at) || dispatch?.available!==true) return;
  if (flowReadinessObservation?.pageChecks.checked_at>pageChecks.checked_at) return;
  flowReadinessObservation={pageChecks,dispatch,receivedAt:performance.now()};
}

async function refreshFlowReadinessStatus() {
  if (Date.now()-flowReadinessReadAt<30000 || governanceAudits.flow?.running) return;
  flowReadinessReadAt=Date.now();
  const [pageChecks,dispatch]=await Promise.all([
    getJson('/api/flow-readiness',{timeoutMs:14000}),getJson('/api/dispatch/concurrency',{timeoutMs:8000})
  ]);
  rememberFlowReadiness(pageChecks,dispatch);
  autoRecoverFlowPages(pageChecks);
  if (state.view==='overview' && state.snapshot) renderAccountInsights(state.snapshot,$('overviewInsights'));
  if (state.view==='accounts') renderAccounts();
}

let autoFlowRecovery = null;
const autoFlowRecoveryAfter = new Map();
let autoFlowRecoveryProfiles = new Set();
function autoRecoverFlowPages(pageChecks) {
  if (autoFlowRecovery || !state.snapshotFresh) return;
  const candidates = Object.entries(pageChecks?.accounts || {}).filter(([profile, value]) => {
    const account = state.snapshot?.real_accounts?.items?.find(a => a.profile === profile);
    return Date.now() >= (autoFlowRecoveryAfter.get(profile) || 0) &&
      ['binding','project','flow_landing','editor','overlay','unconfirmed','unique'].includes(value) && !pageChecks?.review?.[profile] &&
      account?.flow_login === 'verified' && account?.flow_risk_paused !== true && account?.flow_cooling !== true;
  });
  if (!candidates.length) return;
  autoFlowRecoveryProfiles = new Set(candidates.map(([profile]) => profile));
  autoFlowRecovery = postJson('/api/flow-readiness/auto', {}).then(result => {
    if (!result || !result.accounts) return;
    for (const [profile] of candidates) {
      if (result.accounts[profile] !== 'busy') autoFlowRecoveryAfter.set(profile, Date.now() + 300000);
    }
    rememberFlowReadiness(result, flowReadinessObservation?.dispatch);
    if (state.view === 'accounts') renderAccounts();
  }).catch(() => {
    // Preserve the failure reason and wait for the bounded next recovery pass.
  }).finally(() => {
    autoFlowRecovery = null;
    autoFlowRecoveryProfiles.clear();
    if (state.view === 'accounts') renderAccounts();
  });
}

function accountReadinessStatus(row) {
  return row?.checking ? 'checking' : row?.status || 'unknown';
}

function countsAsReady(row) {
  return ['ready','queue'].includes(accountReadinessStatus(row));
}

function readinessBadge(row) {
  return {ready:'就绪',queue:'就绪 · 需排队',blocked:'暂不可用',unknown:'待确认',checking:'验活中'}[accountReadinessStatus(row)];
}

function flowReadiness(snapshot) {
  const obs=flowReadinessObservation;
  // Use time since receipt in this client; Windows and Ubuntu clocks may differ.
  const now=performance.now();
  const age=obs ? (now-obs.receivedAt)/1000 : Infinity;
  const fresh=state.snapshotFresh && snapshot?.real_accounts?.available===true && age<=90;
  const accounts=(snapshot?.real_accounts?.items || []).filter(account=>!account.flow_removed);
  const signature=JSON.stringify(accounts.map(account=>account.key).sort());
  if (!fresh) {
    if (lastFlowReadiness?.signature===signature && now-lastFlowReadiness.receivedAt<=180000)
      return {...lastFlowReadiness.result,stale:true};
    return {ready:null,usableBalance:null,rows:new Map(),stale:false};
  }
  const rows=auditFlowAccounts(snapshot,obs.dispatch,obs.pageChecks,obs.pageChecks.checked_at);
  const eligible=rows.filter(countsAsReady).map(row=>row.account);
  const result={ready:eligible.length,
    usableBalance:eligible.reduce((sum,account)=>sum+account.flow_balance,0),
    rows:new Map(rows.map(row=>[row.account.profile,row])),stale:false};
  lastFlowReadiness={signature,receivedAt:obs.receivedAt,result};
  return result;
}

function geminiReadiness(snapshot) {
  const known=snapshot?.real_accounts?.available===true && state.channelStatus?.available && !channelReadFailed;
  const total=(snapshot?.real_accounts?.items || []).filter(account=>!account.gemini_removed).length;
  const rows=known ? auditGeminiAccounts(snapshot,state.channelStatus,officialQuotaReadFailed ? null : officialQuotaData) : [];
  const ready=rows.filter(countsAsReady).length;
  const checked=known && rows.length>0 && (ready>0 || rows.every(row=>accountReadinessStatus(row)==='blocked'));
  const verifyRow=row=>accountReadinessStatus(row)==='blocked' && (row.issues || []).length > 0 &&
    (row.issues || []).every(issue => issue.key === 'confirmed_verification');
  return {ready:known && total===0 ? 0 : checked ? ready : null, total,
    blocked:rows.filter(row=>accountReadinessStatus(row)==='blocked' && !verifyRow(row)).length,
    verify:rows.filter(verifyRow).length,
    pending:rows.filter(row=>['unknown','checking'].includes(accountReadinessStatus(row))).length,
    rows:new Map(rows.map(row=>[row.account.key,row]))};
}

function renderAccountInsights(snapshot, target) {
  if (!target.querySelector('[data-service-slot]')) return;
  const scope = $('operationsDashboard');
  const opened = !!scope.querySelector('[data-dashboard-running][open]');
  const expandedResults = [...scope.querySelectorAll('[data-activation-feedback] details[open]')].map(d=>d.closest('[data-activation-feedback]').dataset.activationFeedback);
  const focused = scope.contains(document.activeElement) ? document.activeElement : null;
  const focusSelector = focused?.hasAttribute('data-governance') ? `[data-governance="${focused.dataset.governance}"]`
    : focused?.hasAttribute('data-music-readiness') ? '[data-music-readiness]'
    : focused?.hasAttribute('data-insight-accounts') ? '[data-insight-accounts]'
    : focused?.hasAttribute('data-insight-conversations') ? '[data-insight-conversations]'
    : focused?.matches('[data-dashboard-running] > summary') ? '[data-dashboard-running] > summary'
    : focused?.matches('summary') ? `[data-activation-feedback="${focused.closest('[data-activation-feedback]')?.dataset.activationFeedback}"] summary` : null;
  const flow = snapshot?.flow_insights;
  const amount = n=>typeof n==='number' && Number.isFinite(n) ? new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(n) : '—';
  const accounts = snapshot?.real_accounts?.items || [];
  const records = accounts.filter(account=>!account.gemini_removed).map(officialAccount);
  const officialKnown = state.channelStatus?.available && !channelReadFailed;
  const timing = records.map(r=>r.data?.latest_success?.total_ms ?? (r.outcome==='succeeded' ? r.call?.total_ms : null))
    .filter(n=>typeof n==='number' && Number.isFinite(n));
  const latency = officialKnown && timing.length ? (timing.reduce((a,b)=>a+b,0)/timing.length/1000).toFixed(1) : '—';
  const {ready:flowReady,usableBalance,stale:flowStale,rows:flowRows}=flowReadiness(snapshot);
  const flowTotal = accounts.filter(account=>!account.flow_removed).length;
  const health = flowReady!=null && flowTotal ? flowReady/flowTotal*100 : null;
  const healthCounts = flow?.health_counts;
  const flowCurrent = healthCounts && ['ready','cooling','attention','empty','paused'].every(k=>Number.isInteger(healthCounts[k]))
    ? `账号条件正常 ${healthCounts.ready} · 冷却 ${healthCounts.cooling} · 待核对 ${healthCounts.attention} · 积分不足 ${healthCounts.empty} · 暂停 ${healthCounts.paused}` : '账号状态暂未读取';
  const yesterday = flow?.yesterday;
  const recorded = yesterday?.recorded_usage == null ? '未记录' : amount(yesterday.recorded_usage)+((yesterday.unpriced || yesterday.unresolved) ? ' +' : '');
  const coverage = !yesterday || yesterday.coverage==='not_recorded' ? '昨日暂无覆盖记录'
    : `${yesterday.completed ?? '—'} 个完成 · ${yesterday.unpriced ?? '—'} 个未记价格${yesterday.unresolved ? ` · ${yesterday.unresolved} 个待确认` : ''}${yesterday.coverage==='partial' ? ' · 数据不完整' : ''}`;
  const dispatch = state.channelStatus?.dispatch;
  const {ready:eligible,total:geminiTotal,blocked:geminiBlocked,verify:geminiVerifyCount,pending:geminiPending}=geminiReadiness(snapshot);
  const readinessCard = ({name, service, ready, total, known, label = '', metric}) => {
    const key = service.toLowerCase();
    const action = service === 'MUSIC' ? 'data-music-readiness' : `data-governance="${key}"`;
    const count = known ? amount(ready) : '—';
    const capacity = known || total ? total : '—';
    const description = known ? `就绪 ${count} / ${capacity}` : '状态待核对';
    const ringClass = service === 'FLOW' ? '' : `insight-ring--${key}`;
    const readiness = `<strong>${count}<small> / ${capacity}</small></strong>`;
    const center = metric ? `<strong>${esc(metric.value)}${metric.unit ? `<small> ${esc(metric.unit)}</small>` : ''}</strong><span>${esc(metric.label)}</span>${metric.change ? `<span class="ring-music-change">${esc(metric.change)}</span>` : ''}`
      : readiness + '<span>就绪账号</span>';
    const accessibleName = [name + ' 一键就绪', description, label && label !== description ? label : '',
      metric && `${metric.label} ${metric.value}${metric.unit || ''}`, metric?.note, metric?.change].filter(Boolean).join('，');
    return `<button type="button" class="dashboard-ring-item governance-entry" ${action} aria-label="${esc(accessibleName)}">
      <span class="desk-service-body"><span class="desk-service-name">${name}</span>${metric ? `<span class="desk-service-total">${readiness}<span>就绪账号</span></span>${metric.note ? `<span class="desk-service-note">${esc(metric.note)}</span>` : ''}` : ''}</span>
      <span class="insight-ring ${ringClass} ${known ? '' : 'is-unknown'}" data-dashboard-ring="${service}" aria-hidden="true"><span class="governance-ring-center">${center}</span></span>
      <span class="desk-service-footer"><span class="desk-service-action">一键就绪 <span aria-hidden="true">→</span></span><span class="governance-alert" ${label ? '' : 'hidden'}>${esc(label)}</span></span>
    </button>`;
  };
  const musicAccounts = musicServiceStatus?.accounts || [];
  const musicTotal = musicAccounts.length;
  const musicReady = musicAccounts.filter(a => a.ready).length;
  const musicKnown = musicServiceStatus?.available === true && musicTotal > 0;
  const musicCredits = musicServiceStatus?.credits;
  const musicTotalCredits = musicCredits ? musicCredits.total
    : musicServiceStatus?.available === true && musicAccounts.every(account => Number.isFinite(account.credits) && account.credits >= 0)
      ? musicAccounts.reduce((sum, account) => sum + account.credits, 0) : null;
  const musicLabel = !musicServiceStatus?.available ? (musicServiceStatus?.reason || '音乐服务暂不可用')
    : musicTotal === 0 ? '账号池为空' : musicReady < musicTotal ? `${musicTotal - musicReady} 个未就绪` : '';
  const flowKnown = flowReady != null && flowTotal > 0;
  const loginCount = [...flowRows.values()].filter(row => flowLoginNotice(row.page)).length;
  const flowLabel = flowStale ? '同步暂缓 · 上次结果'
    : loginCount ? `${loginCount} 个账号需要用户登录`
    : !flowTotal && snapshot?.real_accounts?.available ? '暂无 Flow 账号' : !flowKnown ? '状态待核对' : flowReady < flowTotal ? `${amount(flowTotal - flowReady)} 个未就绪` : '';
  const geminiLabel = geminiVerifyCount ? `${geminiVerifyCount} 个待 Google 核验`
    : (geminiPending || geminiBlocked) ? [geminiBlocked ? geminiBlocked+' 个受限' : '',geminiPending ? geminiPending+' 个待确认' : ''].filter(Boolean).join(' · ')
    : eligible == null ? '状态待核对' : eligible < geminiTotal ? `${amount(geminiTotal - eligible)} 个未就绪` : '';
  const cards = [
    {name:'Flow', service:'FLOW', ready:flowReady, total:flowTotal, known:flowKnown, label:flowLabel,
      metric:{value:amount(usableBalance), label:'就绪积分', note:`全池总余额 ${amount(flow?.pool_balance)}`}},
    {name:'Gemini', service:'GEMINI', ready:eligible, total:geminiTotal, known:eligible!=null, label:geminiLabel,
      metric:{value:latency, unit:latency==='—' ? '' : '秒', label:'平均耗时', note:'每账号取最近一次成功记录'}},
    {name:'Music', service:'MUSIC', ready:musicReady, total:musicTotal, known:musicKnown, label:musicLabel,
      metric:{value:amount(musicTotalCredits), label:'总积分',
        change:Number.isFinite(musicServiceStatus?.credits?.last_change)
          ? '最近 ' + new Intl.NumberFormat('zh-CN',{signDisplay:'always',maximumFractionDigits:4}).format(musicServiceStatus.credits.last_change)
          : '暂无变动记录'}},
  ];
  for (const card of cards) {
    target.querySelector(`[data-service-slot="${card.service}"]`).innerHTML = readinessCard(card);
  }
  $('dashboardRuntime').innerHTML = `
  <details class="dashboard-runtime-details" data-dashboard-running ${opened ? 'open' : ''}><summary>运行记录与统计说明<span>账号状态、用量和检查结果</span></summary><div class="dashboard-runtime-columns"><article><header><h3>Flow 运行记录</h3><div><button type="button" class="insight-account-link" data-insight-accounts>查看账号 →</button></div></header><p>${esc(flowCurrent)}</p><dl><div><dt>昨日记录用量</dt><dd>${esc(recorded)}</dd></div><div><dt>就绪账号积分</dt><dd>${amount(usableBalance)}</dd></div></dl><p>${esc(coverage)}；按任务记录计算，非实际账单。</p><p>未提供积分总上限，不将余额换算为额度百分比。</p><p>${flow?.updated ? 'Flow 数据更新于 '+esc(dateTime(flow.updated)) : 'Flow 数据暂未读取'}</p><div class="insight-activation-feedback" data-activation-feedback="flow" hidden></div></article><article><header><h3>Gemini 运行记录</h3><button type="button" class="insight-account-link" data-insight-conversations>进入对话 →</button></header><p>${esc(officialSummary())}</p><p>官方账号池：${state.channelStatus?.production_send_available ? '已接入' : '暂不可用'}${dispatch ? ` · 排队 ${amount(dispatch.queued)} · 执行中 ${amount(dispatch.active)}` : ''}。</p><p>新对话按号池优先顺序分配，多轮对话固定账号；按当前派发限制使用。</p><p>圆环与账号页使用同一份就绪状态；Gemini 以云端验活结果为准。</p><p>${state.channelStatus?.read_at ? '实测记录读取于 '+esc(dateTime(state.channelStatus.read_at)) : '实测记录暂未读取'}。耗时不含旧通道；长期稳定性持续观察。</p><div class="insight-activation-feedback" data-activation-feedback="gemini" hidden></div></article></div></details>`;
  scope.querySelector('[data-dashboard-ring="FLOW"]').style.setProperty('--fill',`${health ?? 0}%`);
  scope.querySelector('[data-dashboard-ring="GEMINI"]').style.setProperty('--fill',`${eligible!=null && geminiTotal ? eligible/geminiTotal*100 : 0}%`);
  scope.querySelector('[data-dashboard-ring="MUSIC"]').style.setProperty('--fill',`${musicKnown && musicTotal ? musicReady/musicTotal*100 : 0}%`);
  scope.querySelector('[data-insight-accounts]').addEventListener('click',()=>setView('accounts'));
  scope.querySelector('[data-insight-conversations]').addEventListener('click',()=>setView('conversations'));
  scope.querySelectorAll('[data-governance]').forEach(button =>
    button.addEventListener('click', () => openGovernance(button.dataset.governance)));
  scope.querySelector('[data-music-readiness]')?.addEventListener('click', startMusicReadiness);
  renderMusicReadiness();
  renderGovernanceRings();
  renderServiceActivationFeedback(scope);
  for (const service of expandedResults) {
    const details=scope.querySelector(`[data-activation-feedback="${service}"] details`);
    if(details)details.open=true;
  }
  if(focusSelector)scope.querySelector(focusSelector)?.focus({preventScroll:true});
}

function renderServiceActivationFeedback(target = $('operationsDashboard')) {
  const records = state.serviceActivations || {};
  for (const service of ['flow', 'gemini']) {
    const feedback = target.querySelector(`[data-activation-feedback="${service}"]`);
    if (!feedback) continue;
    const record = records[service];
    feedback.hidden = !record || record.status === 'idle';
    if (feedback.hidden) continue;
    const items = Array.isArray(record.items) ? record.items : [];
    const done = items.filter(item => item.status === 'done').length;
    const pending = items.filter(item => ['queued', 'processing', 'waiting_flow', 'cooling'].includes(item.status)).length;
    const attention = items.length - done - pending;
    const accounts = new Map((state.snapshot?.real_accounts?.items || []).map(item => [item.key.slice('flow-account-'.length), item]));
    const started = typeof record.created === 'number' && Number.isFinite(record.created) && record.created > 0 ? dateTime(record.created) : '时间未记录';
    const label = record.status === 'running' ? `本轮检查（开始于 ${started}）` : `上次检查结果（开始于 ${started}）`;
    const message = record.error || (record.status === 'running' ? `正在逐项检查 · 本轮检查通过 ${done} · 处理中 ${pending} · 待处理 ${attention}` : `当时检查通过 ${done} · 当时待处理 ${attention}`);
    const expanded = feedback.querySelector('details')?.open;
    feedback.innerHTML = `<p role="status" aria-live="polite"><strong>${esc(label)}</strong><br>${esc(message)}</p>${items.length ? `<details ${expanded ? 'open' : ''}><summary>查看本轮账号结果</summary><ul>${items.map(item => `<li><strong>${esc(accounts.get(item.account_key)?.email || accounts.get(item.account_key)?.label || '账号')}</strong>：${esc(item.message)}</li>`).join('')}</ul></details>` : ''}`;
  }
  renderGovernance();
}

async function refreshServiceActivations() {
  const sequence = ++serviceActivationReadSequence;
  await Promise.all(['flow', 'gemini'].map(async service => {
    try {
      const result = await getJson('/api/account-activation?service=' + service, {timeoutMs:8000});
      if (sequence !== serviceActivationReadSequence || result.service !== service || !Array.isArray(result.items)) return;
      const previous = state.serviceActivations[service];
      state.serviceActivations[service] = result;
      if (previous?.status === 'running' && result.status === 'finished') refresh();
    } catch (_) { /* Keep last known progress without resubmitting an operation. */ }
  }));
  renderServiceActivationFeedback();
}

const governanceDialog = $('governanceDialog');
let governanceService = 'gemini', governanceCases = null, governanceRead = null, governanceReadFailed = false;
let governanceError = '';
const governanceAudits = {flow:null,gemini:null};
const governanceAuditLabels = {ready:'可立即派发',queue:'需排队',blocked:'暂不可用',unknown:'待确认'};
const flowRecoveryLabels = {rebind:'重新绑定原窗口',open_project:'进入原账号项目',return_to_project:'尝试返回原任务页',reload:'刷新原页面',restart:'重启该账号浏览器',dismiss_update:'关闭 Flow 更新提示',dismiss_account_panel:'关闭 Flow 账号面板',release_review:'已核对原任务，解除限制',close_duplicate:'关闭同项目空副本',recheck_window:'复查原固定任务页'};

function flowOccupiedSlots(dispatch) {
  return dispatch?.occupied_slots ?? dispatch?.active;
}

function auditDispatchState({issues=[], busy=false, dispatch, gemini=false, quota}={}) {
  if (issues.length) return 'blocked';
  const limit=gemini ? dispatch?.max_concurrency : dispatch?.maximum;
  const occupied=gemini ? dispatch?.active : flowOccupiedSlots(dispatch);
  if (!dispatch || (!gemini && dispatch.available!==true) || !Number.isInteger(limit) ||
      limit<1 || !Number.isInteger(occupied) || occupied<0) return 'unknown';
  if (gemini && quota!=='ok') return 'unknown';
  if (dispatch.paused || busy || occupied>=limit || (gemini && dispatch.queued>0)) return 'queue';
  return 'ready';
}

function auditFlowAccounts(snapshot, dispatch, pageChecks, probeStartedAt) {
  const verifiedEmpty=new Set(Object.entries(pageChecks?.accounts || {}).filter(([,state])=>state==='ok').map(([profile])=>profile));
  const flow=flowGovernance(snapshot,true,verifiedEmpty,probeStartedAt,pageChecks?.accounts);
  const issues=new Map(flow.rows.map(row=>[row.account.profile,row.issues]));
  const windowStates=new Map((snapshot.flow_coordination?.windows || []).map(w=>[w.profile,w.state]));
  return (snapshot.real_accounts?.items || []).filter(account=>!account.flow_removed).map(account=>{
    const profile=account.profile;
    const blocked=[...flow.shared,...(issues.get(account.profile) || [])];
    const page=pageChecks?.accounts?.[profile];
    const evidence=pageChecks?.evidence?.[profile];
    const review=pageChecks?.review?.[profile];
    const repair=pageChecks?.repairs?.[profile];
    const pageIssues={
      binding:['固定任务窗口绑定待核对','在 Flow 页的「账号检查记录」核对这个账号的固定窗口和项目。'],
      unique:['固定任务窗口待复查','算力中心保留已绑定的任务页，只关闭同项目确认空的副本。'],
      project:['任务窗口不在该账号的已登记项目','核对对应项目与固定窗口绑定。'],
      load_failed:['固定任务页加载失败','点 Flow 一键就绪，刷新原任务页并复查。'],
      draft:['固定任务窗口有草稿或附件','算力中心会保留草稿。处理后再检查就绪。'],
      overlay:['任务页有弹窗挡住输入','在原窗口核对提示；如需验证，请按官方提示完成。'],
      editor:['任务页未确认可用的输入框','核对页面是否加载完成或是否需要更新。']};
    const signIn = flowLoginNotice(page);
    if (signIn) {
      const index = blocked.findIndex(issue => issue.title === signIn.label);
      if (index >= 0) blocked.splice(index, 1);
      blocked.unshift({key:'flow_binding', title:signIn.label, action:signIn.text});
    } else if (pageIssues[page]) {
      const [title,action]=pageIssues[page];
      const issue={key:page==='draft' ? 'flow_draft' : 'flow_binding',title,action};
      const existing=blocked.findIndex(i=>i.title===title || (page==='draft' && i.key==='flow_draft'));
      if (existing<0) blocked.push(issue);
      else if (page==='draft') blocked[existing]=issue;
    }
    const busy=page==='busy' || ['preparing','submitting','submitted','generated','downloading'].includes(windowStates.get(profile));
    const proven=page==='ok' || evidence==='generating' || (evidence==='recent_success' && !busy && (page==='unconfirmed'||!page));
    const active=(snapshot.tasks || []).some(t=>t.source_profile===profile && ['image','video'].includes(t.kind) && ['queued','running'].includes(t.status));
    const status=blocked.length || review ? 'blocked' : page==='busy' && active && dispatch?.available===true ? 'queue' : proven && dispatch?.available===true ? 'ready' : 'unknown';
    const history=(repair?.actions || []).map(action=>flowRecoveryLabels[action]).filter(Boolean);
    let note=status==='queue' ? '已有任务正在处理，新任务等待账号空闲。' : status==='ready' ? (evidence==='generating' ? 'Flow 已接收，正在生成。' : evidence==='recent_success' ? '最近 10 分钟已生成产物。' : '输入框和发送入口已核对。') :
      status==='unknown' ? (busy ? '任务处理中，尚未确认 Flow 已接收。' : '实际页面或派发服务尚未核对通过。') : '';
    if (status==='ready' && (dispatch.paused || busy || flowOccupiedSlots(dispatch)>=dispatch.maximum)) note+=' 新任务可接收，等调度放行或空出名额。';
    if (history.length) note+=` 本轮处理：${history.join('、')}。${status==='ready' ? '复查通过。' : '复查未通过。'}`;
    return {account,status,page,evidence,review,issues:blocked,note};
  });
}

const geminiProbeReasons={
  readiness_authorization_required:'授权未通过，云端请求未发出',
  authorization_not_ready:'本机没有可用的 Gemini 授权',
  authorization_required_unsent:'授权未通过，云端请求未发出',
  authorization_still_required:'重新读取授权后仍未通过，云端请求未发出',
  verification_required:'Google 要求额外账号验证',
  quota_or_rate_limit:'Gemini 额度不足或频率受限',
  provider_connection_failed:'连接 Google 失败',
  provider_unavailable:'Google 服务暂时不可用',
  timeout_unconfirmed:'等待云端回复超时，结果未确认',
  readiness_unexpected_reply:'云端已响应，但回复内容不符',
  confirmed_bad_reply:'云端已响应，但返回通用拒答',
  confirmed_incomplete_reply:'云端已响应，但回复不完整',
  temporary_cli_launch_failure:'本机 Gemini 调用程序未能启动',
  provider_error:'Google 返回错误，未提供更具体原因',
  worker_interrupted:'算力中心执行中断，结果未确认',
  workbench_restarted:'算力中心重启，结果未确认',
};

function auditGeminiAccounts(snapshot, channel, quotaData, expected = null) {
  const byKey=new Map((channel?.accounts || []).map(item=>[item.account_key,item]));
  return (snapshot.real_accounts?.items || []).filter(account=>!account.gemini_removed).map(account=>{
    const key=account.key?.replace(/^flow-account-/,'');
    const current=byKey.get(key);
    const quota=quotaData?.accounts?.[key];
    const callable=channel?.available===true && channel.production_send_available===true &&
      current?.dispatch_eligible===true && account.gemini_enabled!==false;
    const issues=[];
    const add=(title,action)=>issues.push({title,action});
    const reasons={
      dispatch_paused:['账号已手动暂停','确认要启用后，在账号页恢复派发。'],
      unresolved_request:['账号有未确认的消息','由对话后台单独处理，本次就绪检查不读取旧对话。'],
      official_call_blocked:[geminiProbeReasons[current?.block_reason] || '账号当前受限','本轮未发送验活请求。'],
      authorization_not_ready:['官方授权未就绪','到账号详情核对授权身份。']};
    reasons.authorization_in_progress=['正在等待本人授权','到 Ubuntu 的授权窗口完成 Google 登录。'];
    if (channel?.production_send_available===false) add('Gemini 官方通道不可用','核对服务连接及账号授权。');
    if (current && current.dispatch_eligible!==true) add(...(reasons[current.restriction] || ['账号暂不具备派发资格','查看账号详情。']));
    const fresh=Number.isFinite(quota?.checked_at) && Math.abs(Date.now()/1000-quota.checked_at)<=600;
    const values=['five_hour','weekly'].map(w=>quota?.[w]?.remaining_percent);
    if (!callable && fresh && quota.status==='authorization_required')
      issues.push({key:'confirmed_reauth',title:'官方要求重新授权',action:'完成 Google 官方授权后再检查。'});
    if (!callable && fresh && quota.status==='verification_required' && quota.identity_matched===true)
      issues.push({key:'confirmed_verification',title:'Google 要求本人验证',action:'在 Google 验证页完成后重新检查。'});
    if (!callable && fresh && quota.status==='ok' && values.includes(0)) add('官方额度已用尽','等待对应周期恢复。');
    const raw=current?.latest_probe;
    // Show the last cloud check until a newer result or dispatch restriction
    // supersedes it. A manual check still displays only its own requests.
    const probe=raw && (!expected || expected.get(key)===raw.request_id) ? raw : null;
    const latestSuccess=current?.latest_success;
    const confirmedAfterProbe=!expected && current?.dispatch_eligible===true && Number.isFinite(latestSuccess?.finished) &&
      (!probe || (['failed','unknown'].includes(probe.status) && Number.isFinite(probe.finished) && latestSuccess.finished>probe.finished));
    if (!callable && !confirmedAfterProbe && (probe?.status==='failed' || probe?.status==='unknown')) {
      const code=probe.reason;
      issues.push({key:code==='verification_required' ? 'confirmed_verification' :
        code==='readiness_authorization_required' ? 'confirmed_reauth' : 'cloud_probe',
        title:geminiProbeReasons[code] || (probe.status==='unknown' ? '未确认云端是否响应' : '云端验活未通过'),
        action:'可在「Gemini 云端验活」对话中查看本次记录。'});
    }
    const shared=channel?.dispatch?.account_dispatch || channel?.dispatch?.shared_dispatch;
    const dispatch=shared?.available===true ? {active:shared.active,max_concurrency:shared.maximum,
      paused:shared.paused,queued:channel.dispatch.queued} : null;
    const busy=current?.busy===true;
    const status=auditDispatchState({issues,busy,
      dispatch:current ? dispatch : null,gemini:true,quota:callable || probe?.status==='completed' || confirmedAfterProbe ? 'ok' : null});
    const timing=Number.isFinite(probe?.total_ms) ? `，耗时 ${(probe.total_ms/1000).toFixed(1)} 秒` : '';
    const note=confirmedAfterProbe ? '最近的真实请求已成功回复 · '+dateTime(latestSuccess.finished)+'。' :
      probe?.status==='completed' ? '上次云端验活通过'+(Number.isFinite(probe.finished) ? ' · '+dateTime(probe.finished) : '')+timing+'。' :
      probe?.status==='queued' ? '云端验活排队中。' : probe?.status==='dispatching' ? '等待 Gemini 云端回应。' :
      probe ? (timing ? '本次验活'+timing.slice(1)+'。' : '') : !issues.length ? '尚无本轮云端验活结果。' : '';
    return {account,status,checking:!callable && (probe?.status==='queued'||probe?.status==='dispatching'),
      issues,note:note+(status==='queue' ? ' 新任务需等待并发空位。' : '')};
  });
}
const governancePosting = new Set();

async function refreshGovernanceCases() {
  if (governanceRead) return governanceRead;
  governanceRead = (async () => {
    try {
      const result = await getJson('/api/official-reauth', {timeoutMs:8000});
      if (!Array.isArray(result.items)) throw new Error('invalid response');
      governanceCases = result.items;
      governanceReadFailed = false;
    } catch (_) { governanceReadFailed = true; }
    finally {
      governanceRead = null;
      renderGovernance();
      if (state.view==='accounts') renderAccounts();
    }
  })();
  return governanceRead;
}

async function startGovernanceAudit(service, repair = true) {
  if (!['flow','gemini'].includes(service) || governanceAudits[service]?.running) return;
  const audit=governanceAudits[service]={running:true,repair,startedAt:Date.now(),rows:[],checkedAt:null,error:''};
  renderGovernance();
  try {
    const flowResult=service==='flow' && repair ? await postJson('/api/flow-readiness',{}) : null;
    let [snapshot, extra]=await Promise.all([
      getJson('/api/snapshot',{timeoutMs:10000}),
      getJson(service==='flow' ? '/api/dispatch/concurrency' : '/api/channel-status',{timeoutMs:8000}).catch(()=>null)
    ]);
    if (snapshot.real_accounts?.available!==true) throw new Error('本轮账号列表暂不可读取。');
    if (service==='flow') {
      const waiting = () => (snapshot.real_accounts?.items || []).some(a =>
        flowResult?.accounts?.[a.profile] === 'ok' && !a.flow_balance_fresh &&
        a.flow_login === 'verified' && !a.flow_risk_paused && !a.flow_cooling);
      for (let i=0; i<8 && repair && waiting(); i++) {
        await new Promise(resolve=>setTimeout(resolve,1500));
        const latest=await getJson('/api/snapshot',{timeoutMs:6000}).catch(()=>null);
        if (latest?.real_accounts?.available) snapshot=latest;
      }
      audit.snapshot=snapshot;
      audit.dispatch=extra;
      const pageChecks=flowResult || await getJson('/api/flow-readiness',{timeoutMs:14000}).catch(()=>null);
      audit.probeStartedAt=pageChecks?.checked_at || Date.now()/1000;
      audit.rows=auditFlowAccounts(snapshot,extra,pageChecks,audit.probeStartedAt);
      rememberFlowReadiness(pageChecks,extra);
      if (!pageChecks) audit.error='实际任务窗口检查暂未返回。';
    } else {
      // Quota is an independent, read-only check. Let dispatch readiness finish while it runs.
      const [latestQuota, readiness] = await Promise.all([
        refreshOfficialQuota(repair),
        repair ? postJson('/api/gemini-readiness',{}) : null
      ]);
      const quota=latestQuota || readiness;
      let latest=extra;
      const expected=new Map();
      const observeProbes=async result=>{
        const probes=Array.isArray(result?.readiness_probes) ? result.readiness_probes : [];
        for (const probe of probes) expected.set(probe.account_key,probe.request_id);
        audit.probes=expected.size;
        audit.rows=auditGeminiAccounts(snapshot,latest,officialQuotaData || quota,expected);
        renderGovernance();
        const probePending=()=>probes.some(probe=>{
          const call=latest?.accounts?.find(a=>a.account_key===probe.account_key)?.latest_probe;
          return !call || call.request_id!==probe.request_id || ['queued','dispatching'].includes(call.status);
        });
        const deadline=Date.now()+Math.min(30,3*probes.length+1)*60*1000;
        while (probePending() && Date.now()<deadline) {
          await new Promise(resolve=>setTimeout(resolve,2000));
          latest=await getJson('/api/channel-status',{timeoutMs:8000}).catch(()=>null) || latest;
          audit.rows=auditGeminiAccounts(snapshot,latest,officialQuotaData || quota,expected);
          renderGovernance();
        }
        if (probePending()) audit.error='部分账号的云端验活还在排队或执行，可在对话管理查看。';
      };
      await observeProbes(readiness);
      await refreshGovernanceCases();
      if (repair) {
        audit.authorization=await postJson('/api/official-reauth/activate',{});
        while (['starting','waiting'].includes(audit.authorization.state)) {
          renderGovernance();
          await new Promise(resolve=>setTimeout(resolve,2000));
          audit.authorization=await getJson('/api/official-reauth/activate',{timeoutMs:8000});
        }
        if (audit.authorization.completed) {
          audit.checkingAfterAuthorization=true;
          renderGovernance();
          try {
            await observeProbes(await postJson('/api/gemini-readiness?scope=recoverable',{}));
          } finally {
            audit.checkingAfterAuthorization=false;
          }
        }
        if (audit.authorization.state==='stopped')
          audit.error=[audit.error,audit.authorization.message || '授权未完成。'].filter(Boolean).join(' ');
      }
      const [currentSnapshot,currentChannel]=await Promise.all([
        getJson('/api/snapshot',{timeoutMs:10000}).catch(()=>null),
        getJson('/api/channel-status',{timeoutMs:8000}).catch(()=>null)]);
      if (currentSnapshot?.real_accounts?.available!==true || currentChannel?.available!==true)
        throw new Error('本轮未能重新核对账号和队列。');
      audit.rows=auditGeminiAccounts(currentSnapshot,currentChannel,officialQuotaData || quota,expected);
      state.channelStatus=currentChannel;
      channelReadFailed=false;
    }
    audit.checkedAt=Date.now()/1000;
  } catch (error) {
    audit.error=error.message || '本轮检查未完成。';
  } finally {
    audit.running=false;
    if (service==='gemini') renderAccountQuotas();
    if (state.view==='overview' && state.snapshot) renderAccountInsights(audit.snapshot || state.snapshot,$('overviewInsights'));
    delete audit.snapshot;
    renderGovernance();
    if (audit.repairNext) void startGovernanceAudit(service);
  }
}

function openGovernance(service) {
  governanceService = service;
  governanceError = '';
  if (governanceDialog) {
    governanceDialog.hidden=service!=='gemini';
    if (!governanceDialog.hidden) governanceDialog.scrollIntoView({block:'nearest'});
  }
  renderGovernance();
  const pending=governanceAudits[service];
  if (service==='flow' && pending?.running && !pending.repair) pending.repairNext=true;
  else void startGovernanceAudit(service);
  if (service==='gemini') void refreshGovernanceCases();
}

function governanceAccountName(account) {
  return account?.email || account?.label || '账号名称暂不可读';
}

function governanceReauthFor(account) {
  const key=account.key?.replace(/^flow-account-/, '');
  const latest=officialAccount(account).data?.latest_probe?.request_id;
  return governanceCases?.find(item=>item.account_key===key && item.mode==='login_only' && item.request_id===latest) ||
    governanceCases?.find(item=>item.account_key===key && (item.state==='waiting' || item.can_start));
}

function renderGovernanceGuidance(service, audit) {
  const panel=$('dashboardGovernanceNext');
  if (!panel) return;
  if (service !== 'flow' || !audit) {
    panel.hidden=true;
    panel.replaceChildren();
    delete panel.dataset.rendered;
    return;
  }
  panel.hidden=false;
  let body;
  if (audit.running) {
    body='<strong>Flow 正在检查账号，你暂时不用操作。</strong>';
  } else {
    const blocked=audit.rows.filter(row=>row.status==='blocked' || row.status==='unknown');
    const actions=blocked.flatMap(({account,page,review})=>{
      const name=esc(governanceAccountName(account));
      let instruction, button='';
      if (review) {
        instruction='上次任务结果未确认。先按原编号核对 Flow 回执或产物；确认不再需要保留执行现场后，才能放行这个账号。放行不会重发原任务。';
        button=`<div class="governance-row-actions"><button type="button" class="button button-light" data-govern-destination="records" data-govern-task="${esc(review)}">查看原任务</button><button type="button" class="button button-primary" data-govern-review="${esc(review)}" data-review-account="${esc(account.key)}">已核对，放行账号</button></div>`;
      } else {
        const signIn = flowLoginNotice(page);
        if (signIn) instruction = `${signIn.label}。 ${signIn.text}`;
      }
      return instruction ? [`<article class="governance-row"><div><strong>${name}</strong><p>${instruction}</p></div>${button}</article>`] : [];
    });
    const keyboard=blocked.some(row=>row.issues?.some(issue=>issue.title==='Ubuntu 桌面键盘未就绪'));
    const internal=blocked.length-actions.length;
    body=`<strong>Flow：${keyboard ? '需要一次 Ubuntu 桌面授权' : actions.length ? '账号授权与处理' : audit.error ? '本轮检查未完成，你暂时不用操作' : '本轮无需你操作'}</strong>`+
      (keyboard ? '<p>在 Ubuntu 的远程交互弹窗中，开启「允许远程交互」并点击「分享」。这是所有账号共用的一次授权。</p>' : '')+
      actions.join('')+(!keyboard && internal ? `<p>另有 ${internal} 个账号需由算力中心侧核对，你不用操作。</p>` : '')+
      (audit.error ? `<p>${esc(audit.error)}</p>` : '')+
      (governanceError ? `<p>${esc(governanceError)}</p>` : '')+
      (keyboard || actions.length || audit.error ? '<button type="button" class="insight-account-link" data-govern-recheck="flow">重新检查 →</button>' : '');
  }
  if (panel.dataset.rendered===body) return;
  panel.innerHTML=body;
  panel.dataset.rendered=body;
}

function renderGovernanceRings() {
  const dashboard = $('operationsDashboard');
  for (const service of ['flow','gemini']) {
    const ring = dashboard.querySelector(`[data-governance="${service}"]`);
    const busy = governanceAudits[service]?.running === true || state.serviceActivations?.[service]?.status === 'running' ||
      (service === 'gemini' && governancePosting.size > 0);
    ring?.setAttribute('aria-busy', String(busy));
  }
  const status = dashboard.querySelector('[data-governance-status]');
  const audit = governanceAudits[governanceService];
  if (!status || (!audit?.repair && !audit?.repairNext)) return;
  const name = governanceService === 'flow' ? 'Flow' : 'Gemini';
  const message = audit.running || audit.repairNext ? `${name} 正在检查账号。`
    : audit.error || `${name} 检查完成 · ${audit.rows.filter(countsAsReady).length} / ${audit.rows.length} 个账号就绪。`;
  if (status.textContent !== message) status.textContent = message;
}

function renderGovernance() {
  renderGovernanceRings();
  const service = governanceService;
  const isGemini = service === 'gemini';
  const audit=governanceAudits[service];
  renderGovernanceGuidance(service,audit);
  if (!governanceDialog || governanceDialog.hidden) return;
  $('governanceTitle').textContent = isGemini ? 'Gemini 一键就绪' : 'Flow 一键就绪';
  for (const button of governanceDialog.querySelectorAll('[data-govern-service]')) {
    button.setAttribute('aria-pressed',String(button.dataset.governService === service));
  }
  $('governanceRecords').textContent = isGemini ? '对话记录 →' : '任务记录 →';
  $('governanceCheck').disabled = audit?.running===true;
  $('governanceCheck').textContent = audit?.running ? '检查中…' : '重新检查';
  const rows=audit?.rows || [];
  const counts=Object.fromEntries(Object.keys(governanceAuditLabels).map(key=>[key,rows.filter(row=>row.status===key).length]));
  const dispatch=audit?.dispatch;
  const occupied=flowOccupiedSlots(dispatch);
  const slots=!isGemini && Number.isInteger(occupied) && Number.isInteger(dispatch?.maximum)
    ? ` · 派发名额 ${occupied} / ${dispatch.maximum}${dispatch.background_videos ? ` · 视频跟进 ${dispatch.background_videos}` : ''}${dispatch.paused ? ' · 调度已暂停' : ''}` : '';
  const summary=rows.length ? [`${isGemini ? '可立即派发' : '就绪'} ${counts.ready} / ${rows.length}`, ...['queue','blocked','unknown'].filter(key=>counts[key]).map(key=>governanceAuditLabels[key]+' '+counts[key])].join(' · ')+slots : '';
  const authorization=audit?.authorization;
  const authProgress=isGemini && authorization?.state==='waiting'
    ? `请在 Ubuntu 完成 ${authorization.email} 的 Google 授权（${authorization.completed+1}/${authorization.total}）。确认后自动打开下一个。`
    : isGemini && authorization?.completed ? `已完成 ${authorization.completed}/${authorization.total} 个账号授权。 ` : '';
  const verification=audit?.quotaVerification;
  const verificationProgress=isGemini && verification?.state==='waiting'
    ? `请在 Ubuntu 完成 ${verification.email} 的 Google 官方验证。完成后自动重读额度。`
    : isGemini && verification?.state==='rechecking' ? '正在重读已验证账号的额度…' : '';
  const quota = officialQuotaReadFailed ? null : officialQuotaData;
  let quotaSummary = '';
  if (isGemini && audit?.checkedAt) {
    quotaSummary = quota?.known_accounts > 0
      ? ` · 两项额度已读取 ${quota.known_accounts}/${quota.total_accounts}` : ' · 额度暂未获取';
  }
  if (isGemini && audit && quota?.refreshing) quotaSummary = ' · 正在读取额度…';
  const runningStatus = authorization?.state==='waiting' ? '' : authorization?.state==='starting' ? '正在打开 Google 授权窗口…' :
    audit?.checkingAfterAuthorization ? '正在复查授权后的派发状态…' : '正在自动检查全部账号…';
  const status = governanceError || verificationProgress || authProgress+(audit?.running ? runningStatus : audit?.checkedAt ? dateTime(audit.checkedAt)+' · '+summary+
    (audit.probes ? ` · 云端验活 ${audit.probes} 个账号` : '') : '') + quotaSummary + (audit?.error ? ' '+audit.error : '');
  if ($('governanceStatus').textContent !== status) $('governanceStatus').textContent = status;
  const detailRows=rows.map(({account,status,issues,note})=>{
    const visibleIssues=isGemini && audit?.running ? issues.filter(issue=>!['confirmed_verification','confirmed_reauth'].includes(issue.key)) : issues;
    const reasons=[...new Set(visibleIssues.map(issue=>issue.title.replace(/^固定任务窗口绑定待核对$/,'任务窗口绑定待核对')))];
    const nextStep=isGemini && !audit?.running && visibleIssues.some(issue=>issue.key==='confirmed_verification')
      ? '<p>请在账号页打开该账号的 Google 验证，完成后再重新检查。</p>' : '';
    return `<article class="governance-row"><div><strong>${esc(governanceAccountName(account))}</strong><span class="governance-result" data-state="${status}">${esc(!isGemini && status==='ready' ? '就绪' : governanceAuditLabels[status])}</span>
      ${note ? '<p>'+esc(note)+'</p>' : ''}${reasons.map(reason=>'<p>'+esc(reason)+'</p>').join('')}${nextStep}</div></article>`;
  }).join('');
  let body;
  if (audit?.running && !rows.length) body='<p class="governance-empty">正在检查账号、授权和队列，你不用操作。</p>';
  else if (isGemini && audit?.checkedAt && !audit.error && !rows.length) body='<p class="governance-empty">Gemini 号池当前没有账号。</p>';
  else if (!rows.length) body='<p class="governance-empty">本轮未能确认账号状态，暂时无需操作。</p>';
  else body=`<details data-govern-details><summary>查看全部 ${rows.length} 个账号的检查明细</summary>${detailRows}</details>`;
  const list=$('governanceItems');
  if (list.dataset.rendered !== body) {
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    const oldRequest = focused?.dataset.governReauth;
    const expanded=list.querySelector('[data-govern-details]')?.open;
    list.innerHTML = body;
    if (expanded && list.querySelector('[data-govern-details]')) list.querySelector('[data-govern-details]').open=true;
    list.dataset.rendered = body;
    if (focused) (oldRequest && list.querySelector(`[data-govern-reauth="${CSS.escape(oldRequest)}"]:not(:disabled)`) || $('governanceCheck')).focus({preventScroll:true});
  }
}

async function handleGovernanceClick(event) {
  const button = event.target.closest('button');
  if (!button || button.disabled) return;
  if (button.dataset.governRecheck) {
    openGovernance(button.dataset.governRecheck);
  } else if (button.dataset.governReview) {
    const task=button.dataset.governReview;
    if (!window.confirm(`已核对任务 ${task}，现在放行账号并删除本次警告截图？原任务不会重发。`)) return;
    button.disabled=true;
    governanceError='';
    try {
      await postJson('/api/flow-readiness/review',{account:button.dataset.reviewAccount,task,confirmed:true});
      await Promise.all([startGovernanceAudit('flow',false), refresh({background:true})]);
    } catch (error) { governanceError=error.message; renderGovernance(); }
    finally { button.disabled=false; }
  } else if (button.dataset.governService) {
    openGovernance(button.dataset.governService);
  } else if (button.id === 'governanceCheck') {
    openGovernance(governanceService);
  } else if (button.hasAttribute('data-govern-close')) {
    if (governanceDialog) governanceDialog.hidden=true;
    document.querySelector(`[data-governance="${governanceService}"]`)?.focus({preventScroll:true});
  } else if (button.dataset.governReauth) {
    const rid = button.dataset.governReauth;
    if (governancePosting.has(rid)) return;
    governancePosting.add(rid); governanceError = ''; renderGovernance();
    button.disabled=true;
    try {
      await postJson('/api/official-reauth/' + encodeURIComponent(rid), {});
      if (state.view==='accounts') $('accountActionMessage').textContent='请切到 Ubuntu，在浏览器中完成 Google 授权。';
      // Read after any refresh that was already in flight when the window opened.
      if (governanceRead) await governanceRead;
      await refreshGovernanceCases();
    } catch (error) {
      governanceError = error.message || '授权窗口状态未确认，请查看最新状态。';
      if (state.view==='accounts') $('accountActionMessage').textContent=governanceError;
      await refreshGovernanceCases();
    } finally { button.disabled=false; governancePosting.delete(rid); renderGovernance(); }
  } else if (button.dataset.governDestination || button.dataset.governChat || button.id === 'governanceRecords') {
    const chat = button.dataset.governChat;
    const destination = button.dataset.governDestination || (chat || governanceService === 'gemini' ? 'conversations' : 'records');
    if (governanceDialog) governanceDialog.hidden=true;
    if (chat) activeConversation = chat;
    setView(destination);
    if (chat) showConversation(chat).catch(()=>{$('chatMessage').textContent='原对话暂不可读取。';});
    if (button.dataset.governTask) {
      $('taskSearch').value = button.dataset.governTask;
      $('taskKindFilter').value = '';
      $('taskStatusFilter').value = '';
      renderTasks();
      $('taskRecords').scrollIntoView({block:'start'});
    }
    if (button.hasAttribute('data-govern-windows')) {
      $('taskWindowsPanel').open = true;
      renderTaskWindows();
      $('taskWindowsPanel').scrollIntoView({block:'start'});
    }
    if (destination==='accounts' && button.dataset.governAccount) {
      const summary = document.querySelector(`summary[aria-controls="account-detail-${CSS.escape(button.dataset.governAccount)}"]`);
      if (summary) { summary.parentElement.open = true; summary.focus({preventScroll:true}); summary.closest('article')?.scrollIntoView({block:'center'}); }
    }
    if (destination==='conversations' && button.dataset.governAccount) {
      $('chatFilterAccount').value = button.dataset.governAccount;
      renderConversationList();
    }
  }
}
governanceDialog?.addEventListener('click', handleGovernanceClick);

function accountUsageBar(label, value, stale) {
  const known = typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100;
  const formatted = known ? `${new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 1}).format(value)}%` : "—";
  const meter = known
    ? `role="meter" aria-label="Gemini ${esc(label)}已用" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${value}"`
    : `role="img" aria-label="Gemini ${esc(label)}用量未知"`;
  return `<div class="account-usage ${stale ? 'is-stale' : ''}"><div class="account-usage-label"><span>${esc(label)}</span><strong>${esc(formatted)}</strong></div><div class="account-usage-track ${known ? '' : 'is-unknown'}" ${meter}>${known ? `<i data-usage-percent="${value}"></i>` : ''}</div></div>`;
}

function updateAccountUsageBars() {
  document.querySelectorAll('[data-usage-percent]').forEach((bar) => {
    const percent = Number(bar.dataset.usagePercent);
    if (Number.isFinite(percent) && percent >= 0 && percent <= 100) bar.style.width = `${percent}%`;
  });
}

function updateAccountCooldowns() {
  document.querySelectorAll('[data-cooldown-end]').forEach((clock) => {
    const deadline = Number(clock.dataset.cooldownEnd);
    if (!Number.isFinite(deadline)) return;
    const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    const time = clock.querySelector('.account-cooldown-time');
    const label = clock.querySelector('.account-cooldown-label');
    if (!time || !label) return;
    if (seconds === 0) {
      time.textContent = '待核对';
      label.textContent = '已到时间';
      clock.setAttribute('aria-label', '本地冷却倒计时结束，请刷新核对状态');
      const status = clock.closest('.account-service')?.querySelector('.account-health');
      if (status && clock.dataset.riskPaused !== 'true') { status.textContent = '待核对'; status.classList.add('is-unknown'); }
      return;
    }
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    const remainder = String(seconds % 60).padStart(2, '0');
    time.textContent = hours ? `${hours}:${String(minutes).padStart(2, '0')}:${remainder}` : `${minutes}:${remainder}`;
    label.textContent = '冷却中';
    clock.setAttribute('aria-label', `本地冷却剩余 ${hours ? hours + ' 小时 ' : ''}${minutes} 分 ${Number(remainder)} 秒`);
  });
}

setInterval(updateAccountCooldowns, 1000);

function geminiCardStatus(canCall, callBlocked, quota, now) {
  if (quota?.status === 'verification_required') return {label:'待 Google 核验', attention:false};
  if (callBlocked) return {label:'暂不可用', attention:true};
  if (!canCall) return {label:'待确认', attention:false};
  const age = now - quota?.checked_at;
  const quotaKnown = quota?.status === 'ok' && Number.isFinite(age) && age >= 0 && age <= 600 &&
    ['five_hour','weekly'].every(w => {
      const value = quota?.[w]?.remaining_percent;
      return Number.isFinite(value) && value >= 0 && value <= 100;
    });
  return {label:'可调用', attention:false};
}

function savedQuotaValue(row, window) {
  const value = row?.[window]?.remaining_percent;
  return row?.status === 'ok' && Number.isFinite(row.checked_at) &&
    Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

function savedQuotaSummary(data) {
  const rows = Object.values(data?.accounts || {}).filter(row =>
    ['five_hour','weekly'].every(window => savedQuotaValue(row,window) !== null));
  const windows = {};
  for (const window of ['five_hour','weekly']) {
    windows[window] = {average_remaining_percent:rows.length ?
      Math.round(rows.reduce((sum,row)=>sum+savedQuotaValue(row,window),0)/rows.length*100)/100 : null};
  }
  return {known_accounts:rows.length,windows};
}

function accountOfficialQuotaMarkup(key) {
  const row = officialQuotaReadFailed ? null : officialQuotaData?.accounts?.[key];
  const now = Date.now()/1000;
  const age = row && typeof row.checked_at === 'number' ? now-row.checked_at : null;
  const fresh = row?.status === 'ok' && age !== null && Number.isFinite(age) && age >= 0 && age <= 600;
  const values = ['five_hour','weekly'].map(window=>savedQuotaValue(row,window));
  const available = values.some(value=>value!==null);
  const partial = available && values.some(value=>value===null);
  const windowMarkup = (window, label, title) => {
    const value = savedQuotaValue(row,window);
    const reset = row?.[window]?.reset_at;
    const text = value === null ? '待读取' : value.toFixed(2).replace(/\.00$/, '')+'%';
    const hasFutureReset = row?.status === 'ok' && typeof reset === 'number' && Number.isFinite(reset) && reset > now;
    const recovery = value === null ? '' : hasFutureReset
      ? '恢复 ' + dateTime(reset) + (fresh ? '' : '（上次读取）')
      : value < 100 ? '恢复时间待官方更新' : '';
    const hint = recovery || (value !== null && !fresh ? '上次读取的剩余比例' : '');
    const tip = hint ? title+' · '+hint : title;
    return `<div class="account-quota-window" title="${esc(tip)}"><div class="account-quota-label"><span>${label}</span><strong>${text}</strong></div><div class="account-quota-track${value === null ? ' is-unknown' : ''}" ${value === null ? `role="img" aria-label="Gemini ${title}尚未读取"` : `role="progressbar" aria-label="Gemini ${title}剩余" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${value}"`}>${value === null ? '' : `<i style="width:${value}%"></i>`}</div></div>`;
  };
  const quotaReason = {
    verification_required:row?.reason==='provider_validation_no_entry' ? '额度查询受限，Google 未提供验证入口' : 'Google 验证尚未通过，额度暂不可读',
    authorization_required:'Google 要求重新授权后读取额度',
  }[row?.status];
  let message = quotaReason || '';
  if (!officialQuotaReadFailed && officialQuotaData?.refreshing && (!fresh || !available))
    message = '正在读取额度…';
  else if (!message && !available) message = '额度暂未获取';
  else if (!message && !fresh) message = '上次读取 '+dateTime(row.checked_at)+'，仅供参考'+(partial ? ' · 部分额度待读取' : '');
  else if (!message && partial) message = '部分额度待读取';
  const html = `<div class="account-quota-windows">${windowMarkup('five_hour','5 小时','5 小时额度')}${windowMarkup('weekly','每周','每周额度')}</div>`;
  return {html, message};
}

function renderAccountQuotas() {
  document.querySelectorAll('[data-account-quota]').forEach(panel=>{
    const view = accountOfficialQuotaMarkup(panel.dataset.accountQuota);
    if (panel.innerHTML !== view.html) panel.innerHTML = view.html;
    const service = panel.closest('.account-service-gemini');
    const status = service?.querySelector('.account-health')?.textContent;
    if (status) {
      const label = 'Gemini：'+status+(view.message ? ' · '+view.message : '');
      service.setAttribute('aria-label', label);
      service.title = label;
    }
  });
}

// 音乐任务持久保存；刷新与重连只读取原任务。
let musicServiceStatus = null;
let musicStatusReadAt = 0;
let musicReadinessState = null;
let musicReadinessPolling = false;
let musicReadinessStarting = false;
function renderMusicReadiness() {
  const button = document.querySelector('[data-music-readiness]');
  if (button) {
    button.disabled = musicReadinessState?.state === 'running' || musicReadinessPolling || musicReadinessStarting;
    button.setAttribute('aria-busy', String(button.disabled));
  }
  let feedback = $('musicReadinessFeedback');
  if (!feedback && $('operationsDashboard')) {
    feedback = document.createElement('div');
    feedback.id = 'musicReadinessFeedback';
    feedback.className = 'dashboard-send-status';
    feedback.setAttribute('role', 'status');
    $('operationsDashboard').after(feedback);
  }
  if (!feedback) return;
  feedback.hidden = !musicReadinessState || musicReadinessState.state === 'idle';
  feedback.innerHTML = musicReadinessState ? `<strong>音乐就绪检查</strong><p>${esc(musicReadinessState.message)}</p>${(musicReadinessState.items || []).map(row => `<p>${esc(row.email)}：${esc(row.result)}</p>`).join('')}` : '';
}
async function pollMusicReadiness() {
  if (musicReadinessPolling) return;
  musicReadinessPolling = true;
  try {
    while (musicReadinessState?.state === 'running') {
      await new Promise(resolve => setTimeout(resolve, 1500));
      musicReadinessState = await getJson('/api/music/readiness', {timeoutMs: 12000});
      renderMusicReadiness();
    }
    await refreshMusicStatus();
  } catch (error) {
    musicReadinessState = {state:'failed', message:'连接中断，无法读取检查结果。重新点击可查询正在执行的检查，不会重复启动。', items:[]};
    renderMusicReadiness();
  } finally { musicReadinessPolling = false; renderMusicReadiness(); }
}
async function startMusicReadiness() {
  if (musicReadinessState?.state === 'running' || musicReadinessPolling || musicReadinessStarting) return;
  musicReadinessStarting = true;
  musicReadinessState = {state:'running', message:'正在连接音乐服务…', items:[]};
  renderMusicReadiness();
  try {
    musicReadinessState = await postJson('/api/music/readiness', {});
    renderMusicReadiness();
    if (musicReadinessState.state === 'running') await pollMusicReadiness();
    else await refreshMusicStatus();
  } catch (error) {
    musicReadinessState = {state:'failed', message:'音乐检查未完成：' + error.message, items:[]};
    renderMusicReadiness();
  } finally { musicReadinessStarting = false; renderMusicReadiness(); }
}

async function refreshMusicStatus() {
  musicStatusReadAt = Date.now();
  const data = await getJson('/api/music/status', {timeoutMs: 12000}).catch(() => null);
  musicServiceStatus = data;
  renderMusicStatus();
  const check = await getJson('/api/music/readiness', {timeoutMs: 12000}).catch(() => null);
  if (check && !musicReadinessStarting && (!musicReadinessState || musicReadinessState.state === 'idle' || check.state === 'running')) musicReadinessState = check;
  if (state.snapshot && state.view === 'overview') renderAccountInsights(state.snapshot, $('overviewInsights'));
  renderMusicReadiness();
  if (musicReadinessState?.state === 'running') void pollMusicReadiness();
}
const musicNumber = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});
function renderMusicStatus() {
  const el = $("musicCredits");
  const details = $("musicAccountDetails");
  if (!el || !details) return;
  if (!musicServiceStatus?.available) details.replaceChildren();
  if (!musicServiceStatus) { el.textContent = "音乐服务状态未读取。"; return; }
  if (!musicServiceStatus.available) { el.textContent = musicServiceStatus.reason || "音乐服务暂不可用。"; return; }
  const accounts = musicServiceStatus.accounts || [];
  if (!accounts.length) {
    el.textContent = '账号池为空，请先导入 Flow Music 账号。';
    details.replaceChildren();
    return;
  }
  const ready = accounts.filter(account => account.ready).length;
  const total = musicServiceStatus.credits?.total;
  el.textContent = `${ready} / ${accounts.length} 就绪 · ${Number.isFinite(total) ? '总积分 '+musicNumber.format(total) : '总积分待读取'}`;
  details.innerHTML = `<table aria-label="音乐账号明细"><thead><tr>
    <th scope="col">账号</th><th scope="col">状态</th><th scope="col">剩余积分</th><th scope="col">今日生成</th>
    </tr></thead><tbody>${accounts.map(account => `<tr>
    <th scope="row">${esc(account.email || '未登记名称')}</th>
    <td${account.ready ? '' : ' class="music-account-not-ready"'}>${account.ready ? '就绪' : '待刷新'}</td>
    <td>${Number.isFinite(account.credits) ? musicNumber.format(account.credits) : '—'}</td>
    <td>${Number.isFinite(account.today_music) ? musicNumber.format(account.today_music)+' 首' : '—'}</td>
    </tr>`).join('')}</tbody></table>`;
}
$('musicPrompt')?.addEventListener('input', event => {
  const count = $('musicPromptCount');
  if (count) count.textContent = musicNumber.format(event.target.value.length) + ' / 2,000';
});
$('musicOpenRecords')?.addEventListener('click', () => {
  setView('records');
  selectRecordsTab('music', true);
});
let musicPolling = false;
let musicRendered = null;
function renderMusicJob(job) {
  if (!job) return;
  const busy = ['generating','ingesting','recovering'].includes(job.status);
  $("musicGenerate").disabled = busy;
  $("musicStatus").textContent = ({recovering:'已找回原任务，正在查询结果或恢复入库…',generating:'音乐生成中，可刷新页面；不会重复提交。',ingesting:'音乐已生成，正在登记素材…',completed:'完成。',unknown:job.error || '结果待核对，不会重新生成。'})[job.status] || job.status;
  const result = job.result;
  const revision = result && JSON.stringify([job.id, result]);
  if (result && musicRendered !== revision) {
    musicRendered = revision;
    const clips = result.clips || [];
    const model = Array.from($('musicModel').options).find(option => option.value === result.model)?.textContent || result.model || job.model;
    const time = Number.isFinite(job.updated) ? ' · ' + dateTime(job.updated) : '';
    $("musicResultSummary").textContent = `${model} · 完成 ${clips.length} 首 · 已入库 ${result.ingested ?? 0} 件${result.ingest_error ? "（" + result.ingest_error + "）" : ""}${time}`;
    $("musicResult").innerHTML = clips.map(clip => `
      <article class="music-result-item"><h3>${esc(clip.title)}</h3>
      ${clip.auto_tags?.length ? `<p>提示词标签 · ${esc(clip.auto_tags.join("、"))}</p>` : ''}
      ${clip.tags?.length ? `<p>手动标签 · <span class="music-result-metadata">${esc(clip.tags.join("、"))}</span></p>` : ''}
      ${typeof clip.style_id === "string" && clip.style_id !== '' ? `<p>画风 ID · <code class="music-result-metadata">${esc(clip.style_id)}</code></p>` : ''}
      ${clip.audio_url ? `<audio controls preload="metadata" src="${esc(clip.audio_url)}"></audio>` : ''}
      <div class="music-result-links">${clip.audio_url ? `<a class="text-link" href="${esc(clip.audio_url)}" download>下载音频</a>` : ''}${clip.wav_url ? `<a class="text-link" href="${esc(clip.wav_url)}" download>下载 WAV</a>` : ''}${clip.cover_url ? `<a class="text-link" href="${esc(clip.cover_url)}" target="_blank" rel="noopener noreferrer">封面</a>` : ''}</div></article>`).join('');
    $("musicResultPanel").hidden = false;

  }
}
async function recoverMusicJob() {
  if (musicPolling) return;
  musicPolling = true;
  try {
    const id = localStorage.getItem('workbench.music.request');
    if (id && !musicRendered) $("musicGenerate").disabled=true;
    const data = await getJson(id ? '/api/music/jobs/'+encodeURIComponent(id) : '/api/music/jobs/latest', {timeoutMs:10000});
    renderMusicJob(data.job);
  } catch (error) {
    $("musicStatus").textContent = '连接暂断或任务状态待核对，恢复后继续读取；不会重新提交。';
  } finally { musicPolling = false; }
}
$("musicGenerate")?.addEventListener("click", async () => {
  const prompt = $("musicPrompt").value.trim();
  if (!prompt) { $("musicStatus").textContent='请先填写音乐描述。'; $("musicPrompt").focus(); return; }
  const id = crypto.randomUUID();
  try {
    localStorage.setItem('workbench.music.request',id);
    $("musicGenerate").disabled=true;
    $("musicStatus").textContent='正在提交音乐任务…';
    const metadata={tags:$('musicTags').value.split(/\r?\n/).filter(t=>t.trim())};
    if ($('musicStyleId').value!=='') metadata.style_id=$('musicStyleId').value;
    const data = await postJson('/api/music/jobs',{request_id:id,prompt,model:$("musicModel").value,...metadata});
    renderMusicJob(data.job);
  } catch (error) {
    $("musicStatus").textContent=error.message || '提交结果待核对，不会重复发送。';
    if (error.status) { localStorage.removeItem('workbench.music.request'); $("musicGenerate").disabled=false; }
  }
});
setInterval(() => { if (location.hash === '#music') void recoverMusicJob(); }, 2500);
window.addEventListener('online', () => void recoverMusicJob());
void recoverMusicJob();

// Account cards keep a user-dragged order; that order is the Gemini round-robin circle.
const bareAccountKey = key => String(key || "").replace(/^flow-account-/, "");
let accountOrderOverride = null;
const accountDrag = {key: null};
let accountPoolService = 'flow';
const accountPoolTabs = [$('flowAccountTab'), $('geminiAccountTab')];

function selectAccountPool(service, focus = false) {
  if (accountDrag.key || accountActionsInFlight.size) return;
  accountPoolService = service === 'gemini' ? 'gemini' : 'flow';
  const gemini = accountPoolService === 'gemini';
  for (const tab of accountPoolTabs) {
    const selected = tab.dataset.accountPool === accountPoolService;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focus) tab.focus();
  }
  const panel = $(gemini ? 'geminiAccountPanel' : 'flowAccountPanel');
  const table = $('realAccountTable');
  if (table.parentElement !== panel) panel.prepend(table);
  $('flowAccountPanel').hidden = gemini;
  $('geminiAccountPanel').hidden = !gemini;
  for (const id of ['geminiPoolOrderHint','geminiPoolActivity','geminiPoolChat','activationScheduleToggle']) $(id).hidden = !gemini;
  if (!gemini) {
    $('activationSchedulePanel').hidden = true;
    $('activationScheduleToggle').setAttribute('aria-expanded','false');
  }
  try { localStorage.setItem('accountPoolService', accountPoolService); } catch (_) {}
  renderAccounts();
}

for (const [index, tab] of accountPoolTabs.entries()) {
  tab.addEventListener('click', () => selectAccountPool(tab.dataset.accountPool));
  tab.addEventListener('keydown', event => {
    const next = {ArrowLeft: 1-index, ArrowRight: 1-index, Home: 0, End: 1}[event.key];
    if (next === undefined) return;
    event.preventDefault();
    selectAccountPool(accountPoolTabs[next].dataset.accountPool, true);
  });
}
function applyAccountOrder(list) {
  const order = accountOrderOverride || state.channelStatus?.shared_account_order || state.channelStatus?.account_order;
  if (!Array.isArray(order) || !order.length) return list;
  const rank = new Map(order.map((key, index) => [key, index]));
  return [...list].sort((x, y) =>
    (rank.get(bareAccountKey(x.key)) ?? order.length) - (rank.get(bareAccountKey(y.key)) ?? order.length));
}
function saveAccountOrderFromDom() {
  if (!accountDrag.key) return;
  const visible = [...document.querySelectorAll("#realAccountTable [data-drag-key]")].map(el => el.dataset.dragKey);
  const reordered = new Set(visible);
  const all = applyAccountOrder(state.snapshot?.real_accounts?.items || []).map(item => bareAccountKey(item.key));
  let index = 0;
  accountOrderOverride = all.map(key => reordered.has(key) ? visible[index++] : key);
  const order = accountOrderOverride;
  accountDrag.key = null;
  renderAccounts();
  postJson("/api/account-order", {order}).then(() => refreshChannelStatus()).catch(() => {});
}
$("realAccountTable").addEventListener("dragstart", event => {
  const card = event.target.closest("[data-drag-key]");
  if (!card) return;
  accountDrag.key = card.dataset.dragKey;
  card.classList.add("is-dragging");
  event.dataTransfer.effectAllowed = "move";
  try { event.dataTransfer.setData("text/plain", accountDrag.key); } catch (_) {}
});
$("realAccountTable").addEventListener("dragover", event => {
  if (!accountDrag.key) return;
  const card = event.target.closest("[data-drag-key]");
  if (!card || card.dataset.dragKey === accountDrag.key) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  const rect = card.getBoundingClientRect();
  const before = (event.clientY - rect.top) / rect.height < .5;
  const dragging = $("realAccountTable").querySelector(".account-card.is-dragging");
  if (dragging && dragging !== card) card.parentNode.insertBefore(dragging, before ? card : card.nextSibling);
});
$("realAccountTable").addEventListener("drop", event => {
  if (!accountDrag.key) return;
  event.preventDefault();
  saveAccountOrderFromDom();
});
$("realAccountTable").addEventListener("dragend", () => {
  $("realAccountTable").querySelector(".account-card.is-dragging")?.classList.remove("is-dragging");
  saveAccountOrderFromDom();
});

function accountAvatarMarkup(identity, modelOptions) {
  const options = Array.isArray(modelOptions) ? modelOptions : [];
  const neon = options.some(option => option?.family === 'gemini-4-neon');
  const carbon = options.some(option => option?.family === 'gemini-4-carbon');
  const models = [neon && 'Gemini 4 Neon', carbon && 'Gemini 4 Carbon'].filter(Boolean);
  const label = `可用模型：${models.join('、')}`;
  const initial = esc(String(identity).slice(0, 1).toUpperCase());
  const attributes = models.length ? `role="img" aria-label="${esc(label)}" title="${esc(label)}"` : 'aria-hidden="true"';
  return `<div class="account-avatar${neon ? ' has-gemini-neon' : ''}${carbon ? ' has-gemini-carbon' : ''}" ${attributes}>${initial}</div>`;
}

function flowNetworkSlot(binding) {
  const label = Number.isInteger(binding?.port)
    ? `端口 ${binding.port} · ${binding.public_ip || '公网 IP 待核验'}`
    : '待分配';
  return `<p class="account-check-status" data-flow-network-slot>网络位 · ${esc(label)}</p>`;
}

function renderAccounts() {
  const opened = [...document.querySelectorAll('.account-details[open] summary')].map(e=>e.getAttribute('aria-controls'));
  const focusedDetail = document.activeElement?.closest('.account-details')?.querySelector('summary')?.getAttribute('aria-controls');
  const table = $('realAccountTable');
  const expanded = new Set([...table.querySelectorAll('[data-account-fold][open]')].map(detail => detail.dataset.accountFold));
  const focusedFold = document.activeElement?.closest('summary')?.parentElement?.dataset.accountFold;
  const focusedRetry = document.activeElement?.closest('select[data-retry-project]')?.dataset.retryProject;
  if (focusedRetry || accountActionsInFlight.size) return;
  const flow = accountPoolService === 'flow';
  const data = state.snapshot?.real_accounts;
  if (!data && !state.snapshotAttempted) {
    $('realAccountTable').innerHTML = empty('正在读取账号记录', '只读取现有状态，不会发起真实调用。');
    return;
  }
  if (!data?.available) {
    for (const id of ['accountPoolTotal','flowAccountReady','geminiAccountReady']) $(id).textContent = '—';
    for (const id of ['flowAccountTotal','geminiAccountTotal']) $(id).textContent = '/ —';
    $('flowAccountBalance').textContent = '就绪积分 —';
    $("realAccountTable").innerHTML = empty("账号暂不可读取", data?.message || "请检查 Flow 后台连接。");
    $("saveDispatchPolicy").disabled = true;
    $('geminiSend').disabled = true; // Legacy Gemini sender removed.
    syncConversationControls(true);
    return;
  }
  const policy = data.dispatch_policy;
  if (policy && Number.isInteger(policy.five_hour_limit) && Number.isInteger(policy.weekly_limit)) {
    if (!dispatchPolicyDirty && !dispatchPolicySaving) {
      $("fiveHourLimit").value = policy.five_hour_limit;
      $("weeklyLimit").value = policy.weekly_limit;
      $("dispatchPolicyMessage").textContent = "当前规则已读取。";
    }
    $("saveDispatchPolicy").disabled = dispatchPolicySaving;
  } else {
    $("saveDispatchPolicy").disabled = true;
    $("dispatchPolicyMessage").textContent = "规则暂不可读取。";
  }
  const registered = Array.isArray(data.items) ? data.items : [];
  const flowAccounts = registered.filter(item => !item.flow_removed);
  const accounts = flow ? flowAccounts : applyAccountOrder(registered).filter(item => !item.gemini_removed);
  const flowReadinessNow = flowReadiness(state.snapshot);
  const geminiReadinessNow = geminiReadiness(state.snapshot);
  const flowBalance = flowReadinessNow.usableBalance == null ? '—' : flowReadinessNow.usableBalance.toLocaleString('zh-CN');
  $('accountPoolTotal').textContent = registered.length;
  $('flowAccountReady').textContent = flowReadinessNow.ready ?? '—';
  $('flowAccountTotal').textContent = '/ ' + flowAccounts.length;
  $('flowAccountBalance').textContent = `就绪积分 ${flowBalance}${flowReadinessNow.stale ? ' · 显示上次核对结果' : ''}`;
  $('geminiAccountReady').textContent = geminiReadinessNow.ready ?? '—';
  $('geminiAccountTotal').textContent = '/ ' + geminiReadinessNow.total;
  for (const select of [$("geminiAccount"), $("chatAccount")]) {
    renderGeminiAccountOptions(select, applyAccountOrder(registered));
  }
  syncConversationControls(true);
  if (!state.geminiBusy) {
    syncQuickGeminiControls();
    if (!sessionStorage.getItem("geminiRequest")) $("geminiMessage").textContent = geminiSelectionReady($('geminiAccount').value) ? "请选择账号，输入内容后发送。" : `Gemini 号池共 ${geminiReadinessNow.total} 个账号；不可用的账号已列出原因。`;
  }
  if (!accounts.length) {
    $("realAccountTable").innerHTML = empty(flow ? "没有已登记的 Flow 账号" : "Gemini 号池暂无账号", "点击上方“添加本机账号”开始接入。");
    return;
  }
  const healthLabels = {ready:"可用", cooling:"冷却中", paused:"已暂停 · 待核查", attention:"需要检查", empty:"积分不足", unknown:"状态未知"};
  const autoLabels = {eligible:"可参与自动选号", refresh_needed:"自动选号前会核对用量", usage_unknown:"用量未知，不能自动选号", limit_reached:"上次用量已达到上限", disabled:"Gemini 已暂停", busy:"账号正在处理", unresolved:"有结果未确认的请求", login_unconfirmed:"Gemini 登录待核对", browser_unknown:"浏览器状态暂不可核对", browser_unavailable:"账号浏览器未确认运行"};
  const flowTasks = (state.snapshot?.tasks || []).filter(t => ['image','video'].includes(t.kind));
  const issueCounts = new Map();
  const affectedAccounts = new Set();
  for (const account of accounts) for (const issue of accountIssues(account,state.snapshot,geminiReadinessNow.rows.get(account.key))) {
    if (issue.key.startsWith('flow_') !== flow) continue;
    if (['historical_unconfirmed', 'quota_stale', 'gemini_confirmed_verification'].includes(issue.key)) continue;
    if (autoFlowRecoveryProfiles.has(account.profile) && ['flow_unknown','flow_page_state'].includes(issue.key)) continue;
    affectedAccounts.add(account.key);
    const previous = issueCounts.get(issue.key);
    issueCounts.set(issue.key, {title: issue.title, count: (previous?.count || 0) + 1});
  }
  const issueSummary = affectedAccounts.size ? `<details class="account-issue-summary" data-account-fold="pool-alerts"><summary><strong>需关注</strong> <span>${affectedAccounts.size} 个账号</span></summary><div class="account-issue-summary-items">${Array.from(issueCounts.values()).map(issue => `<span>${esc(issue.title)} <b>${issue.count}</b></span>`).join('')}</div></details>` : '';
  // An in-progress drag owns the DOM; a snapshot refresh must not rebuild the cards mid-drag.
  if (accountDrag.key) return;
  $("realAccountTable").innerHTML = `${issueSummary}<div class="account-grid">${accounts.map((item) => {
    const identity = item.email || item.label || item.profile;
    const official = officialAccount(item);
    const auditedFlowRow=flowReadinessNow.rows.get(item.profile);
    const flowRow=autoFlowRecoveryProfiles.has(item.profile)
      ? {...auditedFlowRow, checking:true, recovering:true, status:'checking', issues:[]}
      : auditedFlowRow;
    const geminiRow=geminiReadinessNow.rows.get(item.key);
    const auth=item.gemini_removed ? null : governanceReauthFor(item);
    const authControl=auth?.mode!=='login_only' ? '' : auth.state==='waiting' ? '<span class="subtle">请到 Ubuntu 浏览器完成授权</span>' :
      auth.state==='completed' ? '<span class="subtle">授权已更新 · 待验活</span>' :
      auth.can_start ? `<button class="button button-light" type="button" data-govern-reauth="${esc(auth.request_id)}">打开 Ubuntu 授权页</button>` : '';
    const issues = accountIssues(item,state.snapshot,geminiRow).filter(issue => issue.key !== 'historical_unconfirmed'
      && issue.key.startsWith('flow_') === flow);
    const issueBadges = issues.filter(issue => !['quota_stale', 'flow_paused', 'flow_cooling', 'flow_empty', 'flow_unknown', 'unresolved', 'gemini_disabled', 'keepalive_disabled', 'keepalive_paused', 'gemini_confirmed_verification'].includes(issue.key));
    const hasBalance = typeof item.flow_balance === "number" && Number.isFinite(item.flow_balance) && item.flow_balance >= 0;
    const balance = hasBalance ? item.flow_balance.toLocaleString("zh-CN") : "—";
    const freshness = !hasBalance ? "尚未读取" : item.flow_balance_fresh ? "最近读取" : "上次读取，可能已过期";
    const observed = typeof item.flow_balance_observed === "number" ? ` · ${dateTime(item.flow_balance_observed)}` : "";
    const signIn = flowLoginNotice(flowRow?.page);
    const login = signIn?.label || (item.flow_login === 'verified' ? 'Flow 登录已核对' : 'Flow 登录待确认');
    const geminiUsage = item.gemini_usage?.current_5h?.usage_percentage;
    const weeklyUsage = item.gemini_usage?.weekly?.usage_percentage;
    const checkedAt = state.snapshot?.checked_at;
    const usageAge = typeof checkedAt === 'number' && typeof item.gemini_usage_checked === 'number' ? checkedAt - item.gemini_usage_checked : null;
    const usageStale = usageAge === null || !Number.isFinite(usageAge) || usageAge < 0 || usageAge > 900;
    const keepaliveText = item.keepalive_enabled === false ? '自动保活已手动暂停'
      : item.gemini_enabled === false ? '自动保活随 Gemini 使用暂停'
      : item.keepalive_state === 'blocked_result' ? '保活等待旧请求结果核对，不会自动执行'
      : item.keepalive_state === 'paused' ? '保活已暂停；处理原因后可单次恢复'
      : item.keepalive_state === 'dispatching' ? '正在检查或保活，请勿重复操作'
      : item.keepalive_next_due ? `下次保活不早于 ${dateTime(item.keepalive_next_due)}` : '保活时间待确认';
    const keepaliveBadge = item.keepalive_enabled === false || item.gemini_enabled === false || item.keepalive_state === 'paused' ? '保活已暂停'
      : item.keepalive_state === 'blocked_result' ? '保活待核对'
      : item.keepalive_state === 'dispatching' ? '保活处理中'
      : item.keepalive_next_due ? '保活已排程' : '保活待确认';
    const keepaliveWarn = item.keepalive_enabled === false || item.gemini_enabled === false || ['blocked_result', 'paused'].includes(item.keepalive_state);
    const cooling = item.flow_cooling === true || item.flow_health === 'cooling';
    const pageIncident = item.flow_page_incident;
    const flowPaused = item.flow_risk_paused === true || !!pageIncident;
    const flowReady = flowRow?.status === 'ready' || flowRow?.status === 'queue';
    const callState = officialAccount(item);
    const geminiReady = geminiAccountAvailability(item).manual;
    const avatarModels = !flow && geminiReady && !callState.data?.advanced_quota_restriction
      ? item.gemini_model_options : null;
    const callBlocked = callState.paused ||
      (!channelReadFailed && state.channelStatus?.available === true && state.channelStatus.production_send_available === false) ||
      callState.data?.dispatch_eligible === false;
    const quotaKey = item.key.slice('flow-account-'.length);
    const geminiCard = geminiCardStatus(geminiReady, callBlocked,
      officialQuotaReadFailed ? null : officialQuotaData?.accounts?.[quotaKey], Date.now()/1000);
    const flowIssues = flowRow?.issues || [];
    const geminiIssues = issues.filter(issue => !issue.key.startsWith('flow_') && issue.key !== 'quota_stale' && issue.key !== 'gemini_confirmed_verification');
    const windowIssue = flowWindowIssue(item.profile, state.snapshot);
    const flowFallback = (!flowReady && !flowRow?.recovering && !cooling && !flowPaused && !windowIssue && !flowIssues.length)
      ? ({blocked:'账号处于不可派发状态，等待就绪核对通过。', unknown:'账号状态尚未完成核对，等待下次检查。', checking:'正在验活，请稍候。'}[flowRow?.status] || '账号状态待核对，等待下次检查。') : '';
    const geminiFallback = (!item.gemini_removed && !geminiReady && !geminiIssues.length && !(geminiRow?.checking))
      ? ({blocked:'Gemini 处于不可派发状态，等待就绪核对通过。', unknown:'Gemini 状态尚未完成核对，等待下次检查。'}[geminiRow?.status] || 'Gemini 状态待核对，等待下次检查。') : '';
    // Derive each service's presentation from its own recorded state only.
    const flowNeedsAttention = flowRow?.status==='blocked' && !cooling;
    const geminiNeedsAttention = !item.gemini_removed && geminiCard.attention;
    const needsAttention = flow ? flowNeedsAttention : geminiNeedsAttention;
    const geminiLastCheck = item.gemini_enabled === false ? '已暂停' : item.gemini === 'verified' ? '上次已核对' : '待核对';
    const geminiStatusText = item.gemini_removed ? '已从号池删除' : geminiCard.label;
    const cardState = (flow && flowPaused ? ' is-flow-paused' : flow && cooling ? ' is-flow-cooling' : '') + (needsAttention ? ' is-account-issue' : '');
    const flowStatusText = pageIncident ? '已停派' : ['flow_landing','login','verification_required'].includes(flowRow?.page) ? '暂不派发'
      : item.flow_preflight_reasons?.length && !flowPaused && !cooling ? '自检未通过' : readinessBadge(flowRow);
    const cooldownSeconds = typeof item.flow_cooldown_seconds === 'number' && Number.isFinite(item.flow_cooldown_seconds) && item.flow_cooldown_seconds > 0 ? item.flow_cooldown_seconds : null;
    const cooldownEnd = cooldownSeconds === null ? null : (state.snapshotReceivedAt || Date.now()) + cooldownSeconds * 1000;
    const coolingInfo = cooling ? (cooldownSeconds === null ? '本地冷却中，剩余时间待核对。' : '圆圈内为上次读取后估算的本地冷却剩余时间；到时后请刷新核对，服务方是否恢复以实际状态为准。') : '';
    const accountKey = esc(item.key.slice("flow-account-".length));
    const flowReason = flowAccountReason(item, state.snapshot, {...flowRow,recovering:autoFlowRecoveryProfiles.has(item.profile)},
      flowReadinessObservation?.pageChecks?.accounts?.[item.profile]);
    const accountProjects = (state.snapshot?.projects || []).filter(p => p.source_profile === item.profile);
    const selectedRetryProject = retryProjectDrafts.get(accountKey) ?? state.snapshot?.retry_projects?.[item.profile] ?? '';
    const ownFlow = flowTasks.filter(t => t.source_profile === item.profile);
    const flowDone = ownFlow.filter(t => t.status === 'succeeded').length;
    const flowFailed = ownFlow.filter(t => t.status === 'failed').length;
    const ownGemini = state.snapshot?.gemini_stats?.accounts?.[item.key.slice('flow-account-'.length)];
    const flowPerformance = flowDone + flowFailed ? `任务 ${flowDone} 成功 / ${flowFailed} 失败 · 成功率 ${Math.round(flowDone * 100 / (flowDone + flowFailed))}%` : '暂无已结束生成任务';
    const gemPerformance = ownGemini ? `对话 ${ownGemini.completed} 完成 · 平均首段 ${typeof ownGemini.avg_first_ms === 'number' ? (ownGemini.avg_first_ms / 1000).toFixed(1) + ' 秒' : '暂无实测'}` : '暂无算力中心对话记录';
    const quotaView = item.gemini_removed ? {html:'',message:''} : accountOfficialQuotaMarkup(quotaKey);
    const flowNextCredit = item.flow_next_credit_date && /^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$/.test(item.flow_next_credit_date) && item.flow_next_credit_date >= new Date().toLocaleDateString("sv-SE", {timeZone:"Asia/Shanghai"}) ? item.flow_next_credit_date : '';
    const flowBalanceTitle = `积分余额${hasBalance && !item.flow_balance_fresh ? '（上次读取）' : ''}${flowNextCredit ? ' · 下次积分到账 ' + flowNextCredit : ''}`;
    const winRow = (state.snapshot?.flow_coordination?.windows || []).find(w => w.profile === item.profile && w.state === 'result_unknown' && w.task_id);
    const govRelease = winRow ? `<button type="button" class="button button-primary" style="margin-top:6px" data-govern-review="${esc(winRow.task_id)}" data-review-account="${esc(item.key)}">已核对，放行账号</button>` : '';
    const geminiVerify = (geminiRow && geminiRow.status !== 'ready' && geminiRow.status !== 'queue') ? `<button type="button" class="button button-light" style="margin-top:6px" data-gemini-verify>开始 Gemini 验证</button>` : '';
    const removeGemini = item.gemini_registered && !item.gemini_removed
      ? `<button class="button button-light" type="button" data-remove-gemini="${accountKey}" aria-label="从 Gemini 号池删除 ${esc(identity)}"${item.gemini_busy ? ' disabled title="该账号正在处理 Gemini 请求"' : ''}>删除 Gemini 账号</button>` : '';
    const activeFlowTask = ownFlow.some(task => ['queued','running'].includes(task.status)
      || task.retry?.state === 'pending_switch');
    const remoteFlow = flow && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(item.profile || '')
      ? `<a class="button button-light" href="http://127.0.0.1:18888/remote#profile=${encodeURIComponent(item.profile)}" target="_blank" rel="noopener noreferrer" aria-label="远程登录 ${esc(identity)}">远程登录</a>` : '';
    const removeFlow = flow && item.email
      ? `<button class="button button-light" type="button" data-remove-flow="${accountKey}" aria-label="从 Flow 号池移除 ${esc(identity)}"${activeFlowTask ? ' disabled title="该账号仍有进行中的生成任务"' : ''}>移除 Flow 账号</button>` : '';
    const flowNotice = flow ? flowAccountNoticeMarkup(flowReason, pageIncident || state.snapshot?.flow_warning_screenshots?.[item.profile]) : '';
    const diagnostics = (flow ? [
      flowReason?.showOnCard ? '' : flowNotice,
      windowIssue && (flowPaused || cooling) ? `<p><strong>Flow：</strong>${esc(windowIssue.title)}</p>` : '',
      flowFallback ? `<p><strong>Flow：</strong>${esc(flowFallback)}</p>` : '',
    ] : [
      geminiIssues.map(issue => `<p><strong>Gemini · ${esc(issue.title)}</strong><br>${esc(issue.impact || '')} ${esc(issue.action || '')}</p>`).join(''),
      geminiFallback ? `<p><strong>Gemini：</strong>${esc(geminiFallback)}</p>` : '',
    ]).join('');
    return `<article class="account-card${cardState}"${flow ? '' : ` draggable="true" data-drag-key="${esc(quotaKey)}"`}>
      <div class="account-card-heading">${accountAvatarMarkup(identity, avatarModels)}<div class="account-identity"><span class="account-alias">${esc(item.label || item.profile)}</span><h3 title="${esc(identity)}">${esc(identity)}</h3></div>${flow ? `<span class="account-login ${signIn ? 'is-login-required' : item.flow_login === 'verified' ? 'is-ready' : ''}">${esc(login)}</span>` : ''}
        <div class="account-detail-anchor"><details class="account-details"><summary aria-label="查看 ${esc(identity)} 的详细状态" aria-controls="account-detail-${accountKey}">详情</summary></details><div id="account-detail-${accountKey}" class="account-detail-popup"><strong>账号状态详情</strong>${flow ? `<p>Flow：${esc(flowStatusText)} · ${esc(freshness + observed)}。${hasBalance ? '剩余 ' + esc(balance) + ' 积分。' : ''}未提供积分总额度。</p>${coolingInfo ? `<p>${esc(coolingInfo)}</p>` : ''}<p>Flow · ${esc(flowPerformance)}</p>` : `${officialDetails(official)}<p>Gemini · ${esc(gemPerformance)}</p>`}${issues.length ? `<div class="account-detail-issues"><strong>状态说明</strong>${issues.map(issue => `<p><b>${esc(issue.title)}</b><br>${esc(issue.impact)} ${esc(issue.action)}</p>`).join('')}</div>` : ''}</div></div></div>
      <div class="account-services account-services-single">${flow ? `<div class="account-service account-service-flow${flowNeedsAttention ? ' is-service-issue' : ''}" role="group" aria-label="${esc(('Flow：' + flowStatusText))}" title="${esc(('Flow：' + flowStatusText))}"><div class="account-service-heading"><span>Flow</span><strong class="account-health ${flowPaused ? 'is-paused' : cooling ? 'is-cooling' : flowRow?.status!=='ready' ? 'is-unknown' : ''}">${esc(flowStatusText)}</strong></div><div class="account-flow-value"><div title="${esc(flowBalanceTitle)}"><strong>${esc(balance)}</strong><small class="account-quota-reset">下次积分到账 ${esc(flowNextCredit || '待官方更新')}${flowNextCredit && !item.flow_balance_fresh ? '（上次读取）' : ''}</small></div>${cooling ? `<div class="account-cooldown" data-risk-paused="${flowPaused}"${cooldownEnd !== null ? ` data-cooldown-end="${cooldownEnd}"` : ' aria-label="冷却中，剩余时间待核对"'}><span class="account-cooldown-time">—</span><span class="account-cooldown-label">冷却中</span></div>` : '<span class="account-credit-icon" aria-hidden="true">✦</span>'}</div></div>` : `<div class="account-service account-service-gemini${geminiNeedsAttention ? ' is-service-issue' : ''}" role="group" aria-label="${esc('Gemini：'+geminiStatusText+(quotaView.message ? ' · '+quotaView.message : ''))}" title="${esc('Gemini：'+geminiStatusText+(quotaView.message ? ' · '+quotaView.message : ''))}"><div class="account-service-heading"><span>Gemini</span><strong class="account-health${geminiReady && !geminiNeedsAttention ? '' : ' is-unknown'}">${esc(geminiStatusText)}</strong></div>${item.gemini_removed ? '' : `<div data-account-quota="${accountKey}">${quotaView.html}</div>`}</div>`}</div>
      ${flow ? flowNetworkSlot(item.exit_info) : ''}
      ${flow && flowReason?.showOnCard ? flowNotice : ''}
      ${!flow && geminiRow?.checking ? `<p class="account-check-status">Gemini · ${esc(geminiRow.note || '正在检查云端响应')}</p>` : ''}
      ${diagnostics ? `<details class="account-alert" data-account-fold="alert-${accountKey}"><summary><strong>${needsAttention || flow && flowPaused ? '异常详情' : '状态说明'}</strong><span>${flow ? 'Flow' : 'Gemini'}</span></summary><div class="account-alert-body">${diagnostics}</div></details>` : ''}
      ${flow ? `<div class="account-card-footer"><details class="account-retry" data-account-fold="retry-${accountKey}"><summary>切号备用项目</summary><p class="subtle">切号重试将在此账号选定的项目生成，可能消耗积分。未指定时不会用此账号切号。</p><select aria-label="切号备用项目" data-retry-project="${accountKey}"><option value="">不指定</option>${accountProjects.map(p => `<option value="${esc(p.key)}" ${p.key === selectedRetryProject ? 'selected' : ''}>${esc(p.title)} · ${esc(shortId(p.source_id))}</option>`).join('')}</select><button class="text-link" type="button" data-save-retry-project="${accountKey}">保存备用项目</button></details>${removeFlow ? `<details class="account-preferences" data-account-fold="flow-actions-${accountKey}"><summary>账号操作</summary><div class="account-card-actions">${removeFlow}</div></details>` : ''}</div>` : removeGemini ? `<div class="account-card-footer"><details class="account-preferences" data-account-fold="actions-${accountKey}"><summary>账号操作</summary><div class="account-card-actions">${removeGemini}</div></details></div>` : ''}
      <div class="account-card-actions">${flow ? remoteFlow+govRelease : authControl+geminiVerify}${flow && flowPaused ? `<button class="button button-light" type="button" data-resume-flow="${accountKey}"${pageIncident ? ` data-incident="${esc(pageIncident.id)}"` : ''}>已核查，恢复 Flow</button>` : ''}</div></article>`;
  }).join("")}</div>`;
  updateAccountUsageBars();
  updateAccountCooldowns();
  for (const id of opened) { const summary = document.querySelector(`[aria-controls="${id}"]`); if (summary) summary.parentElement.open = true; }
  for (const detail of table.querySelectorAll('[data-account-fold]')) {
    const key = detail.dataset.accountFold;
    detail.open = expanded.has(key);
    if (key === focusedFold) detail.querySelector('summary').focus({preventScroll:true});
  }
  if (focusedDetail) document.querySelector(`[aria-controls="${focusedDetail}"]`)?.focus({preventScroll:true});
}

try {
  if (localStorage.getItem('accountPoolService') === 'gemini') selectAccountPool('gemini');
} catch (_) {}

async function removeFlowAccountFromPanel(button) {
  const key = button.dataset.removeFlow;
  const account = state.snapshot?.real_accounts?.items?.find(item => item.key === 'flow-account-' + key);
  if (button.disabled || !account?.email || account.flow_removed) return;
  if (!window.confirm(`从 Flow 号池移除 ${account.email}？该账号将停止 Flow 派发。退出旧浏览器后，网络位会自动释放。项目、任务历史和 Gemini、Music 使用资格会保留。`)) return;
  const feedback = $('accountActionMessage');
  button.disabled = true;
  accountActionsInFlight.add(button);
  feedback.textContent = '正在从 Flow 号池移除账号…';
  try {
    const result = await postJson('/api/flow/accounts/remove', {account:key, confirmed:true});
    if (result.removed !== true) throw new Error('移除结果未确认，请刷新账号页核对。');
    const current = state.snapshot?.real_accounts?.items?.find(item => item.key === account.key);
    if (current) current.flow_removed = true;
    feedback.textContent = result.message;
    await refresh();
  } catch (error) {
    feedback.textContent = error.message || '移除未完成，请刷新账号页核对。';
  } finally {
    accountActionsInFlight.delete(button);
    button.disabled = false;
    if (state.view === 'accounts') renderAccounts();
  }
}

async function removeGeminiAccountFromPanel(button) {
  const accountKey = button.dataset.removeGemini;
  const findAccount = () => state.snapshot?.real_accounts?.items?.find(item=>item.key==='flow-account-'+accountKey);
  const account = findAccount();
  if (button.disabled || !account?.gemini_registered || account.gemini_removed) return;
  const name = account.email || account.label || account.profile;
  if (!window.confirm(`从 Gemini 号池删除 ${name}？此账号将停止 Gemini 派发，历史对话会保留。Flow 和 Music 不受影响。`)) return;
  const feedback = $('accountActionMessage');
  button.disabled = true;
  accountActionsInFlight.add(button);
  feedback.textContent = '正在从 Gemini 号池删除账号…';
  try {
    const result = await postJson('/api/gemini/accounts/remove', {account:accountKey,confirmed:true});
    if (result.removed !== true) throw new Error('删除结果未确认，请刷新账号页核对。');
    await Promise.allSettled([refresh(), refreshChannelStatus(), refreshOfficialQuota()]);
    const updated = findAccount();
    if (updated) Object.assign(updated,{gemini_removed:true,gemini_registered:false,gemini_enabled:false});
    accountOrderOverride = null;
    feedback.textContent = result.message;
  } catch (error) {
    feedback.textContent = error.message || '删除未完成，请刷新账号页核对。';
  } finally {
    accountActionsInFlight.delete(button);
    button.disabled = false;
    if (state.view==='accounts') renderAccounts();
  }
}

async function refreshActivation() {
  if (activationUpdating) return;
  activationUpdating = true;
  try {
    const response = await fetch('/api/account-activation', {credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    const items = Array.isArray(data.items) ? data.items : [];
    const desktopIssues = Array.isArray(data.desktop_recovery) ? data.desktop_recovery : [];
    const counts = (kind) => items.filter(x => x.status === kind).length;
    $('activateAccounts').disabled = activationPosting || data.status === 'running' || !state.snapshot?.real_accounts?.available;
    const flowNote = {accepted:'Flow 积分刷新已受理，结果以账号卡片为准。', unknown:'Flow 积分刷新未确认，不会自动重复提交。', held:'上次 Flow 刷新未确认，本次跳过。', skipped_busy:'账号有任务，本次不刷新 Flow 积分。', skipped_fresh:'Flow 积分近期已更新，本次不重复刷新。', skipped_safety:'部分账号状态待核查，本次不批量刷新 Flow 积分。', skipped_poll:'轮询读取现有 Flow 状态，不重复刷新全部账号积分。'};
    $('activationProgress').textContent = data.status === 'idle' ? '点击后将逐个检查；不会重发消息、自动重试或换号。'
      : `本轮 ${items.length} 个：完成 ${counts('done')} · 等待冷却 ${counts('cooling')} · 待核查 ${counts('blocked') + counts('failed') + counts('interrupted')} · 处理中 ${counts('queued') + counts('processing') + counts('waiting_flow')}。${flowNote[data.flow_refresh] || (data.service === 'gemini' ? '本轮仅检查 Gemini，不操作 Flow。' : '正在准备 Flow 积分刷新。')}`;
    if (desktopIssues.length) $('activationProgress').textContent += ` · 有 ${desktopIssues.length} 条桌面恢复提示，查看下方详情。`;
    $('activationDetails').hidden = !items.length && !desktopIssues.length;
    const accountMap = new Map((state.snapshot?.real_accounts?.items || []).map(a => [a.key.slice('flow-account-'.length), a]));
    $('activationResults').innerHTML = items.map(i => {
      const account = accountMap.get(i.account_key);
      const classification = ['blocked', 'failed', 'interrupted'].includes(i.status) && account ? accountIssues(account).map(issue => issue.title).join('、') : '';
      return `<p>${esc(account?.email || account?.label || account?.profile || '账号')}${classification ? ` · ${esc(classification)}` : ''}：${esc(i.message)}${i.due && i.status === 'cooling' ? `（预计 ${esc(dateTime(i.due))} 后复查）` : ''}</p>`;
    }).join('') + desktopIssues.map(i => {
      const account = accountMap.get(i.account_key);
      return `<p>${esc(account?.email || account?.label || (i.account_key ? '账号' : '系统'))} · 桌面恢复：${esc(i.message)}</p>`;
    }).join('');
    const signature = JSON.stringify(items.map(i => [i.account_key, i.status]));
    if (activationSignature !== null && signature !== activationSignature) refresh();
    activationSignature = signature;
  } catch (_) {
    $('activateAccounts').disabled = true;
    $('activationProgress').textContent = '一键检查暂不可用，请检查算力中心连接。';
  } finally { activationUpdating = false; }
}

$('activateAccounts').addEventListener('click', async () => {
  if (activationPosting) return;
  activationPosting = true;
  $('activateAccounts').disabled = true;
  $('activationProgress').textContent = '正在创建本轮检查…';
  try {
    const response = await fetch('/api/account-activation', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:'{}'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '创建失败');
  } catch (error) { $('accountActionMessage').textContent = error.message || '创建状态未确认，请刷新后查看；不会自动重复提交。'; }
  finally { activationPosting = false; await refreshActivation(); }
});

let activationScheduleDirty = false;
let activationScheduleSaving = false;
let activationDailyEnabled = false;
let activationPollingEnabled = false;
function renderActivationToggle() {
  const active = [activationDailyEnabled ? '定时' : '', activationPollingEnabled ? '轮询' : ''].filter(Boolean);
  $('activationScheduleToggle').textContent = '通道与设置';
}
const activationSchedulePanel = $('activationSchedulePanel');
const activationScheduleForm = $('activationScheduleForm');
const scheduleDate = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}) : '暂无';
async function refreshActivationSchedule(force = false) {
  try {
    const response = await fetch('/api/account-activation/schedule', {credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!force && activationScheduleSaving) return;
    if (!activationScheduleDirty || force) {
      $('activationScheduleEnabled').checked = data.enabled;
      $('activationScheduleTime').value = data.time;
      activationScheduleDirty = false;
    }
    activationDailyEnabled = data.enabled;
    renderActivationToggle();
    const warning = [data.held_accounts ? `${data.held_accounts} 个账号上次检查失败，需手动核对` : '', data.flow_hold ? 'Flow 积分刷新待人工核对' : ''].filter(Boolean).join('；');
    $('activationScheduleStatus').textContent = `${data.enabled ? '已开启' : '未开启'} · 下次：${scheduleDate(data.next_due)} · 上次：${scheduleDate(data.last_due)}${data.last_note ? '（' + data.last_note + '）' : ''}${warning ? ' · ' + warning : ''}`;
  } catch (_) { $('activationScheduleStatus').textContent = '定时状态暂不可读取，请检查算力中心连接。'; }
}
$('activationScheduleToggle').addEventListener('click', () => {
  const opening = activationSchedulePanel.hidden;
  activationSchedulePanel.hidden = !opening;
  $('activationScheduleToggle').setAttribute('aria-expanded', String(opening));
  if (opening) { refreshActivationSchedule(); refreshActivationPoll(); }
});
activationScheduleForm.addEventListener('input', () => { activationScheduleDirty = true; });
activationScheduleForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (activationScheduleSaving) return;
  activationScheduleSaving = true;
  const button = activationScheduleForm.querySelector('button[type="submit"]');
  button.disabled = true;
  $('activationScheduleStatus').textContent = '正在保存…';
  try {
    const response = await fetch('/api/account-activation/schedule', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'},
      body:JSON.stringify({enabled:$('activationScheduleEnabled').checked, time:$('activationScheduleTime').value})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    await refreshActivationSchedule(true);
  } catch (error) { $('activationScheduleStatus').textContent = `${error.message || '保存状态未确认'}，请刷新核对，不会自动重试。`; }
  finally { button.disabled = false; activationScheduleSaving = false; }
});
refreshActivationSchedule();
let activationPollDirty = false;
let activationPollSaving = false;
const activationPollForm = $('activationPollForm');
async function refreshActivationPoll(force = false) {
  try {
    const response = await fetch('/api/account-activation/poll', {credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (!force && activationPollSaving) return;
    activationPollingEnabled = data.enabled;
    renderActivationToggle();
    if (!activationPollDirty || force) {
      $('activationPollEnabled').checked = data.enabled;
      $('activationPollMinutes').value = data.interval_minutes;
      activationPollDirty = false;
    }
    const account = (state.snapshot?.real_accounts?.items || []).find(a => a.key === 'flow-account-' + data.last_account);
    const name = account ? (account.email || account.label || '账号') : data.last_account ? '账号已不在列表' : '';
    const hold = data.held_accounts ? ` · ${data.held_accounts} 个账号需通过一键检查手动核对` : '';
    $('activationPollStatus').textContent = `${data.enabled ? '轮询已开启' : '轮询未开启'} · 下次：${scheduleDate(data.next_due)} · 上次：${scheduleDate(data.last_due)}${name ? ' · ' + name : ''}${data.last_note ? '（' + data.last_note + '）' : ''}${hold}`;
  } catch (_) { $('activationPollStatus').textContent = '轮询状态暂不可读取，请检查算力中心连接。'; }
}
activationPollForm.addEventListener('input', () => { activationPollDirty = true; });
activationPollForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (activationPollSaving) return;
  activationPollSaving = true;
  const button = activationPollForm.querySelector('button[type="submit"]');
  button.disabled = true;
  $('activationPollStatus').textContent = '正在保存轮询…';
  try {
    const response = await fetch('/api/account-activation/poll', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Workbench-Request':'1'},
      body:JSON.stringify({enabled:$('activationPollEnabled').checked, interval_minutes:Number($('activationPollMinutes').value)})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    await refreshActivationPoll(true);
  } catch (error) { $('activationPollStatus').textContent = `${error.message || '保存状态未确认'}，请刷新核对，不会自动重试。`; }
  finally { button.disabled = false; activationPollSaving = false; }
});
refreshActivationPoll();

function renderFlowForm() {
  const snapshot = state.snapshot;
  const project = $("flowProject");
  const previousProject = project.value;
  const accounts = snapshot?.real_accounts?.items || [];
  const byProfile = new Map(accounts.filter((a) => !a.flow_removed && a.flow_login === "verified").map((a) => [a.profile, a]));
  const projects = (snapshot?.projects || []).filter((p) => byProfile.has(p.source_profile));
  project.innerHTML = '<option value="auto">自动安排账号和项目</option>' + projects.map((p) => {
    const account = byProfile.get(p.source_profile);
    return `<option value="${esc(p.key)}">${esc(p.title)} · ${esc(account.email || account.label || account.profile)} · ${esc(shortId(p.source_id))}</option>`;
  }).join("");
  if (previousProject === "auto" || projects.some((p) => p.key === previousProject)) project.value = previousProject;
  renderFlowModels();
  $("flowSend").disabled = !!state.flowBusy || !$("flowModel").value;
}

function syncFlowRetryOption() {
  $("flowAutoRetry").disabled = !!state.flowBusy || !["image", "video"].includes($("flowKind").value) || Number($("flowCount").value) !== 1;
}

function renderFlowModels() {
  syncFlowRetryOption();
  const kind = $("flowKind").value;
  const model = $("flowModel");
  const oldModel = model.value;
  const models = state.snapshot?.flow_models?.[kind] || [];
  model.innerHTML = models.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
  if (models.some((m) => m.id === oldModel)) model.value = oldModel;
  const aspect = $("flowAspect");
  const oldAspect = aspect.value;
  const options = kind === "image" ? ["16:9", "4:3", "1:1", "3:4", "9:16"] : ["16:9", "9:16"];
  aspect.innerHTML = options.map((x) => `<option value="${x}">${x}</option>`).join("");
  if (options.includes(oldAspect)) aspect.value = oldAspect;
  $("flowVideoOptions").hidden = kind !== "video";
  $("flowSend").disabled = !!state.flowBusy || !model.value;
}


async function getJson(path, {timeoutMs = 0} = {}) {
  const controller = timeoutMs ? new AbortController() : null;
  const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetch(path, {cache: "no-store", credentials: "same-origin", ...(controller ? {signal: controller.signal} : {})});
    if (!response.ok) throw new Error("算力中心连接不可用");
    return await response.json();
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function refresh({background = false} = {}) {
  if (snapshotRefreshInFlight) return snapshotRefreshInFlight;
  if (snapshotRefreshTimer) clearTimeout(snapshotRefreshTimer);
  snapshotRefreshTimer = null;
  syncVisiblePage(state.view, background);
  snapshotRefreshInFlight = refreshSnapshot(background).catch(() => {
    // A transient rendering error must not disable the next automatic sync.
    $("lastUpdated").textContent = '自动同步暂缓 · 稍后继续';
  }).finally(() => {
    snapshotRefreshInFlight = null;
    scheduleSnapshotRefresh();
  });
  return snapshotRefreshInFlight;
}

async function refreshSnapshot(background) {
  const sequence = ++state.refreshSequence;
  if (!background) $("lastUpdated").textContent = "正在刷新…";
  const [status, snapshot] = await Promise.allSettled([
    getJson("/api/status", {timeoutMs: 3500}), getJson("/api/snapshot", {timeoutMs: 10000})
  ]);
  if (sequence !== state.refreshSequence) return;
  showConnectionState(status.status === 'fulfilled' || snapshot.status === 'fulfilled');
  state.snapshotAttempted = true;
  const snapshotReady = snapshot.status === 'fulfilled' && snapshot.value?.flow_state?.available === true;
  state.snapshotFresh = snapshotReady;
  if (snapshotReady) {
    state.snapshot = snapshot.value;
    state.snapshotReceivedAt = Date.now();
  }
  const updated = state.snapshotReceivedAt ? new Date(state.snapshotReceivedAt).toLocaleTimeString('zh-CN',
    {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}) : '';
  const partial = status.status !== 'fulfilled' || (snapshotReady &&
    (snapshot.value.library?.available === false || snapshot.value.real_accounts?.available === false));
  $("lastUpdated").textContent = snapshotReady ? `${partial ? '部分同步' : '自动同步'} · ${updated}`
    : updated ? `同步暂缓 · 保留 ${updated} 数据` : '数据暂不可读取';
  $("lastUpdated").title = snapshotReady
    ? '任务页约每 3 秒同步，其他工作页约每 5～10 秒同步。托盘期间降低频率，恢复或重新联网时立即读取。Google 积分和额度以上次核对时间为准。'
    : '本轮未取得完整快照，保留上次成功读取的数据；恢复连接后会自动同步，不会重新提交任务。';
  renderVisibleSnapshot(snapshotReady);
}

function renderVisibleSnapshot(snapshotReady) {
  if (state.view === 'overview') renderOverview();
  if (state.view === 'accounts' && (snapshotReady || !state.snapshot)) renderAccounts();
  if (state.view === 'records' && snapshotReady) renderTasks();
  if (state.view === 'tasks' && snapshotReady) {
    if ($("taskWindowsHeading")?.closest('details')?.open) renderTaskWindows();
    if (!document.activeElement?.matches('select[id^="flow"], input[id^="flow"]')) renderFlowForm();
  }
  if (state.view === 'projects' && snapshotReady) renderProjects();
  if (state.view === 'conversations') {
    renderConversationAccountFilter();
    renderRealTurnAccountLabels();
    if (state.snapshot) syncConversationControls();
  }
}

let geminiActivitySequence = 0;
async function refreshGeminiActivity({background = false} = {}) {
  const sequence = ++geminiActivitySequence;
  try {
    const data = await getJson('/api/real-pool/activity', {timeoutMs: 10000});
    if (sequence !== geminiActivitySequence) return;
    if (data.source !== 'workbench' || !Array.isArray(data.items)) throw new Error('invalid activity');
    const names = new Map((state.snapshot?.real_accounts?.items || []).map((item) => [item.key.slice('flow-account-'.length), item.email || item.label || item.profile]));
    const statuses = {dispatching:'处理中', completed:'已完成', failed:'失败', unknown:'结果未确认'};
    $('geminiActivity').innerHTML = data.items.length ? `<table><thead><tr><th>时间</th><th>账号</th><th>处理来源</th><th>状态</th><th>说明</th></tr></thead><tbody>${data.items.map((item) => `<tr><td>${esc(dateTime(item.created))}</td><td>${esc(names.get(item.account_key) || '账号已不在当前列表')}</td><td>${item.engine === 'official_antigravity_cli' ? '官方账号池' : /Antigravity CLI/.test(item.note || '') ? '官方 CLI · 独立实测' : item.engine === 'pooldesk' ? (item.engine_version === 'core-v2' ? 'PoolDesk 核心' : 'PoolDesk 适配版') : '原算力中心'}</td><td>${esc(statuses[item.status] || '状态未知')}</td><td>${esc(safeRecordedNote(item.note) || (item.status === 'dispatching' ? '正在处理' : '—'))}</td></tr>`).join('')}</tbody></table>` : empty('暂无算力中心 Gemini 请求记录');
  } catch (_) {
    if (!background && sequence === geminiActivitySequence) $('geminiActivity').innerHTML = empty('请求记录暂不可读取');
  }
}

$('refreshGeminiActivity').addEventListener('click', refreshGeminiActivity);

for (const id of ['fiveHourLimit', 'weeklyLimit']) $(id).addEventListener('input', () => { dispatchPolicyDirty = true; });
$('dispatchPolicyForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (dispatchPolicySaving) return;
  const five = Number($('fiveHourLimit').value), weekly = Number($('weeklyLimit').value);
  if (!Number.isInteger(five) || !Number.isInteger(weekly) || five < 1 || five > 100 || weekly < 1 || weekly > 100) {
    $('dispatchPolicyMessage').textContent = '请输入 1 到 100 的整数。';
    return;
  }
  dispatchPolicySaving = true;
  $('saveDispatchPolicy').disabled = true;
  $('dispatchPolicyMessage').textContent = '正在保存…';
  try {
    await postJson('/api/real-pool/dispatch-policy', {five_hour_limit: five, weekly_limit: weekly});
    dispatchPolicyDirty = false;
    dispatchPolicySaving = false;
    await refresh();
    $('dispatchPolicyMessage').textContent = '规则已保存，仅影响新的自动选号。';
  } catch (_) {
    $('dispatchPolicyMessage').textContent = '保存结果未确认，请刷新页面核对后再操作。';
  } finally {
    dispatchPolicySaving = false;
    $('saveDispatchPolicy').disabled = !state.snapshot?.real_accounts?.available;
  }
});

function showGeminiRecord(record) {
  const status = record?.status;
  const account = state.snapshot?.real_accounts?.items?.find((item) => item.key === "flow-account-" + record?.account_key);
  const accountName = account ? account.email || account.label || account.profile : "";
  $("geminiReply").innerHTML = status === "completed" ? renderGeminiReply(record) : "";
  $("geminiNewRequest").hidden = status !== "unknown";
  state.geminiBusy = status === "dispatching" || status === "unknown";
  syncQuickGeminiControls();
  $("geminiFiles").disabled = state.geminiBusy;
  $("geminiMessage").textContent = status === "dispatching" ? "正在等待 Gemini 回复，请不要重复发送。" :
    status === "completed" ? `已收到 Gemini 的真实回复${accountName ? `（${accountName}）` : ""}。` :
    status === "failed" ? record.note || "本次没有提交，请检查账号后再操作。" :
    "结果尚未确认，不会自动重新发送；请先核对。";
}

async function observeGeminiRequest(id, attempts = 0) {
  if (sessionStorage.getItem("geminiRequest") !== id) return;
  try {
    const response = await fetch(`/api/real-pool/requests/${id}`, {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("not available");
    const record = await response.json();
    showGeminiRecord(record);
    if (record.status === "dispatching" && attempts < 300) setTimeout(() => observeGeminiRequest(id, attempts + 1), 3000);
    else if (record.status === "dispatching") showGeminiRecord({status: "unknown"});
  } catch (_) {
    showGeminiRecord({status: "unknown"});
  }
}

$('realAccountTable').addEventListener('change', event => {
  const select = event.target.closest('select[data-retry-project]');
  if (select) retryProjectDrafts.set(select.dataset.retryProject, select.value);
});
$("realAccountTable").addEventListener("click", async (event) => {
  if (event.target.closest('[data-govern-reauth]')) return handleGovernanceClick(event);
  const govReleaseButton = event.target.closest('[data-govern-review]');
  if (govReleaseButton) {
    if (!window.confirm(`已核对任务 ${govReleaseButton.dataset.governReview}，现在放行账号并删除本次警告截图？原任务不会重发。`)) return;
    govReleaseButton.disabled = true;
    try {
      await postJson('/api/flow-readiness/review', {account: govReleaseButton.dataset.reviewAccount, task: govReleaseButton.dataset.governReview, confirmed: true});
      $("accountActionMessage").textContent = '已放行账号，输入管线恢复。';
      if (typeof startGovernanceAudit === 'function') void startGovernanceAudit('flow', false);
      await refresh();
    } catch (error) { $("accountActionMessage").textContent = error.message || '放行未完成，请稍后重试。'; }
    finally { govReleaseButton.disabled = false; }
    return;
  }
  const geminiVerifyButton = event.target.closest('[data-gemini-verify]');
  if (geminiVerifyButton) {
    geminiVerifyButton.disabled = true;
    try {
      $("accountActionMessage").textContent = '正在核对 Gemini 授权和额度。';
      openGovernance('gemini');
    } catch (error) { $("accountActionMessage").textContent = error.message || '检查未完成。'; }
    finally { geminiVerifyButton.disabled = false; }
    return;
  }
  const removeFlowButton = event.target.closest('[data-remove-flow]');
  if (removeFlowButton) {
    await removeFlowAccountFromPanel(removeFlowButton);
    return;
  }
  const removeGeminiButton = event.target.closest('[data-remove-gemini]');
  if (removeGeminiButton) {
    await removeGeminiAccountFromPanel(removeGeminiButton);
    return;
  }
  const button = event.target.closest("[data-check-gemini], [data-toggle-gemini], [data-keepalive-gemini], [data-recover-gemini], [data-resume-flow], [data-save-retry-project]");
  if (!button) return;
  if (button.hasAttribute('data-save-retry-project')) {
    const select = button.closest('details').querySelector('select[data-retry-project]');
    const value = select.value || null;
    if (value && !window.confirm('保存后，等待切号的任务可能使用该项目自动生成并消耗积分。确认使用此项目？')) return;
    button.disabled = true;
    accountActionsInFlight.add(button);
    try {
      const response = await fetch('/api/real-pool/flow-retry-project', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-Workbench-Request':'1'}, body:JSON.stringify({account:button.dataset.saveRetryProject, project:value})});
      const result = await response.json();
      if (response.ok && result.ok && retryProjectDrafts.get(button.dataset.saveRetryProject) === select.value && select.value === (value ?? '')) retryProjectDrafts.delete(button.dataset.saveRetryProject);
      $('accountActionMessage').textContent = response.ok && result.ok ? '备用项目已保存。' : result.error || '保存状态未确认。';
      await refresh();
    } catch (_) { $('accountActionMessage').textContent = '保存状态未确认，请刷新后核对。'; }
    finally { accountActionsInFlight.delete(button); button.disabled = false; }
    return;
  }
  const isFlowResume = button.hasAttribute('data-resume-flow');
  if (isFlowResume && !window.confirm(button.dataset.incident
    ? '确认已人工核查该账号？此警告的截图将被销毁。恢复后，符合条件的排队任务可能开始执行。'
    : '确认已人工核查该账号的 Flow 状态？恢复后，等待中的任务可能在冷却结束后开始执行。')) return;
  button.disabled = true;
  accountActionsInFlight.add(button);
  const isToggle = button.hasAttribute('data-toggle-gemini');
  const isKeepalive = button.hasAttribute('data-keepalive-gemini');
  const isRecovery = button.hasAttribute('data-recover-gemini');
  $("accountActionMessage").textContent = isFlowResume ? '正在恢复 Flow…' : isToggle || isKeepalive ? "正在更新账号设置…" : isRecovery ? "正在单次检查并续期原账号；不会重发消息或换号…" : "正在核对该账号的 Gemini 登录与额度…";
  try {
    const response = await fetch(isFlowResume ? "/api/real-pool/flow-risk-resume" : isKeepalive ? "/api/real-pool/keepalive-enabled" : isToggle ? "/api/real-pool/enabled" : isRecovery ? "/api/real-pool/recover-account" : "/api/real-pool/check", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Workbench-Request": "1"},
      body: JSON.stringify(isFlowResume ? {account: button.dataset.resumeFlow, incident: button.dataset.incident} : isKeepalive ? {account: button.dataset.keepaliveGemini, enabled: button.dataset.enabled === 'true'} : isToggle ? {account: button.dataset.toggleGemini, enabled: button.dataset.enabled === 'true'} : {account: isRecovery ? button.dataset.recoverGemini : button.dataset.checkGemini})});
    const result = await response.json();
    $("accountActionMessage").textContent = result.message || result.error || "暂未确认，请稍后查看账号状态。";
    await refresh();
  } catch (_) {
    $("accountActionMessage").textContent = "结果暂未确认，请刷新账号列表后查看。";
  } finally { accountActionsInFlight.delete(button); button.disabled = false; }
});

$('geminiAccount').addEventListener('change', syncQuickGeminiControls);

$("geminiSend").addEventListener("click", async () => {
  const account = $("geminiAccount").value;
  const prompt = $("geminiPrompt").value;
  if (!geminiSelectionReady(account)) { $('geminiMessage').textContent = '所选账号当前不能发送，请先到账号页核对状态。'; return; }
  if (!account || !prompt.trim()) { $("geminiMessage").textContent = "请选择账号并填写内容。"; return; }
  const files = Array.from($("geminiFiles").files || []);
  const extensions = files.map((file) => file.name.split(".").pop().toLowerCase());
  if (files.length > 4 || files.reduce((total, file) => total + file.size, 0) > 100 * 1024 * 1024 ||
      files.some((file, index) => !file.size || !["jpg", "jpeg", "png", "webp", "mp4", "mov", "webm", "pdf"].includes(extensions[index]))) {
    $("geminiMessage").textContent = "请检查附件格式，最多 4 个，合计不超过 100 MB。"; return;
  }
  const id = crypto.randomUUID();
  sessionStorage.setItem("geminiRequest", id);
  state.geminiBusy = true;
  $("geminiSend").disabled = true;
  $("geminiFiles").disabled = true;
  $("geminiReply").textContent = "";
  $("geminiMessage").textContent = "正在提交真实请求，请不要重复发送。";
  let chatStarted = false;
  try {
    const attachments = [];
    for (let index = 0; index < files.length; index++) {
      $("geminiMessage").textContent = `正在上传附件 ${index + 1}/${files.length}，尚未发送问题。`;
      const upload = await fetch(`/api/real-pool/uploads/${id}/${index}/${extensions[index]}`, {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/octet-stream", "X-Workbench-Request": "1"}, body: files[index]});
      const result = await upload.json();
      if (!upload.ok) throw new Error(result.error || "附件上传失败，本次没有发送问题。");
      attachments.push(result);
    }
    $("geminiMessage").textContent = "正在提交真实请求，请不要重复发送。";
    chatStarted = true;
    const response = await fetch("/api/real-pool/chat", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Workbench-Request": "1", "Idempotency-Key": id},
      body: JSON.stringify({method: "gemini.chat", account, prompt, attachments})});
    const record = await response.json();
    if (response.status === 400 || response.status === 403) {
      sessionStorage.removeItem("geminiRequest");
      state.geminiBusy = false;
      syncQuickGeminiControls();
      $("geminiFiles").disabled = false;
      $("geminiMessage").textContent = record.error || "请求没有提交，请检查账号和内容。";
      return;
    }
    if (!response.ok) throw new Error("outcome unknown");
    showGeminiRecord(record);
    if (record.status === "dispatching") observeGeminiRequest(id);
  } catch (error) {
    if (!chatStarted) {
      sessionStorage.removeItem("geminiRequest");
      state.geminiBusy = false;
      syncQuickGeminiControls();
      $("geminiFiles").disabled = false;
      $("geminiMessage").textContent = error.message || "附件上传失败，本次没有发送问题。";
    } else await observeGeminiRequest(id);
  }
});

$("geminiNewRequest").addEventListener("click", () => {
  sessionStorage.removeItem("geminiRequest");
  state.geminiBusy = false;
  $("geminiFiles").disabled = false;
  $("geminiNewRequest").hidden = true;
  syncQuickGeminiControls();
  $("geminiMessage").textContent = "已结束这条记录，可发起新的请求。";
});

function showFlowRecord(record) {
  const status = record?.status;
  state.flowBusy = status === "dispatching" || status === "unknown";
  syncFlowRetryOption();
  $("flowSend").disabled = state.flowBusy || !$("flowProject").value || !$("flowModel").value;
  $("flowNewRequest").hidden = status !== "unknown";
  $("flowMessage").textContent = status === "submitted" ? `已交给 Flow 排队（任务 ${record.task_id}），下方会更新执行状态。` :
    status === "dispatching" ? "正在提交，请不要重复操作。" : "结果尚未确认，请先到原后台核对；系统不会自动重复提交。";
}

let activeConversation = null;
let currentConversation = null;
let chatSending = false;
let showingHiddenConversations = false;
let simulatedConversationIds = new Set();
let conversationListRequest = 0;
let conversationDetailRequest = 0;
let conversationEntries = [];
let conversationMetadata = new Map();
let conversationMetadataRun = 0;
let conversationMetadataAbort = null;
let conversationMetadataState = {total: 0, done: 0, failed: 0, loading: false};
let realTurnAccountState = null;
let libraryPage = 1;
let libraryPages = 1;
let libraryRequest = 0;
let materialOpenRequest = 0;
let currentMaterial = null;
let currentMaterialExtra = null;
let materialDirty = false;
let materialSaving = false;
let autoMaterialState = null;
let backfillState = null;
let backfillRequest = null;
let backfillBusy = false;
const materialDimensions = {subject:"主体", scene:"场景", action:"动作", style:"风格", camera:"镜头", color:"色调", keywords:"关键词",music_style:"音乐风格",music_mood:"情绪",music_instrument:"乐器",music_use:"用途",music_voice:"人声"};
const extraDimensions = {shot_size:"景别", viewpoint:"拍摄角度", composition:"构图", movement:"运镜"};
const librarySearchDimensions = {...materialDimensions, ...extraDimensions};

async function postJson(path, body, key) {
  const headers = {"Content-Type":"application/json", "X-Workbench-Request":"1"};
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch(path, {method:"POST", credentials:"same-origin", headers, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) { const error = new Error(data.error || "操作暂未完成。"); error.status = response.status; throw error; }
  return data;
}

function conversationFilterMetadata(item) {
  const turns = Array.isArray(item.turns) ? item.turns : [];
  const imageIntent = /(?:生图|文生图|图生图|text[ -]to[ -]image|image[ -]to[ -]image)|(?:生成|绘制|画|制作|创作|设计|generate|create|draw|make|render)[^。！？\n]{0,32}(?:图片|图像|照片|插画|海报|一张图|一幅画|image|picture|photo|illustration)|(?:图片|图像|照片|插画|海报)[^。！？\n]{0,16}(?:生成|绘制|制作|创作)/i;
  const videoIntent = /(?:生视频|文生视频|图生视频|text[ -]to[ -]video|image[ -]to[ -]video)|(?:生成|制作|创建|做|创作|合成|generate|create|make|render)[^。！？\n]{0,32}(?:视频|短片|动画|影片|video|clip)|(?:视频|短片|动画|影片)[^。！？\n]{0,16}(?:生成|制作|创作|合成)/i;
  const imageResult = turns.some((turn) => Array.isArray(turn.images) && turn.images.length > 0);
  const image = imageResult || turns.some((turn) => (turn.image_status && Number(turn.image_status.expected) > 0)
    || /<Image\b[^>]*\bsrc=["']image_generation_content\//i.test(String(turn.response || ''))
    || imageIntent.test(String(turn.prompt || '')));
  const video = turns.some((turn) => videoIntent.test(String(turn.prompt || '')));
  const attachment = turns.some((turn) => Array.isArray(turn.attachments) && turn.attachments.length > 0);
  const last = turns[turns.length - 1];
  const status = (item.blocked && !officialConversation(item)) || turns.some((turn) => turn.status === 'unknown') ? 'unconfirmed'
    : turns.some((turn) => turn.status === 'dispatching') ? 'processing'
    : !last ? 'empty' : ['completed', 'failed'].includes(last.status) ? last.status : 'unknown';
  return {image, video, imageResult, attachment, status};
}

function renderConversationAccountFilter() {
  const select = $('chatFilterAccount');
  if (!select) return;
  const selected = select.value.startsWith('sim:') ? '' : select.value;
  const snapshotAccounts = state.snapshot?.real_accounts?.items;
  const accountNames = new Map((Array.isArray(snapshotAccounts) ? snapshotAccounts : [])
    .filter((account) => typeof account.key === 'string' && account.key.startsWith('flow-account-'))
    .map((account) => [account.key.slice('flow-account-'.length), account.email || account.label || account.profile || '账号']));
  const choices = new Map();
  let missing = 0;
  for (const item of conversationEntries) {
    if (item.simulated) continue;
    if (item.account_key) {
      const key = 'real:' + item.account_key;
      if (!choices.has(key)) choices.set(key, accountNames.get(item.account_key) || `不在当前号池的账号 ${++missing}`);
    } else choices.set('unbound', '未绑定账号');
  }
  const options = [...choices.entries()].sort((a,b) => a[1].localeCompare(b[1], 'zh-CN'));
  if (selected && !choices.has(selected)) options.push([selected, '当前账号（此列表中暂无对话）']);
  select.replaceChildren(new Option('全部账号', ''), ...options.map(([value, text]) => new Option(text, value)));
  select.value = selected;
}

function conversationFilterResults() {
  const search = ($('chatFilterSearch')?.value || '').trim().toLocaleLowerCase('zh-CN');
  const account = $('chatFilterAccount')?.value || '';
  const status = $('chatFilterStatus')?.value || '';
  const kind = $('chatFilterKind')?.value || '';
  const found = [];
  let pending = 0, unavailable = 0;
  for (const item of conversationEntries) {
    if (search && !String(item.title || '').toLocaleLowerCase('zh-CN').includes(search)) continue;
    if (account.startsWith('real:') && (item.simulated || 'real:' + item.account_key !== account)) continue;
    if (account === 'unbound' && (item.simulated || item.account_key)) continue;
    if (kind === 'simulated' && !item.simulated) continue;
    const meta = conversationMetadata.get(item.id);
    const needsMeta = !!status || (!!kind && kind !== 'simulated');
    if (needsMeta && !meta) { pending++; continue; }
    if (needsMeta && meta.error) { unavailable++; continue; }
    if (status && meta.status !== status) continue;
    if (kind === 'image' && !meta.image) continue;
    if (kind === 'video' && !meta.video) continue;
    if (kind === 'image_result' && !meta.imageResult) continue;
    if (kind === 'attachment' && !meta.attachment) continue;
    if (kind === 'other' && (meta.image || meta.video)) continue;
    found.push(item);
  }
  return {found, pending, unavailable, filtered: !!(search || account || status || kind)};
}

function renderConversationList() {
  const list = $('chatList');
  if (!list) return;
  const {found, pending, unavailable, filtered} = conversationFilterResults();
  const filterCount = ['chatFilterAccount', 'chatFilterStatus', 'chatFilterKind'].filter(id => $(id)?.value).length;
  $('chatFilterReset').hidden = !filtered;
  $('chatFilterCount').hidden = !filterCount;
  $('chatFilterCount').textContent = filterCount ? String(filterCount) : '';
  const scrollTop = list.scrollTop;
  const focused = document.activeElement?.closest?.('#chatList [data-chat]')?.dataset.chat;
  const statusNames = {completed:'已完成', processing:'处理中', failed:'失败', unconfirmed:'待核对', empty:'未开始', unknown:'状态未知'};
  list.innerHTML = found.length ? found.map((item) => {
    const meta = conversationMetadata.get(item.id);
    const badges = [];
    if (officialConversation(item)) badges.push('<span class="chat-row-tag">官方调用</span>');
    if (meta && !meta.error) {
      const statusClass = meta.status === 'unconfirmed' ? ' is-attention' : meta.status === 'processing' ? ' is-processing' : meta.status === 'failed' ? ' is-failed' : '';
      badges.push(`<span class="chat-row-tag${statusClass}">${esc(statusNames[meta.status] || '状态未知')}</span>`);
      if (meta.image) badges.push('<span class="chat-row-tag">生图相关</span>');
      if (meta.video) badges.push('<span class="chat-row-tag is-video">生视频相关</span>');
      if (meta.imageResult) badges.push('<span class="chat-row-tag">已有图片</span>');
      if (meta.attachment) badges.push('<span class="chat-row-tag">含附件</span>');
    } else if (meta?.error) badges.push('<span class="chat-row-tag is-attention">筛选信息未读取</span>');
    return `<button class="chat-list-item${item.id === activeConversation ? ' chat-selected' : ''}" type="button" data-chat="${esc(item.id)}"${item.simulated ? ' data-simulated="1"' : ''}${item.id === activeConversation ? ' aria-current="true"' : ''}><span class="chat-list-title" title="${esc(item.title)}">${item.simulated ? '<span class="simulation-list-badge">模拟</span> ' : ''}${esc(item.title)}</span><span class="chat-list-time">${esc(dateTime(item.updated))}</span>${badges.length ? `<span class="chat-row-tags">${badges.join('')}</span>` : ''}</button>`;
  }).join('') : pending ? empty('正在读取筛选信息', '请稍候。') : filtered ? empty('没有符合筛选的对话', '可调整条件或清空筛选。') : empty(showingHiddenConversations ? '没有隐藏的对话' : '还没有对话', showingHiddenConversations ? '隐藏的对话可在这里恢复。' : '点击新建对话开始。');
  list.scrollTop = scrollTop;
  if (focused) [...list.querySelectorAll('[data-chat]')].find((button) => button.dataset.chat === focused)?.focus({preventScroll:true});
  const summary = $('chatFilterSummary');
  if (summary) {
    let text = `显示 ${found.length} / ${conversationEntries.length} 条`;
    if (conversationMetadataState.loading) text += ` · 正在读取 ${conversationMetadataState.done} / ${conversationMetadataState.total}`;
    if (conversationMetadataState.failed) text += ` · ${conversationMetadataState.failed} 条筛选信息未读取`;
    if (filtered && activeConversation && !found.some((item) => item.id === activeConversation)) text += ' · 当前对话仍可查看';
    if (unavailable) text += ` · ${unavailable} 条未纳入筛选`;
    summary.textContent = text;
  }
}

async function hydrateConversationMetadata(entries, run, controller) {
  let next = 0;
  const workers = Array.from({length: Math.min(4, entries.length)}, async () => {
    while (next < entries.length && run === conversationMetadataRun) {
      const item = entries[next++];
      try {
        const url = (item.simulated ? '/api/simulated-conversations/' : '/api/conversations/') + encodeURIComponent(item.id);
        const response = await fetch(url, {cache:'no-store', credentials:'same-origin', signal:controller.signal});
        if (!response.ok) throw new Error('metadata unavailable');
        const detail = await response.json();
        if (run !== conversationMetadataRun) return;
        conversationMetadata.set(item.id, conversationFilterMetadata(detail));
      } catch (error) {
        if (run !== conversationMetadataRun || error.name === 'AbortError') return;
        conversationMetadata.set(item.id, {error:true});
        conversationMetadataState.failed++;
      }
      if (run !== conversationMetadataRun) return;
      conversationMetadataState.done++;
      if (conversationMetadataState.done % 10 === 0 && $('chatFilterSummary')) {
        $('chatFilterSummary').textContent = $('chatFilterSummary').textContent.replace(/正在读取 \d+ \/ \d+/, `正在读取 ${conversationMetadataState.done} / ${conversationMetadataState.total}`);
      }
    }
  });
  await Promise.all(workers);
  if (run !== conversationMetadataRun) return;
  conversationMetadataState.loading = false;
  renderConversationAccountFilter();
  renderConversationList();
}

for (const id of ['chatFilterSearch', 'chatFilterAccount', 'chatFilterStatus', 'chatFilterKind']) {
  $(id)?.addEventListener(id === 'chatFilterSearch' ? 'input' : 'change', renderConversationList);
}
$('chatFilterReset')?.addEventListener('click', () => {
  for (const id of ['chatFilterSearch', 'chatFilterAccount', 'chatFilterStatus', 'chatFilterKind']) $(id).value = '';
  $('chatFilterSearch').focus();
  renderConversationList();
});

async function loadConversations({background = false, listData = null} = {}) {
  if (!background) await refreshChannelStatus();
  const request = ++conversationListRequest;
  const hidden = showingHiddenConversations;
  if (conversationMetadataAbort) conversationMetadataAbort.abort();
  const run = ++conversationMetadataRun;
  try {
    const data = listData || await getJson(hidden ? '/api/conversations/hidden' : '/api/conversations', {timeoutMs: 8000});
    let simulated = [];
    if (!hidden) {
      try { simulated = (await getJson('/api/simulated-conversations', {timeoutMs: 8000})).items || []; }
      catch (_) {
        if (background) simulated = conversationEntries.filter(item => item.simulated);
        else $('chatCreateStatus').textContent = '模拟测试记录暂不可读取，真实对话不受影响。';
      }
    }
    if (request !== conversationListRequest || hidden !== showingHiddenConversations ||
        (background && (state.view !== 'conversations' || chatSending))) return false;
    const realIds = new Set(data.items.map((item) => item.id));
    simulated = simulated.filter((item) => item.simulated === true && !realIds.has(item.id));
    simulatedConversationIds = new Set(simulated.map((item) => item.id));
    const entries = [...data.items, ...simulated].sort((a,b) => (b.updated || 0) - (a.updated || 0));
    conversationEntries = entries;
    conversationMetadata = new Map();
    if (!activeConversation && entries.length) activeConversation = entries[0].id;
    conversationMetadataState = {total:entries.length, done:0, failed:0, loading:!!entries.length && !!$('chatFilterSearch')};
    renderConversationAccountFilter();
    renderConversationList();
    if ($('chatFilterSearch') && entries.length) {
      const controller = new AbortController();
      conversationMetadataAbort = controller;
      hydrateConversationMetadata(entries, run, controller);
    }
    if (activeConversation) await showConversation(activeConversation, {background});
    return true;
  } catch (_) {
    if (!background && request === conversationListRequest) $('chatList').innerHTML = empty('对话暂不可读取', '请点击页面右上角刷新后再试。');
    return false;
  }
}

const chatEffortLabels={low:'低',medium:'中',high:'高',xhigh:'超高'};
const flashModel='gemini-3.8-flash';
const hydrogenModel='gemini-4-hydrogen';
const hydrogenMedium=hydrogenModel+'-medium';
let chatPreferredModel='';
const flashChatChoices=['low','medium','high'].map(effort=>({
  id:`${flashModel}-${effort}`, family:flashModel, label:'Gemini 3.8 Flash', effort
}));

function geminiChatModelChoices(accountKey, accounts) {
  if (accountKey !== 'auto') {
    const item = accounts.find(row => row.key === 'flow-account-' + accountKey);
    if (!item || officialAccount(item).data?.advanced_quota_restriction) return flashChatChoices;
    const choices = item.gemini_model_options?.filter(option => option.family && option.effort);
    return choices?.length ? choices : flashChatChoices;
  }
  const choices = new Map(flashChatChoices.map(option => [option.id, option]));
  for (const item of accounts) {
    if (!geminiAccountAvailability(item).auto || officialAccount(item).data?.advanced_quota_restriction) continue;
    for (const option of item.gemini_model_options || []) {
      if (option.family && option.effort) choices.set(option.id, option);
    }
  }
  return [...choices.values()];
}

function syncConversationControls(announce = false) {
  const item=currentConversation, ready=!channelReadFailed && state.channelStatus?.production_send_available === true;
  const select=$('chatAccount'), accounts=state.snapshot?.real_accounts?.items || [];
  const removedBinding=!!item?.account_key && accounts.some(row=>row.key==='flow-account-'+item.account_key && row.gemini_removed);
  $('chatNew').disabled=!ready;
  const prior=select.value;
  renderGeminiAccountOptions(select,accounts);
  if(item?.account_key) {
    if(![...select.options].some(o=>o.value===item.account_key)) select.add(new Option(removedBinding ? '绑定账号已从 Gemini 号池删除' : '绑定账号暂不可用',item.account_key));
    select.value=item.account_key;
  } else if([...select.options].some(o=>o.value===prior)) select.value=prior;
  const pending=!!item?.turns?.some(t=>['queued','dispatching','running'].includes(t.status));
  const imageAnalysis=item?.channel==='official_antigravity_image_analysis';
  const archived=!!item && (removedBinding || item.simulated || !!item.blocked || (item.turns.length>0 && item.channel!=='official_antigravity_cli'));
  select.disabled=!item || !!item.account_key || archived || pending || chatSending || !!item.hidden || !ready;
  $('chatPrompt').disabled=!!item?.simulated || imageAnalysis;
  $('chatFiles').disabled=true;
  $('chatFiles').title='当前接入文字对话；已有附件和草稿保留。';
  const modelSelect=$('chatModel'), effortSelect=$('chatEffort');
  const previousEffort=effortSelect.value;
  let choices=geminiChatModelChoices(select.value, accounts);
  const newHydrogenChat = !!item && !item.account_key && state.channelStatus?.dispatch?.model === hydrogenMedium;
  const hydrogenUnavailable = newHydrogenChat && !choices.some(option => option.id === hydrogenMedium);
  if (newHydrogenChat) {
    choices = choices.filter(option => option.family !== flashModel);
    if (hydrogenUnavailable) choices = [...choices,
      {id:hydrogenMedium, family:hydrogenModel, label:'Gemini 4 Hydrogen', effort:'medium'}];
  }
  if(item?.model && (archived || item.account_key)) {
    const saved=choices.find(option=>option.id===item.model);
    const [,family,effort]=item.model.match(/^(.*)-(low|medium|high|xhigh)$/) || [];
    const savedModel=saved || {id:item.model,family:family || item.model,label:family || item.model,effort};
    choices=archived || !saved ? [savedModel] : choices.filter(option=>option.family===savedModel.family);
  }
  const families=new Map(choices.map(option=>[option.family,option.label]));
  modelSelect.replaceChildren(...Array.from(families, ([family,label])=>new Option(label,family)));
  if(families.has(chatPreferredModel)) modelSelect.value=chatPreferredModel;
  else if(choices.some(option=>option.id===hydrogenMedium)) modelSelect.value=hydrogenModel;
  const levels=choices.filter(option=>option.family===modelSelect.value);
  effortSelect.replaceChildren(...levels.map(option=>new Option(chatEffortLabels[option.effort] || '原设置',option.id)));
  const defaultEffort=modelSelect.value==='gemini-4-neon' ? 'high' : 'medium';
  effortSelect.value=levels.find(option=>option.id===previousEffort)?.id
    || levels.find(option=>option.id===item?.model)?.id
    || levels.find(option=>option.effort===defaultEffort)?.id
    || levels[0]?.id || '';
  const locked=!ready || !item || archived || pending || chatSending || !!item.hidden;
  modelSelect.disabled=locked || !!item?.account_key || families.size<2;
  effortSelect.disabled=locked || levels.length<2;
  const blockedByHydrogen = hydrogenUnavailable && modelSelect.value === hydrogenModel;
  $('chatModelNote').textContent=imageAnalysis ? 'API 识图记录 · 每次识图独立提问。' : archived ? '历史归档只读；原问答与草稿保留，请新建官方对话。' : blockedByHydrogen && select.value !== 'auto' ? '所选账号的 HY 中档当前不可用，请选择自动分配或核对授权和额度。' : newHydrogenChat && modelSelect.value === hydrogenModel && select.value === 'auto' ? '首轮优先用 HY，无可用账号时在发送前用 Flash 保底。续聊沿用实际模型和账号。' : '首次发送后固定账号和模型。续聊时可调整思考等级。';
  $('chatSend').disabled=!ready || !item || archived || pending || chatSending || !!item.hidden || (blockedByHydrogen && select.value !== 'auto') || !geminiSelectionReady(select.value);
  if(announce && !chatSending) $('chatMessage').textContent=!item ? '请选择或新建对话。' : imageAnalysis ? (pending ? '正在处理识图请求，请等待结果。' : '继续传图请使用 API 文档里的 Gemini 识图接口。') : archived ? '历史记录保留；请新建对话使用官方通道。' : pending ? '任务正在排队或处理；可在原消息下查看处理和重试进度。' : !ready ? '官方调用暂不可用，草稿保留。' : '';
}

function renderRealTurnAccountLabels() {
  const current = realTurnAccountState;
  if (!current || current.conversation !== activeConversation) return;
  const items = state.snapshot?.real_accounts?.items;
  const accounts = Array.isArray(items) ? items : [];
  const first = current.results[0]?.account_key;
  for (const element of $('chatHistory').querySelectorAll('[data-real-turn-account]')) {
    const index = Number(element.dataset.realTurnAccount);
    const record = current.results[index];
    element.classList.remove('is-changed');
    if (record === undefined) { element.textContent = '本轮账号读取中…'; continue; }
    if (!record?.account_key) { element.textContent = '本轮账号未确认'; continue; }
    const account = accounts.find((candidate) => candidate.key === 'flow-account-' + record.account_key);
    const name = account?.email || account?.label || account?.profile || '账号信息未读取';
    const comparison = index === 0 ? '首轮账号' : !first ? '首轮账号未确认' : record.account_key === first ? '与首轮一致' : '与首轮不同';
    element.classList.toggle('is-changed', index > 0 && !!first && record.account_key !== first);
    element.textContent = `${record.status === 'completed' ? '本轮使用' : '本轮选择'}：${name} · ${comparison}`;
  }
}

function loadRealTurnAccountLabels(item, sequence) {
  const current = {conversation:item.id, results:new Array(item.turns.length)};
  realTurnAccountState = current;
  let next = 0;
  const workers = Array.from({length:Math.min(4, item.turns.length)}, async () => {
    while (next < item.turns.length && sequence === conversationDetailRequest && activeConversation === item.id) {
      const index = next++;
      const id = item.turns[index]?.request_id;
      try {
        if (!/^[0-9a-f-]{36}$/.test(id || '')) throw new Error('invalid request');
        const record = await getJson('/api/real-pool/requests/' + encodeURIComponent(id));
        current.results[index] = record?.request_id === id ? record : null;
      } catch (_) { current.results[index] = null; }
      if (sequence !== conversationDetailRequest || activeConversation !== item.id) return;
      renderRealTurnAccountLabels();
    }
  });
  Promise.all(workers).catch(() => {});
}

let conversationViewRefreshing = false;
async function refreshConversationView() {
  if (conversationViewRefreshing || chatSending || document.querySelector('dialog[open]')) return;
  conversationViewRefreshing = true;
  const hidden = showingHiddenConversations;
  const before = conversationListRequest;
  const active = activeConversation;
  try {
    const data = await getJson(hidden ? '/api/conversations/hidden' : '/api/conversations', {timeoutMs: 8000});
    if (state.view !== 'conversations' || hidden !== showingHiddenConversations ||
        before !== conversationListRequest || chatSending || !Array.isArray(data.items)) return;
    const real = conversationEntries.filter(item => !item.simulated);
    const byId = new Map(real.map(item => [item.id, item]));
    if (!conversationListRequest || data.items.length !== real.length || data.items.some(item => {
      const old = byId.get(item.id);
      return !old || old.updated !== item.updated || old.title !== item.title;
    })) {
      await loadConversations({background: true, listData: data});
    } else if (active && active === activeConversation && !simulatedConversationIds.has(active)) {
      await showConversation(active, {background: true});
    }
  } catch (_) { /* Keep the current list and draft if the connection drops. */ }
  finally { conversationViewRefreshing = false; }
}

function renderConversationPrompt(prompt, index, expanded) {
  const value = prompt || '';
  const full = `<p>${esc(value).replace(/\n/g, '<br>')}</p>`;
  if (value.length <= 300 && value.split('\n').length <= 6) return full;
  const compact = value.replace(/\s+/g, ' ').trim();
  const preview = compact.length > 135 ? compact.slice(0, 135) + '…' : compact;
  return `<details class="conversation-long-prompt" data-chat-prompt-turn="${index}"${expanded ? ' open' : ''}>
    <summary><span class="conversation-prompt-preview">${esc(preview)}</span><span class="conversation-prompt-action prompt-show">展开完整提问</span><span class="conversation-prompt-action prompt-hide">收起提问</span></summary>
    ${full}</details>`;
}

function renderConversationTurn(turn, index, expanded) {
  const timing = (value) => typeof value === 'number' ? (value / 1000).toFixed(1) + ' 秒' : '未采集';
  return `<article class="conversation-turn">
    <section class="conversation-message conversation-question">
      <div class="conversation-message-header"><strong>你</strong></div>
      ${renderConversationPrompt(turn.prompt, index, expanded)}
      ${turn.attachments.length ? `<span class="subtle">附件：${esc(turn.attachments.join('、'))}</span>` : ''}
    </section>
    <section class="conversation-message conversation-answer">
      <div class="conversation-message-header"><strong>Gemini</strong><span class="chat-turn-account" data-real-turn-account="${index}">本轮账号读取中…</span></div>
      ${turn.selected_model ? `<small class="response-timing">所选模型：${esc(turn.selected_model)}</small>` : ''}
      ${renderGeminiReply(turn)}${renderGeminiAttempts(turn.attempts)}${renderGeminiReauth(turn)}
      ${turn.status === 'completed' ? `<small class="response-timing">首段 ${timing(turn.first_text_ms)} · 完成 ${timing(turn.total_ms)} · 排队 ${timing(turn.queue_wait_ms)}</small>` : ''}
      ${turn.reconciliation_note ? `<p class="subtle">${esc(turn.reconciliation_note)}</p>` : ''}
    </section></article>`;
}

async function showConversation(id, {background = false} = {}) {
  const request = ++conversationDetailRequest;
  const item = await getJson((simulatedConversationIds.has(id) ? '/api/simulated-conversations/' : '/api/conversations/') + encodeURIComponent(id), {timeoutMs: 12000});
  if (request !== conversationDetailRequest) return null;
  if (background && (chatSending || document.querySelector('dialog[open]') || id !== activeConversation || state.view !== 'conversations' ||
      JSON.stringify(item) === JSON.stringify(currentConversation))) return currentConversation;
  const history = $('chatHistory');
  const sameConversation = currentConversation?.id === item.id;
  const chatScroll = sameConversation ? history.scrollTop : 0;
  const expanded = new Set(sameConversation ? [...history.querySelectorAll('details[open][data-chat-prompt-turn]')].map(node => node.dataset.chatPromptTurn) : []);
  const focusedPrompt = sameConversation && history.contains(document.activeElement)
    ? document.activeElement.closest('details[data-chat-prompt-turn]')?.dataset.chatPromptTurn : undefined;
  if (!sameConversation) {
    chatPreferredModel = '';
    $('chatModel').value = '';
    $('chatEffort').value = '';
    if (!item.account_key && [...$('chatAccount').options].some((option) => option.value === 'auto')) $('chatAccount').value = 'auto';
  }
  activeConversation = item.id;
  currentConversation = item;
  $('chatTitle').textContent = item.title;
  $('chatRename').disabled = !!item.simulated;
  $('chatHide').disabled = !!item.simulated;
  $('chatHide').textContent = item.hidden ? '恢复对话' : '隐藏';
  $('chatHide').dataset.hidden = item.hidden ? '1' : '0';
  syncConversationControls(!background || !sameConversation);
  if (item.simulated) {
    realTurnAccountState = null;
    const changed = item.turns.filter((turn) => turn.matches_first === false).length;
    const unknown = item.turns.filter((turn) => turn.matches_first === null).length;
    const result = changed ? `发现 ${changed} 轮账号与首轮不同` : unknown ? `有 ${unknown} 轮账号未确认` : '各轮使用同一模拟账号';
    history.innerHTML = `<div class="simulation-notice"><strong>模拟测试 · ${esc(result)}</strong><span>问题、回复和账号由程序模拟。</span></div>` + item.turns.map((turn, index) => {
      const account = turn.account_label || '未记录';
      const compare = turn.matches_first === false ? '发生切号' : turn.matches_first === true ? (turn.ordinal === 1 ? '首轮账号' : '与首轮一致') : '账号待确认';
      return `<article class="conversation-turn simulation-turn" data-simulated-turn="${turn.ordinal}">
        <section class="conversation-message conversation-question"><div class="conversation-message-header"><strong>第 ${turn.ordinal} 轮 · 提问</strong></div>
          ${renderConversationPrompt(turn.prompt, index, expanded.has(String(index)))}</section>
        <section class="conversation-message conversation-answer"><div class="conversation-message-header"><strong>模拟回复</strong><span class="simulation-account${turn.matches_first === false ? ' is-changed' : ''}">本轮使用：${esc(account)} · ${esc(compare)}</span></div>
          <p>${esc(turn.response).replace(/\n/g, '<br>')}</p><small class="response-timing">模拟耗时 ${(turn.elapsed_ms / 1000).toFixed(2)} 秒 · 提交 ${turn.attempts} 次${turn.status !== 'completed' ? ' · 未完成' : ''}</small></section>
        </article>`;
    }).join('');
  } else {
    history.innerHTML = item.turns.length
      ? item.turns.map((turn, index) => renderConversationTurn(turn, index, expanded.has(String(index)))).join('')
      : empty('开始对话', '发送第一条消息后将保存记录。');
    loadRealTurnAccountLabels(item, request);
  }
  history.scrollTop = chatScroll;
  if (focusedPrompt !== undefined) history.querySelector(`[data-chat-prompt-turn="${focusedPrompt}"] > summary`)?.focus({preventScroll:true});
  $('chatList').querySelectorAll('[data-chat]').forEach((button) => {
    const selected = button.dataset.chat === item.id;
    button.classList.toggle('chat-selected', selected);
    if (selected) button.setAttribute('aria-current','true'); else button.removeAttribute('aria-current');
  });
  return item;
}

$('chatList').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-chat]');
  if (!button) return;
  try {
    $('chatCreateStatus').textContent = '';
    if (await showConversation(button.dataset.chat) && matchMedia('(max-width: 900px)').matches) {
      $('chatTitle').focus({preventScroll:true});
      $('chatTitle').scrollIntoView({block:'start'});
    }
  } catch (_) { $('chatMessage').textContent = '对话暂不可读取。'; }
});
$('chatHistory').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-reauth-request]');
  if (!button || button.disabled) return;
  button.disabled = true;
  const id = activeConversation;
  try {
    await postJson('/api/official-reauth/' + encodeURIComponent(button.dataset.reauthRequest), {});
    if (id === activeConversation) await showConversation(id, {background:true});
  } catch (error) {
    if (button.isConnected) {
      button.disabled = false;
      button.previousElementSibling.textContent = error.message || '暂时无法打开授权窗口。';
    }
  }
});
$('chatNew').addEventListener('click', async () => {
  const button = $('chatNew');
  if (button.disabled) return;
  button.disabled = true; button.textContent = '创建中…';
  $('chatCreateStatus').textContent = '正在新建对话…';
  try {
    showingHiddenConversations = false; $('chatShowHidden').textContent = '查看已隐藏';
    const item = await postJson('/api/conversations', {title:'新对话'});
    ++conversationDetailRequest; currentConversation = null;
    activeConversation = item.id;
    if (!await loadConversations()) {
      $('chatCreateStatus').textContent = '对话已创建，列表暂时无法刷新，请点击右上角刷新。';
      return;
    }
    $('chatCreateStatus').textContent = '对话已创建，可以输入消息。';
    if (state.view === 'conversations') {
      $('chatPrompt').focus({preventScroll:true});
      $('chatPrompt').scrollIntoView({block:'center', behavior:'smooth'});
    }
  } catch (error) {
    $('chatCreateStatus').textContent = [400,403].includes(error.status) ? error.message : '创建结果暂未确认，请先刷新列表查看。';
    await loadConversations();
  } finally { button.disabled = false; button.textContent = '新建对话'; }
});
$('chatAccount').addEventListener('change', () => syncConversationControls(true));
$('chatModel').addEventListener('change', () => {
  chatPreferredModel = $('chatModel').value;
  syncConversationControls();
});
$('chatShowHidden').addEventListener('click', async () => {
  showingHiddenConversations = !showingHiddenConversations;
  $('chatShowHidden').textContent = showingHiddenConversations ? '查看当前对话' : '查看已隐藏';
  activeConversation = null; currentConversation = null; ++conversationDetailRequest; $('chatCreateStatus').textContent = ''; $('chatTitle').textContent = '请选择或新建对话'; $('chatHistory').innerHTML = '';
  $('chatRename').disabled = true; $('chatHide').disabled = true; $('chatSend').disabled = true; // Legacy Gemini sender removed.
  syncConversationControls();
  await loadConversations();
});
$('chatHide').addEventListener('click', async () => {
  if (!activeConversation) return;
  try {
    await postJson('/api/conversations/' + activeConversation + '/visibility', {hidden:$('chatHide').dataset.hidden !== '1'});
    activeConversation = null; currentConversation = null; ++conversationDetailRequest; $('chatCreateStatus').textContent = ''; $('chatTitle').textContent = '请选择或新建对话'; $('chatHistory').innerHTML = '';
    $('chatRename').disabled = true; $('chatHide').disabled = true; $('chatSend').disabled = true; // Legacy Gemini sender removed.
    syncConversationControls();
    await loadConversations();
  } catch (error) { $('chatMessage').textContent = error.message; }
});
let renameConversationId = null;
$('chatRename').addEventListener('click', () => {
  if (!activeConversation) return;
  renameConversationId = activeConversation;
  $('chatRenameInput').value = $('chatTitle').textContent;
  $('chatRenameStatus').textContent = '';
  $('chatRenameDialog').showModal();
  $('chatRenameInput').focus(); $('chatRenameInput').select();
});
$('chatRenameCancel').addEventListener('click', () => $('chatRenameDialog').close());
$('chatRenameForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!renameConversationId || $('chatRenameSave').disabled) return;
  $('chatRenameSave').disabled = true;
  try {
    await postJson('/api/conversations/' + renameConversationId + '/rename', {title: $('chatRenameInput').value});
    $('chatRenameDialog').close(); await loadConversations();
  } catch (error) { $('chatRenameStatus').textContent = error.message; }
  finally { $('chatRenameSave').disabled = false; }
});
$('chatSend').addEventListener('click', async () => {
  if (!activeConversation || chatSending || currentConversation?.simulated) return;
  const account = $('chatAccount').value || 'auto';
  if (!geminiSelectionReady(account)) { $('chatMessage').textContent = '所选账号当前不能发送，请先到账号页核对状态。'; return; }
  const model = $('chatEffort').value || null;
  const prompt = $('chatPrompt').value;
  if (!prompt.trim()) { $('chatMessage').textContent = '请输入消息。'; return; }
  const files = Array.from($('chatFiles').files || []);
  const extensions = files.map((file) => file.name.split('.').pop().toLowerCase());
  if (files.length > 4 || files.reduce((total,file) => total + file.size,0) > 100 * 1024 * 1024 || files.some((file,index) => !file.size || !['jpg','jpeg','png','webp','mp4','mov','webm','pdf'].includes(extensions[index]))) {
    $('chatMessage').textContent = '请检查附件格式，最多 4 个，合计不超过 100 MB。'; return;
  }
  const conversation = activeConversation, id = crypto.randomUUID();
  chatSending = true; $('chatSend').disabled = true; // Legacy Gemini sender removed. $('chatFiles').disabled = true;
  syncConversationControls();
  let submitted = false;
  try {
    const attachments = [];
    for (let index = 0; index < files.length; index++) {
      $('chatMessage').textContent = `正在上传附件 ${index + 1}/${files.length}，尚未发送消息。`;
      const response = await fetch(`/api/real-pool/uploads/${id}/${index}/${extensions[index]}`, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/octet-stream','X-Workbench-Request':'1'}, body:files[index]});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '附件上传失败。');
      attachments.push(result);
    }
    $('chatMessage').textContent = '正在等待回复，请勿重复发送。';
    submitted = true;
    sessionStorage.setItem('conversationRequest', JSON.stringify({id,conversation,prompt}));
    const record = await postJson('/api/real-pool/chat', {method:'gemini.chat',account,prompt,attachments,conversation_id:conversation,model}, id);
    if (['queued','dispatching','running'].includes(record.status)) {
      await loadConversations(); observeConversationRequest(id,conversation); return;
    }
    sessionStorage.removeItem('conversationRequest');
    if (record.status === 'completed') { $('chatPrompt').value = ''; $('chatFiles').value = ''; }
    chatSending = false; await loadConversations();
    if (record.status === 'failed') await refresh();
  } catch (error) {
    if (!submitted || [400,403,409].includes(error.status)) {
      sessionStorage.removeItem('conversationRequest'); chatSending = false;
      syncConversationControls();
      $('chatMessage').textContent = error.message;
    } else {
      $('chatMessage').textContent = '结果正在核对，系统不会自动重新发送。';
      observeConversationRequest(id, conversation);
    }
  }
});

async function observeConversationRequest(id, conversation, attempts=0) {
  try {
    const record = await getJson('/api/real-pool/requests/' + id);
    if (!['queued','dispatching','running'].includes(record.status)) {
      const saved=JSON.parse(sessionStorage.getItem('conversationRequest') || '{}');
      if(record.status==='completed' && activeConversation===conversation && saved.prompt===$('chatPrompt').value) $('chatPrompt').value='';
      sessionStorage.removeItem('conversationRequest'); chatSending = false;
      if (activeConversation === conversation) await loadConversations();
      if (record.status === 'failed') await refresh();
      return;
    }
  } catch (_) { /* A missing response is not evidence of non-submission. */ }
  if (attempts < 300) setTimeout(() => observeConversationRequest(id, conversation, attempts + 1), 3000);
  else $('chatMessage').textContent = '结果尚未确认，请稍后刷新核对；系统不会自动重复发送。';
}

function materialSize(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + ' GB';
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + ' MB';
  return Math.ceil(n / 1024) + ' KB';
}
function materialSource(origin) {
  if (origin.source === 'studiowright') return 'StudioWright';
  if (origin.source === 'windows_local') return 'Windows 素材';
  if (origin.source === 'flow2api-windows') return 'Flow2API · Windows';
  if (origin.source === 'flow2api-ubuntu') return 'Flow2API · Ubuntu';
  return origin.source || '原来源未记录';
}
function updateOptions(select, first, entries) {
  const previous = select.value;
  const options = [new Option(first, '')];
  for (const entry of entries || []) {
    const value = typeof entry === 'string' ? entry : entry?.value;
    if (typeof value !== 'string' || !value) continue;
    options.push(new Option(typeof entry === 'string' ? value : value + '（' + entry.count + '）', value));
  }
  if (previous && !options.some((option) => option.value === previous)) options.push(new Option(previous, previous));
  if (select.options.length === options.length && options.every((option, i) =>
    select.options[i].value === option.value && select.options[i].text === option.text)) return;
  select.replaceChildren(...options);
  select.value = previous;
}
function materialValues(list) {
  return Array.isArray(list) ? [...new Set(list.filter((v) => typeof v === 'string').map((v) => v.trim()).filter(Boolean))] : [];
}
function materialTagGroups(item, compact = false) {
  const fields = {...item.structured_tags, ...item.shot_tags, custom: item.tags};
  const styleIds=[...new Set((item.origins||[]).filter(o=>typeof o.style_id==='string').map(o=>o.style_id))];
  const labels = {...materialDimensions, ...extraDimensions, custom: '自定义'};
  const order = ['subject', 'scene', 'viewpoint', 'composition', 'movement', 'shot_size', 'action', 'style', 'camera', 'color', 'music_style', 'music_mood', 'music_instrument', 'music_use', 'music_voice', 'keywords', 'custom'];
  const rows = order.flatMap((key) => {
    if (key === 'movement' && item.media_type !== 'video') return [];
    const values = key==='custom' && item.media_type==='audio' ? (fields[key]||[]) : materialValues(fields[key]);
    if (!values.length) return [];
    return ['<div class="material-tag-row"><dt>' + esc(labels[key]) + '：</dt><dd>'
      + esc(values.join('、')) + '</dd></div>'];
  });
  if (item.media_type === 'audio') {
    const category = {music: '音乐', sound_effect: '音效', other: '其他'}[item.audio_category] || '其他';
    const duration = Number.isFinite(item.duration) && item.duration > 0
      ? item.duration.toLocaleString('zh-CN', {maximumFractionDigits: 2}) + ' 秒' : '未读取';
    rows.unshift(
      '<div class="material-tag-row"><dt>音频时长：</dt><dd>' + esc(duration) + '</dd></div>',
      '<div class="material-tag-row"><dt>音频分类：</dt><dd>' + esc(category) + '</dd></div>'
    );
  }
  if (styleIds.length) rows.push('<div class="material-tag-row"><dt>画风 ID：</dt><dd style="white-space:pre-wrap">'+styleIds.map(esc).join('、')+'</dd></div>');
  if (item.media_type==='audio' && (item.tag_sources||[]).some(t=>t.source==='music_prompt')) rows.push('<div class="subtle">自动标签来自生成要求，未做音频分析</div>');
  if (!rows.length) return '<p class="material-tag-empty">暂无标签</p>';
  const groups = (list, label) => '<dl class="material-tag-groups" aria-label="' + label + '">' + list.join('') + '</dl>';
  if (!compact) return groups(rows, '分类标签');
  return '<div class="material-tag-preview" tabindex="0">'
    + groups(rows.slice(0, 3), '标签摘要')
    + '<span class="material-tag-hint">全部标签</span>'
    + '<div class="material-tag-panel" tabindex="0">' + groups(rows, '全部分类标签') + '</div></div>';
}
function materialCard(item) {
  const id = encodeURIComponent(item.id);
  const origin = item.origins?.[0] || {};
  const title = item.original_name || origin.prompt || '原记录未保存提示词';
  const audio = item.media_type === 'audio';
  const video = item.media_type === 'video';
  const url = '/api/library/' + id + '/file';
  const media = audio
    ? '<span class="material-audio-icon" aria-hidden="true">♪</span>'
    : video
    ? '<video preload="metadata" muted playsinline src="' + url + '#t=0.1" aria-hidden="true"></video><span class="material-play" aria-hidden="true">▶</span>'
    : '<img loading="lazy" src="/api/library/' + id + '/thumbnail" alt="">';
  const source = materialSource(origin) + ((item.origins || []).length > 1 ? ' · ' + item.origins.length + ' 条来源' : '');
  return '<article class="material-card"><button type="button" class="material-cover' + (audio ? ' is-audio' : '') + '" data-material="' + esc(item.id)
    + '" aria-label="查看' + (audio ? '音频' : video ? '视频' : '图片') + '：' + esc(title.slice(0, 60)) + '">' + media
    + '<span class="material-kind">' + (audio ? '音频' : video ? '视频' : '图片') + ' · ' + esc(materialSize(item.size))
    + '</span></button><div class="material-card-body"><h3 title="' + esc(title) + '">' + esc(title) + '</h3>'
    + '<p class="material-card-meta">' + esc(origin.account_email || origin.account_id || '原账号未记录') + '</p>'
    + '<p class="material-card-meta">' + esc(source) + '</p>' + materialTagGroups(item, true)
    + '<div class="material-card-actions"><button class="text-link" type="button" data-material="' + esc(item.id)
    + '">预览与管理 →</button><a href="' + url + '?download=1" download>下载</a></div></div></article>';
}
function updateLibraryFilters(data) {
  updateOptions($('libraryTag'), '全部标签', data.tags || []);
  updateOptions($('libraryAccount'), '全部账号', data.accounts || []);
  for (const [key, label] of Object.entries(librarySearchDimensions)) {
    updateOptions($('libraryFilter_' + key), '全部' + label, data.facets?.dimensions?.[key] || []);
  }
  if (data.stats) {
    $('libraryTotal').innerHTML = esc((data.stats.image || 0) + (data.stats.video || 0) + (data.stats.audio || 0)) + '<small>份已保存素材</small>';
    $('libraryBreakdown').innerHTML = esc(data.stats.image || 0) + ' 张图片 · ' + esc(data.stats.video || 0)
      + ' 段视频 · ' + esc(data.stats.audio || 0) + ' 段音频<small>' + esc(materialSize(data.stats.bytes || 0)) + ' · Ubuntu 素材库</small>';
  }
}
$('libraryDimensionFilters').innerHTML = Object.entries(librarySearchDimensions).map(([key, label]) =>
  '<label>' + esc(label) + '<select id="libraryFilter_' + esc(key) + '"><option value="">全部' + esc(label) + '</option></select></label>').join('');

let libraryViewRefreshing = false;
async function refreshLibraryView({background = true} = {}) {
  if (libraryViewRefreshing || document.querySelector('dialog[open]') ||
      document.activeElement?.closest('#libraryView input, #libraryView select')) return;
  libraryViewRefreshing = true;
  try { await loadLibrary({background}); }
  finally { libraryViewRefreshing = false; }
}

const libraryAudioFilters = {
  audio_category: $('libraryAudioCategory'),
  duration_min: $('libraryDurationMin'),
  duration_max: $('libraryDurationMax')
};

function updateLibraryAudioFilters() {
  const disabled = !['', 'audio'].includes($('libraryKind').value);
  for (const control of Object.values(libraryAudioFilters)) {
    control.disabled = disabled;
    control.closest('label').hidden = disabled;
  }
}

async function loadLibrary({background = false} = {}) {
  const sequence = ++libraryRequest;
  const query = new URLSearchParams({page: String(libraryPage), limit: '12'});
  const text = $('librarySearch').value.trim();
  if (text) query.set('q', text);
  if ($('libraryTag').value) query.set('tag',$('libraryTag').value);
  if ($('libraryStyleId').value) query.set('style_id',$('libraryStyleId').value);
  if ($('libraryKind').value) query.set('kind', $('libraryKind').value);
  const {duration_min: min, duration_max: max} = libraryAudioFilters;
  if (!min.disabled && (!min.validity.valid || !max.validity.valid
      || (min.value !== '' && max.value !== '' && min.valueAsNumber > max.valueAsNumber))) {
    if (!background) $('librarySummary').textContent = '请输入有效秒数，最短时长不能大于最长时长。';
    return;
  }
  for (const [name, control] of Object.entries(libraryAudioFilters)) {
    if (!control.disabled && control.value !== '') query.set(name, control.value);
  }
  if ($('libraryAccount').value) query.set('account', $('libraryAccount').value);
  for (const key of Object.keys(librarySearchDimensions)) {
    if ($('libraryFilter_' + key).value) query.set(key, $('libraryFilter_' + key).value);
  }
  if (!background) $('librarySummary').textContent = '正在读取素材…';
  try {
    const data = await getJson('/api/library?' + query.toString(), {timeoutMs: 10000});
    if (sequence !== libraryRequest || (background && (state.view !== 'library' || document.querySelector('dialog[open]')))) return;
    updateLibraryFilters(data);
    libraryPages = data.pages || 1;
    libraryPage = data.page || 1;
    $('librarySummary').textContent = '找到 ' + data.total + ' 份素材';
    $('libraryPage').textContent = '第 ' + libraryPage + ' / ' + libraryPages + ' 页';
    $('libraryPrev').disabled = libraryPage <= 1;
    $('libraryNext').disabled = libraryPage >= libraryPages;
    const items = $('libraryItems');
    const markup = data.items.length ? data.items.map(materialCard).join('')
      : empty('没有找到素材', '试试更换关键词或清空筛选条件。');
    if (!background || lastLibraryMarkup !== markup) {
      items.innerHTML = markup;
      lastLibraryMarkup = markup;
    }
  } catch (_) {
    if (background || sequence !== libraryRequest) return;
    lastLibraryMarkup = null;
    $('librarySummary').textContent = '素材库暂不可读取，请检查原素材库服务。';
    $('libraryItems').innerHTML = empty('暂时无法读取素材', '请稍后刷新重试。');
  }
}

let autoMaterialSaving = false, autoMaterialRead = 0;
async function loadAutoMaterialState({background = false} = {}) {
  const read = ++autoMaterialRead;
  const button = $('autoAnalysisToggle');
  try {
    const data = await getJson('/api/auto-material-analysis', {timeoutMs: 8000});
    if (read !== autoMaterialRead) return;
    autoMaterialState = data;
    const counts = data.counts || {};
    const summary = '等待 ' + (counts.waiting || 0) + ' · 已完成 ' + (counts.completed || 0)
      + ' · 需核对 ' + ((counts.unknown || 0) + (counts.failed || 0));
    $('autoAnalysisStatus').textContent = !data.enabled ? '已关闭' : data.paused_reason ? '已暂停：' + data.paused_reason : '已开启 · ' + summary;
    button.textContent = !data.enabled ? '开启自动分析' : data.paused_reason ? '继续自动分析' : '关闭自动分析';
    button.disabled = false;
  } catch (_) {
    if (read !== autoMaterialRead || (background && autoMaterialState)) return;
    autoMaterialState = null;
    $('autoAnalysisStatus').textContent = '设置暂不可读取。';
    button.textContent = '暂不可用'; button.disabled = true;
  }
}
$('autoAnalysisToggle').addEventListener('click', async () => {
  if (!autoMaterialState || autoMaterialSaving) return;
  autoMaterialSaving = true;
  ++autoMaterialRead;
  const button = $('autoAnalysisToggle');
  button.disabled = true;
  try {
    const enabled = !autoMaterialState.enabled || !!autoMaterialState.paused_reason;
    await postJson('/api/auto-material-analysis', {enabled});
    await loadAutoMaterialState();
  } catch (error) {
    $('autoAnalysisStatus').textContent = error.message;
    button.disabled = false;
  } finally { autoMaterialSaving = false; }
});

function renderBackfill() {
  const run = backfillState?.run, counts = backfillState?.counts || {};
  const pending = (counts.waiting || 0) + (counts.running || 0);
  const paused = !!run?.paused_reason;
  const summary = (run?.scope === 'windows_import' ? 'Windows 素材标签' : '补打') + '：已完成 '
    + (counts.completed || 0) + ' · 等待 ' + (counts.waiting || 0)
    + ' · 处理中 ' + (counts.running || 0) + ' · 需核对 ' + ((counts.failed || 0) + (counts.unknown || 0));
  $('backfillStatus').textContent = run ? summary + (paused ? ' · 已暂停' : '') : '';
  $('backfillResume').hidden = !(paused && pending && ['untagged', 'unanalyzed', 'windows_import'].includes(run.scope));
  $('backfillResume').disabled = backfillBusy;
  $('backfillOpen').disabled = backfillBusy || !backfillState || !!pending;
  $('backfillOpen').textContent = !backfillState ? '读取中…' : backfillBusy ? '正在处理…' : pending ? (paused ? '补打已暂停' : '补打进行中') : '补打标签';
  $('backfillIssues').innerHTML = (backfillState?.issues || []).map((issue) =>
    '<p class="storage-warning">' + esc(issue.note || '分析结果需要核对') + ' <button type="button" class="text-link" data-backfill-issue="' + esc(issue.asset_id) + '">查看素材</button></p>').join('');
  if (!backfillBusy && paused) $('backfillMessage').textContent = run.paused_reason;
  else if (!backfillBusy && pending) $('backfillMessage').textContent = '后台处理中；没有可用账号或额度时会等待。';
  else if (!backfillBusy && $('backfillMessage').textContent === '后台处理中；没有可用账号或额度时会等待。') $('backfillMessage').textContent = '本轮补打已结束。';
}

async function loadBackfillState({background = false} = {}) {
  try {
    const previous = backfillState, next = await getJson('/api/material-backfill', {timeoutMs: 10000});
    backfillState = next;
    if (backfillRequest && next.run?.id === backfillRequest) backfillRequest = null;
    renderBackfill();
    if (!background && previous && next.run && previous.run?.id === next.run.id && (next.counts?.completed || 0) > (previous.counts?.completed || 0) && state.view === 'library') loadLibrary();
    return true;
  } catch (_) {
    if (background) return false;
    backfillState = null; renderBackfill();
    $('backfillStatus').textContent = '补打进度暂不可读取。';
    return false;
  }
}

$('backfillOpen').addEventListener('click', async () => {
  if (backfillBusy || !backfillState || (backfillState.counts?.waiting || 0) + (backfillState.counts?.running || 0)) return;
  backfillBusy = true; renderBackfill();
  $('backfillMessage').textContent = '正在查找尚未进行 Gemini 分析的素材…';
  try {
    if (backfillRequest) {
      $('backfillMessage').textContent = '上次提交状态尚未确认，请查看补打进度，不会重复提交。';
      return;
    }
    const preview = await getJson('/api/material-backfill/preview');
    if (!Number.isInteger(preview.unanalyzed) || preview.unanalyzed < 0) throw new Error('素材状态暂不可读取。');
    if (!preview.unanalyzed) {
      $('backfillMessage').textContent = '当前没有可补充分析的素材。已有分析、失败或结果未确认的素材不会重复提交。';
      return;
    }
    $('backfillPreviewCount').textContent = String(preview.unanalyzed);
    $('backfillMessage').textContent = '请在弹窗中确认补打范围。';
    if (!$('backfillDialog').open) $('backfillDialog').showModal();
  } catch (error) {
    $('backfillMessage').textContent = error.message || '素材库暂不可读取，请稍后再试。';
  } finally {
    backfillBusy = false;
    renderBackfill();
  }
});
$('backfillCancel').addEventListener('click', () => {
  $('backfillDialog').close();
  $('backfillMessage').textContent = '已取消，本次没有提交分析。';
});
$('backfillDialog').addEventListener('cancel', (event) => {
  if (backfillBusy) { event.preventDefault(); return; }
  $('backfillMessage').textContent = '已取消，本次没有提交分析。';
});
$('backfillConfirm').addEventListener('click', async () => {
  if (backfillBusy || backfillRequest) return;
  backfillBusy = true;
  $('backfillConfirm').disabled = true;
  $('backfillCancel').disabled = true;
  renderBackfill();
  try {
    backfillRequest = crypto.randomUUID();
    backfillState = await postJson('/api/material-backfill', {scope:'unanalyzed'}, backfillRequest);
    backfillRequest = null;
    $('backfillDialog').close();
    $('backfillMessage').textContent = '已加入后台队列。';
  } catch (error) {
    const request = backfillRequest;
    const confirmed = await loadBackfillState();
    $('backfillDialog').close();
    if (request && backfillState?.run?.id === request) {
      backfillRequest = null;
      $('backfillMessage').textContent = '已加入后台队列。';
    } else {
      $('backfillMessage').textContent = error.status ? error.message : (confirmed && !request ? '素材库暂不可读取，请稍后再试。' : '提交状态暂未确认，请查看补打进度。');
      if (error.status) backfillRequest = null;
    }
  } finally {
    backfillBusy = false;
    $('backfillConfirm').disabled = false;
    $('backfillCancel').disabled = false;
    renderBackfill();
  }
});
$('backfillIssues').addEventListener('click', (event) => {
  const button = event.target.closest('[data-backfill-issue]');
  if (button) showMaterial(button.dataset.backfillIssue).catch(() => { $('backfillMessage').textContent = '素材详情暂不可读取。'; });
});
$('backfillResume').addEventListener('click', async () => {
  if (backfillBusy || !['untagged', 'unanalyzed'].includes(backfillState?.run?.scope)) return;
  backfillBusy = true; renderBackfill();
  try {
    backfillState = await postJson('/api/material-backfill/resume', {});
    $('backfillMessage').textContent = '已继续处理剩余素材，不会重复提交之前的素材。';
  } catch (error) { $('backfillMessage').textContent = error.message; }
  finally { backfillBusy = false; renderBackfill(); }
});

let materialActivityLoading = false, materialActivityPage = 1, materialActivityPages = 1;
function materialAnalysisAccount(key) {
  if (!key) return '未记录账号';
  const accounts = state.snapshot?.real_accounts?.items || [];
  const match = accounts.find((a) => a.key === key || a.key === 'flow-account-' + key);
  return match ? (match.email || match.label || match.profile || '账号名称未记录') : '账号信息暂不可用';
}
function materialAnalysisResult(record) {
  const labels = {...materialDimensions, ...extraDimensions};
  const values = Object.entries(record.result_tags || {}).filter(([, v]) => Array.isArray(v) && v.length)
    .map(([key, v]) => esc(labels[key] || key) + '：' + v.map(esc).join('、'));
  return values.length ? '<details><summary>查看本次返回的标签</summary>' + values.join('<br>') + '</details>' : '<small>本次没有可展示的标签结果</small>';
}
function materialAnalysisStatus(record) {
  return ({completed:'已完成', preparing:'处理中', saving:'保存中', failed:'失败', invalid:'回复格式待核对', unknown:'结果待核对'})[record.status] || '状态待核对';
}
let materialActivitySignature = null;
async function loadMaterialActivity(page = materialActivityPage, {background = false} = {}) {
  if (materialActivityLoading) return;
  materialActivityLoading = true;
  $('materialActivityRefresh').disabled = true;
  if (!background) $('materialActivityStatus').textContent = '正在读取素材分析记录…';
  try {
    const data = await getJson('/api/material-analysis-history?page=' + page + '&limit=20', {timeoutMs:8000});
    if (!Array.isArray(data.items) || !Number.isInteger(data.pages)) throw new Error('记录格式不正确');
    const signature = JSON.stringify(data);
    if (background && signature === materialActivitySignature) return;
    materialActivityPage = data.page; materialActivityPages = data.pages;
    const ids = [...new Set(data.items.map((row) => row.asset_id))];
    const results = await Promise.allSettled(ids.map((id) => getJson('/api/library/' + encodeURIComponent(id), {timeoutMs:8000})));
    if (results.every(result => result.status === 'fulfilled')) materialActivitySignature = signature;
    const assets = new Map(ids.map((id, i) => [id, results[i].status === 'fulfilled' ? results[i].value : null]));
    $('materialActivityStatus').textContent = '共 ' + data.total + ' 条分析记录。';
    $('materialActivityPage').textContent = '第 ' + materialActivityPage + ' / ' + materialActivityPages + ' 页';
    $('materialActivityPrev').disabled = materialActivityPage <= 1;
    $('materialActivityNext').disabled = materialActivityPage >= materialActivityPages;
    $('materialActivityList').innerHTML = data.items.length ? '<table><thead><tr><th>时间</th><th>素材</th><th>调用账号</th><th>状态与本次结果</th><th>查看素材</th></tr></thead><tbody>' + data.items.map((record) => {
      const item = assets.get(record.asset_id), title = (item?.origins?.[0]?.prompt || '素材信息暂不可读取').slice(0, 60);
      return '<tr><td>' + esc(dateTime(record.created)) + '</td><td>' + (item?.media_type === 'video' ? '视频 · ' : item ? '图片 · ' : '') + esc(title)
        + '</td><td>' + esc(materialAnalysisAccount(record.account_key)) + '</td><td>' + esc(materialAnalysisStatus(record)) + materialAnalysisResult(record)
        + '</td><td><button type="button" class="text-link" data-analysis-material="' + esc(record.asset_id) + '">查看素材和标签</button></td></tr>';
    }).join('') + '</tbody></table>' : empty('暂无素材分析记录');
  } catch (_) {
    if (!background) $('materialActivityStatus').textContent = '素材分析记录暂不可读取，稍后将自动更新。';
  } finally {
    materialActivityLoading = false;
    $('materialActivityRefresh').disabled = false;
  }
}
$('materialActivityPanel').addEventListener('toggle', () => { if ($('materialActivityPanel').open) loadMaterialActivity(); });
$('materialActivityRefresh').addEventListener('click', () => loadMaterialActivity());
$('materialActivityPrev').addEventListener('click', () => { if (materialActivityPage > 1) loadMaterialActivity(materialActivityPage-1); });
$('materialActivityNext').addEventListener('click', () => { if (materialActivityPage < materialActivityPages) loadMaterialActivity(materialActivityPage+1); });
$('materialActivityList').addEventListener('click', (event) => {
  const button = event.target.closest('[data-analysis-material]');
  if (button) showMaterial(button.dataset.analysisMaterial).catch(() => { $('materialActivityStatus').textContent = '素材详情暂不可读取。'; });
});

function materialTagSources(item, extra) {
  const groups = {'提示词标签': [], '视觉分析（与 Gemini 结果一致）': [], '视觉分析（来源待核实）': [], '手动补充': [], '其他或来源未记录': []};
  const labels = {...materialDimensions, ...extraDimensions};
  const known = new Set();
  for (const tag of item.tag_sources || []) {
    if (!tag || typeof tag.dimension !== 'string' || typeof tag.value !== 'string') continue;
    known.add(tag.dimension + '\u0000' + tag.value);
    const matches = extra.analysis?.status === 'completed' && Array.isArray(extra.suggested?.[tag.dimension]) && extra.suggested[tag.dimension].includes(tag.value);
    const group = ['prompt_analysis','music_prompt'].includes(tag.source) ? '提示词标签' : tag.source === 'visual_analysis' ? (matches ? '视觉分析（与 Gemini 结果一致）' : '视觉分析（来源待核实）') : '其他或来源未记录';
    groups[group].push((labels[tag.dimension] || tag.dimension) + '：' + tag.value);
  }
  for (const [key, values] of Object.entries(item.structured_tags || {})) {
    for (const value of Array.isArray(values) ? values : []) if (!known.has(key + '\u0000' + value)) groups['其他或来源未记录'].push((labels[key] || key) + '：' + value);
  }
  for (const value of item.tags || []) groups['其他或来源未记录'].push('自定义：' + value);
  for (const [key, values] of Object.entries(extra.manual || {})) for (const value of Array.isArray(values) ? values : []) groups['手动补充'].push((labels[key] || key) + '：' + value);
  return Object.entries(groups).filter(([, values]) => values.length).map(([name, values]) =>
    '<div class="material-suggestions"><strong>' + esc(name) + '</strong><br>' + values.map(esc).join('、') + '</div>').join('');
}

function materialShotNotes(item, extra) {
  const video = item.media_type === 'video';
  const complete = extra.analysis?.status === 'completed';
  const analyzed = complete ? extra.suggested : null;
  const fields = video ? ['movement', 'composition', 'viewpoint', 'shot_size'] : ['composition', 'viewpoint', 'shot_size'];
  const camera = materialValues(analyzed?.camera);
  const rows = fields.map((key) => {
    const manual = materialValues(extra.manual?.[key]);
    let result = materialValues(analyzed?.[key]);
    let source = manual.length ? '（手动）' : '';
    // Some analyses report a stationary camera only in the camera field.
    if (!manual.length && !result.length && key === 'movement' && camera.length && camera.every((v) => ['固定镜头', '固定机位', '静止镜头'].includes(v))) {
      result = ['固定镜头'];
      source = '（据镜头标签）';
    }
    const label = (manual.length ? manual : result).join('、');
    const explanation = extra.shot_notes?.[key];
    return '<div class="material-shot-row"><strong>' + esc(extraDimensions[key]) + '：</strong>'
      + esc(label || (explanation ? '' : '暂无记录')) + esc(source)
      + (key !== 'shot_size' && (label || explanation)
        ? '<p class="material-shot-explanation">' + esc(explanation || '暂无具体说明') + '</p>' : '')
      + '</div>';
  });
  const note = extra.shot_notes_source === 'gemini'
    ? (video ? '具体说明由 Gemini 根据按时间排序的视频抽帧生成。' : '具体说明由 Gemini 根据原图生成。')
    : complete && /抽帧/.test(extra.analysis?.note || '') && video
    ? 'Gemini 根据视频抽帧分析，细微运镜以原片为准。'
    : complete ? '说明来自已保存的 Gemini 分析和手动记录。' : '已有手动记录会显示在这里；未确认的分析不会当作结果。';
  return '<section class="material-suggestions" aria-label="镜头说明"><h3>镜头说明</h3>'
    + rows.join('') + '<small>' + esc(note) + '</small></section>';
}

let materialHistoryRead = 0, materialHistoryPage = 1;
async function loadMaterialHistory(id, page = 1, {background = false} = {}) {
  const read = ++materialHistoryRead;
  const panel = $('materialHistory');
  if (!panel) return;
  if (!background) panel.textContent = '正在读取此素材的分析历史…';
  try {
    const data = await getJson('/api/material-analysis-history?asset_id=' + encodeURIComponent(id) + '&page=' + page + '&limit=10', {timeoutMs:8000});
    if (read !== materialHistoryRead || currentMaterial?.id !== id || $('materialHistory') !== panel) return;
    materialHistoryPage = data.page || 1;
    const markup = data.items.length ? data.items.map((record) => '<div class="material-suggestions"><strong>' + esc(dateTime(record.created))
      + ' · ' + esc(materialAnalysisStatus(record)) + '</strong><br>调用账号：' + esc(materialAnalysisAccount(record.account_key))
      + '<br>' + materialAnalysisResult(record) + '</div>').join('')
      + '<div class="library-pagination"><button class="button button-light" type="button" data-material-history-page="' + (data.page-1) + '"' + (data.page <= 1 ? ' disabled' : '') + '>上一页</button><span>第 ' + data.page + ' / ' + data.pages + ' 页</span><button class="button button-light" type="button" data-material-history-page="' + (data.page+1) + '"' + (data.page >= data.pages ? ' disabled' : '') + '>下一页</button></div>'
      : '<p>暂无分析记录。</p>';
    if (panel.dataset.markup !== markup || !background) { panel.dataset.markup = markup; panel.innerHTML = markup; }
  } catch (_) { if (!background && read === materialHistoryRead && $('materialHistory') === panel) panel.textContent = '分析历史暂不可读取。'; }
}

function materialSuggestions(extra) {
  const labels = {...materialDimensions, ...extraDimensions};
  const values = Object.entries(extra.suggested || {}).filter(([, values]) => values.length)
    .map(([key, values]) => (labels[key] || key) + '：' + values.join('、'));
  return values.length ? '<div class="material-suggestions"><strong>Gemini 分析标签</strong><br>' + values.map(esc).join('<br>') + '</div>' : '';
}
function syncMaterialMarkup(id, markup) {
  const element = $(id);
  if (element.dataset.markup === markup) return;
  element.dataset.markup = markup;
  element.innerHTML = markup;
}

let materialOperation = false;
async function refreshOpenMaterial() {
  if (!currentMaterial) return;
  const id = currentMaterial.id, sequence = materialOpenRequest;
  const history = $('materialHistory');
  if (history?.closest('details')?.open) loadMaterialHistory(id, materialHistoryPage, {background:true});
  if (materialOperation || materialSaving) return;
  const [item, extra] = await Promise.all([
    getJson('/api/library/' + encodeURIComponent(id), {timeoutMs:8000}),
    getJson('/api/material-extras/' + encodeURIComponent(id), {timeoutMs:8000})
  ]);
  if (sequence !== materialOpenRequest || materialOperation || materialSaving || !$('libraryDetailPanel').open) return;
  syncMaterialMarkup('materialShotContent', materialShotNotes(item, extra));
  syncMaterialMarkup('materialDetailTags', materialTagGroups(item));
  syncMaterialMarkup('materialDetailSources', materialTagSources(item, extra) || '<p>没有记录标签来源。</p>');
  const suggestions = materialSuggestions(extra);
  syncMaterialMarkup('materialDetailSuggestions', suggestions);
  $('materialApply').hidden = !suggestions;
  $('materialApply').disabled = materialDirty;
  $('materialShotAnalyze').hidden = !!(Object.keys(extra.shot_notes || {}).length || extra.shot_job);
  $('materialShotAnalyze').disabled = $('materialShotAnalyze').hidden || $('materialShotAnalyze').dataset.unconfirmed === '1';
  const shotNote = extra.shot_job?.note || '';
  if (shotNote || extra.shot_job || currentMaterialExtra?.shot_job) $('materialShotStatus').textContent = shotNote;
  if (extra.analysis && JSON.stringify(extra.analysis) !== $('materialAnalyze').dataset.priorAnalysis) {
    delete $('materialAnalyze').dataset.unconfirmed;
  }
  $('materialAnalyze').disabled = $('materialAnalyze').dataset.unconfirmed === '1' ||
    ['preparing', 'saving', 'unknown'].includes(extra.analysis?.status);
  const analysisNote = extra.analysis?.note || extra.automatic?.note || '';
  if (analysisNote || extra.analysis || currentMaterialExtra?.analysis) $('analysisMessage').textContent = analysisNote;
  // Leave the edit baseline and the user's fields together while a draft exists.
  if (materialDirty || document.activeElement?.closest('#materialEdit input')) return;
  currentMaterial = item;
  currentMaterialExtra = extra;
  $('materialTags').value = (item.tags || []).join('，');
  for (const key of Object.keys(materialDimensions)) $('dimension_' + key).value = (item.structured_tags?.[key] || []).join('，');
  for (const key of Object.keys(extraDimensions)) $('dimension_' + key).value = (extra.manual?.[key] || []).join('，');
}

async function showMaterial(id) {
  const sequence = ++materialOpenRequest;
  const [item, extra] = await Promise.all([getJson('/api/library/' + encodeURIComponent(id)), getJson('/api/material-extras/' + encodeURIComponent(id))]);
  if (sequence !== materialOpenRequest) return;
  currentMaterial = item;
  currentMaterialExtra = extra;
  materialDirty = false;
  const url = '/api/library/' + encodeURIComponent(id) + '/file';
  const technical = extra.technical || item;
  const resolution = technical.width && technical.height ? technical.width + ' × ' + technical.height : '未读取';
  const duration = item.media_type === 'image' ? '不适用' : (typeof technical.duration === 'number' ? technical.duration.toFixed(1) + ' 秒' : '未读取');
  const suggested = materialSuggestions(extra);
  const analysisBusy = ['preparing', 'saving', 'unknown'].includes(extra.analysis?.status);
  const origin = item.origins?.[0] || {};
  const preview = item.media_type === 'audio'
    ? '<audio src="' + url + '" controls preload="metadata" style="width:100%;max-width:560px"></audio>'
    : item.media_type === 'image'
    ? '<img src="' + url + '" alt="素材原图">'
    : '<video src="' + url + '" controls playsinline preload="metadata"></video>';
  const editFields = Object.entries(materialDimensions).map(([key, label]) =>
    '<label>' + esc(label) + '<input id="dimension_' + esc(key) + '" value="' + esc((item.structured_tags?.[key] || []).join('，')) + '" maxlength="3000"></label>').join('')
    + Object.entries(extraDimensions).map(([key, label]) =>
      '<label>' + esc(label) + '<input id="dimension_' + esc(key) + '" value="' + esc((extra.manual?.[key] || []).join('，')) + '" maxlength="3000"></label>').join('');
  $('libraryDetail').innerHTML = '<div class="material-layout"><div><div class="material-preview">' + preview + '</div>'
    + '<p class="material-facts">' + (item.media_type === 'audio' ? '文件大小：' : '分辨率：' + esc(resolution) + '　·　时长：' + esc(duration) + '　·　') + esc(materialSize(item.size)) + '</p>'
    + '<p class="material-facts">来源：' + esc(materialSource(origin)) + '　·　账号：'
    + esc(origin.account_email || origin.account_id || '未记录') + ((item.origins || []).length > 1 ? '　·　共 ' + item.origins.length + ' 条来源' : '') + '</p>'
    + (item.origins || []).filter(o => typeof o.style_id === 'string').map(o =>
      '<p>画风 ID：<code style="white-space:pre-wrap">' + esc(o.style_id) + '</code> · ' + esc(o.request_id || o.source_id || '') + '</p>').join('')
    + '<div class="material-prompt">' + esc(origin.prompt || item.original_name || '原记录未保存提示词') + '</div></div><div class="material-panel"><h3>素材与标签</h3>'
    + '<a class="text-link material-download" href="' + url + '?download=1" download>下载原文件 ↓</a>'
    + '<div id="materialShotContent">' + materialShotNotes(item, extra) + '</div>'
    + '<button id="materialShotAnalyze" class="button button-light" type="button"' + (Object.keys(extra.shot_notes || {}).length || extra.shot_job ? ' hidden' : '') + '>用 Gemini 补充镜头说明</button>'
    + '<p id="materialShotStatus" role="status">' + esc(extra.shot_job?.note || '') + '</p>'
    + '<div id="materialDetailTags">' + materialTagGroups(item) + '</div>'
    + '<details class="material-edit" open><summary>标签来源</summary><p>提示词标签来自文字，不代表画面已核实；“与 Gemini 结果一致”表示内容匹配，不单独证明写入来源。</p><div id="materialDetailSources">' + (materialTagSources(item, extra) || '<p>没有记录标签来源。</p>') + '</div></details>'
    + '<button id="materialAnalyze" class="button button-light" type="button"' + (analysisBusy ? ' disabled' : '') + '>用 Gemini 分析素材</button>'
    + '<p>会真实调用 Gemini，可能产生费用；不会覆盖已有标签。</p><p id="analysisMessage" role="status">' + esc(extra.analysis?.note || extra.automatic?.note || '') + '</p>'
    + '<div id="materialDetailSuggestions">' + suggested + '</div>'
    + '<button id="materialApply" class="button button-light" type="button"' + (suggested ? '' : ' hidden') + '>将分析结果填入空白标签</button>'
    + '<details class="material-edit" open><summary>此素材的 Gemini 分析历史</summary><div id="materialHistory" aria-live="polite">正在读取…</div></details>'
    + '<details class="material-edit" id="materialEdit"><summary>编辑标签与分析维度</summary><div class="material-fields">'
    + '<label class="wide">自定义标签<input id="materialTags" value="' + esc((item.tags || []).join('，')) + '" maxlength="2400"></label>' + editFields
    + '</div><button id="materialSave" class="button button-primary material-save" type="button">保存标签</button><p id="materialMessage" role="status"></p></details></div></div>';
  $('materialSave').addEventListener('click', saveMaterialTags);
  $('materialAnalyze').addEventListener('click', analyzeMaterial);
  $('materialShotAnalyze')?.addEventListener('click', async (event) => {
    if (materialOperation) return;
    const button = event.currentTarget, version = materialOpenRequest;
    materialOperation = true;
    button.disabled = true;
    $('materialShotStatus').textContent = '正在把素材交给 Gemini…';
    try {
      const result = await postJson('/api/material-shot-notes/' + id, {}, crypto.randomUUID());
      if (version === materialOpenRequest) {
        button.hidden = true;
        syncMaterialMarkup('materialShotContent', materialShotNotes(item, result));
        $('materialShotStatus').textContent = result.shot_job?.note || '';
      }
    } catch (error) {
      if (version === materialOpenRequest) { button.dataset.unconfirmed = '1'; $('materialShotStatus').textContent = '提交未确认，正在自动核对。' + error.message; }
    } finally { materialOperation = false; }
  });
  $('materialHistory').addEventListener('click', (event) => {
    const button = event.target.closest('[data-material-history-page]');
    if (button && !button.disabled) loadMaterialHistory(id, Number(button.dataset.materialHistoryPage));
  });
  materialHistoryPage = 1;
  loadMaterialHistory(id);
  if ($('materialApply')) $('materialApply').addEventListener('click', () => {
    for (const [key, values] of Object.entries(currentMaterialExtra.suggested || {})) {
      const field = $('dimension_' + key);
      if (field && !field.value.trim()) field.value = values.join('，');
    }
    materialDirty = true;
    $('materialEdit').open = true;
    $('materialMessage').textContent = '分析结果已填入空白项，请核对后保存。';
  });
  if (!$('libraryDetailPanel').open) $('libraryDetailPanel').showModal();
}
function splitTags(value) { return value.split(/[,，]/).map((part) => part.trim()).filter(Boolean); }
async function saveMaterialTags() {
  if (!currentMaterial || materialSaving) return;
  const data = {}, tags = splitTags($('materialTags').value), dimensions = {};
  if (JSON.stringify(tags) !== JSON.stringify(currentMaterial.tags || [])) data.tags = tags;
  for (const key of Object.keys(materialDimensions)) {
    const values = splitTags($('dimension_' + key).value);
    if (JSON.stringify(values) !== JSON.stringify(currentMaterial.structured_tags?.[key] || [])) dimensions[key] = values;
  }
  const extra = {};
  for (const key of Object.keys(extraDimensions)) extra[key] = splitTags($('dimension_' + key).value);
  const extraChanged = Object.keys(extraDimensions).some((key) => JSON.stringify(extra[key]) !== JSON.stringify(currentMaterialExtra.manual?.[key] || []));
  if (Object.keys(dimensions).length) data.structured_tags = dimensions;
  if (!Object.keys(data).length && !extraChanged) { materialDirty = false; $('materialMessage').textContent = '没有需要保存的更改。'; return; }
  let originalSaved = false, extraSaved = false, refreshing = false;
  const id = currentMaterial.id;
  materialSaving = true;
  $('materialSave').disabled = true;
  $('libraryDetail').querySelectorAll('input, #materialApply, #materialAnalyze').forEach((control) => { control.disabled = true; });
  $('materialMessage').textContent = '正在保存…';
  try {
    if (Object.keys(data).length) { await postJson('/api/library/' + id + '/tags', data); originalSaved = true; }
    if (extraChanged) { await postJson('/api/material-extras/' + id, extra); extraSaved = true; }
    materialDirty = false;
    refreshing = true;
    await showMaterial(id);
    $('materialEdit').open = true;
    $('materialMessage').textContent = '标签已保存。';
    loadLibrary();
  } catch (error) {
    $('materialMessage').textContent = refreshing
      ? '标签保存已提交，但详情刷新失败，请关闭后重新打开核对。'
      : ((originalSaved && !extraSaved ? '原素材库标签已保存；其他维度未保存：' : '') + error.message);
  } finally {
    materialSaving = false;
    if ($('materialSave')) $('materialSave').disabled = false;
    $('libraryDetail').querySelectorAll('input, #materialApply').forEach((control) => { control.disabled = false; });
    if ($('materialAnalyze') && !['preparing', 'saving', 'unknown'].includes(currentMaterialExtra?.analysis?.status)) $('materialAnalyze').disabled = false;
  }
}
async function analyzeMaterial() {
  if (!currentMaterial || materialOperation) return;
  if (materialDirty) { $('analysisMessage').textContent = '请先保存或放弃未保存的标签，再分析素材。'; return; }
  const id = currentMaterial.id, request = crypto.randomUUID();
  materialOperation = true;
  $('materialAnalyze').dataset.priorAnalysis = JSON.stringify(currentMaterialExtra?.analysis);
  $('materialAnalyze').disabled = true;
  $('libraryDetail').querySelectorAll('input, #materialSave, #materialApply').forEach((control) => { control.disabled = true; });
  $('analysisMessage').textContent = '正在分析，请勿重复提交。';
  try {
    await postJson('/api/material-analysis/' + id, {}, request);
    if (currentMaterial?.id === id) await showMaterial(id);
  } catch (_) {
    if (currentMaterial?.id === id) {
      $('libraryDetail').querySelectorAll('input, #materialSave, #materialApply').forEach((control) => { control.disabled = false; });
      $('materialAnalyze').dataset.unconfirmed = '1';
      $('analysisMessage').textContent = '结果暂未确认，正在自动核对；系统不会自动重复提交。';
    }
  } finally { materialOperation = false; }
}
$('libraryItems').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-material]');
  if (button) { try { await showMaterial(button.dataset.material); } catch (_) { $('librarySummary').textContent = '素材详情暂不可读取。'; } }
});
$('libraryDetail').addEventListener('input', (event) => { if (event.target.matches('input')) materialDirty = true; });
$('libraryDetailPanel').addEventListener('cancel', (event) => {
  if (materialSaving || (materialDirty && !confirm('标签尚未保存，确定关闭并放弃修改吗？'))) event.preventDefault();
});
$('libraryDetailPanel').addEventListener('close', () => { materialOpenRequest++; currentMaterial = null; currentMaterialExtra = null; materialDirty = false; });
$('libraryClose').addEventListener('click', () => {
  if (materialSaving || (materialDirty && !confirm('标签尚未保存，确定关闭并放弃修改吗？'))) return;
  $('libraryDetailPanel').close();
});
$('libraryFind').addEventListener('click', () => { libraryPage = 1; loadLibrary(); });
$('librarySearch').addEventListener('input', () => { libraryRequest++; });
$('librarySearch').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); libraryPage = 1; loadLibrary(); } });
$('libraryKind').addEventListener('change', () => { updateLibraryAudioFilters(); libraryPage = 1; loadLibrary(); });
for (const control of Object.values(libraryAudioFilters)) {
  control.addEventListener('change', () => { libraryPage = 1; loadLibrary(); });
  if (control.type === 'number') {
    control.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); libraryPage = 1; loadLibrary(); }
    });
  }
}
$('libraryTag').addEventListener('change',()=>{libraryPage=1;loadLibrary();});
$('libraryStyleId').addEventListener('change',()=>{libraryPage=1;loadLibrary();});
$('libraryAccount').addEventListener('change', () => { libraryPage = 1; loadLibrary(); });
$('libraryDimensionFilters').addEventListener('change', () => { libraryPage = 1; loadLibrary(); });
$('libraryReset').addEventListener('click', () => {
  $('librarySearch').value = '';
  $('libraryKind').value = '';
  $('libraryAccount').value = '';
  $('libraryTag').value=''; $('libraryStyleId').value='';
  for (const control of Object.values(libraryAudioFilters)) control.value = '';
  updateLibraryAudioFilters();
  $('libraryDimensionFilters').querySelectorAll('select').forEach((control) => { control.value = ''; });
  libraryPage = 1; loadLibrary();
});
$('libraryPrev').addEventListener('click', () => { if (libraryPage > 1) { libraryPage--; loadLibrary(); } });
$('libraryNext').addEventListener('click', () => { if (libraryPage < libraryPages) { libraryPage++; loadLibrary(); } });

let storageRead = 0;
async function showLibraryStorage({background = false} = {}) {
  const read = ++storageRead;
  if (!background) {
    $('libraryStorageMessage').textContent = '';
    $('libraryStoragePath').textContent = '正在读取…';
    $('libraryStorageSpace').textContent = '';
    $('libraryStoragePending').textContent = '';
    if (!$('libraryStorageDialog').open) $('libraryStorageDialog').showModal();
  }
  try {
    const data = await getJson('/api/storage/library', {timeoutMs:8000});
    if (read !== storageRead || !$('libraryStorageDialog').open || (background && $('libraryStoragePlan').disabled)) return;
    $('libraryStoragePath').textContent = data.current;
    $('libraryStorageSpace').textContent = '所在磁盘可用 ' + materialSize(data.available_bytes);
    if (data.plan) {
      if (!background) $('libraryStorageTarget').value = data.plan.target;
      $('libraryStoragePending').textContent = '已保存计划位置（尚未生效）：' + data.plan.target
        + (data.plan.current !== data.current ? '。当前库位置已变化，请重新核对。' : '');
    } else $('libraryStoragePending').textContent = '';
  } catch (_) { if (!background && read === storageRead) $('libraryStoragePath').textContent = '暂时无法读取，请检查原素材库服务。'; }
}
$('libraryStorageOpen').addEventListener('click', showLibraryStorage);
$('libraryStorageClose').addEventListener('click', () => $('libraryStorageDialog').close());
$('libraryStoragePlan').addEventListener('click', async () => {
  const button = $('libraryStoragePlan');
  ++storageRead;
  button.disabled = true;
  $('libraryStorageMessage').textContent = '正在检查目录…';
  try {
    const result = await postJson('/api/storage/library/plan', {target: $('libraryStorageTarget').value.trim()});
    $('libraryStorageTarget').value = result.target;
    $('libraryStoragePending').textContent = '已保存计划位置（尚未生效）：' + result.target;
    $('libraryStorageMessage').textContent = '目录可写、可用空间 ' + materialSize(result.available_bytes) + '。当前素材仍保存在原位置，切换前还需停机迁移并复核。';
  } catch (error) { $('libraryStorageMessage').textContent = error.message; }
  finally { button.disabled = false; }
});

async function observeFlowRequest(id, attempts = 0) {
  if (sessionStorage.getItem("flowRequest") !== id) return;
  try {
    const response = await fetch(`/api/real-pool/flow-requests/${id}`, {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("not available");
    const record = await response.json();
    showFlowRecord(record);
    if (record.status === "dispatching" && attempts < 10) setTimeout(() => observeFlowRequest(id, attempts + 1), 2000);
    else if (record.status === "dispatching") showFlowRecord({status: "unknown"});
    else if (record.status === "submitted") { refreshRecentTasks(); refresh(); }
  } catch (_) { showFlowRecord({status: "unknown"}); }
}

$("flowKind").addEventListener("change", renderFlowModels);
$("flowCount").addEventListener("change", syncFlowRetryOption);
$("flowProject").addEventListener("change", renderFlowModels);
$("flowSend").addEventListener("click", async () => {
  const project_key = $("flowProject").value;
  const kind = $("flowKind").value;
  const params = {prompt: $("flowPrompt").value, model: $("flowModel").value, aspect: $("flowAspect").value,
    count: Number($("flowCount").value), resolution: $("flowResolution").value,
    duration: Number($("flowDuration").value)};
  const styleId = $("flowStyleId").value;
  if (styleId !== "") params.style_id = styleId;
  if (!params.prompt.trim() || !$("flowConfirm").checked) {
    $("flowMessage").textContent = "请填写提示词并确认可能的积分消耗。"; return;
  }
  const id = crypto.randomUUID();
  const payload = {method: "flow.generate", kind, params, confirm: true};
  if (project_key && project_key !== "auto") payload.project_key = project_key;
  if (params.count === 1) payload.auto_retry = $("flowAutoRetry").checked;
  $("flowAutoRetry").disabled = true;
  sessionStorage.setItem("flowRequest", id);
  state.flowBusy = true;
  $("flowSend").disabled = true;
  $("flowMessage").textContent = "正在提交真实任务，请不要重复操作。";
  try {
    const response = await fetch("/api/real-pool/flow", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Workbench-Request": "1", "Idempotency-Key": id},
      body: JSON.stringify(payload)});
    const record = await response.json();
    if (response.status === 400 || response.status === 403) {
      sessionStorage.removeItem("flowRequest"); state.flowBusy = false;
      $("flowAutoRetry").disabled = true;
      $("flowMessage").textContent = record.error || "任务未提交，请检查填写内容。";
      renderFlowForm(); return;
    }
    if (!response.ok) throw new Error("outcome unknown");
    showFlowRecord(record);
    if (record.status === "dispatching") observeFlowRequest(id);
    else if (record.status === "submitted") { $("flowConfirm").checked = false; refreshRecentTasks(); await refresh(); }
  } catch (_) { await observeFlowRequest(id); }
});

$("flowNewRequest").addEventListener("click", () => {
  sessionStorage.removeItem("flowRequest"); state.flowBusy = false;
  $("flowAutoRetry").disabled = true;
  $("flowNewRequest").hidden = true;
  $("flowMessage").textContent = "已结束这条记录，可创建新的任务。";
  renderFlowForm();
});

let dispatchConcurrencySaving = false;
let dispatchConcurrencySequence = 0;
let dispatchConcurrencyMaximum = null;
function dispatchStateText(paused) { return state.channelStatus?.production_send_available ? '官方调用已接入 · 新对话按号池优先顺序分配，多轮固定账号，单条新文字对话确认超时后，最多用原账号重试三次。' : '官方调用暂不可用；消息不会自动改走旧通道。'; }
function updateDispatchState(paused) {
  state.dispatchPaused = typeof paused === 'boolean' ? paused : null;
  const detail = document.querySelector('[data-gemini-dispatch-state]');
  if (detail) detail.textContent = dispatchStateText(state.dispatchPaused);
}
function showDispatchConcurrency(data) {
  const occupied=flowOccupiedSlots(data);
  if (!Number.isInteger(data.maximum) || data.maximum < 1 || data.maximum > 5 || !Number.isInteger(occupied)) throw new Error('并发设置暂不可读取。');
  updateDispatchState(data.available === true ? data.paused : null);
  dispatchConcurrencyMaximum = data.maximum;
  $('dispatchConcurrency').value = String(data.maximum);
  $('dispatchConcurrency').disabled = false;
  const following=data.background_videos ? ` · 视频跟进 ${data.background_videos}` : '';
  $('dispatchConcurrencyStatus').textContent = data.paused ? '维护中' : `占用 ${occupied} / ${data.maximum}${following}`;
  $('dispatchConcurrencyStatus').title = 'Flow 确认接收视频后释放派发名额，成片继续在后台跟进。调低名额时，已有任务继续完成。';
}
async function refreshDispatchConcurrency() {
  if (dispatchConcurrencySaving || document.activeElement === $('dispatchConcurrency')) return;
  const sequence = ++dispatchConcurrencySequence;
  try {
    const data = await getJson('/api/dispatch/concurrency', {timeoutMs:8000});
    if (sequence !== dispatchConcurrencySequence || dispatchConcurrencySaving) return;
    showDispatchConcurrency(data);
  } catch (error) {
    if (sequence !== dispatchConcurrencySequence || dispatchConcurrencySaving) return;
    updateDispatchState(null);
    $('dispatchConcurrency').disabled = true;
    $('dispatchConcurrencyStatus').textContent = '并发设置暂不可读取';
  }
}
$('dispatchConcurrency').addEventListener('change', async () => {
  if (dispatchConcurrencySaving) return;
  dispatchConcurrencySaving = true;
  ++dispatchConcurrencySequence;
  const control = $('dispatchConcurrency');
  control.disabled = true;
  $('dispatchConcurrencyStatus').textContent = '正在保存…';
  try {
    showDispatchConcurrency(await postJson('/api/dispatch/concurrency', {maximum:Number(control.value)}));
    $('dispatchConcurrencyStatus').textContent += ' · 已保存';
  } catch (error) {
    if (dispatchConcurrencyMaximum !== null) control.value = String(dispatchConcurrencyMaximum);
    control.disabled = false;
    $('dispatchConcurrencyStatus').textContent = error.message || '保存状态未确认，请刷新核对。';
  } finally {
    dispatchConcurrencySaving = false;
  }
});
const geminiConcurrencyInput = $('geminiConcurrency');
const geminiConcurrencySave = $('geminiConcurrencySave');
const geminiConcurrencyStatus = $('geminiConcurrencyStatus');
const geminiConcurrencyMeter = $('geminiConcurrencyMeter');
const geminiConcurrencyUsageText = $('geminiConcurrencyUsageText');
let geminiConcurrencyMaximum = null;
let geminiConcurrencySequence = 0;
let geminiConcurrencySaving = false;
let geminiConcurrencyDirty = false;

function updateGeminiConcurrencyButton() {
  geminiConcurrencySave.disabled = geminiConcurrencySaving || geminiConcurrencyInput.disabled ||
    !geminiConcurrencyDirty || !geminiConcurrencyInput.validity.valid ||
    Number(geminiConcurrencyInput.value) === geminiConcurrencyMaximum;
}

function showGeminiConcurrencyUsage(data) {
  geminiConcurrencyMeter.classList.toggle('is-unknown', !data);
  const fill = geminiConcurrencyMeter.firstElementChild;
  if (!data) {
    geminiConcurrencyMeter.removeAttribute('aria-valuenow');
    geminiConcurrencyMeter.removeAttribute('aria-valuemax');
    geminiConcurrencyUsageText.textContent = '并发暂不可读取';
    fill.style.width = '0%';
    return;
  }
  const occupied = Math.min(data.active, data.maximum);
  const usage = (data.paused ? '维护中 · ' : '') + `占用 ${data.active} / ${data.maximum} · 排队 ${data.queued}`;
  geminiConcurrencyMeter.setAttribute('aria-valuenow', String(occupied));
  geminiConcurrencyMeter.setAttribute('aria-valuemax', String(data.maximum));
  if (geminiConcurrencyUsageText.textContent !== usage) geminiConcurrencyUsageText.textContent = usage;
  fill.style.width = `${occupied / data.maximum * 100}%`;
}

function showGeminiConcurrency(data, saved = false) {
  if (data.available !== true || !Number.isInteger(data.maximum) ||
      !Number.isInteger(data.ceiling) || data.maximum < 1 || data.maximum > data.ceiling ||
      !Number.isInteger(data.active) || data.active < 0 ||
      !Number.isInteger(data.queued) || data.queued < 0) throw new Error('Gemini 并发设置暂不可读取。');
  geminiConcurrencyMaximum = data.maximum;
  geminiConcurrencyInput.max = String(data.ceiling);
  geminiConcurrencyInput.disabled = false;
  if (!geminiConcurrencyDirty && (saved || document.activeElement !== geminiConcurrencyInput))
    geminiConcurrencyInput.value = String(data.maximum);
  showGeminiConcurrencyUsage(data);
  geminiConcurrencyStatus.textContent = saved ? '已保存' : '';
  updateGeminiConcurrencyButton();
}

async function refreshGeminiConcurrency() {
  if (!geminiConcurrencyInput || geminiConcurrencySaving) return;
  const sequence = ++geminiConcurrencySequence;
  try {
    const data = await getJson('/api/gemini/concurrency', {timeoutMs:8000});
    if (sequence === geminiConcurrencySequence && !geminiConcurrencySaving) showGeminiConcurrency(data);
  } catch (_) {
    if (sequence !== geminiConcurrencySequence || geminiConcurrencySaving) return;
    if (geminiConcurrencyMaximum === null) geminiConcurrencyInput.disabled = true;
    showGeminiConcurrencyUsage(null);
    if (geminiConcurrencyMaximum === null) geminiConcurrencyStatus.textContent = '设置暂不可读取';
    updateGeminiConcurrencyButton();
  }
}

geminiConcurrencyInput?.addEventListener('input', () => {
  geminiConcurrencyDirty = true;
  updateGeminiConcurrencyButton();
});

$('geminiConcurrencyForm')?.addEventListener('submit', async event => {
  event.preventDefault();
  if (geminiConcurrencySave.disabled) return;
  geminiConcurrencySaving = true;
  ++geminiConcurrencySequence;
  geminiConcurrencyInput.disabled = true;
  updateGeminiConcurrencyButton();
  geminiConcurrencyStatus.textContent = '正在保存…';
  try {
    const data = await postJson('/api/gemini/concurrency', {maximum:Number(geminiConcurrencyInput.value)});
    geminiConcurrencyDirty = false;
    showGeminiConcurrency(data, true);
  } catch (error) {
    geminiConcurrencyDirty = true;
    geminiConcurrencyStatus.textContent = error.message || '保存状态未确认，请稍后查看。';
  } finally {
    geminiConcurrencySaving = false;
    geminiConcurrencyInput.disabled = false;
    updateGeminiConcurrencyButton();
  }
});
$('overviewSchedule').addEventListener('click', () => {
  selectAccountPool('gemini');
  setView('accounts');
  if (activationSchedulePanel.hidden) $('activationScheduleToggle').click();
  $('activationScheduleToggle').focus({preventScroll:true});
  $('activationSchedulePanel').scrollIntoView({block:'nearest'});
});

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", (event) => {
  if (button.closest("#workBar")) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
  }
  setView(button.dataset.view);
}));
apiChapterButtons.forEach((button) => button.addEventListener('click', () => {
  history.replaceState(null, '', '/#' + button.dataset.apiTarget);
  focusApiSection(button.dataset.apiTarget);
}));
for (const button of document.querySelectorAll('[data-copy-code]')) {
  const label = button.textContent;
  let resetLabel;
  button.addEventListener('click', async () => {
    const source = $(button.dataset.copyCode);
    if (!source) return;
    const kind = source.id === 'apiBaseAddress' ? '地址' : '代码';
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(source.textContent);
      button.textContent = '已复制';
      $('apiCopyStatus').textContent = kind + '已复制。';
    } catch (_) {
      const selection = window.getSelection();
      if (selection) {
        const range = document.createRange();
        range.selectNodeContents(source);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      button.textContent = '请按 Ctrl+C';
      $('apiCopyStatus').textContent = '请手动复制' + kind + '。';
    }
    clearTimeout(resetLabel);
    resetLabel = setTimeout(() => { button.textContent = label; }, 3500);
  });
}
$("refresh").addEventListener("click", () => window.location.reload());
followWorkbenchReleases();
$("projectSearch").addEventListener("input", renderProjects);
$('projectTable').addEventListener('click', event => {
  const button = event.target.closest('button[data-project-action]');
  if (button && !button.disabled) startProjectAction(button.dataset.projectKey,button.dataset.projectAction);
});
$("taskWindowsHeading")?.closest("details")?.addEventListener("toggle", (event) => {
  if (event.currentTarget.open) {
    renderTaskWindows();
    renderSchedulerTaskRecords(recentTasks || state.snapshot);
  }
});
$("taskFiltersReset")?.addEventListener("click", () => {
  for (const id of ["taskSearch", "taskStatusFilter", "taskKindFilter"]) $(id).value = "";
  renderTasks();
  $("taskSearch").focus();
});
$("taskSearch").addEventListener("input", renderTasks);
$("taskStatusFilter").addEventListener("change", renderTasks);
$("taskKindFilter").addEventListener("change", renderTasks);
initApiDocumentation();
void getJson('/api/health', {timeoutMs:3000}).then(info => {
  const version = $('serverVersion');
  if (typeof info.source_version !== 'string' || !/^\d+(?:\.\d+){1,3}$/.test(info.source_version)) return;
  const commit = typeof info.source_commit === 'string' && /^[a-f0-9]{40}$/.test(info.source_commit) ? info.source_commit : null;
  version.textContent = 'v' + info.source_version;
  if (commit) version.title = 'Ubuntu · ' + commit;
  version.hidden = false;
}).catch(() => {});
setView(location.hash.slice(1) || "overview");
refresh();
const previousGeminiRequest = sessionStorage.getItem("geminiRequest");
if (previousGeminiRequest && /^[0-9a-f-]{36}$/.test(previousGeminiRequest)) {
  state.geminiBusy = true;
  $("geminiSend").disabled = true; // Legacy Gemini sender removed.
  observeGeminiRequest(previousGeminiRequest);
}
const previousFlowRequest = sessionStorage.getItem("flowRequest");
if (previousFlowRequest && /^[0-9a-f-]{36}$/.test(previousFlowRequest)) {
  state.flowBusy = true;
  $("flowAutoRetry").disabled = true;
  $("flowSend").disabled = true;
  observeFlowRequest(previousFlowRequest);
}
try {
  const previous = JSON.parse(sessionStorage.getItem('conversationRequest') || 'null');
  if (previous && /^[0-9a-f-]{36}$/.test(previous.id) && /^[0-9a-f-]{36}$/.test(previous.conversation)) {
    activeConversation = previous.conversation; chatSending = true;
    observeConversationRequest(previous.id, previous.conversation);
  }
} catch (_) { /* Ignore malformed local state. */ }



// Contextual explanations stay separate from changing status and action warnings.

(() => {

  let serial = 0;

  const tips = new Set();

  function positionTip(tip) {

    const button = tip.querySelector('.ui-help-button');

    const popup = tip.querySelector('.ui-help-popup');

    if (!button || !popup || !button.isConnected) return;

    const anchor = button.getBoundingClientRect();

    const box = popup.getBoundingClientRect();

    const left = Math.max(8, Math.min(anchor.left, window.innerWidth - box.width - 8));

    const below = anchor.bottom;

    const top = below + box.height <= window.innerHeight - 8 ? below : Math.max(8, anchor.top - box.height);

    popup.style.left = `${left}px`;

    popup.style.top = `${top}px`;

  }

  function closeTip(tip) {

    tip.dataset.open = 'false';

    const button = tip.querySelector('.ui-help-button');

    if (button) button.setAttribute('aria-expanded', 'false');

  }

  function attach(anchor, sources, label) {

    tips.forEach(tip => { if (!tip.isConnected) tips.delete(tip); });

    const eligible = sources.filter(source => source && !source.classList.contains('ui-help-source-hidden') && source.textContent.trim());

    if (!anchor || !eligible.length) return;

    const tip = document.createElement('span');

    tip.className = 'ui-help-tip';

    const button = document.createElement('button');

    const popup = document.createElement('span');

    const id = 'ui-help-' + (++serial);

    button.type = 'button';

    button.className = 'ui-help-button';

    button.setAttribute('aria-label', `查看${label}说明`);

    button.setAttribute('aria-expanded', 'false');

    button.setAttribute('aria-describedby', id);

    button.textContent = 'i';

    popup.id = id;

    popup.className = 'ui-help-popup';

    popup.setAttribute('role', 'tooltip');

    popup.textContent = eligible.map(source => {

      const content = source.classList.contains('notice') ? source.querySelector('span:last-child') : null;

      return (content || source).textContent.trim();

    }).join('\n');

    eligible.forEach(source => source.classList.add('ui-help-source-hidden'));

    tip.append(button, popup);

    anchor.append(tip);

    tips.add(tip);

    tip.addEventListener('mouseenter', () => positionTip(tip));

    button.addEventListener('focus', () => positionTip(tip));

    button.addEventListener('click', () => {

      const shouldOpen = tip.dataset.open !== 'true';

      tips.forEach(other => { if (other !== tip) closeTip(other); });

      tip.dataset.open = String(shouldOpen);

      button.setAttribute('aria-expanded', String(shouldOpen));

      positionTip(tip);

    });

  }

  const one = selector => document.querySelector(selector);

  function page(id, extra) {

    const heading = one('#' + id);

    const header = heading?.closest('.page-heading');

    const intro = header?.querySelector(':scope > div > p:not([id])');

    attach(heading, [intro, extra ? one(extra) : null], heading?.textContent.trim() || '页面');

  }

  page('overviewHeading');

  page('accountsHeading', '#accountsView > .notice');

  page('projectsHeading', '#projectsView > .notice');

  page('tasksHeading', '#tasksView > .notice');

  page('conversationsHeading', '#conversationsView > .notice');

  page('libraryHeading');

  page('apiHeading');

  attach(one('#realAccountsHeading'), [one('#accountsView section[aria-labelledby="realAccountsHeading"] > p.panel-caption:not([id])')], '通道状态');

  attach(one('#activationScheduleForm .activation-schedule-fields'), [one('#activationScheduleForm p.panel-caption:not([id])')], '定时检查');

  attach(one('#activationPollForm .activation-schedule-fields'), [one('#activationPollForm p.panel-caption:not([id])')], '轮询检查');

  attach(one('#dispatchPolicyHeading'), [one('#dispatchPolicyHeading + p.panel-caption')], '自动选号规则');

  attach(one('#geminiActivityHeading'), [one('#geminiActivityHeading + p.panel-caption')], '请求记录');

  attach(one('#libraryView .library-intro h2'), [one('#libraryView .library-intro p')], '素材库');

  document.addEventListener('pointerdown', event => {

    tips.forEach(tip => { if (!tip.contains(event.target)) closeTip(tip); });

  });

  document.addEventListener('keydown', event => {

    if (event.key === 'Escape' && document.activeElement?.classList.contains('ui-help-button')) {

      const button = document.activeElement;

      closeTip(button.parentElement);

      button.blur();

    }

  });

  window.addEventListener('resize', () => tips.forEach(tip => { if (tip.isConnected) positionTip(tip); }));

  window.addEventListener('scroll', () => tips.forEach(tip => { if (tip.isConnected && (tip.dataset.open === 'true' || tip.contains(document.activeElement))) positionTip(tip); }), true);

})();


// Keep the account summary and completed activation details in the existing help tip.
(() => {
  const summary = document.querySelector('#realAccountsSummary');
  const progress = document.querySelector('#activationProgress');
  const popup = document.querySelector('#realAccountsHeading .ui-help-popup');
  if (!summary || !progress || !popup) return;
  const explanation = popup.textContent.trim();
  function sync() {
    const current = progress.textContent.trim();
    summary.classList.remove('ui-account-summary-hidden');
    // Keep an active operation or an issue that may need attention directly visible.
    const needsAttention = /不可用|未确认|失败|待人工|正在创建|处理中\s*[1-9]/.test(current);
    progress.classList.toggle('ui-account-summary-hidden', !needsAttention);
    popup.textContent = [explanation, summary.textContent.trim(), current].filter(Boolean).join('\n');
  }
  const observer = new MutationObserver(sync);
  for (const node of [summary, progress]) observer.observe(node, {childList:true, characterData:true, subtree:true});
  sync();
})();

// Collect automatic-analysis explanations and routine counts in one help tip.
(() => {
  const section = document.querySelector('#libraryView .auto-analysis');
  const heading = section?.querySelector('strong');
  const explanation = section?.querySelector('small');
  const status = document.querySelector('#autoAnalysisStatus');
  const backfill = document.querySelector('#backfillStatus');
  const actions = section?.querySelector('.auto-analysis-actions');
  if (!heading || !explanation || !status || !backfill || !actions || section.querySelector('.library-analysis-help')) return;
  const tip = document.createElement('span');
  tip.className = 'ui-help-tip library-analysis-help';
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'ui-help-button'; button.textContent = 'i';
  button.setAttribute('aria-label', '查看素材自动分析说明');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-describedby', 'library-analysis-help');
  const popup = document.createElement('span');
  popup.id = 'library-analysis-help'; popup.className = 'ui-help-popup'; popup.setAttribute('role', 'tooltip');
  tip.append(button, popup); heading.append(tip);
  const cost = document.createElement('span');
  cost.className = 'library-analysis-cost'; cost.textContent = '可能产生费用';
  actions.prepend(cost);
  explanation.classList.add('ui-account-summary-hidden');
  function position() {
    const anchor = button.getBoundingClientRect(), box = popup.getBoundingClientRect();
    popup.style.left = Math.max(8, Math.min(anchor.left, innerWidth - box.width - 8)) + 'px';
    popup.style.top = (anchor.bottom + box.height <= innerHeight - 8 ? anchor.bottom : Math.max(8, anchor.top - box.height)) + 'px';
  }
  function sync() {
    const auto = status.textContent.trim(), fill = backfill.textContent.trim();
    const autoWarning = !/^(已开启|已关闭|正在读取设置)/.test(auto) || /需核对\s*[1-9]/.test(auto);
    const fillWarning = !!fill && (!/^补打：/.test(fill) || /已暂停|需核对\s*[1-9]/.test(fill));
    status.classList.toggle('ui-account-summary-hidden', !autoWarning);
    backfill.classList.toggle('ui-account-summary-hidden', !fillWarning);
    popup.textContent = [explanation.textContent.trim(), auto, fill].filter(Boolean).join('\n');
    if (tip.dataset.open === 'true' || tip.contains(document.activeElement)) position();
  }
  new MutationObserver(sync).observe(status, {childList:true, characterData:true, subtree:true});
  new MutationObserver(sync).observe(backfill, {childList:true, characterData:true, subtree:true});
  tip.addEventListener('mouseenter', position);
  button.addEventListener('focus', position);
  button.addEventListener('click', () => {
    const open = tip.dataset.open !== 'true'; tip.dataset.open = String(open);
    button.setAttribute('aria-expanded', String(open)); position();
  });
  document.addEventListener('pointerdown', event => {
    if (!tip.contains(event.target)) { tip.dataset.open = 'false'; button.setAttribute('aria-expanded', 'false'); }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.activeElement === button) {
      tip.dataset.open = 'false'; button.setAttribute('aria-expanded', 'false'); button.blur();
    }
  });
  window.addEventListener('resize', position);
  window.addEventListener('scroll', () => { if (tip.dataset.open === 'true' || tip.contains(document.activeElement)) position(); }, true);
  sync();
})();

// Keep unsupported legacy quick-send and attachment paths disabled.
for (const id of ['geminiSend','chatFiles','geminiFiles']) {
  const button=document.getElementById(id);
  if(button){button.disabled=true;button.title='请在对话管理使用官方文字调用；附件暂未启用';}
}

async function readRetainedSettings() {
  const target = $('retainedSettings');
  try {
    const [daily, poll] = await Promise.all([getJson('/api/account-activation/schedule'),getJson('/api/account-activation/poll')]);
    target.textContent = `旧设置已保留：每天定时 ${daily.enabled ? '开启（'+daily.time+'）' : '关闭'} · 轮询 ${poll.enabled ? '开启（'+poll.interval_minutes+' 分钟）' : '关闭'}。这些设置不驱动官方 CLI 调用。`;
  } catch (_) { target.textContent = '旧定时与轮询设置暂不可读取；官方通道未接入这些设置。'; }
}
readRetainedSettings();


// Official quota is a separate read-only source; request counts are never quotas.
function renderOfficialQuota() {
  const target = $('geminiQuotaOverview');
  if (!target) return;
  const opened = !!target.querySelector('details[open]');
  const focused = target.contains(document.activeElement) ? document.activeElement?.dataset.quotaAction : null;
  const data = officialQuotaReadFailed ? null : officialQuotaData;
  const saved = savedQuotaSummary(data);
  const known = saved.known_accounts;
  const total = data?.total_accounts ?? (state.snapshot?.real_accounts?.items || []).filter(account=>!account.gemini_removed).length;
  const refreshing = data?.refreshing === true;
  const number = value => value == null ? '—' : value.toFixed(2).replace(/\.00$/, '') + '%';
  const resetText = seconds => typeof seconds === 'number' ? `最近恢复 ${dateTime(seconds)}` : '恢复时间以官方状态为准';
  const taskKnown = state.snapshot?.flow_state?.available === true && Array.isArray(state.snapshot?.tasks);
  const tasks = taskKnown ? state.snapshot.tasks.filter(t=>['image','video'].includes(t.kind)) : [];
  const completed = tasks.filter(t=>t.status==='succeeded').length;
  const failed = tasks.filter(t=>t.status==='failed').length;
  const success = completed+failed ? Math.round(completed/(completed+failed)*100) : null;
  $('flowSuccessOverview').innerHTML = `<div class="quota-window-label"><span>FLOW · 图片 / 视频成功率</span><strong>${success == null ? '—' : success+'%'}<small> 任务成功率</small></strong></div><div class="quota-track ${success == null ? 'is-unknown' : ''}" ${success == null ? 'role="img" aria-label="Flow 任务成功率未知"' : `role="progressbar" aria-label="Flow 图片与视频任务成功率" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${success}"`}><i data-quota-fill="${success ?? 0}"></i></div><span class="quota-reset">${taskKnown ? `${completed} 成功 · ${failed} 失败 · 含任务下载阶段` : '任务记录暂未读取'}</span>`;
  const accounts = state.snapshot?.real_accounts?.items || [];
  const names = new Map(accounts.map(a=>[a.key.replace(/^flow-account-/,''),a.email || a.label || a.profile]));
  const rows = Object.entries(data?.accounts || {});
  const rowMarkup = rows.map(([key,row],index)=>{
    const fresh = row.status === 'ok' && Date.now()/1000-row.checked_at <= 600;
    const values = ['five_hour','weekly'].map(w=>savedQuotaValue(row,w));
    const resets = ['five_hour','weekly'].map(w=>row[w]?.reset_at).filter(t=>fresh && typeof t==='number' && t>Date.now()/1000);
    let status = '额度暂未获取';
    if (row.status==='verification_required') status = '额度查询提示核验（不限制对话）';
    else if (row.status==='authorization_required') status = '官方要求重新授权';
    else if (row.status==='not_read' && refreshing) status = '等待本轮读取';
    else if (values.every(value=>value!==null)) {
      status = fresh ? (resets.length ? resetText(Math.min(...resets)) : '已读取额度')
        : '上次读取 '+dateTime(row.checked_at)+'，仅供参考';
    } else if (values.some(value=>value!==null)) {
      status = fresh ? '部分额度待读取' : '上次读取 '+dateTime(row.checked_at)+'，仅供参考 · 部分额度待读取';
    }
    return `<tr><th scope="row">${esc(names.get(key) || `账号 ${index+1}`)}</th>${values.map((v,i)=>`<td><div class="quota-account-value">${number(v)}</div><div class="quota-account-track" role="img" aria-label="${i ? '每周' : '5小时'}剩余 ${v == null ? '未知' : v+'%'}"><i data-quota-fill="${v ?? 0}"></i></div></td>`).join('')}<td>${esc(status)}</td></tr>`;
  }).join('');
  const checked = rows.map(([,r])=>r.checked_at).filter(t=>typeof t==='number');
  const advanced = data?.advanced_pool;
  const advancedDispatch = state.channelStatus?.dispatch?.advanced_pool;
  const advancedRows = Object.entries(advanced?.accounts || {});
  const hydrogenRows = advancedRows.filter(([,row]) => row.models?.includes(hydrogenMedium));
  const now = Date.now()/1000;
  const sharedWindowMarkup = ['five_hour','weekly'].map(window=>{
    const values = hydrogenRows
      .filter(([,row])=>now-row.checked_at >= 0 && now-row.checked_at <= 600)
      .map(([,row])=>savedQuotaValue(row,window))
      .filter(value=>value!==null);
    const average = values.length ? Math.round(values.reduce((sum,value)=>sum+value,0)/values.length*100)/100 : null;
    const title = window === 'five_hour' ? '5 小时' : '每周';
    const meter = average === null ? `role="img" aria-label="${title}额度待读取"`
      : `role="progressbar" aria-label="${title}平均剩余额度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${average}"`;
    return `<div class="quota-shared-window"><span>${title}额度</span><strong>${average === null ? '待读取' : number(average)}${average === null ? '' : '<small>平均剩余</small>'}</strong><div class="quota-track${average === null ? ' is-unknown' : ''}" ${meter}><i data-quota-fill="${average ?? 0}"></i></div><em>${values.length} / ${hydrogenRows.length} 个账号已读</em></div>`;
  }).join('');
  const sharedTopMarkup = advanced ? `<article class="quota-shared-card"><div class="quota-shared-heading"><strong>HY 默认池额度</strong><span>Gemini Models 共享</span></div><div class="quota-shared-windows">${sharedWindowMarkup}</div><p>按已开通 HY 中档且近期读到额度的账号取平均。</p></article>` : '';
  const advancedQuotaMarkup = advancedRows.length ? `
    <div class="quota-table-wrap">
      <table aria-label="高级模型账号额度">
        <caption>高级模型 · ${advancedDispatch?.ready_accounts ?? '—'} / ${advancedRows.length} 个可派发 · 额度与该账号的 Gemini 模型共享</caption>
        <thead><tr><th>账号</th><th>5 小时剩余</th><th>每周剩余</th><th>状态</th></tr></thead>
        <tbody>${advancedRows.map(([key,row])=>{
          const account = state.channelStatus?.accounts?.find(item=>item.account_key===key);
          const status = row.status==='verification_required' || account?.block_reason==='verification_required' ? '等待 Google 验证'
            : account?.advanced_quota_restriction==='quota_exhausted' ? '等待额度恢复'
            : account?.advanced_quota_restriction==='authorization_required' ? '等待重新授权'
            : !row.models?.length ? '等待实测开通'
            : account?.dispatch_eligible===false ? '暂不可派发' : '已开通';
          return `<tr><th scope="row">${esc(names.get(key) || '已登记账号')}</th><td>${number(savedQuotaValue(row,'five_hour'))}</td><td>${number(savedQuotaValue(row,'weekly'))}</td><td>${status}</td></tr>`;
        }).join('')}</tbody>
      </table>
    </div>` : '';
  const coverage = refreshing ? `正在读取 ${known} / ${total} 个账号` : known ? `已保存 ${known} / ${total} 个账号${known<total ? ' · 汇总仅含已读账号' : ''}` : '额度暂未获取';
  target.setAttribute('aria-busy',String(refreshing));
  $('geminiSharedQuota').innerHTML = sharedTopMarkup;
  $('geminiQuotaBreakdown').innerHTML = `<div class="quota-footer"><div class="quota-source"><span>Gemini Models：${esc(coverage)}${checked.length ? ` · 更新于 ${esc(dateTime(Math.max(...checked)))}` : ''}</span></div><button type="button" class="quota-refresh" data-quota-action="refresh" ${refreshing || officialQuotaRequest ? 'disabled' : ''}>${refreshing ? '额度读取中…' : '刷新 Gemini 额度'}</button></div><details class="quota-details" ${opened ? 'open' : ''}><summary data-quota-action="details">账号与普通 Gemini 额度明细<span>查看账号剩余与恢复时间</span></summary>${advancedQuotaMarkup}<div class="quota-table-wrap"><table><thead><tr><th>账号</th><th>5 小时剩余</th><th>每周剩余</th><th>状态 / 最近恢复</th></tr></thead><tbody>${rowMarkup || `<tr><td colspan="4">${refreshing ? '正在读取额度…' : '额度暂未获取'}</td></tr>`}</tbody></table></div></details>`;
  target.querySelectorAll('[data-quota-fill]').forEach(meter=>{meter.style.width=Number(meter.dataset.quotaFill)+'%';});
  target.querySelector('[data-quota-action="refresh"]').addEventListener('click',()=>refreshOfficialQuota(true));
  if (focused) target.querySelector(`[data-quota-action="${focused}"]`)?.focus({preventScroll:true});
}
async function refreshOfficialQuota(force=false) {
  if (officialQuotaRequest) {
    if (!force || officialQuotaRequest.force) return officialQuotaRequest.pending;
    await officialQuotaRequest.pending;
    return refreshOfficialQuota(true);
  }
  officialQuotaReadAt = Date.now();
  const pending = Promise.resolve().then(async () => {
    try {
      let data;
      if (force) {
        const response = await fetch('/api/official-quota/refresh',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Workbench-Request':'1'},body:'{}',signal:AbortSignal.timeout(8000)});
        if (!response.ok) throw new Error('unavailable');
        data = await response.json();
      } else data = await getJson('/api/official-quota',{timeoutMs:8000});
      if (data.schema_version !== 1 || data.source !== 'official_cli_usage' || !data.accounts || !data.windows) throw new Error('unavailable');
      officialQuotaData = data;
      officialQuotaReadFailed = false;
      return data;
    } catch (_) {
      if (force || !officialQuotaData || officialQuotaData.refreshing) officialQuotaReadFailed = true;
      return null;
    } finally {
      officialQuotaRequest = null;
      renderOfficialQuota();
      renderAccountQuotas();
      if (state.snapshot && state.view==='accounts') renderAccounts();
      if (state.snapshot && state.view==='overview') renderAccountInsights(state.snapshot, $('overviewInsights'));
      else renderGovernance();
    }
  });
  officialQuotaRequest = {force,pending};
  return pending;
}
