'use strict';
async function copyTextSafely(text){
 try{await navigator.clipboard.writeText(text);return true;}catch{
  const box=document.createElement('textarea');box.value=text;box.setAttribute('aria-label','待复制内容');box.style.position='fixed';box.style.opacity='0';document.body.append(box);box.select();let ok=false;try{ok=document.execCommand('copy');}finally{box.remove();}if(ok)return true;
  const fallbackBox=document.getElementById('copyFallbackText');fallbackBox.value=text;document.getElementById('copyFallback').showModal();fallbackBox.select();return false;
 }
}
let latestLibraryExample=null;
async function docsApi(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':'application/json','X-Console-Request':'1'},body:body===undefined?undefined:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw new Error(data.error||'请求失败');return data;}
async function loadKeys(){try{const data=await docsApi('/api/keys');const list=el('keyList');list.replaceChildren();for(const k of data.items){const row=document.createElement('div');row.className='key-row';const label=document.createElement('span');label.textContent=`${k.name} · ${k.scope==='read'?'只读素材与任务':'生成与读取'} · ${k.revoked?'已撤销':'有效'}`;row.append(label);if(!k.revoked){const b=document.createElement('button');b.className='secondary';b.textContent='撤销';b.onclick=async()=>{try{await docsApi('/api/keys/'+k.id+'/revoke',{});el('keyStatus').textContent='密钥已撤销，使用它的项目将无法继续调用。';await loadKeys();}catch(e){el('keyStatus').textContent=e.message;}};row.append(b);}list.append(row);}if(!data.items.length)list.textContent='还没有 API Key。建议为每个调用项目创建一个只读密钥。';}catch(e){el('keyStatus').textContent=e.message;}}
el('keyForm').onsubmit=async e=>{e.preventDefault();const button=el('createKey');button.disabled=true;try{const k=await docsApi('/api/keys',{name:el('keyName').value.trim(),scope:el('keyScope').value});el('newKey').value=k.key;el('newKeyArea').hidden=false;el('keyStatus').textContent='已创建。完整密钥仅在这次显示，刷新后不能找回；遗失可撤销后重新创建。';await loadKeys();}catch(e){el('keyStatus').textContent=e.message;}finally{button.disabled=false;}};
el('copyKey').onclick=async()=>{if(await copyTextSafely(el('newKey').value))el('keyStatus').textContent='已复制密钥，请保存到调用项目的私密配置，不要写入公开文档。';};
el('hideKey').onclick=()=>{el('newKey').value='';el('newKeyArea').hidden=true;};
async function loadLibraryExample(){try{const d=await docsApi('/api/library?limit=1');latestLibraryExample=d.items[0]||null;el('libraryResponse').textContent=JSON.stringify(d,null,2);const i=latestLibraryExample;el('detailResponse').textContent=i?JSON.stringify(i,null,2):'当前没有素材。新增已保存素材后可读取完整返回。';el('facetsResponse').textContent=JSON.stringify(d.facets,null,2);el('libraryDocStatus').textContent=`已读取真实素材库：${d.total} 份素材；示例仅展示一条，未运行分析或生成。`;}catch(e){el('libraryDocStatus').textContent=e.message;}}
el('refreshLibraryExample').onclick=loadLibraryExample;
el('keyConnectCode').textContent=`# 在调用项目的私密环境变量中保存生成的密钥，不要把实际密钥写进公开文档。
$base = '${location.origin}'
$headers = @{ Authorization = "Bearer $env:STUDIOWRIGHT_API_KEY"; 'X-Console-Request' = '1' }
# 以下只读查询，不生成、不调用标签分析模型。
$state = Invoke-RestMethod "$base/api/state" -Headers $headers
$assets = Invoke-RestMethod "$base/api/library?scene=%E5%9F%8E%E5%B8%82&style=%E5%86%99%E5%AE%9E&limit=30&offset=0" -Headers $headers
# 也可沿用 Flow2API 七维标签格式（tags 为七维对象，custom_tags 为普通标签）。
$legacyShape = Invoke-RestMethod "$base/api/material-library/assets?scene=%E5%9F%8E%E5%B8%82&limit=30&offset=0" -Headers $headers
$facets = Invoke-RestMethod "$base/api/library/facets" -Headers $headers
if ($assets.items.Count -gt 0) {
  $item = $assets.items[0]
  $detail = Invoke-RestMethod "$base/api/library/$($item.id)" -Headers $headers
  # 选择自己的输出文件名；避免覆盖已有素材。
  $target = Join-Path (Get-Location) ($item.id + '.media')
  if (Test-Path -LiteralPath $target) { throw '同名文件已存在，请更换路径' }
  Invoke-WebRequest "$base$($item.url)" -Headers $headers -OutFile $target
}`;
el('copyCompleteDoc').onclick=async()=>{
 const parts=['# StudioWright 完整 API 调用说明',`调用基址：${location.origin}`,`快照时间：${new Date().toLocaleString('zh-CN')}`,state?`源程序版本：${state.source_version}；提交：${state.source_commit}；接口版本：${state.console_api_version}；环境：${state.test?'模拟':'正式'}`:'版本尚未读取，请先检查连接。','API Key：在此后台的 /static/api.html → 独立 API Key 中，为调用项目随机生成密钥。默认只读；生成权限可消耗积分。完整密钥只显示一次，保存在调用程序的 STUDIOWRIGHT_API_KEY 私密环境变量中；遗失或泄漏请在后台撤销重建。插件连接 Token 不能当作 API Key。'];
 for(const panel of document.querySelectorAll('main > section.panel')){
  if(panel.id==='keyManager')continue;
  const clone=panel.cloneNode(true);for(const node of clone.querySelectorAll('button,input,select,textarea,[role="status"]'))node.remove();
  for(const node of clone.querySelectorAll('h2,h3,p,pre,tr,summary')){node.before(document.createTextNode('\n'));node.after(document.createTextNode('\n'));}
  // textContent includes collapsed examples too; input values and secrets never enter the export.
  parts.push(clone.textContent.trim());
 }
 if(state)parts.push('## 本次读取的完整可用模型列表\n'+JSON.stringify(state.models,null,2));
 parts.push('说明：API Key 不包含在此文档中；所有复制操作都只生成文字，不提交任务。');
 if(await copyTextSafely(parts.join('\n\n')))el('copyStatus').textContent='已复制完整调用说明：地址、模型、请求、实际返回、七维检索及鉴权；未包含实际密钥，未执行任务。';
};
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
loadKeys();loadLibraryExample();
