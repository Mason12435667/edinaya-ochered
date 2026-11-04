'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {sendOnce} = require('../queue_delivery');
const {InboundSpool} = require('../queue_inbound_spool');
const avatars = require('../queue_avatars');

test('all local connector imports exist and load', () => {
  const entry = path.join(__dirname, '..', 'whatsapp_qr_connector.js');
  const source = fs.readFileSync(entry, 'utf8');
  const specs = [...source.matchAll(/(?<![\w.])require\(['"](\.\/[^'"]+)['"]\)/g)].map(m => m[1]);
  assert.ok(specs.length >= 4);
  for (const spec of specs) assert.ok(require(path.resolve(path.dirname(entry), spec)));
});

test('send is called exactly once after a durable begin', async () => {
  const client = new EventEmitter();
  const order = [];
  client.sendMessage = async () => { order.push('send'); return {id: {_serialized: 'sent-id'}}; };
  const result = await sendOnce({client, recipient: '1@c.us', content: 'hello', options: {},
    begin: async () => { order.push('begin'); return true; }, waitMs: 0});
  assert.equal(result.status, 'sent');
  assert.deepEqual(order, ['begin', 'send']);
  assert.equal(client.listenerCount('message_create'), 0);
});

test('an ambiguous send is never retried inside delivery', async () => {
  const client = new EventEmitter(); let sends = 0;
  client.sendMessage = async () => { sends++; throw new Error('transport lost'); };
  const result = await sendOnce({client, recipient: '1@c.us', content: 'hello', options: {}, begin: async () => true, waitMs: 0});
  assert.equal(result.status, 'uncertain'); assert.equal(sends, 1);
  assert.equal(client.listenerCount('message_create'), 0);
});

test('failure to commit begin prevents sending', async () => {
  const client = new EventEmitter();
  client.sendMessage = async () => assert.fail('must not send');
  const result = await sendOnce({client, recipient: '1@c.us', content: 'hello', options: {}, begin: async () => false});
  assert.equal(result.status, 'failed');
});

function spoolFixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'queue-spool-tests-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  return {directory, spool: new InboundSpool(directory)};
}

test('failed inbound submission survives restart and is recovered', async t => {
  const {directory, spool} = spoolFixture(t);
  const payload = {external_id: 'persisted-id', chat_id: '1@c.us', text: 'hello'};
  await assert.rejects(spool.submit(payload, async () => { throw new Error('offline'); }));
  const recovered = new InboundSpool(directory);
  const [{file, job}] = recovered.jobs();
  job.nextAttemptAt = 0; recovered.write(file, job);
  let delivered = 0;
  assert.equal(await recovered.drain(async p => { assert.deepEqual(p, payload); return {reply: 'ok'}; },
    async (p, result) => { assert.equal(result.reply, 'ok'); delivered++; }), 1);
  assert.equal(delivered, 1); assert.deepEqual(recovered.jobs(), []);
});

test('live messages from the same chat are submitted in order', async t => {
  const {spool} = spoolFixture(t); const seen = [];
  const request = async payload => { seen.push(payload.external_id); await new Promise(r => setImmediate(r)); return {ok: true}; };
  await Promise.all(['first', 'second'].map(external_id => spool.submit({external_id, chat_id: '1@c.us'}, request)));
  assert.deepEqual(seen, ['first', 'second']); assert.deepEqual(spool.jobs(), []);
});

test('avatar URLs and image bytes are validated', () => {
  assert.equal(avatars.trusted('https://cdn.whatsapp.net/image').hostname, 'cdn.whatsapp.net');
  for (const source of ['http://cdn.whatsapp.net/x', 'https://whatsapp.net.evil.test/x', 'https://user:pass@whatsapp.net/x']) {
    assert.throws(() => avatars.trusted(source));
  }
  assert.equal(avatars.image(Buffer.from('<html>login</html>')), false);
  assert.equal(avatars.image(Buffer.from([255, 216, 255, 0])), true);
});
