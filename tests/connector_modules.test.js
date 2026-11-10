'use strict';
// node --test tests/ : вспомогательные модули коннектора и проверка, что его зависимости находятся.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const delivery = require('../connector/queue_delivery');
const {InboundSpool, MIN_AGE_MS, isFinalRejection} = require('../connector/queue_inbound_spool');
const largeMedia = require('../connector/queue_large_media');
const avatars = require('../connector/queue_avatars');

test('every module the connector requires loads', () => {
  // Loading (not just resolving) also catches modules that throw at import time.
  // No WhatsApp client or browser is started by requiring them.
  const {dependencies, ENTRY} = require('../scripts/check_connector');
  const load = require('node:module').createRequire(ENTRY);
  const names = dependencies();
  assert.ok(names.includes('whatsapp-web.js') && names.includes('./queue_delivery'));
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
  const row = {id: 'true_9@lid_A', fromMe: true, timestamp: 101, type: 'chat', text: 'hi', remote: '9@lid', phone: '1@c.us'};
  const inChat = {...row, remote: '1@c.us'};
  assert.equal(delivery.findSentMessage([row], lookup()), row, '@lid of the chat');
  assert.equal(delivery.findSentMessage([row, inChat], lookup()), inChat, 'a known address wins');
  assert.equal(delivery.findSentMessage([row, {...row, remote: '8@lid'}], lookup()), null, 'ambiguous');
  assert.equal(delivery.findSentMessage([{...row, remote: '2@c.us', phone: '2@c.us'}], lookup()), null, 'another chat');
  assert.equal(delivery.findSentMessage([{...row, phone: '2@c.us'}], lookup()), null, '@lid of another chat');
  assert.equal(delivery.findSentMessage([{...row, phone: ''}], lookup()), null, '@lid that cannot be tied to the chat');
  assert.equal(delivery.findSentMessage([{...row, timestamp: 99}], lookup()), null, 'too old');
  assert.equal(delivery.findSentMessage([{...row, text: 'other'}], lookup()), null, 'other text');
  assert.equal(delivery.findSentMessage([{...row, fromMe: false}], lookup()), null, 'not ours');
  assert.equal(delivery.findSentMessage([{...row, id: ''}], lookup()), null, 'no id to report');
  assert.equal(delivery.findSentMessage([row], lookup({isKnown: () => true})), null, 'already linked');
  assert.equal(delivery.findSentMessage(undefined, lookup()), null);
});

test('findSentMessage tells media from text', () => {
  const lookup = (extra = {}) => ({text: '', hasMedia: true, since: 100, aliases: new Set(['1@c.us']), isKnown: () => false, ...extra});
  const photo = {id: 'true_9@lid_B', fromMe: true, timestamp: 101, type: 'image', text: '', remote: '9@lid', phone: '1@c.us'};
  assert.equal(delivery.findSentMessage([photo], lookup()), photo, 'no caption');
  assert.equal(delivery.findSentMessage([{...photo, phone: '2@c.us'}], lookup()), null, 'a photo sent to another chat');
  assert.equal(delivery.findSentMessage([{...photo, text: 'cap'}], lookup({text: 'cap'})).type, 'image', 'caption');
  assert.equal(delivery.findSentMessage([{...photo, type: 'chat'}], lookup()), null, 'text is not media');
  assert.equal(delivery.findSentMessage([photo], lookup({text: 'cap'})), null, 'other caption');
  assert.equal(delivery.findSentMessage([photo], lookup({text: 'hi', hasMedia: false})), null, 'media is not text');
});

test('an automatic reply is saved under the chat it was addressed to', () => {
  assert.equal(delivery.replyChatId('1@c.us', '9@lid'), '1@c.us', 'WhatsApp reported an unknown @lid');
  assert.equal(delivery.replyChatId('1@c.us', ''), '1@c.us');
  assert.equal(delivery.replyChatId('9@lid', '1@c.us'), '1@c.us', 'the phone was learned from the sent message');
  assert.equal(delivery.replyChatId('9@lid', ''), '9@lid');
});

test('only a digit or the finish command bypasses the repeat protection', () => {
  assert.equal(delivery.isDeliberateStep('1', false), true);
  assert.equal(delivery.isDeliberateStep('', true), true);
  assert.equal(delivery.isDeliberateStep('', false), false, 'free text before a language is chosen');
  assert.equal(delivery.isDeliberateStep('9', false), false);
  assert.equal(delivery.isDeliberateStep(undefined, undefined), false);
});

test('a forwarded copy is recorded with its attachment', () => {
  const text = delivery.forwardedCopyItem({id: 'true_1@c.us_A', actor: 'Анна', body: 'hi', timestamp: 50});
  assert.deepEqual(text, {
    id: 'true_1@c.us_A', from_me: true, sender: 'Анна', body: 'hi', type: 'chat', ack: 1, notify: false,
    forwarded: true, timestamp: 50,
  });
  const photo = delivery.forwardedCopyItem({
    id: 'true_1@c.us_B', actor: 'Анна', body: '', type: 'image', timestamp: 51,
    media: {mimetype: 'image/png', data: 'QUJD', filename: ''},
  });
  assert.equal(photo.media_base64, 'QUJD');
  assert.equal(photo.media_mime, 'image/png');
  assert.equal(photo.media_name, 'WhatsApp image');
  assert.equal(photo.forwarded, true);
});

test('a failure to save a sent message is reported but does not fail the action', async () => {
  const reported = [];
  assert.equal(await delivery.savedAfterSend(async () => {}, (error) => reported.push(error)), true);
  assert.equal(await delivery.savedAfterSend(async () => { throw new Error('HTTP 503'); }, (error) => reported.push(error.message)), false);
  assert.deepEqual(reported, ['HTTP 503']);
});

test('only a missing forward module allows a resend', () => {
  assert.ok(delivery.isForwardUnavailable(new Error("Cannot read properties of undefined (reading 'forwardMessages')")));
  assert.equal(delivery.isForwardUnavailable(new Error('Protocol error: Target closed')), false);
  assert.equal(delivery.isForwardUnavailable(null), false);
});

test('a text is resent once and marked as forwarded', async () => {
  const sent = [];
  const client = fakeClient((target, content, options) => { sent.push({content, options}); return {id: 'ok'}; });
  const outcome = await delivery.resendAsForward({client, target: 'a@c.us', text: 'hi'});
  assert.equal(outcome.status, 'sent');
  assert.deepEqual(client.calls, ['a@c.us']);
  assert.equal(sent[0].content, 'hi');
  assert.deepEqual(sent[0].options.extra, {isForwarded: true, forwardingScore: 1});
});

test('media is downloaded and resent with its caption', async () => {
  const media = {mimetype: 'image/png'};
  const sent = [];
  const client = fakeClient((target, content, options) => { sent.push({content, options}); return {id: 'ok'}; });
  await delivery.resendAsForward({
    client, hasMedia: true, type: 'document', download: async () => media, target: 'a@c.us', text: 'cap',
  });
  assert.equal(sent[0].content, media);
  assert.equal(sent[0].options.caption, 'cap');
  assert.equal(sent[0].options.sendMediaAsDocument, true);
});

for (const [type, option] of [['ptt', 'sendAudioAsVoice'], ['sticker', 'sendMediaAsSticker']]) {
  test(`a resent ${type} keeps its kind`, async () => {
    const sent = [];
    const client = fakeClient((target, content, options) => { sent.push(options); return {id: 'ok'}; });
    await delivery.resendAsForward({client, hasMedia: true, type, download: async () => ({}), target: 'a@c.us', text: ''});
    assert.equal(sent[0][option], true);
  });
}

test('nothing is sent when there is no content to forward', async () => {
  const client = fakeClient(() => ({id: 'ok'}));
  await assert.rejects(delivery.resendAsForward({client, target: 'a@c.us', text: ''}), /нет текста/);
  await assert.rejects(delivery.resendAsForward({
    client, hasMedia: true, download: async () => null, target: 'a@c.us', text: '',
  }), /вложение/);
  assert.deepEqual(client.calls, []);
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
  assert.deepEqual(sent, ['old1', 'old2']);
  assert.deepEqual(replied, ['old1', 'old2']);
  assert.deepEqual(files(), ['fresh.json']);
});

const httpError = (status) => Object.assign(new Error(`HTTP ${status}`), {httpStatus: status});
const tick = () => new Promise((resolve) => setImmediate(resolve));
const savedAt = (dir, name) => JSON.parse(fs.readFileSync(path.join(dir, `${name}.json`), 'utf8')).saved_at;

function gate() {
  let open;
  const opened = new Promise((resolve) => { open = resolve; });
  return {opened, open};
}

test('a failed recovery keeps the arrival order for the next attempt', async () => {
  const {dir, spool, files} = tempSpool();
  spool.enqueue({external_id: 'first'});
  spool.enqueue({external_id: 'second'});
  age(dir, MIN_AGE_MS + 1000);
  await assert.rejects(spool.drain(async () => { throw httpError(503); }));
  assert.deepEqual(files().sort(), ['first.json', 'second.json']);
  const sent = [];
  await spool.drain(async (p) => { sent.push(p.external_id); return {}; });
  assert.deepEqual(sent, ['first', 'second']);
  assert.deepEqual(files(), []);
});

test('a retry updates the record but keeps its place in the queue', async () => {
  const {dir, spool} = tempSpool();
  spool.enqueue({external_id: 'm', text: 'short'});
  const before = savedAt(dir, 'm');
  await new Promise((resolve) => setTimeout(resolve, 5));
  await assert.rejects(spool.submit({external_id: 'm', text: 'complete'}, async () => { throw httpError(503); }));
  const record = JSON.parse(fs.readFileSync(path.join(dir, 'm.json'), 'utf8'));
  assert.equal(record.saved_at, before);
  assert.equal(record.payload.text, 'complete');
});

test('messages saved in the same millisecond keep their order', () => {
  const {dir, spool} = tempSpool();
  const {now} = Date;
  Date.now = () => 1000;
  try {
    for (const id of ['a', 'b', 'c']) spool.enqueue({external_id: id});
  } finally {
    Date.now = now;
  }
  assert.ok(savedAt(dir, 'a') < savedAt(dir, 'b') && savedAt(dir, 'b') < savedAt(dir, 'c'));
});

for (const status of [400, 413, 422]) {
  test(`HTTP ${status} is final and removes the message`, async () => {
    const {spool, files} = tempSpool();
    await assert.rejects(spool.submit({external_id: 'm'}, async () => { throw httpError(status); }));
    assert.deepEqual(files(), []);
    assert.equal(isFinalRejection(httpError(status)), true);
  });
}

for (const status of [401, 403, 404, 408, 425, 429, 500, 503]) {
  test(`HTTP ${status} keeps the message for a retry`, async () => {
    const {spool, files} = tempSpool();
    await assert.rejects(spool.submit({external_id: 'm'}, async () => { throw httpError(status); }));
    assert.deepEqual(files(), ['m.json']);
    assert.equal(isFinalRejection(httpError(status)), false);
  });
}

test('a network failure keeps the message for a retry', async () => {
  const {spool, files} = tempSpool();
  await assert.rejects(spool.submit({external_id: 'm'}, async () => { throw new Error('ECONNREFUSED'); }));
  assert.deepEqual(files(), ['m.json']);
});

test('one chat sends one message at a time, in order', async () => {
  const {spool} = tempSpool();
  const first = gate();
  const started = [];
  const send = async (p) => {
    started.push(p.external_id);
    if (p.external_id === 'first') await first.opened;
    return {id: p.external_id};
  };
  const one = spool.submit({external_id: 'first', chat_id: 'c'}, send);
  const two = spool.submit({external_id: 'second', chat_id: 'c'}, send);
  await tick();
  assert.deepEqual(started, ['first'], 'the second message waits');
  first.open();
  assert.deepEqual(await Promise.all([one, two]), [{id: 'first'}, {id: 'second'}]);
  assert.deepEqual(started, ['first', 'second']);
});

test('a slow chat does not hold up another chat', async () => {
  const {spool} = tempSpool();
  const slow = gate();
  const started = [];
  const send = async (p) => {
    started.push(p.external_id);
    if (p.chat_id === 'slow') await slow.opened;
    return {};
  };
  const blocked = spool.submit({external_id: 'a', chat_id: 'slow'}, send);
  await spool.submit({external_id: 'b', chat_id: 'fast'}, send);
  assert.deepEqual(started, ['a', 'b']);
  slow.open();
  await blocked;
});

test('a failed message does not block the next task of its chat forever', async () => {
  const {spool, files} = tempSpool();
  await assert.rejects(spool.submit({external_id: 'bad', chat_id: 'c'}, async () => { throw httpError(400); }));
  assert.deepEqual(await spool.submit({external_id: 'next', chat_id: 'c'}, async () => ({ok: true})), {ok: true});
  assert.deepEqual(files(), []);
  await tick();
  assert.equal(spool.chatLines.size, 0, 'finished chats are forgotten');
});

test('a new message cannot overtake one that failed before it', async () => {
  const {spool, files} = tempSpool();
  await assert.rejects(spool.submit({external_id: 'first', chat_id: 'c'}, async () => { throw httpError(503); }));
  const sent = [];
  const replied = [];
  const result = await spool.submit({external_id: 'second', chat_id: 'c'},
    async (p) => { sent.push(p.external_id); return {reply: p.external_id}; },
    async (p, r) => { replied.push([p.external_id, r.reply]); });
  assert.deepEqual(sent, ['first', 'second']);
  assert.deepEqual(result, {reply: 'second'}, 'the caller gets the answer to its own message');
  assert.deepEqual(replied, [['first', 'first']], 'only the recovered message is answered here');
  assert.deepEqual(files(), []);
});

test('a new message stays behind an older one that still fails', async () => {
  const {spool, files} = tempSpool();
  await assert.rejects(spool.submit({external_id: 'first', chat_id: 'c'}, async () => { throw httpError(503); }));
  const attempted = [];
  await assert.rejects(spool.submit({external_id: 'second', chat_id: 'c'}, async (p) => { attempted.push(p.external_id); throw httpError(503); }));
  assert.deepEqual(attempted, ['first'], 'the new message is not sent');
  assert.deepEqual(files().sort(), ['first.json', 'second.json']);
  const third = [];
  await spool.submit({external_id: 'third', chat_id: 'c'}, async (p) => { third.push(p.external_id); return {}; });
  assert.deepEqual(third, ['first', 'second', 'third'], 'a held message is older than the next one too');
});

test('a message left by a previous run goes before a new one of its chat', async () => {
  const {dir, spool, files} = tempSpool();
  spool.enqueue({external_id: 'old', chat_id: 'c'});
  age(dir, MIN_AGE_MS + 1000);
  const sent = [];
  await spool.submit({external_id: 'new', chat_id: 'c'}, async (p) => { sent.push(p.external_id); return {}; });
  assert.deepEqual(sent, ['old', 'new']);
  assert.deepEqual(files(), []);
});

test('a fresh message of the same chat is left to its own live path', async () => {
  const {spool, files} = tempSpool();
  spool.enqueue({external_id: 'waiting', chat_id: 'c'}); // saved, its request is still being prepared
  const sent = [];
  await spool.submit({external_id: 'new', chat_id: 'c'}, async (p) => { sent.push(p.external_id); return {}; });
  assert.deepEqual(sent, ['new']);
  assert.deepEqual(files(), ['waiting.json']);
});

test('a message does not wait for one that arrived after it', async () => {
  const {spool, files} = tempSpool();
  spool.enqueue({external_id: 'earlier', chat_id: 'c'}); // saved first, its request is still being prepared
  await assert.rejects(spool.submit({external_id: 'later', chat_id: 'c'}, async () => { throw httpError(503); }));
  const sent = [];
  await spool.submit({external_id: 'earlier', chat_id: 'c'}, async (p) => { sent.push(p.external_id); return {}; });
  assert.deepEqual(sent, ['earlier']);
  assert.deepEqual(files(), ['later.json']);
});

test('other chats are not delivered by a new message', async () => {
  const {dir, spool, files} = tempSpool();
  spool.enqueue({external_id: 'other', chat_id: 'x'});
  age(dir, MIN_AGE_MS + 1000);
  const sent = [];
  await spool.submit({external_id: 'new', chat_id: 'c'}, async (p) => { sent.push(p.external_id); return {}; });
  assert.deepEqual(sent, ['new']);
  assert.deepEqual(files(), ['other.json']);
});

test('an older message rejected for good does not stop the new one', async () => {
  const {dir, spool, files} = tempSpool();
  spool.enqueue({external_id: 'bad', chat_id: 'c'});
  age(dir, MIN_AGE_MS + 1000);
  const sent = [];
  await spool.submit({external_id: 'new', chat_id: 'c'}, async (p) => {
    sent.push(p.external_id);
    if (p.external_id === 'bad') throw httpError(400);
    return {};
  });
  assert.deepEqual(sent, ['bad', 'new']);
  assert.deepEqual(files(), []);
});

test('a failed reply to a recovered message does not fail the new one', async (t) => {
  t.mock.method(console, 'warn', () => {});
  const {dir, spool, files} = tempSpool();
  spool.enqueue({external_id: 'old', chat_id: 'c'});
  age(dir, MIN_AGE_MS + 1000);
  const result = await spool.submit({external_id: 'new', chat_id: 'c'}, async () => ({ok: true}), async () => { throw new Error('no reply'); });
  assert.deepEqual(result, {ok: true});
  assert.deepEqual(files(), []);
});

test('recovery does not resend what the live path already delivered', async () => {
  const {dir, spool, files} = tempSpool();
  spool.enqueue({external_id: 'old', chat_id: 'c'});
  age(dir, MIN_AGE_MS + 1000);
  const sent = [];
  const send = async (p) => { sent.push(p.external_id); return {}; };
  const live = spool.submit({external_id: 'new', chat_id: 'c'}, send);
  const drained = spool.drain(send); // saw 'old' before the live path took it
  await live;
  assert.equal(await drained, 0);
  assert.deepEqual(sent, ['old', 'new']);
  assert.deepEqual(files(), []);
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
