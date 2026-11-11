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
  downloading: 'connector_media',
  calling: 'connector_calls',
  recovering: 'connector_recovery',
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
    const source = sourceOf(file);
    for (const [, alias, name] of source.matchAll(new RegExp(`(?<![\\w.])(${Object.keys(PARTS).join('|')})\\.(\\w+)`, 'g'))) {
      assert.equal(typeof parts[alias][name], 'function', `${file}: ${alias}.${name}`);
      assert.match(source, new RegExp(`const ${alias} = require\\('\\./${PARTS[alias]}'\\)`), `${file}: ${alias} is not required`);
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

async function withFetch(answers, run) {
  const realFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url: String(url), body: JSON.parse(options.body)});
    const status = answers.length > 1 ? answers.shift() : answers[0];
    return {ok: status < 400, status, json: async () => (status < 400 ? {ok: true} : {error: `HTTP ${status}`})};
  };
  try {
    await run();
    return calls;
  } finally {
    globalThis.fetch = realFetch;
  }
}

test('an inbound message rejected by the server is not retried', async () => {
  const calls = await withFetch([400], () => assert.rejects(common.postJsonWithRetry('/api/whatsapp', {external_id: 'x'}), /HTTP 400/));
  assert.equal(calls.length, 1);
});

test('rate limiting of an inbound message is retried', async () => {
  const calls = await withFetch([429, 200], async () => {
    assert.deepEqual(await common.postJsonWithRetry('/api/whatsapp', {external_id: 'x'}), {ok: true});
  });
  assert.equal(calls.length, 2);
});

test('other requests are still not retried', async () => {
  const calls = await withFetch([503], () => assert.rejects(common.postJsonWithRetry('/api/contact-policy', {})));
  assert.equal(calls.length, 1);
});

test('only a real bot answer to a recovered message is sent', async () => {
  const payload = {external_id: 'x', chat_id: 'c'};
  const calls = await withFetch([200], async () => {
    await common.replyToRecovered(payload, {reply: 'Ответ'});
    for (const skip of [{reply: ''}, {reply: 'a', duplicate: true}, {reply: 'a', silent: true}, {reply: 'a', manual_contact: true}]) {
      await common.replyToRecovered(payload, skip);
    }
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.endsWith('/api/inbound-recovery-reply'));
  assert.deepEqual(calls[0].body, {external_id: 'x', chat_id: 'c', reply: 'Ответ'});
});

test('the connector starts only from its entry point', () => {
  for (const file of FILES.filter((name) => name !== 'whatsapp_qr_connector')) {
    assert.doesNotMatch(sourceOf(file), /client\.initialize\(/, file);
  }
  assert.match(sourceOf('whatsapp_qr_connector'), /client\.initialize\(/);
});
