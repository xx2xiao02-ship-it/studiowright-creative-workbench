'use strict';

const $ = id => document.getElementById(id);
const localBridgeIds = ['aaipdbnbbgeadlodajhcobjmomnemclj', 'kbnibljeinmjmmpholicjkaiehmilbbk'];

async function pairPlugin(extensionId) {
  const response = await fetch('/api/bridge/pair', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Console-Request': '1'},
    body: JSON.stringify({extensionId}),
    credentials: 'same-origin',
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '连接插件失败，请稍后重试。');
  return result;
}

function pluginCommand(id, message, timeout = 15000) {
  return new Promise((resolve, reject) => {
    if (!globalThis.chrome?.runtime?.sendMessage) {
      reject(new Error('当前窗口尚未接通插件'));
      return;
    }
    const timer = setTimeout(() => reject(new Error('插件响应超时')), timeout);
    try {
      chrome.runtime.sendMessage(id, message, result => {
        clearTimeout(timer);
        if (chrome.runtime.lastError) {
          reject(new Error('插件未接通，请在该 Chrome 用户中安装或重新加载'));
        } else if (!result?.ok) {
          reject(new Error(result?.error || '插件未响应'));
        } else {
          resolve(result);
        }
      });
    } catch (error) {
      clearTimeout(timer);
      reject(error);
    }
  });
}

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
  help.textContent=confirmed?'即将返回算力中心的账号管理。':'可返回算力中心查看账号状态。';
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
  const config=await pairPlugin(extensionId);
  await pluginCommand(extensionId,{action:'sw_pair',config});
  await report('running','插件已连接，正在同步并核对账号');
  statusText.textContent='正在同步 Flow 登录并核对实际邮箱…';
  help.textContent='请保持此窗口打开，直到显示导入结果。';
  const result=await pluginCommand(extensionId,{action:'sw_sync'},180000);
  const message=result.message||'后台仍在核对，请在算力中心的任务队列查看。';
  const confirmed=result.status==='confirmed' || (!result.status && message.startsWith('Ubuntu 登录核对成功：'));
  await report(confirmed?'succeeded':'running',message);
  showResult(message,confirmed);
 }catch(e){
  showResult('本次同步未完成：'+e.message);
  if(started)try{await report('failed',e.message);}catch{}
 }
}

const job = new URLSearchParams(location.hash.slice(1));
if (job.has('chrome-job')) {
  $('computeCenterRedirect').hidden = true;
  $('chromeImport').hidden = false;
  document.title = '导入 Flow 账号 · 算力中心';
  void runChromeJob(job.get('chrome-job'));
} else {
  location.replace($('computeCenterHome').href);
}
