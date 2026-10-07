const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('app/static/legacy/index.html', 'utf8');
const method = html.match(/async connectYoutube\(\) \{([\s\S]*?)\n                \},\n                async submitAuthCode/);
assert.ok(method, 'YouTube connection handler must exist');
async function check(embedded) {
  const url = 'https://accounts.google.com/o/oauth2/v2/auth?state=test';
  const location = { href: '/static/legacy/index.html' };
  const top = embedded ? { location: { href: '/static/pages/legacy-settings/index.html' } } : null;
  const window = { location, open() { throw new Error('Popup unexpectedly used'); } };
  window.top = top || window;
  const connect = vm.runInNewContext('(async function(){' + method[1] + '})', { window, alert: message => { throw new Error(message); } });
  const context = { authFetch: async () => ({ ok: true, text: async () => JSON.stringify({ auth_url: url }) }) };
  await connect.call(context);
  assert.equal(window.top.location.href, url);
  if (embedded) assert.equal(location.href, '/static/legacy/index.html', 'Google authorization must leave the iframe');
}
(async () => {
  await check(true);
  await check(false);
  console.log('YouTube OAuth navigation passed: embedded and standalone UI');
})().catch(error => { console.error(error); process.exitCode = 1; });
