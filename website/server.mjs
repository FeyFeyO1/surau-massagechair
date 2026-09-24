import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import mqtt from 'mqtt';

const root = resolve('.');
await loadLocalEnv();

const port = Number(process.env.PORT || 3003);
const ziplineUrl = (process.env.ZIPLINE_URL || 'https://app.duniapalsu.com').replace(/\/$/, '');
const ziplineToken = process.env.ZIPLINE_TOKEN;
const googleSheetsWebhookUrl = process.env.GOOGLE_SHEETS_WEBHOOK_URL;
const mqttUrl = process.env.MQTT_URL || 'mqtt://minecraft.duniapalsu.com';
const mqttTopic = process.env.MQTT_TOPIC || 'surau/setia-eco-glades/massage-chair/1/session';
const shellyMqttPrefix = process.env.SHELLY_MQTT_PREFIX || 'surau/setia-eco-glades/massage-chair/1/shelly-1pm';
const shellySwitchId = Number(process.env.SHELLY_SWITCH_ID || 0);
const processingTimeoutMs = Number(process.env.MQTT_PROCESSING_TIMEOUT_MS || 180000);
const deadlineGraceMs = Number(process.env.MQTT_DEADLINE_GRACE_MS || 30000);
const setupDelayMs = Number(process.env.SETUP_DELAY_MS || 2000);
const setupDurationMs = Number(process.env.SETUP_DURATION_MS || 60_000);
const chairResetOffMs = Number(process.env.CHAIR_RESET_OFF_MS || 10_000);
const chairResetOnMs = Number(process.env.CHAIR_RESET_ON_MS || 30_000);
const shellyCommandTopic = `${shellyMqttPrefix.replace(/\/+$/, '')}/rpc`;
const controllerId = `surau-payment-${randomUUID()}`;
const mqttClient = mqtt.connect(mqttUrl, {
  clientId: controllerId,
  username: process.env.MQTT_USERNAME || undefined,
  password: process.env.MQTT_PASSWORD || undefined,
  reconnectPeriod: 2000,
  clean: true,
});
let sessionState = idleState('backend-startup');
let watchdogResetInProgress = false;
let phaseTimer = null;
let resetInProgress = false;

mqttClient.on('connect', () => {
  console.log(`MQTT connected to ${mqttUrl}; session topic: ${mqttTopic}`);
  mqttClient.subscribe(mqttTopic, { qos: 1 }, (error) => {
    if (error) console.error('MQTT subscription failed:', error);
  });
  recoverRelayState().catch((error) => console.error('Could not restore relay state:', error.message));
});
mqttClient.on('message', (topic, payload) => {
  if (topic !== mqttTopic) return;
  try {
    const next = JSON.parse(payload.toString('utf8'));
    // Local transitions already update state and control the relay. Ignore our
    // broker echo; retained state from a previous server instance still recovers.
    if (next?.controllerId === controllerId) return;
    if (isSessionState(next)) {
      sessionState = next;
      scheduleCurrentState();
      if (next.state === 'setup' || next.state === 'active') {
        setRelay(true).catch((error) => console.error('Could not restore active relay state:', error.message));
      } else if (next.state === 'idle') {
        setRelay(false).catch((error) => console.error('Could not restore idle relay state:', error.message));
      }
    } else if (next?.chairId === 'chair-1') {
      console.error('Resetting invalid retained MQTT session state:', next);
      sessionState = next;
      resetStaleSession('invalid-mqtt-state');
    }
  } catch (error) {
    console.error('Ignored invalid MQTT session payload:', error.message);
  }
});
mqttClient.on('error', (error) => console.error('MQTT error:', error.message));

setInterval(() => {
  if (!mqttClient.connected || sessionState.state === 'idle' || resetInProgress) return;
  const now = Date.now();
  if (sessionState.state === 'processing') {
    const updatedAt = Date.parse(sessionState.updatedAt);
    if (!Number.isFinite(updatedAt) || now - updatedAt > processingTimeoutMs) {
      resetStaleSession('processing-timeout');
    }
    return;
  }
  const phaseEndsAt = Number(sessionState.phaseEndsAt);
  if (!Number.isFinite(phaseEndsAt)) {
    resetStaleSession(`${sessionState.state}-missing-deadline`);
  } else if (sessionState.state === 'setup' && now > phaseEndsAt) {
    startActiveSession(sessionState.sessionId, sessionState.minutes).catch(reportSessionError);
  } else if (sessionState.state === 'active' && now > phaseEndsAt) {
    resetChair(sessionState.sessionId).catch(reportSessionError);
  } else if (now > phaseEndsAt + deadlineGraceMs) {
    resetStaleSession(`${sessionState.state}-deadline-expired`);
  }
}, 5000).unref();
const allowedTypes = new Set(['image/png', 'image/jpeg', 'application/pdf']);
const mimeTypes = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.png': 'image/png',
  '.bmp': 'image/bmp', '.ico': 'image/x-icon',
};

const server = createServer(async (request, response) => {
  try {
    if (request.method === 'GET' && request.url === '/api/health') {
      return json(response, 200, { ok: true, service: 'surau-payment', mqttConnected: mqttClient.connected });
    }
    if (request.method === 'GET' && request.url === '/api/session') {
      return json(response, 200, publicSessionState());
    }
    if (request.method === 'POST' && request.url === '/api/receipts') {
      await uploadReceipt(request, response);
      return;
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') return json(response, 405, { error: 'Method not allowed.' });
    await serveStatic(request, response);
  } catch (error) {
    console.error(error);
    json(response, 500, { error: 'The server could not complete this request.' });
  }
});

async function uploadReceipt(request, response) {
  if (!ziplineToken || !googleSheetsWebhookUrl) return json(response, 503, { error: 'Receipt storage is not configured.' });
  const body = await readJson(request, 15 * 1024 * 1024);
  const submittedPhone = String(body.phone || '');
  if (!/^\d{10,15}$/.test(submittedPhone)) return json(response, 400, { error: 'Phone number must contain only 10 to 15 digits.' });
  const phone = submittedPhone;
  const minutes = Number(body.minutes);
  const fileType = String(body.fileType || '');
  if (![10, 20].includes(minutes)) return json(response, 400, { error: 'Invalid session duration.' });
  if (!allowedTypes.has(fileType)) return json(response, 400, { error: 'Please upload a PNG, JPG or PDF receipt.' });

  const fileBuffer = Buffer.from(String(body.dataBase64 || ''), 'base64');
  if (!fileBuffer.length || fileBuffer.length > 10 * 1024 * 1024) return json(response, 400, { error: 'Receipt must be smaller than 10 MB.' });
  if (!mqttClient.connected) return json(response, 503, { error: 'The chair service is temporarily unavailable. Please try again.' });
  if (sessionState.state !== 'idle') return json(response, 409, { error: 'The massage chair is currently in use. Please wait for the countdown to finish.' });

  const sessionId = randomUUID();
  const processingState = {
    version: 1,
    chairId: 'chair-1',
    state: 'processing',
    sessionId,
    minutes,
    phaseEndsAt: null,
    updatedAt: new Date().toISOString(),
    source: 'website',
  };
  // Reserve the single chair immediately so a second receipt cannot race this one.
  sessionState = processingState;
  try {
    await publishSession(processingState);
  } catch (error) {
    sessionState = idleState('mqtt-publish-failed');
    console.error('Could not reserve chair over MQTT:', error);
    return json(response, 503, { error: 'The chair could not be reserved. Please try again.' });
  }
  const extension = fileType === 'application/pdf' ? 'pdf' : fileType === 'image/png' ? 'png' : 'jpg';
  const safePhone = phone.replace(/\D/g, '');
  const filename = `receipt-${minutes}min-${safePhone}-${Date.now()}.${extension}`;
  const form = new FormData();
  form.append('file', new Blob([fileBuffer], { type: fileType }), filename);

  let upstream;
  let responseText;
  try {
    upstream = await fetch(`${ziplineUrl}/api/upload`, {
      method: 'POST',
      headers: { authorization: ziplineToken, 'x-zipline-filename': filename, 'x-zipline-original-name': 'true' },
      body: form,
      signal: AbortSignal.timeout(30_000),
    });
    responseText = await upstream.text();
  } catch (error) {
    console.error('Could not reach Zipline:', error);
    await releaseFailedSession(sessionId);
    return json(response, 502, { error: 'The server could not connect to receipt storage. Please try again shortly.' });
  }
  if (!upstream.ok) {
    console.error('Zipline upload failed:', upstream.status, responseText);
    await releaseFailedSession(sessionId);
    return json(response, 502, { error: 'Receipt upload failed. Please try again.' });
  }
  let uploaded;
  try { uploaded = JSON.parse(responseText); } catch { uploaded = { url: responseText.trim() }; }
  const receiptUrl = uploaded.files?.[0]?.url || uploaded.url || null;
  if (!receiptUrl) {
    await releaseFailedSession(sessionId);
    return json(response, 502, { error: 'Receipt was uploaded, but no receipt URL was returned.' });
  }

  const price = minutes === 10 ? 5 : 10;
  const sheetsPayload = JSON.stringify({ phone, minutes, price, receiptUrl });
  let sheetsResponse = null;
  let sheetsError = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    if (!ownsProcessingSession(sessionId)) break;
    try {
      sheetsResponse = await fetch(googleSheetsWebhookUrl, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: sheetsPayload,
        signal: AbortSignal.timeout(20_000),
      });
      if (sheetsResponse.ok) break;
      sheetsError = `HTTP ${sheetsResponse.status}: ${await sheetsResponse.text()}`;
    } catch (error) {
      sheetsError = error;
    }
    if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, attempt * 400));
  }
  if (!sheetsResponse?.ok) {
    console.error('Google Sheets webhook failed after 3 attempts:', sheetsError);
    await releaseFailedSession(sessionId);
    return json(response, 502, { error: 'Receipt was uploaded, but its details could not be recorded. Please contact the administrator.' });
  }

  if (!ownsProcessingSession(sessionId)) {
    return json(response, 409, { error: 'The chair reservation expired while recording your receipt. Please contact the administrator before submitting again.' });
  }

  const confirmedState = {
    ...processingState,
    confirmed: true,
    updatedAt: new Date().toISOString(),
  };
  sessionState = confirmedState;
  try {
    await publishSession(confirmedState);
    scheduleSetup(sessionId, minutes);
  } catch (error) {
    console.error('Receipt succeeded, but confirmation publish failed:', error);
    await releaseFailedSession(sessionId);
    return json(response, 503, { error: 'Payment was recorded, but the chair could not be started. Please contact the administrator.' });
  }

  json(response, 201, { ok: true, file: receiptUrl, session: publicSessionState() });
}

function ownsProcessingSession(sessionId) {
  return sessionState.state === 'processing' && sessionState.sessionId === sessionId
    && Date.now() - Date.parse(sessionState.updatedAt) < processingTimeoutMs;
}

function idleState(source = 'pi') {
  return {
    version: 1, chairId: 'chair-1', state: 'idle', sessionId: null,
    minutes: 0, phaseEndsAt: null, updatedAt: new Date().toISOString(), source,
  };
}

function isSessionState(value) {
  return value && value.version === 1 && value.chairId === 'chair-1'
    && ['idle', 'processing', 'setup', 'active'].includes(value.state)
    && Number.isFinite(Number(value.minutes))
    && (value.state === 'idle' || [10, 20].includes(Number(value.minutes)));
}

function publicSessionState() {
  return { ...sessionState, mqttConnected: mqttClient.connected, topic: mqttTopic };
}

async function publishSession(value) {
  await mqttClient.publishAsync(mqttTopic, JSON.stringify({ ...value, controllerId }), { qos: 1, retain: true });
}

async function setRelay(on) {
  const payload = {
    id: Date.now(),
    src: 'surau-payment-website',
    method: 'Switch.Set',
    params: { id: shellySwitchId, on },
  };
  await mqttClient.publishAsync(shellyCommandTopic, JSON.stringify(payload), { qos: 1, retain: false });
  console.log(`Shelly relay ${on ? 'on' : 'off'} published to ${shellyCommandTopic}`);
}

function scheduleSetup(sessionId, minutes) {
  clearPhaseTimer();
  phaseTimer = setTimeout(() => startSetup(sessionId, minutes).catch(reportSessionError), setupDelayMs);
}

async function startSetup(sessionId, minutes) {
  if (sessionState.state !== 'processing' || !sessionState.confirmed || sessionState.sessionId !== sessionId) return;
  const setup = sessionPayload('setup', sessionId, minutes, Date.now() + setupDurationMs);
  await setRelay(true);
  sessionState = setup;
  await publishSession(setup);
  scheduleCurrentState();
}

async function startActiveSession(sessionId, minutes) {
  if (sessionState.state !== 'setup' || sessionState.sessionId !== sessionId) return;
  const active = sessionPayload('active', sessionId, minutes, Date.now() + minutes * 60_000);
  sessionState = active;
  await publishSession(active);
  scheduleCurrentState();
}

async function resetChair(sessionId) {
  if (resetInProgress || sessionState.state !== 'active' || sessionState.sessionId !== sessionId) return;
  if (Date.now() < Number(sessionState.phaseEndsAt)) return;
  resetInProgress = true;
  clearPhaseTimer();
  try {
    await setRelay(false);
    await delay(chairResetOffMs);
    await setRelay(true);
    await delay(chairResetOnMs);
    await setRelay(false);
    const idle = idleState('website-session-complete');
    sessionState = idle;
    await publishSession(idle);
  } finally {
    resetInProgress = false;
  }
}

function scheduleCurrentState() {
  clearPhaseTimer();
  if (sessionState.state === 'processing' && sessionState.confirmed) {
    scheduleSetup(sessionState.sessionId, sessionState.minutes);
    return;
  }
  if (!['setup', 'active'].includes(sessionState.state)) return;
  const deadline = Number(sessionState.phaseEndsAt);
  if (!Number.isFinite(deadline)) return;
  const delayMs = Math.max(0, deadline - Date.now());
  const { state, sessionId, minutes, phaseEndsAt } = sessionState;
  phaseTimer = setTimeout(() => {
    // A callback belongs only to the phase for which it was scheduled.
    if (sessionState.state !== state || sessionState.sessionId !== sessionId
      || sessionState.phaseEndsAt !== phaseEndsAt) return;
    if (state === 'setup') {
      startActiveSession(sessionId, minutes).catch(reportSessionError);
    } else if (state === 'active') {
      resetChair(sessionId).catch(reportSessionError);
    }
  }, delayMs);
}

function clearPhaseTimer() {
  if (phaseTimer) clearTimeout(phaseTimer);
  phaseTimer = null;
}

async function recoverRelayState() {
  if (sessionState.state === 'setup' || sessionState.state === 'active') await setRelay(true);
  if (sessionState.state === 'idle') await setRelay(false);
}

function sessionPayload(state, sessionId, minutes, phaseEndsAt) {
  return {
    version: 1, chairId: 'chair-1', state, sessionId, minutes, phaseEndsAt,
    updatedAt: new Date().toISOString(), source: 'website',
  };
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function reportSessionError(error) {
  console.error('Session controller error:', error);
  resetStaleSession('website-controller-error');
}

async function releaseFailedSession(sessionId) {
  if (sessionState.sessionId !== sessionId) return;
  const idle = idleState('backend-error');
  clearPhaseTimer();
  sessionState = idle;
  try {
    await setRelay(false);
    await publishSession(idle);
  } catch (error) { console.error('Could not release failed session:', error); }
}

async function resetStaleSession(source) {
  if (watchdogResetInProgress || sessionState.state === 'idle') return;
  watchdogResetInProgress = true;
  const previous = sessionState;
  const idle = idleState(source);
  clearPhaseTimer();
  sessionState = idle;
  try {
    await setRelay(false);
    await publishSession(idle);
    console.log(`MQTT watchdog reset ${previous.state} session ${previous.sessionId || '(none)'} to idle: ${source}`);
  } catch (error) {
    sessionState = previous;
    console.error('MQTT watchdog could not publish idle state:', error);
  } finally {
    watchdogResetInProgress = false;
  }
}

async function serveStatic(request, response) {
  const rawPath = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
  const relativePath = rawPath === '/' ? 'index.html' : rawPath.replace(/^\/+/, '');
  const filePath = resolve(join(root, relativePath));
  if (!filePath.startsWith(root)) return json(response, 403, { error: 'Forbidden.' });
  try {
    const content = await readFile(filePath);
    response.writeHead(200, {
      'content-type': mimeTypes[extname(filePath).toLowerCase()] || 'application/octet-stream',
      'cache-control': 'no-store, no-cache, must-revalidate',
    });
    response.end(request.method === 'HEAD' ? undefined : content);
  } catch {
    json(response, 404, { error: 'Not found.' });
  }
}

async function readJson(request, maxBytes) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw new Error('Request is too large.');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function json(response, status, value) {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  response.end(JSON.stringify(value));
}

async function loadLocalEnv() {
  try {
    const contents = await readFile(join(root, '.env'), 'utf8');
    for (const line of contents.split(/\r?\n/)) {
      const match = line.match(/^([A-Z_][A-Z0-9_]*)=(.*)$/);
      if (match && process.env[match[1]] === undefined) process.env[match[1]] = match[2];
    }
  } catch {}
}

server.on('error', (error) => {
  if (error.code === 'EADDRINUSE') {
    console.error(`Port ${port} is already in use. The payment server may already be running.`);
    process.exit(1);
  }
  throw error;
});

server.listen(port, () => console.log(`Surau payment site running at http://localhost:${port}`));
