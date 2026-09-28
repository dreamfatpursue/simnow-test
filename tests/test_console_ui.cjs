// Run with: node tests/test_console_ui.cjs. Renderer checks need no browser or CTP.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../trading_console/index.html'), 'utf8');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'console_ui_fixture.json'), 'utf8'));

// Minimal DOM boundary: rendering writes are captured; layout/real disclosure behavior is checked in the offline browser preview.
class Element {
  constructor() { this.dataset = {}; this.listeners = {}; this.classList = {toggle() {}}; this.value = ''; this.hidden = false; this.open = false; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  querySelectorAll() { return []; }
  contains() { return false; }
  setAttribute(name, value) { this[name] = value; }
  append(option) { if (!this.value && !option.disabled) this.value = option.value; }
}
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((m) => ['#' + m[1], new Element()]));
const tabs = ['orders', 'trades', 'timeline', 'parameters'].map((panel) => { const el = new Element(); el.dataset.panel = panel; return el; });
const get = (key) => { assert.ok(elements.has(key), 'Missing element: ' + key); return elements.get(key); };
get('#run-raw').parentElement = new Element();
get('#strategy').value = 'strategy.json'; get('#environment').value = 'first'; get('#order-filter').value = 'active';
const context = vm.createContext({document: {querySelector:get, querySelectorAll:() => tabs, createElement:() => new Element()}, window:{}, location:{protocol:'file:'}, Intl, AbortSignal, structuredClone});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
const run = (script) => vm.runInContext(script, context);
context.snapshot = structuredClone(fixture);
run('renderOverview(snapshot)');
assert.equal(get('#last-price').textContent, '4,509.6');
assert.equal(get('#order-count').textContent, '2');
assert.match(get('#orders-table').innerHTML, /撤单确认中/);
assert.doesNotMatch(get('#orders-table').innerHTML, /全部成交/);
assert.match(get('#trades-table').innerHTML, /09\/02 20:01:46/);
{
  const tradesHtml = get('#trades-table').innerHTML;
  assert.ok(tradesHtml.indexOf('09/02 20:01:45') < tradesHtml.indexOf('09/02 20:01:46'), 'trades should follow exchange time');
  assert.ok(tradesHtml.indexOf('开仓') < tradesHtml.indexOf('平仓'), 'open fill should appear before close fill');
}
run('snapshot.contracts[0].trades = snapshot.contracts[0].trades.slice().reverse(); renderOverview(snapshot)');
{
  const tradesHtml = get('#trades-table').innerHTML;
  assert.ok(tradesHtml.indexOf('09/02 20:01:45') < tradesHtml.indexOf('09/02 20:01:46'), 'arrival order must not invert displayed trade time');
  assert.ok(tradesHtml.indexOf('开仓') < tradesHtml.indexOf('平仓'), 'open fill should appear before close fill after reversed payload');
}
run('snapshot.contracts[0].trades = snapshot.contracts[0].trades.slice().reverse(); renderOverview(snapshot)');
assert.match(get('#contract-facts').innerHTML, /最近核对净仓/);
assert.match(get('#contract-facts').innerHTML, /CTP 查询快照/);
assert.match(get('#run-parameters').innerHTML, /重定锚步长 S/);
assert.doesNotMatch(get('#run-parameters').innerHTML, /<pre>|\[object Object\]/);
assert.match(get('#run-parameters').innerHTML, /<dt>重定锚步长 S<\/dt><dd>1 tick<\/dd>/);
assert.doesNotMatch(get('#run-parameters').innerHTML, /rb2610|历史审计|运行共享参数/);
assert.equal(get('#launch-panel').open, false);
assert.equal(get('#preview').disabled, true);
assert.equal(get('#stop').disabled, false);
assert.equal(run('relativeTime(245)'), '审计 +02:25');
assert.equal(run('number(null)'), '—');
assert.equal(run('number(0)'), '0');

// One contract is the same list view with one card, not a separate display mode.
assert.equal(get('#contract-switcher').hidden, false);
assert.equal((get('#contract-switcher').innerHTML.match(/data-contract=/g) || []).length, 2);
run('renderOverview({...snapshot, contracts: snapshot.contracts.slice(0, 1)})');
assert.equal(get('#contract-switcher').hidden, false);
assert.equal((get('#contract-switcher').innerHTML.match(/data-contract=/g) || []).length, 1);
assert.match(get('#contract-switcher').innerHTML, /IF2610@CFFEX/);
assert.equal(get('#last-price').textContent, '4,509.6');
run('renderOverview(snapshot)');

// Different contracts, tab and filter selections survive polling.
tabs[1].listeners.click();
get('#order-filter').value = 'all';
run('renderOverview(snapshot)');
assert.equal(get('#panel-trades').hidden, false);
assert.equal(get('#order-filter').value, 'all');
assert.match(get('#orders-table').innerHTML, /全部成交/);
{
  const ordersHtml = get('#orders-table').innerHTML;
  assert.ok(ordersHtml.indexOf('quote-1-buy') < ordersHtml.indexOf('flatten-2'), 'orders should follow first_at');
  assert.ok(ordersHtml.indexOf('开仓') < ordersHtml.indexOf('平仓'), 'open order should appear before close order');
}
run('snapshot.contracts[0].logical_orders = snapshot.contracts[0].logical_orders.slice().reverse(); renderOverview(snapshot)');
{
  const ordersHtml = get('#orders-table').innerHTML;
  assert.ok(ordersHtml.indexOf('quote-1-buy') < ordersHtml.indexOf('flatten-2'), 'arrival order must not invert displayed order time');
  assert.ok(ordersHtml.indexOf('开仓') < ordersHtml.indexOf('平仓'), 'open order should appear before close order after reversed payload');
}
run('snapshot.contracts[0].logical_orders = snapshot.contracts[0].logical_orders.slice().reverse(); renderOverview(snapshot)');
run('selectedContract = "rb2610@SHFE"; renderOverview(snapshot)');
assert.match(get('#contract-name').textContent, /rb2610/);
assert.match(get('#run-parameters').innerHTML, /<dt>重定锚步长 S<\/dt><dd>5 tick<\/dd>/);
assert.doesNotMatch(get('#run-parameters').innerHTML, /IF2610/);
assert.match(get('#contract-facts').innerHTML, /未核对/);
assert.equal(get('#last-price').textContent, '—');

// A stale projection does not remove the ability to stop a known active run.
run('snapshot.data_stale = true; renderOverview(snapshot)');
assert.equal(get('#freshness-banner').hidden, false);
assert.equal(get('#stop').disabled, false);
run('snapshot.stop_requested = true; renderOverview(snapshot); renderOverview(snapshot)');
assert.equal(get('#stop').disabled, true);
assert.equal(get('#stop').textContent, '正在安全停止…');
run('snapshot.status = "terminal"; renderOverview(snapshot)');
assert.equal(get('#freshness-banner').hidden, true);
assert.equal(get('#stop').hidden, true);
assert.match(get('#run-status').textContent, /已结束/);

// Unknown order states and arbitrary audit text must not turn into executable markup.
run('snapshot.status="active"; snapshot.stop_requested=false; selectedContract="IF2610@CFFEX"; snapshot.contracts[0].logical_orders[0].status_unknown=true; snapshot.contracts[0].logical_orders[0].client_id="<img src=x onerror=alert(1)>"; renderOverview(snapshot)');
assert.match(get('#orders-table').innerHTML, /状态未知，待核对/);
assert.match(get('#orders-table').innerHTML, /&lt;img/);
assert.doesNotMatch(get('#orders-table').innerHTML, /<img/);
run('confirmation="old"; previewReady=true; invalidatePreview()');
assert.equal(run('confirmation'), '');
assert.equal(get('#start').disabled, true);

// Preview expands each contract's own effective parameters; legacy audits remain readable.
run('renderParameters("#preview-parameters", snapshot.effective)');
for (const text of ['逐合约策略参数', '高级安全参数', 'IF2610', 'rb2610', '报价窗口（该合约）', '每侧手数', '撤单超时']) assert.ok(get('#preview-parameters').innerHTML.includes(text), text);
assert.match(get('#preview-parameters').innerHTML, /20:00—23:00；每段结束前 5 秒撤单/);
run('renderEditableParameters("#preview-parameters", snapshot.effective)');
for (const text of ['保存本次策略修改', 'data-edit-param="w_ticks"', 'type="number"', 'type="time"', '添加报价窗口', '删除']) assert.ok(get('#preview-parameters').innerHTML.includes(text), text);
assert.doesNotMatch(get('#preview-parameters').innerHTML, /<pre>|\[object Object\]/);
run('renderParameters("#preview-parameters", {version:2, w_ticks:25, max_round_trips:7, contracts:[{symbol:"old",exchange:"SHFE",target_lots:1}]})');
assert.match(get('#preview-parameters').innerHTML, /历史审计：原公共参数按合约展开/);
assert.match(get('#preview-parameters').innerHTML, /25 tick/);
for (const match of html.matchAll(/<pre[^>]*id="([^"]+)"/g)) assert.ok(['run-raw','preview-raw'].includes(match[1]));
console.log('Console UI renderer checks passed (formatting, tables, missing data, state, selection, escaping, preview).');

// Manual close is scoped to the selected active risk-held contract.
run('snapshot.status="active"; snapshot.stop_requested=false; pendingStopRunId=""; selectedContract="IF2610@CFFEX"; snapshot.contracts[0].state="RISK_HOLD"; snapshot.contracts[0].manual_flatten_supported=true; renderOverview(snapshot)');
assert.equal(get('#flatten').hidden, false);
assert.equal(get('#flatten').disabled, false);
run('snapshot.contracts[0].confirmed_position={net_position:0,gross_position:2}; renderOverview(snapshot)');
assert.equal(get('#flatten').disabled, true);
assert.match(get('#contract-facts').innerHTML, /持仓总量：<strong>2 手<\/strong>/);
assert.match(get('#flatten-status').textContent, /多空两边仍有持仓/);
run('snapshot.contracts[0].confirmed_position={net_position:1,gross_position:1}; renderOverview(snapshot)');
assert.equal(get('#flatten').disabled, false);
run('snapshot.contracts[0].manual_flatten_pending=true; renderOverview(snapshot)');
assert.equal(get('#flatten').disabled, true);
assert.match(get('#flatten-status').textContent, /正在等待 CTP/);
assert.equal(get('#flatten').textContent, '等待平仓核对…');
assert.match(get('#latest-event').textContent, /仍在等待 CTP 回报/);
run('snapshot.contracts[0].manual_flatten_pending=false; snapshot.contracts[0].state="FLATTENING"; renderOverview(snapshot)');
assert.equal(get('#flatten').disabled, true);
run('snapshot.contracts[0].state="WAITING_FOR_STABLE_QUOTE"; snapshot.contracts[0].resume_wait_seconds=4.2; renderOverview(snapshot)');
assert.match(get('#flatten-status').textContent, /剩余 5 秒/);
run('snapshot.contracts[0].state="RISK_HOLD"; snapshot.contracts[0].manual_flatten_supported=false; snapshot.contracts[0].resume_wait_seconds=0; renderOverview(snapshot)');
assert.equal(get('#flatten').disabled, true);
assert.match(get('#flatten-status').textContent, /未加载/);
