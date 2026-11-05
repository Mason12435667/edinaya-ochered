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

test('every module the connector requires loads', () => {
  // Loading (not just resolving) also catches modules that throw at import time.
  // No WhatsApp client or browser is started by requiring them.
  const entry = path.join(ROOT, 'whatsapp_qr_connector.js');
  const load = require('node:module').createRequire(entry);
  const source = fs.readFileSync(entry, 'utf8');
  // window.require('WAWeb…') runs inside the WhatsApp Web page, not in Node
  const names = [...new Set([...source.matchAll(/(?<![\w.])require\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1]))];
  const failed = [];
  for (const name of names) {
    try {
      load(name);
    } catch (error) {
      failed.push(`${name}: ${String(error && error.message).split('\n')[0]}`);
    }
  }
  assert.deepEqual(failed, [], 'run `npm ci` first if only npm packages are listed');
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

test('a send that returned nothing is confirmed without sending again', async () => {
  const client = fakeClient(() => undefined);
  const echo = {id: {_serialized: 'true_a@c.us_2'}};
  const outcome = await delivery.sendOnce({
    client, recipient: 'a@c.us', aliases: ['b@lid'], content: 'hi', begin: async () => true,
    confirm: async () => echo,
  });
  assert.equal(outcome.status, 'sent');
  assert.equal(outcome.message, echo);
  assert.deepEqual(client.calls, ['a@c.us']);
});

for (const [name, confirm] of [
  ['finds nothing', async () => null],
  ['fails', async () => { throw new Error('wid error: invalid wid'); }],
]) {
  test(`a send stays uncertain and is not repeated when confirmation ${name}`, async () => {
    const client = fakeClient(() => undefined);
    const outcome = await delivery.sendOnce({
      client, recipient: 'a@c.us', aliases: ['b@lid'], content: 'hi', begin: async () => true, confirm,
    });
    assert.equal(outcome.status, 'uncertain');
    assert.deepEqual(client.calls, ['a@c.us']);
  });
}

test('waitForValue returns a late value and gives up after the timeout', async () => {
  let value = null;
  setTimeout(() => { value = 'row'; }, 10);
  assert.equal(await delivery.waitForValue(async () => value, 500, 5), 'row');
  assert.equal(await delivery.waitForValue(() => null, 20, 5), null);
});

test('findSentMessage finds a message stored under the @lid of a @c.us chat', () => {
  const lookup = (extra = {}) => ({
    text: 'hi', since: 100, aliases: new Set(['1@c.us']), isKnown: () => false, ...extra,
  });
  const row = {id: 'true_9@lid_A', fromMe: true, timestamp: 101, type: 'chat', text: 'hi', remote: '9@lid'};
  const inChat = {...row, remote: '1@c.us'};
  assert.equal(delivery.findSentMessage([row], lookup()), row, 'unknown @lid of the chat');
  assert.equal(delivery.findSentMessage([row, inChat], lookup()), inChat, 'a known address wins');
  assert.equal(delivery.findSentMessage([row, {...row, remote: '8@lid'}], lookup()), null, 'ambiguous');
  assert.equal(delivery.findSentMessage([{...row, remote: '2@c.us'}], lookup()), null, 'another chat');
  assert.equal(delivery.findSentMessage([{...row, timestamp: 99}], lookup()), null, 'too old');
  assert.equal(delivery.findSentMessage([{...row, text: 'other'}], lookup()), null, 'other text');
  assert.equal(delivery.findSentMessage([{...row, fromMe: false}], lookup()), null, 'not ours');
  assert.equal(delivery.findSentMessage([{...row, id: ''}], lookup()), null, 'no id to report');
  assert.equal(delivery.findSentMessage([row], lookup({isKnown: () => true})), null, 'already linked');
  assert.equal(delivery.findSentMessage(undefined, lookup()), null);
});

test('findSentMessage tells media from text', () => {
  const lookup = (extra = {}) => ({text: '', hasMedia: true, since: 100, aliases: new Set(['1@c.us']), isKnown: () => false, ...extra});
  const photo = {id: 'true_9@lid_B', fromMe: true, timestamp: 101, type: 'image', text: '', remote: '9@lid'};
  assert.equal(delivery.findSentMessage([photo], lookup()), photo, 'no caption');
  assert.equal(delivery.findSentMessage([{...photo, text: 'cap'}], lookup({text: 'cap'})).type, 'image', 'caption');
  assert.equal(delivery.findSentMessage([{...photo, type: 'chat'}], lookup()), null, 'text is not media');
  assert.equal(delivery.findSentMessage([photo], lookup({text: 'cap'})), null, 'other caption');
  assert.equal(delivery.findSentMessage([photo], lookup({text: 'hi', hasMedia: false})), null, 'media is not text');
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

test('spool flushes a record to disk before it becomes visible', () => {
  const {spool, files} = tempSpool();
  const steps = [];
  const {fsyncSync, renameSync} = fs;
  fs.fsyncSync = (descriptor) => { steps.push('fsync'); return fsyncSync(descriptor); };
  fs.renameSync = (from, to) => { steps.push('rename'); return renameSync(from, to); };
  try {
    spool.enqueue({external_id: 'durable', text: 'a'});
  } finally {
    Object.assign(fs, {fsyncSync, renameSync});
  }
  assert.deepEqual(steps, process.platform === 'win32' ? ['fsync', 'rename'] : ['fsync', 'rename', 'fsync']);
  assert.deepEqual(files(), ['durable.json']);
});

const wwebjsUtils = path.join(__dirname, '..', 'node_modules', 'whatsapp-web.js', 'src', 'util', 'Injected', 'Utils.js');

test('media fix is applied to the installed whatsapp-web.js', {skip: !fs.existsSync(wwebjsUtils)}, () => {
  const source = fs.readFileSync(wwebjsUtils, 'utf8');
  assert.match(source, /delete message\.__x_id;/);
  assert.ok(source.indexOf('delete message.__x_id;') > source.indexOf('...mediaOptions'));
});
