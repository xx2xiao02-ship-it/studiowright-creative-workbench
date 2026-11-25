'use strict';
const $ = id => document.getElementById(id);
const labels = {queued:'排队中',running:'执行中',succeeded:'已完成',failed:'失败',interrupted:'已中断',cancelled:'已取消',verified:'登录已核对',unknown:'待检查',attention:'需检查'};
const kinds = {session_import:'登录信息导入',image:'图片生成',video:'视频生成',sync:'登录与项目检查',login:'打开登录窗口',media:'读取已有素材',doctor:'原版页面检查',quote:'视频报价检查'};
let data=null, view='overview', polling=false, refreshPromise=null, toastTimer, currentDetail=null, returnToDraft=false, draftRequest=null;
const syncRequests=new Set(), syncErrors=new Map();
let batchSubmitting=false;const batchChecks=new Map();
const esc = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = ts => ts ? new Date(ts*1000).toLocaleString('zh-CN',{hour12:false}) : '尚未检查';
const badge = status => `<span class="badge ${esc(status)}">${esc(labels[status]||status)}</span>`;
const accountName = profile => { const a=data.accounts.find(a=>a.profile===profile);return a?.email||a?.label||profile; };
const cooling = a => a.generation_state==='cooling_down' && a.cooldown_remaining>0;
const remainingTime = seconds => `${Math.floor(seconds/60)} 分 ${Math.ceil(seconds%60)} 秒`;
function generationStatus(a,compact=false){
 if(cooling(a))return compact?`<span class="badge cooling_down">冷却中 · 剩余 ${esc(remainingTime(a.cooldown_remaining))}</span>`:`<div class="account-cooldown" role="status"><strong>生成冷却中 · 剩余 ${esc(remainingTime(a.cooldown_remaining))}</strong><p>异常活动触发暂停，预计 ${esc(date(a.cooldown_until))} 解除。</p><small>到期自动解除冷却；失败任务不会自动重试，账号未停用。</small></div>`;
 if(a.generation_state==='unknown')return '<span class="minor">生成冷却状态暂未读到；不能据此判断账号可生成。</span>';
 return '';
}
function toast(message,error=false){$('toast').textContent=message;$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,6500);}
async function api(path,body){
 const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-Console-Request':'1'},body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin'});
 const result=await response.json();
 if(!response.ok){if(response.status===401)connected(false);throw new Error(result.error||'请求失败，请稍后重试。');}
 return result;
}
function connected(yes){$('webAuthSettings').hidden=true;$('logout').hidden=true;$('connectPanel').hidden=yes;$('app').hidden=!yes;$('connection').textContent=yes?'已连接 Ubuntu':'未连接';$('connection').className='connection'+(yes?' online':'');}
function switchView(next){if(view!==next)window.scrollTo({top:0,behavior:'instant'});view=next;if(next==='library')loadLibrary(1);const info={overview:['创作工作台','管理账号、选择项目，把创作交给 Ubuntu 执行。','工作台'],accounts:['账号与项目','核对各自的登录状态，选择确切的已有项目。','账号与项目'],tasks:['任务队列','排队、执行和结果状态，集中查看。','任务队列'],results:['生成结果','从任务回到原账号、原项目与下载文件。','生成结果'],library:['素材库','集中浏览、检索和下载已经保存的图片与视频。','素材库']}[next];$('pageTitle').textContent=info[0];$('pageSubtitle').textContent=info[1];$('breadcrumb').textContent=info[2];for(const name of ['overview','accounts','tasks','results','library'])$(name+'View').hidden=name!==next;document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===next);if(b.dataset.view===next)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});}
function empty(title,desc){return `<div class="empty"><b>${esc(title)}</b>${esc(desc||'')}</div>`;}
function renderSummary(){
 renderAccountInsights(data.account_insights);
 $('accountTotal').textContent=data.accounts.length;
 $('verifiedTotal').textContent=`${data.accounts.filter(a=>a.state==='verified').length} 个账号最近核对过在线登录 · ${data.accounts.filter(cooling).length} 个生成冷却中`;
 const queued=data.tasks.filter(t=>t.status==='queued').length,running=data.tasks.filter(t=>t.status==='running').length;
 $('pendingTotal').innerHTML=`${queued} <em>/ ${running}</em>`;$('queueCount').textContent=queued+running;
 $('doneTotal').textContent=data.tasks.filter(t=>['image','video'].includes(t.kind)&&t.status==='succeeded').length;
 $('failedTotal').textContent=data.tasks.filter(t=>['failed','interrupted'].includes(t.status)).length;
 $('accountSummary').innerHTML=[...data.accounts].sort((a,b)=>Number(cooling(b))-Number(cooling(a))).slice(0,4).map(a=>`<div class="summary-row"><span class="avatar">${esc(a.profile.slice(-2).toUpperCase())}</span><div class="summary-info"><b>${esc(a.email||a.label)}</b><small>${esc(a.profile)} · ${esc(date(a.checked))}</small>${generationStatus(a,true)}</div>${badge(a.state)}</div>`).join('')||empty('先登记一个账号','登录后即可读取已有项目');
 $('recentTasks').innerHTML=taskTable(data.tasks.slice(0,5));
}
function accountCheck(a){
 const rows=data.tasks.filter(t=>t.profile===a.profile&&['sync','session_import'].includes(t.kind)).sort((x,y)=>y.created-x.created);
 const task=rows.find(t=>['queued','running'].includes(t.status))||rows[0];
 const submitting=syncRequests.has(a.profile), pending=submitting||task&&['queued','running'].includes(task.status);
 let title='尚未检查',text='点击下方按钮，检查 Ubuntu 浏览器的实际登录和已有项目。',status='unknown';
 if(submitting){title='正在提交检查…';text='等待后台接收请求，请勿重复点击。';status='queued';}
 else if(task){status=task.status;if(status==='queued'){title='已排队，等待检查';text='任务已接收，轮到此账号后会自动检查。';}else if(status==='running'){title='正在检查登录与项目…';text='Ubuntu 正在核对在线账号和读取已有项目；结果会自动显示。';}else if(status==='succeeded'){title='检查成功';text=task.kind==='session_import'?(task.result?.message||'登录信息已导入，并已核对账号身份。'):('已核对 '+(task.result?.email||a.email||'当前账号')+'，本次读到 '+(task.result?.projects?.length||0)+' 个项目。');}else{title=status==='failed'?'检查失败':labels[status]||status;text=task.error||'本次检查没有完成，可以查看详情后重新检查。';}}
 else if(a.error){title='上次检查未通过';text=a.error;status='failed';}
 if(syncErrors.has(a.profile)){title='检查请求未确认';text=syncErrors.get(a.profile);status='failed';}
 return {pending,html:`<div class="account-check ${esc(status)}" role="status" aria-live="polite"><strong>${esc(title)}</strong><p>${esc(text)}</p>${task?`<small>${esc(date(task.finished||task.started||task.created))}</small> <button class="text-button" data-detail="${esc(task.id)}">查看本次详情</button>`:''}</div>`};
}
function renderBatchChecks(){
 const rows=[...batchChecks.entries()];let pending=0,passed=0,failed=0,cold=0;
 for(const [profile,id] of rows){const t=id&&data.tasks.find(t=>t.id===id);if(!id){failed++;continue;}if(!t||['queued','running'].includes(t.status)){pending++;continue;}if(t.status==='succeeded'){passed++;if(cooling(data.accounts.find(a=>a.profile===profile)||{}))cold++;}else failed++;}
 $('checkAllAccounts').disabled=batchSubmitting||pending>0||!data.accounts.length;
 $('checkAllAccounts').textContent=batchSubmitting?'正在提交检测…':pending?'检测中 · 剩余 '+pending+' 个':'一键检测全部账号';
 $('batchCheckStatus').textContent=rows.length?`本轮 ${rows.length} 个账号：登录与项目检查通过 ${passed} 个（其中冷却 ${cold} 个），未通过 ${failed} 个，等待或检查中 ${pending} 个。逐账号原因见下方；余额另行只读刷新，检查通过不保证生成成功。`:'只检查实际登录、已有项目和冷却状态，同时刷新余额；不创建项目、不生成、不扣积分。';
}
async function checkAllAccounts(){
 if(batchSubmitting||$('checkAllAccounts').disabled)return;
 const accounts=[...data.accounts];batchSubmitting=true;batchChecks.clear();renderBatchChecks();
 try{for(const a of accounts){let existing=data.tasks.find(t=>t.profile===a.profile&&['sync','session_import'].includes(t.kind)&&['queued','running'].includes(t.status));if(existing){batchChecks.set(a.profile,existing.id);continue;}
 syncRequests.add(a.profile);syncErrors.delete(a.profile);renderAccounts();
 try{const task=await submit('sync',a.profile);if(refreshPromise)await refreshPromise;if(!data.tasks.some(t=>t.id===task.id))data.tasks.unshift(task);batchChecks.set(a.profile,task.id);}catch(e){batchChecks.set(a.profile,null);syncErrors.set(a.profile,e.message);}finally{syncRequests.delete(a.profile);renderAccounts();}}
 try{await api('/api/accounts/credits/refresh',{});}catch(e){toast('登录检测已提交；余额刷新未确认：'+e.message,true);}
 renderTasks();toast('全部账号检测已提交，结果自动显示；未触发生成。');
 }finally{batchSubmitting=false;renderAccounts();}
}
function renderAccounts(){
 renderBatchChecks();
 $('accountsGrid').innerHTML=data.accounts.map(a=>{const check=accountCheck(a);return `<article class="account-card"><header><span class="avatar">${esc(a.profile.slice(-2).toUpperCase())}</span><b>${esc(a.label)}</b>${badge(a.state)}</header><h3>${esc(a.email||'尚未识别在线邮箱')}</h3><p class="minor">${esc(a.profile)} · ${esc(date(a.checked))}</p>${generationStatus(a)}${check.html}<div class="account-actions"><button class="secondary" data-remote="${esc(a.profile)}" ${check.pending?'disabled':''}>远程登录</button><button class="primary" data-sync="${esc(a.profile)}" ${check.pending?'disabled':''}>${check.pending?'检查中…':'检查登录与项目'}</button></div></article>`;}).join('')||empty('还没有账号','点击右上方「登记账号」开始');
 const filter=$('projectFilter').value;
 $('projectFilter').innerHTML='<option value="">全部账号</option>'+data.accounts.map(a=>`<option value="${esc(a.profile)}">${esc(a.email||a.label)}</option>`).join('');$('projectFilter').value=filter;
 renderProjects();
}
function renderProjects(){
 const rows=data.projects.filter(p=>!$('projectFilter').value||p.profile===$('projectFilter').value);
 $('projectsTable').innerHTML=rows.length?`<div class="table-wrap"><table><thead><tr><th>项目 / 完整 ID</th><th>所属账号</th><th>最近读取</th><th>操作</th></tr></thead><tbody>${rows.map(p=>`<tr><td><b>${esc(p.title)}</b><span class="minor mono">${esc(p.id)}</span></td><td>${esc(accountName(p.profile))}</td><td>${esc(date(p.checked))}</td><td><button class="text-button" data-media="${esc(p.id)}" data-profile="${esc(p.profile)}">查看素材</button><br><button class="text-button" data-create="${esc(p.id)}" data-profile="${esc(p.profile)}">在此项目新建任务</button></td></tr>`).join('')}</tbody></table></div>`:empty('尚未读取项目','点击账号上的「检查登录与项目」，或登记已知的完整项目 ID');
}
function taskTable(rows){return rows.length?`<div class="table-wrap"><table><thead><tr><th>任务</th><th>执行账号</th><th>状态</th><th>提交时间</th><th>操作</th></tr></thead><tbody>${rows.map(t=>`<tr><td><span class="task-title">${esc(kinds[t.kind])}</span><span class="minor">${esc((t.params.prompt||t.id).slice(0,55))}</span></td><td>${esc(accountName(t.profile))}${t.project?`<span class="minor mono">${esc(t.project)}</span>`:''}</td><td>${badge(t.status)}${t.error?'<span class="minor">查看具体原因</span>':''}</td><td>${esc(date(t.created))}</td><td><button class="text-button" data-detail="${t.id}">详情</button>${t.status==='queued'?`<br><button class="text-button" data-cancel="${t.id}">取消排队</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:empty('这里还没有任务','检查登录与项目也会留下任务记录，生成操作需要你确认提交');}
function renderTasks(){const filter=$('taskFilter').value;$('allTaskRows').innerHTML=taskTable(data.tasks.filter(t=>!filter||t.status===filter));}
function renderResults(){const rows=data.tasks.filter(t=>['image','video'].includes(t.kind)&&!['queued','running'].includes(t.status));$('resultGrid').innerHTML=rows.map(t=>`<article class="result-card">${badge(t.status)}<h3>${esc((t.params.prompt||'').slice(0,70))}</h3><p class="minor">${esc(kinds[t.kind])} · ${esc(accountName(t.profile))}</p><p class="mono">${esc(t.project)}</p><p class="minor">${esc(date(t.finished))}</p>${t.downloads.length?t.downloads.map(f=>`<a class="primary" href="${esc(f.url)}" download>${esc(f.name)}</a>`).join(''):`<p class="hint">${t.status==='succeeded'?'暂无可下载文件，请查看详情。':'未确认完成，请查看详情；不会自动重新生成。'}</p>`}<br><button class="secondary" data-detail="${t.id}">任务详情</button></article>`).join('')||empty('还没有生成结果','提交生成任务后，在这里查看来源和下载文件');}
function detail(ident){currentDetail=ident;const t=data.tasks.find(t=>t.id===ident);if(!t)return;$('detailTitle').textContent=kinds[t.kind]+' · '+(labels[t.status]||t.status);$('detailContent').innerHTML=`<dl class="detail-grid"><dt>执行账号</dt><dd>${esc(accountName(t.profile))}</dd><dt>完整项目 ID</dt><dd class="mono">${esc(t.project||'此操作不指定项目')}</dd><dt>任务编号</dt><dd class="mono">${esc(t.id)}</dd><dt>提交时间</dt><dd>${esc(date(t.created))}</dd><dt>完成时间</dt><dd>${esc(t.finished?date(t.finished):'尚未完成')}</dd></dl>${t.error?`<div class="notice warning">${esc(t.error)}</div>`:''}${t.result?.message?`<div class="notice">${esc(t.result.message)}</div>`:''}${t.kind==='quote'&&t.result?`<div class="notice">${t.result.credits===null?'原项目未能识别报价；网页会阻止提交视频。':'本次视频报价：'+esc(t.result.credits)+' 积分。'} 此操作未提交生成。</div>`:''}${t.result?.media?`<p class="hint">已读取 ${esc(t.result.total)} 条素材记录，不包含文件下载。</p>`:''}<div class="detail-downloads">${t.downloads.map(f=>`<a href="${esc(f.url)}" download>${esc(f.name)}</a>`).join('')}</div>${t.params.prompt?`<h3>提示词</h3><div class="detail-data">${esc(t.params.prompt)}</div>`:''}${t.result?`<h3>原任务结果与素材记录</h3><pre class="detail-data">${esc(JSON.stringify(t.result,null,2))}</pre>`:''}`;if(!$('detailDialog').open)$('detailDialog').showModal();}
async function refresh(silent=false){
 if(refreshPromise)return refreshPromise;
 polling=true;
 refreshPromise=(async()=>{try{const prev=data;data=await api('/api/state');connected(true);$('testBanner').hidden=!data.test;$('sourceVersion').textContent=data.source_version;const accountsChanged=!prev||JSON.stringify(prev.accounts)!==JSON.stringify(data.accounts);const projectsChanged=!prev||JSON.stringify(prev.projects)!==JSON.stringify(data.projects);const tasksChanged=!prev||JSON.stringify(prev.tasks)!==JSON.stringify(data.tasks);if(accountsChanged||tasksChanged)renderSummary();if(accountsChanged||projectsChanged||tasksChanged)renderAccounts();if(tasksChanged){renderTasks();renderResults();if(currentDetail&&$('detailDialog').open)detail(currentDetail);}if(prev){for(const t of data.tasks){const old=prev.tasks.find(o=>o.id===t.id);if(old&&old.status!==t.status&&['succeeded','failed','interrupted'].includes(t.status))toast(`${kinds[t.kind]}：${labels[t.status]}${t.error?'，'+t.error:''}`,t.status!=='succeeded');}}if(!silent)toast('列表已更新；不会触发生成或重复任务。');}catch(e){if(!silent)toast(e.message,true);else{$('connection').textContent='连接暂时中断';$('connection').className='connection';}}})();
 try{return await refreshPromise;}finally{refreshPromise=null;polling=false;}
}
async function checkAccount(profile){
 if(syncRequests.has(profile))return;
 const pending=data.tasks.find(t=>t.profile===profile&&t.kind==='sync'&&['queued','running'].includes(t.status));
 if(pending){detail(pending.id);return;}
 syncRequests.add(profile);syncErrors.delete(profile);renderAccounts();
 try{
  const t=await submit('sync',profile);
  // Await any older polling request before merging the POST response. Its older
  // snapshot must not hide the just-submitted task or prevent the dialog opening.
  if(refreshPromise)await refreshPromise;
  if(!data.tasks.some(row=>row.id===t.id))data.tasks.unshift(t);
  renderSummary();renderTasks();detail(t.id);toast('检查已提交，进度和结果会自动显示在该账号下方。');
 }catch(e){syncErrors.set(profile,e.message);toast(e.message,true);}
 finally{syncRequests.delete(profile);renderAccounts();}
}
async function submit(kind,profile,project=null,params={},confirm=false,key=crypto.randomUUID()) {return await api('/api/tasks',{kind,profile,project,params,confirm_generation:confirm,request_key:key});}
function setOptions(el,options,selected){el.innerHTML=options.map(o=>`<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('');if(selected&&options.some(o=>o.id===selected))el.value=selected;}
function projectOptions(selected){const profile=$('taskAccount').value;const projects=data.projects.filter(p=>p.profile===profile);setOptions($('taskProject'),projects.map(p=>({id:p.id,label:p.title+' · '+p.id})),selected);if(!projects.length)$('taskProject').innerHTML='<option value="">此账号尚无已登记项目</option>';projectId();}
function projectId(){$('selectedProjectId').textContent=$('taskProject').value?'完整项目 ID：'+$('taskProject').value:'';}
function modelOptions(){const kind=$('taskKind').value;setOptions($('taskModel'),data.models[kind]);setOptions($('taskAspect'),(kind==='image'?['16:9','4:3','1:1','3:4','9:16']:['16:9','9:16']).map(id=>({id,label:id})));$('budgetLabel').hidden=kind!=='video';$('quote').hidden=kind!=='video';$('confirmGeneration').checked=false;modelFields();}
function modelFields(){const omni=$('taskKind').value==='video'&&$('taskModel').value==='omni-flash';$('durationLabel').hidden=!omni;$('resolutionLabel').hidden=!omni;}
function openTask(profile,project){draftRequest=null;$('taskForm').reset();$('taskError').textContent='';setOptions($('taskAccount'),data.accounts.filter(a=>a.state==='verified').map(a=>({id:a.profile,label:a.email||a.label})),profile);if(!$('taskAccount').options.length){toast('请先检查账号的在线登录与项目。',true);switchView('accounts');return;}projectOptions(project);modelOptions();$('taskDialog').showModal();}
function formParams(){return{prompt:$('taskPrompt').value,model:$('taskModel').value,aspect:$('taskAspect').value,count:Number($('taskCount').value),duration:Number($('taskDuration').value),resolution:$('taskResolution').value,max_credits:Number($('taskBudget').value)};}
async function withButton(button,action){const text=button.textContent;button.disabled=true;button.textContent='正在处理…';try{return await action();}finally{button.disabled=false;button.textContent=text;}}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>switchView(b.dataset.view)));
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>{const dialog=b.closest('dialog');if(dialog.id==='materialDialog'&&(materialSaving||materialDirty&&!confirm('标签尚未保存，确定关闭并放弃修改吗？')))return;dialog.close();}));
$('refresh').onclick=()=>withButton($('refresh'),async()=>{await refresh();if(view==='library')await loadLibrary(libraryPage);});$('newTask').onclick=()=>openTask();$('goAccounts').onclick=$('allAccounts').onclick=()=>switchView('accounts');$('allTasks').onclick=()=>switchView('tasks');$('taskFilter').onchange=renderTasks;$('projectFilter').onchange=renderProjects;
$('taskAccount').onchange=()=>projectOptions();$('taskProject').onchange=projectId;$('taskKind').onchange=modelOptions;$('taskModel').onchange=modelFields;
$('checkAllAccounts').onclick=checkAllAccounts;
$('addAccount').onclick=()=>{$('accountForm').reset();$('accountError').textContent='';$('accountDialog').showModal();};
$('registerProject').onclick=()=>{if(!data.accounts.length){toast('请先登记账号。',true);return;}$('projectForm').reset();$('projectError').textContent='';setOptions($('projectAccount'),data.accounts.map(a=>({id:a.profile,label:a.email||a.label})));$('projectDialog').showModal();};
$('accountForm').onsubmit=async e=>{e.preventDefault();const profile=$('profileName').value;const tab=window.open('/remote','_blank');let registered=false;await withButton(e.submitter,async()=>{try{await api('/api/accounts',{profile,label:$('profileLabel').value});registered=true;const result=await api('/api/accounts/'+encodeURIComponent(profile)+'/remote',{});if(tab)tab.location.href=result.url;else location.href=result.url;$('accountDialog').close();await refresh(true);toast('登录窗口已打开，请完成 Google 登录；登录资料保存在该账号独立环境中。');}catch(err){tab?.close();if(registered){$('accountDialog').close();await refresh(true);toast('账号已保存，登录窗口未打开：'+err.message+'。请点击该账号的「远程登录」重试。',true);}else $('accountError').textContent=err.message;}});};
$('projectForm').onsubmit=async e=>{e.preventDefault();await withButton(e.submitter,async()=>{try{await api('/api/projects',{profile:$('projectAccount').value,id:$('projectId').value.trim().toLowerCase(),title:$('projectTitle').value||'已登记项目'});$('projectDialog').close();await refresh(true);toast('项目关联已保存；未创建云端项目。');}catch(e){$('projectError').textContent=e.message;}});};
$('taskForm').onsubmit=async e=>{e.preventDefault();await withButton($('submitTask'),async()=>{try{const snapshot=JSON.stringify([$ ('taskKind').value,$('taskAccount').value,$('taskProject').value,formParams()]);if(!draftRequest||draftRequest.snapshot!==snapshot)draftRequest={snapshot,key:crypto.randomUUID()};const t=await submit($('taskKind').value,$('taskAccount').value,$('taskProject').value,formParams(),$('confirmGeneration').checked,draftRequest.key);$('taskDialog').close();switchView('tasks');await refresh(true);detail(t.id);toast('任务已加入队列。');}catch(e){$('taskError').textContent=e.message+' 如提交结果不明，请先查看任务队列；保持参数不变再次提交会复用同一编号。';}});};
$('quote').onclick=()=>withButton($('quote'),async()=>{try{const t=await submit('quote',$('taskAccount').value,$('taskProject').value,formParams());$('taskDialog').close();switchView('tasks');await refresh(true);detail(t.id);returnToDraft=true;toast('报价检查已排队；关闭详情可返回填写中的任务。');}catch(e){$('taskError').textContent=e.message;}});
$('detailDialog').addEventListener('close',()=>{if(returnToDraft){returnToDraft=false;$('taskDialog').showModal();}});
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{if(b.dataset.remote){const tab=window.open('/remote','_blank');try{const result=await withButton(b,()=>api('/api/accounts/'+encodeURIComponent(b.dataset.remote)+'/remote',{}));if(tab)tab.location.href=result.url;else location.href=result.url;toast('远程登录窗口已打开。');}catch(err){tab?.close();throw err;}}else if(b.dataset.detail)detail(b.dataset.detail);else if(b.dataset.create)openTask(b.dataset.profile,b.dataset.create);else if(b.dataset.sync){await checkAccount(b.dataset.sync);}else if(b.dataset.login){const t=await withButton(b,()=>submit('login',b.dataset.login));await refresh(true);detail(t.id);}else if(b.dataset.media){await withButton(b,async()=>{const t=await submit('media',b.dataset.profile,b.dataset.media);await refresh(true);detail(t.id);});}else if(b.dataset.cancel){await api(`/api/tasks/${b.dataset.cancel}/cancel`,{});await refresh(true);toast('已取消排队，未执行生成。');}}catch(e){toast(e.message,true);}});
$('logout').onclick=async()=>{try{await api('/api/logout',{});connected(false);toast('已退出后台，不会退出 Google 账号。');}catch(e){toast(e.message,true);}};
async function loadWebAuthStatus(){try{const status=await api('/api/auth/status');$('webLoginForm').hidden=false;$('loginHint').textContent=status.configured?'使用后台账号密码登录；与 Google 账号无关。':'后台尚未保存任何账号密码。请先在已授权页面完成首次设置；保存后，所有浏览器都使用下方同一组账号密码登录。';return status.configured;}catch(e){$('connectError').textContent=e.message;}}
$('webLoginForm').onsubmit=async e=>{e.preventDefault();await withButton(e.submitter,async()=>{try{await api('/api/auth/login',{username:$('webLoginUsername').value.trim(),password:$('webLoginPassword').value,remember:$('webLoginRemember').checked});$('webLoginPassword').value='';$('connectError').textContent='';await refresh(true);}catch(e){$('connectError').textContent=e.message;}});};
$('webAuthSettings').onclick=async()=>{const configured=await loadWebAuthStatus();$('webAuthForm').reset();$('webAuthError').textContent='';$('webAuthCurrentLabel').hidden=!configured;$('webAuthCurrent').required=!!configured;$('webAuthTitle').textContent=configured?'修改后台账号密码':'首次设置后台账号密码';$('webAuthDialog').showModal();};
$('webAuthForm').onsubmit=async e=>{e.preventDefault();if($('webAuthPassword').value!==$('webAuthConfirm').value){$('webAuthError').textContent='两次新密码不一致。';return;}await withButton(e.submitter,async()=>{try{await api('/api/auth/settings',{username:$('webAuthUsername').value.trim(),password:$('webAuthPassword').value,current_password:$('webAuthCurrent').value});$('webAuthForm').reset();$('webAuthDialog').close();await loadWebAuthStatus();toast('后台登录已保存，其他浏览器现在可用账号密码登录。');}catch(e){$('webAuthError').textContent=e.message;}});};
async function start(){
 const params=new URLSearchParams(location.hash.slice(1));
 if(params.has('chrome-job')){
  document.title='导入 Flow 账号 · StudioWright';
  $('chromeImport').hidden=false;
  await runChromeJob(params.get('chrome-job'));
  return;
 }
 for(const id of ['legacySkip','legacySidebar','legacyWorkspace'])$(id).hidden=false;
 if(location.hash.startsWith('#connect='))history.replaceState(null,'',location.pathname);
 await refresh(true);
 if(!data){connected(false);$('webLoginForm').hidden=true;$('loginHint').textContent='后台连接暂未恢复，请稍后刷新页面，无需账号密码。';}
 setInterval(()=>{if(!document.hidden)refresh(true);},3000);
}
start();
let libraryPage=1, libraryRequest=0, selectedMaterial=null;
const fileSize = n => n>=1024**3?(n/1024**3).toFixed(1)+' GB':n>=1024**2?(n/1024**2).toFixed(1)+' MB':Math.ceil(n/1024)+' KB';
function sourceLabel(o){return o.source==='studiowright'?'StudioWright':o.source==='flow2api-windows'?'Flow2API · Windows':'Flow2API · Ubuntu';}
async function loadLibrary(page=1){
 const request=++libraryRequest;
 $('libraryStatus').textContent='正在读取素材…';
 const params=libraryFilters();params.set('page',String(page));
 try{const lib=await api('/api/library?'+params);if(request!==libraryRequest)return;libraryPage=lib.page;updateDimensionFilters(lib);
 $('libraryStats').innerHTML=`<b>${(lib.stats.image||0)+(lib.stats.video||0)}<small>份已保存素材</small></b><span>${lib.stats.image||0} 张图片 · ${lib.stats.video||0} 段视频<small>${esc(fileSize(lib.stats.bytes||0))} · 本地持久保存</small></span>`;
 const account=$('libraryAccount').value,tag=$('libraryTag').value;
 $('libraryAccount').innerHTML='<option value="">全部账号</option>'+lib.accounts.map(a=>`<option value="${esc(a)}">${esc(a)}</option>`).join('');$('libraryAccount').value=account;
 $('libraryTag').innerHTML='<option value="">全部标签</option>'+lib.tags.map(t=>`<option value="${esc(t)}">${esc(t)}</option>`).join('');$('libraryTag').value=tag;
 $('libraryStatus').textContent=`找到 ${lib.total} 份素材`+(lib.pending?` · ${lib.pending} 份等待归档${lib.errors?'，其中 '+lib.errors+' 份保存失败，正在重试':''}`:'')+'。旧库保留；此处不会创建或删除云端项目。';
 $('libraryGrid').innerHTML=lib.items.map(item=>{const o=item.origins[0];return `<article class="library-card"><button class="library-cover" data-material="${esc(item.id)}" aria-label="查看${item.media_type==='video'?'视频':'图片'}：${esc((o.prompt||'无提示词').slice(0,60))}">${item.media_type==='image'?`<img loading="lazy" src="${esc(item.thumbnail)}" alt="${esc((o.prompt||'历史图片').slice(0,80))}">`:`<video preload="metadata" muted src="${esc(item.url)}#t=0.1" aria-hidden="true"></video><span class="play-symbol" aria-hidden="true">▶</span>`}<span class="media-chip">${item.media_type==='video'?'视频':'图片'} · ${esc(fileSize(item.size))}</span></button><div class="library-card-body"><h3>${esc(o.prompt||'原记录未保存提示词')}</h3><p class="minor">${esc(o.account_email||sourceLabel(o)+' / '+o.account_id||'原账号未知')}</p><p class="library-origin">${esc(sourceLabel(o))}${item.origins.length>1?' · '+item.origins.length+' 条来源记录':''}</p><div class="library-tags">${materialTagBadges(item)}</div><div class="library-card-actions"><button class="text-button" data-material="${esc(item.id)}">预览与来源 →</button><a href="${esc(item.url)}?download=1" download>下载</a></div></div></article>`;}).join('')||empty('没有找到素材','换个关键词或筛选条件试试。新生成并下载的文件会自动保存到这里。');
 $('libraryPage').textContent=`第 ${lib.page} / ${lib.pages} 页`;$('libraryPrev').disabled=lib.page<=1;$('libraryNext').disabled=lib.page>=lib.pages;
 }catch(e){if(request===libraryRequest)$('libraryStatus').textContent='读取失败：'+e.message;}
}
async function openMaterial(ident){
 if(materialSaving)return;if(materialDirty&&!confirm('标签尚未保存，确定放弃修改吗？'))return;const request=++materialOpenRequest;
 try{const item=await api('/api/library/'+ident);if(request!==materialOpenRequest)return;selectedMaterial=item;fillMaterialDimensions(item);$('materialTitle').textContent=item.media_type==='video'?'视频素材':'图片素材';
 $('materialPreview').innerHTML=item.media_type==='video'?`<video controls playsinline preload="metadata" src="${esc(item.url)}"></video>`:`<img src="${esc(item.url)}" alt="原图预览">`;
 $('materialInfo').innerHTML=`<p class="hint">${esc(item.mime)} · ${esc(fileSize(item.size))}${item.width?' · '+esc(item.width)+' × '+esc(item.height):''}</p>`+item.origins.map(o=>`<section class="material-origin"><h3>${esc(sourceLabel(o))}</h3><dl class="detail-grid"><dt>原账号</dt><dd>${esc(o.account_email||'原记录未保存邮箱')}<small>${esc(o.account_id||'账号编号未知')}</small></dd><dt>完整项目 ID</dt><dd class="mono">${esc(o.project_id||'原记录未保存')}</dd><dt>原库记录编号</dt><dd class="mono">${esc(o.source_id)}</dd><dt>原素材编号</dt><dd class="mono">${esc(o.media_id||'原记录未保存')}</dd><dt>原任务编号</dt><dd class="mono">${esc(o.request_id||'原记录未保存')}</dd><dt>生成模型</dt><dd>${esc(o.model||'原记录未保存')}</dd><dt>原始时间</dt><dd>${esc(o.created||'原记录未保存')}</dd></dl><h4>提示词</h4><div class="material-prompt">${esc(o.prompt||'原记录未保存')}</div>${o.details.prompt_may_be_truncated?'<p class="hint">原历史记录的提示词可能不完整。</p>':''}</section>`).join('');
 $('materialTags').value=item.tags.join('，');$('materialDownload').href=item.url+'?download=1';$('materialTagStatus').textContent='标签保存在新素材库，不修改原库。';$('materialDialog').showModal();
 }catch(e){toast(e.message,true);}
}
$('librarySearch').onsubmit=e=>{e.preventDefault();loadLibrary(1);};
for(const id of ['libraryKind','libraryAccount','libraryTag'])$(id).onchange=()=>loadLibrary(1);
$('libraryPrev').onclick=()=>loadLibrary(libraryPage-1);$('libraryNext').onclick=()=>loadLibrary(libraryPage+1);
document.addEventListener('click',e=>{const b=e.target.closest('[data-material]');if(b)openMaterial(b.dataset.material);});
$('materialDialog').addEventListener('close',()=>{$('materialPreview').innerHTML='';selectedMaterial=null;materialOpenRequest++;materialDirty=false;});
$('materialTagForm').onsubmit=async e=>{e.preventDefault();if(!selectedMaterial||materialSaving)return;const ident=selectedMaterial.id;const tags=splitMaterialTags($('materialTags').value),structured_tags=Object.fromEntries(Object.keys(materialDimensions).map(d=>[d,splitMaterialTags($('edit-'+d).value)]));const snapshot=JSON.stringify({tags,structured_tags});materialSaving=true;await withButton(e.submitter,async()=>{try{const result=await api('/api/library/'+ident+'/tags',{tags,structured_tags});if(selectedMaterial?.id===ident){selectedMaterial=result.item;const current=JSON.stringify({tags:splitMaterialTags($('materialTags').value),structured_tags:Object.fromEntries(Object.keys(materialDimensions).map(d=>[d,splitMaterialTags($('edit-'+d).value)]))});materialDirty=current!==snapshot;$('materialTagStatus').textContent=materialDirty?'已保存提交时的标签；你后续输入的修改还未保存。':'七类标签与自定义标签已保存。';}loadLibrary(libraryPage);}catch(e){$('materialTagStatus').textContent=e.message;}finally{materialSaving=false;}});};

const localBridgeIds=['aaipdbnbbgeadlodajhcobjmomnemclj','kbnibljeinmjmmpholicjkaiehmilbbk'];
let pluginPairRunning=false;
async function connectLocalPlugin(){
 if(pluginPairRunning)return;
 pluginPairRunning=true;$('pluginPairDialog').showModal();$('pluginPairStatus').textContent='正在连接当前浏览器用户的插件…';$('pluginPairHelp').textContent='只保存连接配置，不上传 Google 登录信息，不提交生成。';$('pluginPairRetry').disabled=true;$('pairPlugin').disabled=true;
 try{
  const extensionId=await findLocalBridge();
  const config=await api('/api/bridge/pair',{extensionId});
  await pluginCommand(extensionId,{action:'sw_pair',config});
  $('pluginPairStatus').textContent='连接成功：已保存到本机共享配置。';$('pluginPairHelp').textContent='下一步：打开该 Chrome 用户的 Flow 页面，点击插件图标，在插件内点击“同步到 StudioWright”。本按钮只连接插件，不会自动上传账号。';
 }catch(e){$('pluginPairStatus').textContent='连接未完成：'+e.message;$('pluginPairHelp').textContent='当前页面未采集或上传 Google 登录信息。若提示本机共享组件不可用，需要修复插件的本机组件。';}
 finally{pluginPairRunning=false;$('pluginPairRetry').disabled=false;$('pairPlugin').disabled=false;}
}
$('pairPlugin').onclick=()=>{window.open('http://127.0.0.1:18890/','_blank','noopener');};$('pluginPairRetry').onclick=connectLocalPlugin;

setupLibraryTools();
setupAccountInsights();

function pluginCommand(id,message,timeout=15000){return new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('插件响应超时')),timeout);try{if(!globalThis.chrome?.runtime?.sendMessage)throw Error('当前窗口尚未接通插件');chrome.runtime.sendMessage(id,message,result=>{clearTimeout(timer);const error=chrome.runtime.lastError;if(error)reject(Error('插件未接通，请在该 Chrome 用户中安装或重新加载'));else if(!result?.ok)reject(Error(result?.error||'插件未响应'));else resolve(result);});}catch(e){clearTimeout(timer);reject(e);}});}

async function findLocalBridge(){
 if(!globalThis.chrome?.runtime?.sendMessage)throw Error('请在对应的 Chrome 用户窗口打开后台，再确认插件已启用。');
 for(const id of localBridgeIds){
  try{await pluginCommand(id,{action:'sw_probe'},2500);return id;}
  catch{}
 }
 throw Error('未检测到插件，请在该 Chrome 用户中安装或重新加载。');
}

async function runChromeJob(nonce){
 const statusText=$('chromeImportStatus');
 const help=$('chromeImportHelp');
 const back=$('chromeImportReturn');
 function showResult(message, confirmed=false){
  statusText.textContent=message;
  help.textContent=confirmed?'即将返回创作工作台的账号管理。':'可返回创作工作台查看账号状态。';
  back.hidden=false;
  if(confirmed)setTimeout(()=>location.replace(back.href),1400);
 }
 if(!/^[A-Za-z0-9_-]{40,60}$/.test(nonce)){
  showResult('导入链接无效，请从账号管理重新发起。');
  return;
 }
 const base='http://127.0.0.1:18890/api/job/'+nonce;
 async function report(status,message){
  const response=await fetch(base,{method:'POST',headers:{'Content-Type':'application/json','X-Console-Request':'1'},body:JSON.stringify({status,message})});
  if(!response.ok)throw Error((await response.json()).error||'无法更新本机导入状态');
 }
 let started=false;
 try{
  const response=await fetch(base);
  const job=await response.json();
  if(!response.ok)throw Error(job.error||'无法读取本机导入请求');
  if(job.status!=='waiting'){
   showResult(job.message||'请在账号管理中查看本次同步状态。',job.status==='succeeded');
   return;
  }
  started=true;
  if(job.action==='check'){
   if(!localBridgeIds.includes(job.extension_id))throw Error('安装检查链接无效，请从本机账号管理重新发起。');
   statusText.textContent='正在检查插件和 Windows 共享组件…';
   const probe=await pluginCommand(job.extension_id,{action:'sw_probe'},5000);
   if(probe.nativeReady===undefined)throw Error('当前插件版本尚不支持连接检查，请重新加载新版账号助手。');
   if(probe.nativeReady!==true)throw Error('插件已加载，Windows 共享组件未接通。请修复 StudioWright 桌面版后再检查。');
   const message=probe.connected ? '插件和共享组件可用，可以发起同步。' : '插件和共享组件可用，首次同步时会连接 Ubuntu。';
   await report('succeeded',message);
   showResult(message);
   return;
  }
  statusText.textContent='正在连接 Chrome 插件（'+job.profile+'）…';
  help.textContent='如尚未安装插件，请在此 Chrome 用户下安装或重新加载插件。连接成功后会自动继续。';
  let extensionId;
  const deadline=Date.now()+600000;
  while(Date.now()<deadline){
   try{extensionId=await findLocalBridge();break;}
   catch(e){await report('waiting',e.message);await new Promise(resolve=>setTimeout(resolve,8000));}
  }
  if(!extensionId)throw Error('等待插件超过 10 分钟，请安装后从本机账号管理重新同步。');
  const config=await api('/api/bridge/pair',{extensionId});
  await pluginCommand(extensionId,{action:'sw_pair',config});
  await report('running','插件已连接，正在同步并核对账号');
  statusText.textContent='正在同步 Flow 登录并核对实际邮箱…';
  help.textContent='请保持此窗口打开，直到显示导入结果。';
  const result=await pluginCommand(extensionId,{action:'sw_sync'},180000);
  const message=result.message||'后台仍在核对，请在创作工作台的任务队列查看。';
  const confirmed=result.status==='confirmed' || (!result.status && message.startsWith('Ubuntu 登录核对成功：'));
  await report(confirmed?'succeeded':'running',message);
  showResult(message,confirmed);
 }catch(e){
  showResult('本次同步未完成：'+e.message);
  if(started)try{await report('failed',e.message);}catch{}
 }
}
