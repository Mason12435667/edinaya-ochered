'use strict';
// node --test tests/ : connector helper modules and that the connector's requires resolve.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const delivery = require('../queue_delivery');
const {InboundSpool, MIN_AGE_MS} = require('../queue_inbound_spool');
const largeMedia = require('../queue_large_media');
const avatars = require('../queue_avatars');

test('every module the connector requires can be resolved', () => {
  const source = fs.readFileSync(path.join(ROOT, 'whatsapp_qr_connector.js'), 'utf8');
  const names = [...new Set([...source.matchAll(/require\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1]))];
  const missing = [];
  for (const name of names) {
    if (/^WAWeb/.test(name)) continue; // resolved inside the WhatsApp Web page, not by Node
    try {
      require.resolve(name.startsWith('.') ? path.join(ROOT, name) : name, {paths: [ROOT]});
    } catch (_) {
      missing.push(name);
    }
  }
  assert.deepEqual(missing, [], 'run `npm ci` first if only npm packages are listed');
});

function fakeClient(behaviour) {
  const calls = [];
  return {
    calls,
    async sendMessage(target, content, options) {
      calls.push(target);
      return behaviour(target, content, options);
    },
  };
}

test('sendOnce does not send when the server refuses to begin', async () => {
  const client = fakeClient(() => ({id: 'x'}));
  const outcome = await delivery.sendOnce({client, recipient: 'a@c.us', content: 'hi', begin: async () => false});
  assert.equal(outcome.status, 'failed');
  assert.deepEqual(client.calls, []);
});

test('sendOnce reports a sent message', async () => {
  const client = fakeClient(() => ({id: {_serialized: 'true_a@c.us_1'}}));
  const outcome = await delivery.sendOnce({client, recipient: 'a@c.us', content: 'hi', begin: async () => true});
  assert.equal(outcome.status, 'sent');
  assert.deepEqual(client.calls, ['a@c.us']);
});

test('an error after begin is uncertain and is never retried on another alias', async () => {
  const client = fakeClient(() => { throw new Error('Protocol error: Target closed'); });
  const outcome = await delivery.sendOnce({
    client, recipient: 'a@c.us', aliases: ['b@lid'], content: 'hi', begin: async () => true,
  });
  assert.equal(outcome.status, 'uncertain');
  assert.deepEqual(client.calls, ['a@c.us']);
});

test('an invalid address moves on to the next alias', async () => {
  const client = fakeClient((target) => {
    if (target === 'a@c.us') throw new Error('wid error: invalid wid');
    return {id: 'ok'};
  });
  const outcome = await delivery.sendOnce({
    client, recipient: 'a@c.us', aliases: ['b@lid'], content: 'hi', begin: async () => true,
  });
  assert.equal(outcome.status, 'sent');
  assert.deepEqual(client.calls, ['a@c.us', 'b@lid']);
});

test('editError gives a readable reason', () => {
  assert.match(delivery.editError({reason: 'not_found'}), /не найдено/);
  assert.match(delivery.editError({}), /Не удалось/);
});

function tempSpool() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'eo-spool-'));
  return {dir, spool: new InboundSpool(dir), files: () => fs.readdirSync(dir).filter((n) => n.endsWith('.json'))};
}

function age(dir, ms) {
  for (const name of fs.readdirSync(dir)) {
    const file = path.join(dir, name);
    const record = JSON.parse(fs.readFileSync(file, 'utf8'));
    record.saved_at -= ms;
    fs.writeFileSync(file, JSON.stringify(record));
  }
}

test('spool keeps a message until the application answered', async () => {
  const {spool, files} = tempSpool();
  const result = await spool.submit({external_id: 'm1', text: 'a'}, async () => ({ok: true}));
  assert.deepEqual(result, {ok: true});
  assert.deepEqual(files(), []);

  const unavailable = Object.assign(new Error('down'), {httpStatus: 503});
  await assert.rejects(spool.submit({external_id: 'm2'}, async () => { throw unavailable; }));
  assert.deepEqual(files(), ['m2.json'], 'a retryable failure keeps the message');

  const rejected = Object.assign(new Error('bad'), {httpStatus: 400});
  await assert.rejects(spool.submit({external_id: 'm3'}, async () => { throw rejected; }));
  assert.deepEqual(files(), ['m2.json'], 'a 4xx answer is not retried forever');
});

test('drain delivers old messages in order and leaves fresh ones to the live path', async () => {
  const {dir, spool, files} = tempSpool();
  spool.enqueue({external_id: 'old1', text: '1'});
  spool.enqueue({external_id: 'old2', text: '2'});
  age(dir, MIN_AGE_MS + 1000);
  spool.enqueue({external_id: 'fresh', text: '3'});
  const sent = [];
  const replied = [];
  const delivered = await spool.drain(async (p) => { sent.push(p.external_id); return {reply: 'ok'}; },
    async (p) => { replied.push(p.external_id); });
  assert.equal(delivered, 2);
  assert.deepEqual(sent.sort(), ['old1', 'old2']);
  assert.deepEqual(replied.sort(), ['old1', 'old2']);
  assert.deepEqual(files(), ['fresh.json']);
});

test('drain stops at the first retryable failure and keeps the rest', async () => {
  const {dir, spool, files} = tempSpool();
  spool.enqueue({external_id: 'a'});
  spool.enqueue({external_id: 'b'});
  age(dir, MIN_AGE_MS + 1000);
  const down = Object.assign(new Error('down'), {httpStatus: 503});
  await assert.rejects(spool.drain(async () => { throw down; }));
  assert.deepEqual(files().sort(), ['a.json', 'b.json']);
});

test('large media is prepared as MessageMedia', async () => {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'eo-media-')), 'scan.pdf');
  fs.writeFileSync(file, Buffer.from('%PDF-1.4 test'));
  class MessageMedia { constructor(mimetype, data, filename, filesize) { Object.assign(this, {mimetype, data, filename, filesize}); } }
  const prepared = await largeMedia.prepare({}, file, MessageMedia, {name: 'scan.pdf', mime: 'application/pdf', size: 13});
  assert.equal(prepared.content.mimetype, 'application/pdf');
  assert.equal(Buffer.from(prepared.content.data, 'base64').toString(), '%PDF-1.4 test');
  assert.equal(typeof prepared.dispose, 'function');
  assert.ok(largeMedia.threshold > 1024 * 1024);
});

test('avatars: missing photo is reported, a chat is not re-synced too often', async () => {
  const posts = [];
  const client = {getProfilePicUrl: async () => ''};
  const postJson = async (endpoint, payload) => { posts.push([endpoint, payload]); return {}; };
  assert.equal(await avatars.sync(client, postJson, ['77010000001@c.us', '77010000001@c.us']), 1);
  assert.deepEqual(posts, [['/api/avatar-sync', {chat_id: '77010000001@c.us', missing: true}]]);
  assert.equal(await avatars.sync(client, postJson, ['77010000001@c.us']), 0);
});
