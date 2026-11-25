// Read the account displayed by Flow. This module never reads Cookies,
// saves credentials, calls the backend, or starts a synchronization.
class FlowIdentityUnavailable extends Error {}
class FlowIdentityAmbiguous extends FlowIdentityUnavailable {}

async function readFlowAccount(tabId) {
    const results = await chrome.scripting.executeScript({
        target: {tabId},
        world: 'MAIN',
        func: () => {
            if (location.hostname !== 'flow.google.com') return [];
            const state = window.WIZ_global_data || {};
            const validEmail = value => typeof value === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
            // Account menus can also list other signed-in Google accounts.
            if (validEmail(state.oPEP7c)) return [state.oPEP7c.trim().toLowerCase()];
            if (state.SNlM0e && state.FdrFJe) {
                const signedInEmails = [...new Set(Object.values(state).filter(validEmail)
                    .map(email => email.trim().toLowerCase()))];
                if (signedInEmails.length) return signedInEmails;
            }
            const emails = new Set();
            for (const node of document.querySelectorAll('[aria-label],[title]')) {
                if (!node.getClientRects().length) continue;
                const text = [node.getAttribute('aria-label'), node.getAttribute('title')].filter(Boolean).join(' ');
                if (!/(Google\s*(Account|账号|帳戶|帐户)|Google-Konto|Compte Google)/i.test(text)) continue;
                for (const email of text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || []) {
                    emails.add(email.toLowerCase());
                }
            }
            return [...emails];
        }
    });
    const emails = results[0]?.result || [];
    if (!emails.length) throw new FlowIdentityUnavailable('当前 Flow 页面未显示登录邮箱。');
    if (emails.length !== 1) throw new FlowIdentityAmbiguous('未能唯一识别当前 Flow 邮箱，请确认页面已登录；也可以手动填写。');
    const email = String(emails[0]).trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new Error('Flow 返回的邮箱格式无效。');
    return email;
}

async function detectFlowAccount() {
    const active = await chrome.tabs.query({active: true, lastFocusedWindow: true, url: 'https://flow.google.com/*'});
    if (active.length === 1) return readFlowAccount(active[0].id);
    const tabs = await chrome.tabs.query({url: 'https://flow.google.com/*'});
    if (!tabs.length) throw new Error('请在当前 Chrome 用户窗口打开已登录的 Flow 页面，邮箱会自动读取；也可以手动填写。');
    const emails = new Set();
    for (const tab of tabs) emails.add(await readFlowAccount(tab.id));
    if (emails.size !== 1) throw new Error('多个 Flow 页面显示不同邮箱，请切到要同步的 Flow 页面后重新打开插件。');
    return [...emails][0];
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action !== 'detectAccount') return false;
    detectFlowAccount().then(email => sendResponse({success: true, email}))
        .catch(error => sendResponse({success: false, error: error.message}));
    return true;
});
