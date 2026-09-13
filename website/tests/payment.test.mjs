import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { Readable } from 'node:stream';
import { randomUUID } from 'node:crypto';
import { resolve, join, extname } from 'node:path';

const source = (await readFile(new URL('../server.mjs', import.meta.url), 'utf8'))
  .replace(/^import .*;\r?\n/gm, '');

async function fixture(fetch) {
  const published = [];
  const timeouts = [];
  let now = Date.now();
  let timerId = 0;
  const timers = new Map();
  const handlers = {};
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const mqttClient = {
    connected: true,
    on(event, callback) { handlers[event] = callback; },
    publishAsync: async (topic, payload) => {
      published.push(JSON.parse(payload));
      // Model the subscribed backend receiving its own QoS 1 publication.
      if (topic.endsWith('/session')) handlers.message(topic, Buffer.from(payload));
    },
  };
  async function settle() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
  async function advance(ms) {
    const target = now + ms;
    await settle();
    while (true) {
      const next = [...timers.entries()].sort((a,b) => a[1].at-b[1].at)[0];
      if (!next || next[1].at > target) break;
      timers.delete(next[0]);
      now = next[1].at;
      next[1].callback();
      await settle();
    }
    now = target;
    await settle();
  }
  const api = await runInNewContext(`(async () => { ${source}; return {
    uploadReceipt, startSetup, scheduleCurrentState, resetChair,
    getState: () => sessionState,
    setState: value => { sessionState = value; },
  }; })()`, {
    process: { env: { ZIPLINE_TOKEN: 'test-token', GOOGLE_SHEETS_WEBHOOK_URL: 'https://example.test/sheets' } },
    readFile: async () => { throw new Error('No local env in tests'); },
    resolve, join, extname, randomUUID, Buffer, Blob, FormData, fetch, Date: ClockDate,
    AbortSignal: { timeout: ms => { timeouts.push(ms); return AbortSignal.timeout(ms); } },
    mqtt: { connect: () => mqttClient },
    createServer: () => ({ on() {}, listen() {} }),
    console: { log() {}, error() {} },
    setInterval: () => ({ unref() {} }),
    setTimeout: (callback, ms) => { const id = ++timerId; timers.set(id, {callback, at:now+ms}); return id; },
    clearTimeout: id => timers.delete(id),
  });
  async function submit() {
    const request = Readable.from([Buffer.from(JSON.stringify({phone:'0123456789',minutes:10,fileType:'image/png',dataBase64:'dGVzdA=='}))]);
    const response = { writeHead(status) { this.status = status; }, end(body) { this.body = JSON.parse(body); } };
    await api.uploadReceipt(request, response);
    return response;
  }
  return { ...api, submit, published, timeouts, advance, handlers, now: () => now, timers };
}

const uploaded = () => ({ok:true, text:async () => JSON.stringify({url:'https://example.test/receipt.png'})});

test('successful upload bounds both upstream requests and confirms the reservation', async () => {
  let calls = 0;
  const f = await fixture(async () => ++calls === 1 ? uploaded() : {ok:true});
  const response = await f.submit();
  assert.equal(response.status, 201);
  assert.equal(f.getState().confirmed, true);
  assert.deepEqual(f.timeouts, [30000, 20000]);
});

test('storage response body failure releases the reservation and returns an error', async () => {
  const f = await fixture(async () => ({ok:true, text:async () => { throw new DOMException('Timed out', 'TimeoutError'); }}));
  const response = await f.submit();
  assert.equal(response.status, 502);
  assert.equal(f.getState().state, 'idle');
});

test('late Sheets success cannot overwrite a newer session', async () => {
  let calls = 0;
  const f = await fixture(async () => {
    if (++calls === 1) return uploaded();
    f.setState({state:'processing',sessionId:'new-session',updatedAt:new Date().toISOString()});
    return {ok:true};
  });
  const response = await f.submit();
  assert.equal(response.status, 409);
  assert.equal(f.getState().sessionId, 'new-session');
  assert.equal(f.published.some(value => value.confirmed), false);
});

test('late Sheets success cannot confirm an expired reservation before the watchdog runs', async () => {
  let calls = 0;
  const f = await fixture(async () => {
    if (++calls === 1) return uploaded();
    f.setState({...f.getState(),updatedAt:new Date(Date.now()-181000).toISOString()});
    return {ok:true};
  });
  const response = await f.submit();
  assert.equal(response.status, 409);
  assert.equal(f.published.some(value => value.confirmed), false);
});

for (const minutes of [10, 20]) {
  test(`${minutes}-minute session keeps power through setup and full massage, then resets once`, async () => {
    const f = await fixture(async () => { throw new Error('No receipt request expected'); });
    f.setState({version:1,chairId:'chair-1',state:'processing',sessionId:'timer-test',minutes,confirmed:true,updatedAt:new Date(f.now()).toISOString()});
    f.scheduleCurrentState();
    const relayCommands = () => f.published.filter(p => p.method === 'Switch.Set').map(p => p.params.on);
    await f.advance(2000);
    assert.equal(f.getState().state, 'setup');
    assert.deepEqual(relayCommands(), [true]);
    await f.advance(60000);
    assert.equal(f.getState().state, 'active');
    assert.equal(f.getState().phaseEndsAt-f.now(), minutes*60000);
    assert.deepEqual(relayCommands(), [true], 'setup completion must not reset the chair');
    await f.advance(minutes*60000-1);
    assert.equal(f.getState().state, 'active');
    assert.deepEqual(relayCommands(), [true]);
    await f.advance(1);
    assert.deepEqual(relayCommands(), [true,false]);
    await f.advance(10000);
    assert.deepEqual(relayCommands(), [true,false,true]);
    await f.advance(15000);
    assert.equal(f.getState().state, 'idle');
    assert.deepEqual(relayCommands(), [true,false,true,false], 'MQTT echoes must not repeat relay commands');
  });
}

test('early reset request cannot shorten an active session', async () => {
  const f = await fixture();
  f.setState({state:'active',sessionId:'active-test',minutes:10,phaseEndsAt:f.now()+600000});
  await f.resetChair('active-test');
  assert.equal(f.getState().state, 'active');
  assert.equal(f.published.length, 0);
});

test('stale setup callback cannot act on a replacement session', async () => {
  const f = await fixture();
  f.setState({state:'setup',sessionId:'old',minutes:10,phaseEndsAt:f.now()+60000});
  f.scheduleCurrentState();
  const callback = [...f.timers.values()][0].callback;
  f.setState({state:'active',sessionId:'replacement',minutes:20,phaseEndsAt:f.now()+1200000});
  callback();
  await f.advance(0);
  assert.equal(f.getState().sessionId, 'replacement');
  assert.equal(f.published.length, 0);
});

test('retained state from an earlier backend instance still restores relay and timer', async () => {
  const f = await fixture();
  const state = {version:1,chairId:'chair-1',controllerId:'previous-instance',state:'active',sessionId:'recovered',minutes:10,phaseEndsAt:f.now()+600000};
  f.handlers.message('surau/setia-eco-glades/massage-chair/1/session', Buffer.from(JSON.stringify(state)));
  await f.advance(0);
  assert.equal(f.getState().sessionId, 'recovered');
  assert.equal(f.published.filter(p=>p.method==='Switch.Set').length,1);
  assert.equal(f.timers.size,1);
});
