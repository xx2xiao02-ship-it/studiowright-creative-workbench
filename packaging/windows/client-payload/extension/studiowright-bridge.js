/* StudioWright manual bridge; independent of the legacy Flow2API endpoint. */
let swSyncRunning = false;

async function swNative(action, config) {
    const result = await chrome.runtime.sendNativeMessage('com.studiowright.bridge', {action, config});
    if (!result?.ok) throw new Error(result?.error || '本机共享组件不可用。');
    return result.config;
}

async function swConnectionStatus() {
    const version = chrome.runtime.getManifest().version;
    try {
        const config = await swNative('sw_get');
        return {ok:true, version, nativeReady:true,
            connected:config?.apiUrl === 'http://127.0.0.1:18888/api/plugin/session'};
    } catch {
        return {ok:true, version, nativeReady:false, connected:false};
    }
}

async function swRequest(config, path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000);
    try {
        const response = await fetch(config.apiUrl + path, {
            method: body === undefined ? 'GET' : 'POST',
            headers: {'Authorization': 'Bearer ' + config.connectionToken,
                ...(body === undefined ? {} : {'Content-Type': 'application/json', 'X-Console-Request': '1'})},
            body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal
        });
        const result = await response.json();
        if (!response.ok) {
            const error = new Error(result.error || '后台未接收登录信息。');
            error.confirmedRejected = true;
            throw error;
        }
        return result;
    } catch (error) {
        if (error.name === 'AbortError') throw new Error('后台连接超时，请确认 StudioWright 后台已打开。');
        throw error;
    } finally {
        clearTimeout(timeout);
    }
}

function swNormalizeEmail(value) {
    const email = String(value || '').trim().toLowerCase();
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ? email : '';
}

// Read the identity of the Chrome profile explicitly selected for this job.
// The page already has read-only access to its own local profile manager.
async function swExpectedFromJob(sender, status = 'running') {
    if (!Number.isInteger(sender?.tab?.id)) throw new Error('无法确认所选 Chrome 用户，已停止采集。');
    let results;
    try {
        results = await chrome.scripting.executeScript({
        target: {tabId: sender.tab.id}, world: 'MAIN', args: [status],
        func: async requiredStatus => {
            if (!['http://127.0.0.1:18888', 'http://localhost:18888'].includes(location.origin)) return null;
            const match = /^#chrome-job=([A-Za-z0-9_-]{40,60})$/.exec(location.hash);
            if (!match) return null;
            const base = 'http://127.0.0.1:18890';
            const jobUrl = new URL('/api/job/' + encodeURIComponent(match[1]), base);
            if (jobUrl.protocol !== 'http:' || jobUrl.hostname !== '127.0.0.1' || jobUrl.port !== '18890') return null;
            const jobResponse = await fetch(jobUrl, {cache:'no-store'});
            if (!jobResponse.ok) return null;
            const job = await jobResponse.json();
            if (job.status !== requiredStatus || !/^(Default|Profile \d+)$/.test(job.profile || '')) return null;
            const listResponse = await fetch(base + '/api/profiles', {cache:'no-store'});
            if (!listResponse.ok) return null;
            const list = await listResponse.json();
            const selected = (Array.isArray(list.profiles) ? list.profiles : []).filter(p => p.profile === job.profile);
            return selected.length === 1 ? selected[0].email : null;
        }
        });
    } catch (error) {
        throw new Error('无法核实当前采集任务和所选 Chrome 用户，已停止采集。');
    }
    const expected = swNormalizeEmail(results[0]?.result);
    if (!expected) throw new Error('无法核实所选 Chrome 用户的邮箱，已停止采集。');
    return expected;
}

const flowLanding = /^https:\/\/flow\.google\.com\/about\/?(?:[?#]|$)/;

function swCanWaitForFlow(error) {
    return error instanceof FlowIdentityUnavailable || /Cannot access contents of the page|showing error page/i.test(error.message);
}

async function swFindFlowAccount(expected) {
    const active = (await chrome.tabs.query({active:true, lastFocusedWindow:true, url:'https://flow.google.com/*'}))
        .filter(tab => !flowLanding.test(tab.url || ''));
    if (active.length === 1) {
        try {
            const email = swNormalizeEmail(await readFlowAccount(active[0].id));
            if (!email) throw new FlowIdentityUnavailable('当前 Flow 页面未显示有效的登录邮箱。');
            if (expected && email !== expected) throw new Error('当前 Flow 邮箱与所选 Chrome 用户不一致，已停止采集。');
            return email;
        } catch (error) {
            if (!expected || !swCanWaitForFlow(error)) throw error;
        }
    }
    if (!expected) throw new Error('请先切到要同步账号的 Flow 页面，再使用插件同步。');
    const tabs = await chrome.tabs.query({url:'https://flow.google.com/*'});
    const found = new Set();
    for (const tab of tabs) {
        if (flowLanding.test(tab.url || '')) continue;
        try {
            const email = swNormalizeEmail(await readFlowAccount(tab.id));
            if (email) found.add(email);
        } catch (error) {
            // A browser error page cannot contain a usable Flow identity.
            if (swCanWaitForFlow(error)) continue;
            throw error;
        }
    }
    if (found.has(expected)) return expected;
    if (found.size) throw new Error('Flow 页面邮箱与所选 Chrome 用户不一致，已停止采集。');
    throw new FlowIdentityUnavailable('所选 Chrome 用户的 Flow 页面尚未显示登录邮箱。');
}

async function swEnterFlowWorkspace(tabId) {
    const [result] = await chrome.scripting.executeScript({
        target: {tabId},
        world: 'MAIN',
        func: () => {
            if (location.origin !== 'https://flow.google.com' || !/^\/about\/?$/.test(location.pathname)) return false;
            const labels = /^(使用 Google Flow 创建|使用 Google Flow 建立|Create with (?:Google )?Flow)$/i;
            const buttons = [...document.querySelectorAll('button')].filter(button => !button.disabled
                && button.getClientRects().length && labels.test((button.getAttribute('aria-label') || button.innerText || '').trim()));
            if (buttons.length !== 1) return false;
            buttons[0].click();
            return true;
        },
    });
    return result?.result === true;
}

async function swPrepareFlowAccount(expected) {
    try {
        return await swFindFlowAccount(expected);
    } catch (error) {
        if (!swCanWaitForFlow(error)) throw error;
    }
    const tabs = await chrome.tabs.query({url:'https://flow.google.com/*'});
    const target = tabs.find(tab => flowLanding.test(tab.url || ''))
        || await chrome.tabs.create({url:'https://flow.google.com/', active:false});
    let entered = false;
    for (let i = 0; i < 24; i++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        try {
            return await swFindFlowAccount(expected);
        } catch (error) {
            if (!swCanWaitForFlow(error)) throw error;
        }
        const tab = await chrome.tabs.get(target.id);
        if (!entered && flowLanding.test(tab.url || '')) entered = await swEnterFlowWorkspace(tab.id);
        if (tab.status === 'complete' && tab.url?.startsWith('https://accounts.google.com/')) {
            throw new Error('Google 要求登录，请在这个 Windows Chrome 用户中确认 Flow 登录。');
        }
    }
    throw new Error('已打开 Flow，但没有读到已登录邮箱，请查看这个 Windows Chrome 用户的 Flow 页面。');
}

async function swCheckReceipt(config, receipt) {
    const pending = 'Ubuntu 已接收，仍在核对。请在 StudioWright 任务队列查看结果。';
    for (let i = 0; i < 70; i++) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        let state;
        try {
            state = await swRequest(config, '/' + encodeURIComponent(receipt.id));
        } catch {
            const message = 'Ubuntu 已接收，核对连接中断。再次点击同步会查询原回执。';
            await chrome.storage.local.set({swLast:{...receipt,message}});
            return {ok:true, status:'pending', message};
        }
        if (state.status === 'succeeded') {
            if (swNormalizeEmail(state.email) !== receipt.email) {
                const error = new Error('Ubuntu 核对的邮箱与所选账号不一致，不能报告同步成功，请检查任务队列。');
                error.terminalReceipt = true;
                throw error;
            }
            const message = 'Ubuntu 登录核对成功：' + state.email;
            await chrome.storage.local.set({swLast:{...receipt,status:'success',time:Date.now(),message}});
            return {ok:true, status:'confirmed', message};
        }
        if (['failed','interrupted','cancelled'].includes(state.status)) {
            const error = new Error(state.error || 'Ubuntu 未能确认登录，请使用后台的远程登录。');
            error.terminalReceipt = true;
            throw error;
        }
    }
    return {ok:true, status:'pending', message:pending};
}

async function swSync(expectedEmail = '', receiptOnly = false) {
    if (swSyncRunning) throw new Error('此窗口正在同步，请等待结果。');
    swSyncRunning = true;
    try {
        const config = await swNative('sw_get');
        if (!config) throw new Error('请先打开 StudioWright 后台，在「账号与项目」点击「连接本机插件」。');
        if (config.apiUrl !== 'http://127.0.0.1:18888/api/plugin/session') throw new Error('连接地址不正确，请重新连接本机插件。');
        const last = (await chrome.storage.local.get('swLast')).swLast;
        if (receiptOnly) {
            if (last?.status === 'pending' && last.id && last.email === expectedEmail) return await swCheckReceipt(config, last);
            return {ok:true, status:'pending', message:'未找到可继续查询的回执，请在算力中心查看账号检查记录。'};
        }
        const email = expectedEmail ? await swPrepareFlowAccount(expectedEmail) : await swFindFlowAccount('');
        if (last?.status === 'pending' && last.id) {
            if (last.email !== email) return {ok:false,error:'这个 Chrome 用户还有另一账号的同步待核对，请先查看任务队列。'};
            return await swCheckReceipt(config, last);
        }
        const allowed = new Set(['google.com', 'flow.google.com', 'accounts.google.com', 'www.google.com']);
        const all = await chrome.cookies.getAll({domain:'google.com'});
        const cookies = all.filter(c => allowed.has(c.domain.replace(/^\./,'')) && (!c.expirationDate || c.expirationDate > Date.now()/1000))
            .map(c => ({name:c.name,value:c.value,domain:c.domain,path:c.path,secure:c.secure,httpOnly:c.httpOnly,
                hostOnly:c.hostOnly,sameSite:c.sameSite,expirationDate:c.expirationDate,partitionKey:c.partitionKey}));
        if (!cookies.length) throw new Error('当前 Chrome 用户未找到 Flow 登录 Cookie。');
        let result;
        try {
            result = await swRequest(config, '', {email, cookies});
        } catch (error) {
            if (error.confirmedRejected) throw error;
            throw new Error('提交后连接中断，结果未确认；请先查看 StudioWright 任务队列，不要重复采集。');
        }
        if (typeof result?.id !== 'string' || !result.id) {
            throw new Error('请求结果尚未确认，请先查看 StudioWright 任务队列，不要重复采集。');
        }
        const receipt = {status:'pending',email,id:result.id,time:Date.now(),message:'已接收，正在 Ubuntu 核对实际邮箱。'};
        await chrome.storage.local.set({swLast:receipt});
        return await swCheckReceipt(config, receipt);
    } catch (error) {
        const previous = (await chrome.storage.local.get('swLast')).swLast;
        const failure = previous?.status === 'pending' && previous.id && !error.terminalReceipt
            ? {...previous,message:error.message}
            : {status:'error',time:Date.now(),message:error.message};
        await chrome.storage.local.set({swLast:failure});
        throw error;
    } finally {
        swSyncRunning = false;
    }
}

chrome.runtime.onMessage.addListener((request, sender, respond) => {
    if (request?.action !== 'sw_sync' && request?.action !== 'sw_status') return false;
    const promise = request.action === 'sw_sync' ? swSync() :
        Promise.all([swConnectionStatus(), chrome.storage.local.get('swLast')]).then(([state,data]) => ({...state,last:data.swLast}));
    promise.then(respond).catch(error => respond({ok:false,error:error.message}));
    return true;
});

chrome.runtime.onMessageExternal.addListener((request, sender, respond) => {
    if (!['sw_pair','sw_probe','sw_sync','sw_refresh_import'].includes(request?.action)) return false;
    if (!/^http:\/\/(127\.0\.0\.1|localhost):18888\/(?:[?#].*)?$/.test(sender.url || '')) {
        respond({ok:false,error:'仅允许本机 StudioWright 后台连接。'});
        return false;
    }
    const promise = request.action === 'sw_probe' ? swConnectionStatus() :
        request.action === 'sw_sync' ? swExpectedFromJob(sender).then(expected => swSync(expected)) :
        request.action === 'sw_refresh_import' ? swExpectedFromJob(sender, 'pending').then(expected => swSync(expected, true)) :
        swNative('sw_set',request.config).then(() => ({ok:true}));
    promise.then(respond).catch(error => respond({ok:false,error:error.message}));
    return true;
});
