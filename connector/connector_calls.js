'use strict';
// Звонки: отклоняет входящие звонки и отправляет уведомление. При подключении файла регистрирует
// обработчики звонков.

module.exports = {
  startIncomingCallPolling, stopIncomingCallPolling,
};

const {
  runtime, backgroundWorkAllowed, callRejectNoticeAt, client, LANGUAGE_SELECTION_TEXT,
  noteBrowserPressure, postJson, resolveDirectPhoneId, serializedId,
} = require('./connector_common');
const sending = require('./connector_send');

const CALL_REJECT_NOTICE_COOLDOWN_MS = 60000;
const CALL_REJECT_NOTICE_RU = `🛑 Пожалуйста, отправляйте всю информацию **одним сообщением**.
(Голосовые звонки бот не принимает, пишите текстом).
Чтобы вернуться в меню, отправьте 0.`;
const CALL_REJECT_NOTICE_KZ = `🛑 Барлық ақпаратты **бір хабарламамен** жіберіңіз.
(Бот дауыстық қоңырауларды қабылдамайды, мәтінмен жазыңыз).
Мәзірге оралу үшін 0 жіберіңіз.`;

// Резервный контроль звонков. В некоторых текущих сборках WhatsApp Web
// событие whatsapp-web.js не приходит, поэтому дополнительно наблюдаем
// внутреннюю WAWebCallCollection напрямую.
let callPollTimer = null;
let callPollBusy = false;
let callPollBaselineReady = false;
let callPollModuleWarningShown = false;
const callPollKnownIds = new Set();
const processedIncomingCalls = new Map();
const CALL_POLL_INTERVAL_MS = Math.max(10000, Number(process.env.QUEUE_CALL_POLL_INTERVAL_MS || 15000));
const CALL_DEDUP_TTL_MS = 10 * 60 * 1000;

async function rejectIncomingCallRobust(call) {
  // Current WhatsApp Web no longer reliably rejects through the old IQ stanza
  // used by whatsapp-web.js 1.34.7. The working path goes through the VoIP
  // stack itself (same approach as upstream PR #201825).
  let voipAttempted = false;
  let voipRejected = false;
  let voipError = '';
  try {
    voipAttempted = true;
    const result = await client.pupPage.evaluate(async () => {
      try {
        const module = window.require && window.require('WAWebVoipStackInterface');
        const stack = module && typeof module.getVoipStackInterface === 'function'
          ? await module.getVoipStackInterface()
          : null;
        if (!stack || typeof stack.rejectCall !== 'function') {
          return { ok: false, reason: 'WAWebVoipStackInterface.rejectCall unavailable' };
        }
        await stack.rejectCall();
        return { ok: true, reason: '' };
      } catch (error) {
        return { ok: false, reason: String((error && error.message) || error) };
      }
    });
    voipRejected = Boolean(result && result.ok);
    voipError = result && result.reason ? String(result.reason) : '';
    if (!voipRejected && voipError) {
      console.warn('VoIP rejectCall() не сработал:', voipError);
    }
  } catch (error) {
    voipError = String((error && error.message) || error || '');
    console.warn('Не удалось вызвать VoIP rejectCall():', voipError);
  }

  let nativeAttempted = false;
  let nativeError = '';
  if (!voipRejected && call && typeof call.reject === 'function') {
    nativeAttempted = true;
    try {
      await call.reject();
    } catch (error) {
      nativeError = String((error && error.message) || error || '');
      console.warn('Стандартный call.reject() вернул ошибку:', nativeError);
    }
  } else if (!voipRejected) {
    const peerJid =
      serializedId(call && call.from) ||
      serializedId(call && call.peerJid) ||
      String((call && (call.from || call.peerJid)) || '').trim();
    const callId = serializedId(call && call.id) || String((call && call.id) || '').trim();
    if (peerJid && callId) {
      nativeAttempted = true;
      try {
        await client.pupPage.evaluate((peer, id) => {
          if (window.WWebJS && typeof window.WWebJS.rejectCall === 'function') {
            return window.WWebJS.rejectCall(peer, id);
          }
          return false;
        }, peerJid, callId);
      } catch (error) {
        nativeError = String((error && error.message) || error || '');
        console.warn('Прямой WWebJS.rejectCall() вернул ошибку:', nativeError);
      }
    }
  }

  let uiClicked = false;
  if (!voipRejected) {
    for (let attempt = 0; attempt < 20 && !uiClicked; attempt += 1) {
      try {
        uiClicked = await client.pupPage.evaluate(() => {
          const normalize = (value) => String(value || '').trim().toLowerCase();
          const declineWords = [
            'decline', 'decline call', 'reject', 'reject call', 'end call',
            'отклонить', 'отклонить звонок', 'отклонить вызов',
            'сбросить', 'сбросить звонок', 'завершить звонок',
          ];
          const candidates = Array.from(document.querySelectorAll('button,[role="button"]'));
          for (const element of candidates) {
            const rect = element.getBoundingClientRect();
            if (!rect.width || !rect.height) continue;
            const aria = normalize(element.getAttribute('aria-label'));
            const title = normalize(element.getAttribute('title'));
            const text = normalize(element.textContent);
            const iconElement = element.querySelector('[data-icon]');
            const icon = normalize(iconElement && iconElement.getAttribute('data-icon'));
            const byLabel = [aria, title, text].some((label) =>
              declineWords.some((word) => label === word || label.includes(word))
            );
            const byIcon = /(?:call[-_ ]?(?:reject|decline|end)|(?:reject|decline|end)[-_ ]?call)/.test(icon);
            if (byLabel || byIcon) {
              element.click();
              return true;
            }
          }
          return false;
        });
      } catch (error) {
        console.warn('Не удалось проверить кнопку сброса звонка в интерфейсе:', error.message || error);
        break;
      }
      if (!uiClicked) await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
  return { voipAttempted, voipRejected, voipError, nativeAttempted, nativeError, uiClicked };
}

function incomingCallKey(call) {
  const id = serializedId(call && call.id) || String((call && call.id) || '').trim();
  const peer =
    serializedId(call && call.from) ||
    serializedId(call && call.peerJid) ||
    String((call && (call.from || call.peerJid)) || '').trim();
  return id || (peer ? `${peer}:${Number((call && call.timestamp) || 0)}` : '');
}

function markIncomingCallProcessed(call) {
  const now = Date.now();
  for (const [key, timestamp] of processedIncomingCalls) {
    if (now - timestamp > CALL_DEDUP_TTL_MS) processedIncomingCalls.delete(key);
  }
  const key = incomingCallKey(call);
  if (!key) return true;
  if (processedIncomingCalls.has(key)) return false;
  processedIncomingCalls.set(key, now);
  return true;
}

async function handleIncomingCall(call, source = 'event') {
  if (!call || call.fromMe || call.outgoing) return;
  if (!markIncomingCallProcessed(call)) return;

  const callerId =
    serializedId(call.from) ||
    serializedId(call.peerJid) ||
    String(call.from || call.peerJid || '').trim();
  const callKey = incomingCallKey(call);
  console.log(`Получен входящий звонок [${source}]: ${callerId || 'неизвестный'}; id=${callKey || 'нет'}`);

  try {
    const phoneId = await resolveDirectPhoneId(callerId);
    const canonicalChatId = phoneId || callerId;
    const permission = await postJson('/api/call-permission', {
      chat_id: canonicalChatId,
      phone: phoneId ? `+${phoneId.split('@')[0]}` : '',
    });

    if (permission.allowed && permission.manual_contact) {
      console.log(`Разрешён входящий звонок от контакта администратора: ${canonicalChatId}`);
      return;
    }

    const rejectResult = await rejectIncomingCallRobust(call);
    console.log(
      `Отклоняю звонок обычного пользователя: ${canonicalChatId || 'неизвестный'}; ` +
      `voip=${rejectResult.voipRejected ? 'rejected' : 'failed'}; native=${rejectResult.nativeAttempted ? 'yes' : 'no'}; ui=${rejectResult.uiClicked ? 'clicked' : 'not-found'}`
    );

    const now = Date.now();
    const lastNoticeAt = Number(callRejectNoticeAt.get(canonicalChatId) || 0);
    if (!canonicalChatId || now - lastNoticeAt < CALL_REJECT_NOTICE_COOLDOWN_MS) return;
    callRejectNoticeAt.set(canonicalChatId, now);

    const userLanguage = String(permission.language || '').trim().toLowerCase();
    const callReply = userLanguage === 'kz'
      ? CALL_REJECT_NOTICE_KZ
      : (userLanguage === 'ru' ? CALL_REJECT_NOTICE_RU : `${CALL_REJECT_NOTICE_RU}\n\n${LANGUAGE_SELECTION_TEXT}`);
    const replyResult = {
      reply: callReply,
      menu_gate: false,
    };
    await sending.sendAutomaticReply([phoneId, callerId], replyResult);
    console.log(`После отклонения звонка отправлена инструкция на языке пользователя: ${canonicalChatId}`);
  } catch (error) {
    console.error('Не удалось автоматически обработать входящий звонок:', error.message || error);
  }
}

async function readIncomingCallCollection() {
  return client.pupPage.evaluate(() => {
    const serialize = (value) => {
      if (typeof value === 'string') return value;
      if (value && typeof value._serialized === 'string') return value._serialized;
      if (value && typeof value.$1 === 'string') return value.$1;
      return '';
    };
    const snapshotCall = (value, fallbackId = '') => {
      if (!value) return null;
      let state = value.state || value.status || value.callState || '';
      try {
        if (typeof value.getState === 'function') state = value.getState();
      } catch (_) {}
      // State 0 is the ended call state in current WhatsApp Web.
      if (state === 0 || state === '0') return null;
      const id = serialize(value.id) || String(value.id || fallbackId || '').trim();
      const peerJid =
        serialize(value.peerJid) ||
        serialize(value.from) ||
        serialize(value.peer) ||
        serialize(value.chatId) ||
        '';
      if (!id && !peerJid) return null;
      return {
        id,
        peerJid,
        outgoing: Boolean(value.outgoing || value.fromMe),
        isVideo: Boolean(value.isVideo),
        isGroup: Boolean(value.isGroup),
        state: String(state ?? ''),
        timestamp: Number(value.timestamp || value.t || value.offerTime || value.startTime || 0),
      };
    };
    try {
      if (typeof window.require !== 'function') {
        return { ok: false, reason: 'window.require unavailable', calls: [] };
      }
      const collection = window.require('WAWebCallCollection');
      if (!collection) return { ok: false, reason: 'WAWebCallCollection missing', calls: [] };

      // Current WhatsApp Web reports calls via change:activeCall. The old
      // internal Map no longer changes. Install a tiny in-page queue so polling
      // cannot miss the transition before lastActiveCall updates.
      if (!window.__edinayaIncomingCallQueue) window.__edinayaIncomingCallQueue = [];
      if (!window.__edinayaActiveCallListenerInstalled && typeof collection.on === 'function') {
        window.__edinayaActiveCallListenerInstalled = true;
        collection.on('change:activeCall', (call) => {
          const item = snapshotCall(call);
          if (!item) return;
          window.__edinayaIncomingCallQueue.push(item);
          if (window.__edinayaIncomingCallQueue.length > 20) {
            window.__edinayaIncomingCallQueue.splice(0, window.__edinayaIncomingCallQueue.length - 20);
          }
        });
      }

      const calls = [];
      const seen = new Set();
      const add = (item) => {
        if (!item) return;
        const unique = item.id || `${item.peerJid}:${item.timestamp}:${item.state}`;
        if (!unique || seen.has(unique)) return;
        seen.add(unique);
        calls.push(item);
      };

      // Drain captured change:activeCall events.
      const queued = Array.isArray(window.__edinayaIncomingCallQueue)
        ? window.__edinayaIncomingCallQueue.splice(0)
        : [];
      queued.forEach(add);

      // Also inspect the current/last active call for resilience if the listener
      // was installed after ringing had already started.
      add(snapshotCall(collection.activeCall));
      try {
        if (typeof collection.get === 'function') add(snapshotCall(collection.get('activeCall')));
      } catch (_) {}
      add(snapshotCall(collection.attributes && collection.attributes.activeCall));
      add(snapshotCall(collection.lastActiveCall));

      // Legacy fallback for older builds that still insert calls into a Map.
      const maps = [];
      if (collection instanceof Map) maps.push(collection);
      for (const key of Object.keys(collection)) {
        if (collection[key] instanceof Map) maps.push(collection[key]);
      }
      for (const map of maps) {
        for (const [mapKey, value] of map.entries()) {
          add(snapshotCall(value, mapKey));
        }
      }
      return { ok: true, reason: '', calls };
    } catch (error) {
      return { ok: false, reason: String((error && error.message) || error), calls: [] };
    }
  });
}

async function pollIncomingCalls() {
  if (callPollBusy || runtime.shutdownStarted || !runtime.connectorOperational || !backgroundWorkAllowed()) return;
  callPollBusy = true;
  try {
    const snapshot = await readIncomingCallCollection();
    if (!snapshot || !snapshot.ok) {
      if (!callPollModuleWarningShown) {
        callPollModuleWarningShown = true;
        console.warn(`Резервный контроль звонков пока недоступен: ${(snapshot && snapshot.reason) || 'unknown'}`);
      }
      return;
    }
    callPollModuleWarningShown = false;
    const calls = Array.isArray(snapshot.calls) ? snapshot.calls : [];
    if (!callPollBaselineReady) {
      for (const call of calls) {
        const key = incomingCallKey(call);
        if (key) callPollKnownIds.add(key);
      }
      callPollBaselineReady = true;
      console.log(`Резервный контроль звонков активен. Исходный снимок: ${calls.length}.`);
      return;
    }

    for (const call of calls) {
      const key = incomingCallKey(call);
      if (!key || callPollKnownIds.has(key)) continue;
      callPollKnownIds.add(key);
      if (!call.outgoing) await handleIncomingCall(call, 'WAWebCallCollection-poll');
    }
    if (callPollKnownIds.size > 500) {
      const tail = Array.from(callPollKnownIds).slice(-250);
      callPollKnownIds.clear();
      tail.forEach((key) => callPollKnownIds.add(key));
    }
  } catch (error) {
    noteBrowserPressure(error, 'call polling');
    console.warn('Ошибка резервного контроля звонков:', error.message || error);
  } finally {
    callPollBusy = false;
  }
}

function startIncomingCallPolling() {
  if (callPollTimer) return;
  callPollBaselineReady = false;
  callPollKnownIds.clear();
  callPollTimer = setInterval(() => { void pollIncomingCalls(); }, CALL_POLL_INTERVAL_MS);
  setTimeout(() => { void pollIncomingCalls(); }, 250);
}

function stopIncomingCallPolling() {
  if (callPollTimer) {
    clearInterval(callPollTimer);
    callPollTimer = null;
  }
  callPollBusy = false;
  callPollBaselineReady = false;
  callPollKnownIds.clear();
}

client.on('call', (call) => { void handleIncomingCall(call, 'event:call'); });
client.on('incoming_call', (call) => { void handleIncomingCall(call, 'event:incoming_call'); });

