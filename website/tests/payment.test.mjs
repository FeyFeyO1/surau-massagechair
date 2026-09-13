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
  const mqttClient = { connected: true, on() {}, publishAsync: async (topic, payload) => published.push(JSON.parse(payload)) };
  const api = await runInNewContext(`(async () => { ${source}; return {
    uploadReceipt,
    getState: () => sessionState,
    setState: value => { sessionState = value; },
  }; })()`, {
    process: { env: { ZIPLINE_TOKEN: 'test-token', GOOGLE_SHEETS_WEBHOOK_URL: 'https://example.test/sheets' } },
    readFile: async () => { throw new Error('No local env in tests'); },
    resolve, join, extname, randomUUID, Buffer, Blob, FormData, fetch,
    AbortSignal: { timeout: ms => { timeouts.push(ms); return AbortSignal.timeout(ms); } },
    mqtt: { connect: () => mqttClient },
    createServer: () => ({ on() {}, listen() {} }),
    console: { log() {}, error() {} },
    setInterval: () => ({ unref() {} }),
    setTimeout: () => 1, clearTimeout() {},
  });
  async function submit() {
    const request = Readable.from([Buffer.from(JSON.stringify({phone:'0123456789',minutes:10,fileType:'image/png',dataBase64:'dGVzdA=='}))]);
    const response = { writeHead(status) { this.status = status; }, end(body) { this.body = JSON.parse(body); } };
    await api.uploadReceipt(request, response);
    return response;
  }
  return { ...api, submit, published, timeouts };
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
