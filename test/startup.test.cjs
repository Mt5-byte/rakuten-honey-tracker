const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

test('snapshot errors replace the visible loading message', async () => {
  const boot = { innerHTML: 'Loading the latest rates…', setAttribute(name, value) { this[name] = value; } };
  const app = { hidden: true };
  const elements = { '#theme': { addEventListener() {} }, '#boot': boot, '#app': app };
  const document = { querySelector: selector => elements[selector] };
  const code = readFileSync(new URL('../assets/app.js', require('node:url').pathToFileURL(__filename)), 'utf8');
  // Let the real entry point initialize and handle the failed request.
  await vm.runInNewContext(code, {
    document, localStorage: { getItem: () => null }, URLSearchParams, location: { hash: '' },
    fetch: async () => ({ ok: false, status: 503 }),
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(boot.role, 'alert');
  assert.match(boot.innerHTML, /tracker data could not be loaded/);
  assert.match(boot.innerHTML, /HTTP 503/);
  assert.equal(app.hidden, true);
});
