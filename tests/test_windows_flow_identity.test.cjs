const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { createContext, runInContext } = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

const extension = resolve(__dirname, '../packaging/windows/client-payload/extension');
const expected = 'account@example.test';

function fixture(pages, activeId, receipt = null, openedPage = {}) {
    const stats = {opened: 0, clicks: 0, cookieReads: 0, tabReads: 0};
    const realm = createContext({URL, setTimeout: run => run()});
    realm.chrome = {
        runtime: {onMessage: {addListener() {}}, onMessageExternal: {addListener() {}}},
        storage: {local: {async get() { return {swLast: receipt}; }}},
        cookies: {async getAll() { stats.cookieReads++; throw new Error('unexpected collection'); }},
        tabs: {
            async query(options) {
                stats.tabReads++;
                return pages.filter(page => !options.active || page.id === activeId);
            },
            async get(id) { return pages.find(page => page.id === id); },
            async create({url}) {
                stats.opened++;
                const page = {id: pages.length + 1, url, status: 'complete', ...openedPage};
                pages.push(page);
                return page;
            },
        },
        scripting: {
            async executeScript({target, func, args = []}) {
                const page = pages.find(page => page.id === target.tabId);
                realm.location = new URL(page.url);
                realm.window = {WIZ_global_data: page.email ? {oPEP7c: page.email} : page.flowState || {}};
                const button = {
                    disabled: false, innerText: '使用 Google Flow 创建',
                    getClientRects: () => [{}], getAttribute: () => null,
                    click() { stats.clicks++; page.url = 'https://flow.google.com/'; page.email = expected; },
                };
                const accountLabels = (page.accountLabels || []).map(email => ({
                    getClientRects: () => [{}],
                    getAttribute: name => name === 'aria-label' ? `Google Account: ${email}` : null,
                }));
                realm.document = {querySelectorAll: selector => selector === 'button' ? [button] : accountLabels};
                return [{result: await func(...args)}];
            },
        },
    };
    for (const name of ['account-identification.js', 'studiowright-bridge.js']) {
        runInContext(readFileSync(resolve(extension, name), 'utf8'), realm);
    }
    return {stats, run: code => runInContext(code, realm)};
}

test('sync enters the Flow workspace from its public landing page once', async () => {
    const {stats, run} = fixture([{id: 1, url: 'https://flow.google.com/about', status: 'complete'}], 1);
    assert.equal(await run(`swPrepareFlowAccount('${expected}')`), expected);
    assert.equal(stats.clicks, 1);
    assert.equal(stats.opened, 0);
    assert.equal(stats.cookieReads, 0);
});

test('an unready tab does not hide an already signed-in Flow tab', async () => {
    const {stats, run} = fixture([
        {id: 1, url: 'https://flow.google.com/', status: 'loading'},
        {id: 2, url: 'https://flow.google.com/project/example', email: expected},
    ], 1);
    assert.equal(await run(`swPrepareFlowAccount('${expected}')`), expected);
    assert.equal(stats.opened + stats.clicks, 0);
});

test('a signed-in active tab with a different identity stops sync', async () => {
    const {stats, run} = fixture([
        {id: 1, url: 'https://flow.google.com/', email: 'another@example.test'},
        {id: 2, url: 'https://flow.google.com/project/example', email: expected},
    ], 1);
    await assert.rejects(run(`swPrepareFlowAccount('${expected}')`), /不一致/);
    assert.equal(stats.opened + stats.clicks + stats.cookieReads, 0);
});

test('a second account in the Google menu does not obscure the active Flow identity', async () => {
    const {run} = fixture([{
        id: 1, url: 'https://flow.google.com/', email: expected,
        accountLabels: [expected, 'another@example.test'],
    }], 1);
    assert.equal(await run(`swPrepareFlowAccount('${expected}')`), expected);
});

test('the selected account in the Google menu cannot override a different active Flow identity', async () => {
    const {stats, run} = fixture([{
        id: 1, url: 'https://flow.google.com/', email: 'another@example.test',
        accountLabels: [expected],
    }], 1);
    await assert.rejects(run(`swPrepareFlowAccount('${expected}')`), /不一致/);
    assert.equal(stats.cookieReads, 0);
});

test('visible account labels are usable only when they identify one account', async () => {
    const page = {id: 1, url: 'https://flow.google.com/', accountLabels: [expected]};
    const {stats, run} = fixture([page], 1);
    assert.equal(await run(`swPrepareFlowAccount('${expected}')`), expected);
    page.accountLabels.push('another@example.test');
    await assert.rejects(run(`swPrepareFlowAccount('${expected}')`), /无法确认当前账号/);
    assert.equal(stats.opened, 1);
    assert.equal(stats.cookieReads, 0);
});

test('ambiguous labels in a background tab do not hide a confirmed account in another tab', async () => {
    const {stats, run} = fixture([
        {id: 1, url: 'https://flow.google.com/', accountLabels: [expected, 'another@example.test']},
        {id: 2, url: 'https://flow.google.com/project/example', email: expected},
    ], null);
    assert.equal(await run(`swPrepareFlowAccount('${expected}')`), expected);
    assert.equal(stats.opened, 0);
});

test('an ambiguous active tab can be followed by one fresh workspace with a verified identity', async () => {
    const {stats, run} = fixture([
        {id: 1, url: 'https://flow.google.com/', accountLabels: [expected, 'another@example.test']},
    ], 1, null, {email: expected});
    assert.equal(await run(`swPrepareFlowAccount('${expected}')`), expected);
    assert.equal(stats.opened, 1);
});

test('signed-in workspace state can provide a unique email when the usual field is missing', async () => {
    const {run} = fixture([{
        id: 1, url: 'https://flow.google.com/', accountLabels: [expected, 'another@example.test'],
        flowState: {SNlM0e: 'test', FdrFJe: 'test', alternateField: expected},
    }], 1);
    assert.equal(await run(`swPrepareFlowAccount('${expected}')`), expected);
});

test('checking a queued receipt never opens Flow or collects cookies again', async () => {
    const receipt = {status: 'pending', id: 'fixture-receipt', email: expected};
    const {stats, run} = fixture([], null, receipt);
    run(`swNative = async () => ({apiUrl:'http://127.0.0.1:18888/api/plugin/session'});
         swCheckReceipt = async (_config, value) => value.id;`);
    assert.equal(await run(`swSync('${expected}', true)`), receipt.id);
    assert.equal(stats.tabReads + stats.opened + stats.cookieReads, 0);
});
