'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../frontend/static/http.js'), 'utf8');
function browser(fetcher = async () => new Response('{"ok":true}', {headers:{'content-type':'application/json'}})) {
  const calls = [];
  const window = {fetch: async (...args) => { calls.push(args); return fetcher(...args); }};
  const context = vm.createContext({window, URL, Request, Headers, Promise, Set,
    location:{href:'https://queue.example/whatsapp', origin:'https://queue.example'},
    document:{querySelector: () => ({content:'test-csrf'})}});
  vm.runInContext(source, context);
  return {window, context, calls};
}

test('same-origin writes receive CSRF without mutating caller headers', async () => {
  const {window, calls} = browser();
  const headers = new Headers({'X-Caller':'value'});
  const init = {method:'POST', headers, body:'original'};
  await window.fetch('/api/chat-send', init);
  assert.equal(calls[0][1].headers.get('X-CSRF-Token'), 'test-csrf');
  assert.equal(calls[0][1].headers.get('X-Caller'), 'value');
  assert.equal(calls[0][1].body, 'original');
  assert.equal(headers.has('X-CSRF-Token'), false);
});

test('Request inputs keep method, body and explicit tokens', async () => {
  const {window, calls} = browser();
  const request = new Request('https://queue.example/api/action',
    {method:'PUT', body:'request body', headers:{'X-CSRF-Token':'explicit'}});
  await window.fetch(request);
  assert.equal(calls[0][0], request);
  assert.equal(calls[0][1].headers.get('X-CSRF-Token'), 'explicit');
  assert.equal(await request.text(), 'request body');
});

test('read requests and foreign origins receive no automatic token', async () => {
  const {window, calls} = browser();
  await window.fetch('/api/state');
  await window.fetch('/api/state', {method:'HEAD'});
  await window.fetch('https://foreign.example/action', {method:'POST'});
  for (const [,init] of calls) assert.equal(new Headers(init.headers).has('X-CSRF-Token'), false);
});

test('two observers and the caller can each consume the response', async () => {
  const {window} = browser();
  const observed = [];
  const pending = [];
  for (let i=0; i<2; i++) window.QueueHTTP.subscribe({
    match: ({url}) => url.pathname === '/api/state',
    response: ({response}) => { const result=response.json().then(data=>observed.push(data)); pending.push(result); return result; },
  });
  const result = await window.fetch('/api/state');
  assert.deepEqual(await result.json(), {ok:true});
  await Promise.all(pending);
  assert.deepEqual(observed, [{ok:true}, {ok:true}]);
});

test('observer failures never turn a successful operation into a failed request', async () => {
  const {window, calls} = browser();
  window.QueueHTTP.subscribe({match:()=>{throw Error('bad predicate');}, response:()=>{}});
  window.QueueHTTP.subscribe({response:()=>{throw Error('render failed');}});
  window.QueueHTTP.subscribe({response:async()=>{throw Error('async render failed');}});
  assert.deepEqual(await (await window.fetch('/api/state')).json(), {ok:true});
  assert.equal(calls.length, 1);
});

test('abort signals and the original error propagate without a retry', async () => {
  const controller = new AbortController();
  controller.abort();
  const reason = new Error('aborted');
  const {window,calls} = browser(async (input, init) => { assert.equal(init.signal, controller.signal); throw reason; });
  let observed;
  window.QueueHTTP.subscribe({error: ({error}) => { observed=error; }});
  await assert.rejects(window.fetch('/api/state', {signal:controller.signal}), error=>error===reason);
  assert.equal(observed, reason);
  assert.equal(calls.length, 1);
});

test('unsubscribed and unmatched observers do not consume response copies', async () => {
  const response = new Response('bytes');
  response.clone = () => assert.fail('must not clone an unmatched download');
  const {window} = browser(async()=>response);
  const cancel = window.QueueHTTP.subscribe({response:()=>assert.fail('unsubscribed')});
  cancel();
  window.QueueHTTP.subscribe({match:()=>false, response:()=>assert.fail('unmatched')});
  assert.equal(await (await window.fetch('/api/download')).text(), 'bytes');
});

test('loading the adapter twice preserves subscriptions and a single fetch call', async () => {
  const {window, context, calls} = browser();
  const adapter = window.fetch;
  let observed = 0;
  window.QueueHTTP.subscribe({response:()=>{observed++;}});
  vm.runInContext(source, context);
  assert.equal(window.fetch, adapter);
  await window.fetch('/api/state');
  assert.equal(calls.length, 1);
  assert.equal(observed, 1);
});
