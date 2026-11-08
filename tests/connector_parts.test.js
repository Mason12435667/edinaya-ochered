'use strict';
// node --test tests/ : части коннектора правильно связаны между собой. WhatsApp и браузер не запускаются.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Module = require('module');
const {EventEmitter} = require('events');

const DIR = path.join(__dirname, '..', 'connector');
const PARTS = {
  sending: 'connector_send',
  receiving: 'connector_receive',
  syncing: 'connector_sync',
  whatsappActions: 'connector_actions',
  lifecycle: 'connector_lifecycle',
};
const FILES = ['connector_common', ...Object.values(PARTS), 'whatsapp_qr_connector'];
const sourceOf = (name) => fs.readFileSync(path.join(DIR, `${name}.js`), 'utf8');

function loadConnector() {
  class Client extends EventEmitter {}
  class LocalAuth {}
  class MessageMedia {}
  const stubs = {
    'whatsapp-web.js': {Client, LocalAuth, MessageMedia},
    qrcode: {},
    'qrcode-terminal': {},
  };
  process.env.WEBHOOK_TOKEN = 'test-token-for-connector-parts-0123456789';
  process.env.QUEUE_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'eo-parts-'));
  const original = Module._load;
  Module._load = function load(request, ...rest) {
    return Object.hasOwn(stubs, request) ? stubs[request] : original.call(this, request, ...rest);
  };
  try {
    const common = require('../connector/connector_common');
    const parts = Object.fromEntries(Object.entries(PARTS).map(([alias, file]) => [alias, require(`../connector/${file}`)]));
    return {common, parts};
  } finally {
    Module._load = original;
  }
}

const {common, parts} = loadConnector();

test('every WhatsApp event has exactly one handler', () => {
  const events = ['qr', 'authenticated', 'change_state', 'ready', 'auth_failure', 'disconnected', 'message_revoke_everyone',
    'call', 'incoming_call', 'message_reaction', 'message_ack', 'message_create', 'message'];
  assert.deepEqual([...common.client.eventNames()].sort(), [...events].sort());
  for (const name of events) assert.equal(common.client.listenerCount(name), 1, name);
});

test('references between the parts point at existing functions', () => {
  for (const file of FILES) {
    for (const [, alias, name] of sourceOf(file).matchAll(/(?<![\w.])(sending|receiving|syncing|whatsappActions|lifecycle)\.(\w+)/g)) {
      assert.equal(typeof parts[alias][name], 'function', `${file}: ${alias}.${name}`);
    }
  }
});

test('shared state and imports from the common part exist', () => {
  for (const file of FILES) {
    const source = sourceOf(file);
    for (const [, name] of source.matchAll(/(?<![\w.])runtime\.(\w+)/g)) {
      assert.ok(Object.hasOwn(common.runtime, name), `${file}: runtime.${name}`);
    }
    const imports = source.match(/const \{([^}]+)\} = require\('\.\/connector_common'\)/);
    if (file === 'connector_common') continue;
    assert.ok(imports, file);
    for (const name of imports[1].split(',').map((item) => item.trim()).filter(Boolean)) {
      assert.notEqual(common[name], undefined, `${file}: ${name}`);
    }
  }
});

test('the common part depends on no other part', () => {
  const requires = [...sourceOf('connector_common').matchAll(/require\('\.\/([^']+)'\)/g)].map((match) => match[1]);
  assert.deepEqual(requires.filter((name) => FILES.includes(name)), []);
});

test('media recovered by the server is remembered and the request succeeds', async () => {
  const ids = ['true_77010000001@c.us_RECOVERED1', 'true_77010000001@c.us_RECOVERED2'];
  const answer = {local_media_recovered: ids};
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ok: true, status: 200, json: async () => answer});
  try {
    assert.deepEqual(await common.postJsonOnce('/api/chat-messages-sync', {messages: []}), answer);
  } finally {
    globalThis.fetch = realFetch;
  }
  for (const id of ids) {
    assert.ok(common.locallyRecoveredMediaIds.has(id), id);
    assert.ok(common.isRememberedInternalOutgoing(id), id);
  }
});

test('the connector starts only from its entry point', () => {
  for (const file of FILES.filter((name) => name !== 'whatsapp_qr_connector')) {
    assert.doesNotMatch(sourceOf(file), /client\.initialize\(/, file);
  }
  assert.match(sourceOf('whatsapp_qr_connector'), /client\.initialize\(/);
});
