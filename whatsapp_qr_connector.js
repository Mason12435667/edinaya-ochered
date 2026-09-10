'use strict';

const fs = require('fs');
const avatarSync = require('./queue_avatars');
const largeMedia = require('./queue_large_media');
const delivery = require('./queue_delivery');
const path = require('path');
const qrcode = require('qrcode-terminal');
const QRCode = require('qrcode');
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');

const ROOT = __dirname;
const API_URL = process.env.TICKET_API_URL || 'http://127.0.0.1:8000';
const WEBHOOK_TOKEN = String(process.env.WEBHOOK_TOKEN || '').trim();
if (WEBHOOK_TOKEN.length < 32) {
  console.error('WEBHOOK_TOKEN не настроен. Храните секреты вне проекта: /etc/edinaya-ochered/secrets.env');
  process.exit(1);
}
const USER_MESSAGE_TYPES = new Set([
  'chat',
  'image',
  'video',
  'audio',
  'ptt',
  'document',
  'sticker',
  'location',
  'vcard',
  'multi_vcard',
  'list_response',
  'buttons_response',
]);

const MENU_ROWS = [
  { id: 'topic_seal', title: '1. Навигационная пломба' },
  { id: 'topic_transport', title: '2. Перевозка' },
  { id: 'topic_bin', title: '3. Корректировка БИН' },
  { id: 'topic_keden', title: '4. Keden' },
  { id: 'topic_package', title: '5. Пакеты' },
  { id: 'topic_database', title: '6. База данных' },
  { id: 'topic_mobile', title: '7. Мобилка' },
  { id: 'topic_general', title: '8. Другая проблема' },
  { id: 'topic_support', title: '9. Вопрос в поддержку' },
];

const MENU_CHOICE_NUMBERS = {
  topic_seal: '1',
  topic_transport: '2',
  topic_bin: '3',
  topic_keden: '4',
  topic_package: '5',
  topic_database: '6',
  topic_mobile: '7',
  topic_general: '8',
  topic_support: '9',
  support_text: '1',
};

const SUPPORT_ROWS = [
  { id: 'support_text', title: '1. Написать вопрос' },
];

const START_MENU_TEXT = `Это автоматическая система регистрации заявок.

Чтобы открыть меню заявок и выбрать нужную проблему, отправьте цифру 1.

Пока вы не отправите 1, меню заявок не появится и описание заявки система не обработает.`;

const MAIN_MENU_TEXT = `Меню заявок

1. Навигационная пломба / НП
2. Перевозка
3. Корректировка БИН
4. Keden
5. Пакеты
6. База данных
7. Мобилка
8. Другая проблема
9. Задать вопрос в поддержку

Отправьте только номер нужного пункта, например 1, 2 или 3.
После выбора система покажет понятный шаблон и подскажет, как правильно заполнить заявку.

Чтобы вернуться к этому меню позже, отправьте 0 или слово «меню».`;

async function postJson(endpoint, payload) {
  if (endpoint === '/api/chat-messages-sync' && Array.isArray(payload.messages)) {
    for (const item of payload.messages) {
      if (!item.media_base64 || item.media_base64.length <= 1024*1024) continue;
      try {
        const base64=item.media_base64, total=Buffer.byteLength(base64,'base64');
        // 524286 is divisible by three, so base64 slices end on complete groups.
        const step=524286;
        let offset=0, receipt='';
        while(offset<total) {
          const end=Math.min(total,offset+step);
          const chunk=base64.slice(offset/3*4, Math.ceil(end/3)*4);
          const result=await postJson('/api/media-stage',{chat_id:payload.chat_id,message_id:item.id,total,offset,data:chunk});
          if(Number(result.offset)<=offset)throw new Error('Media transfer stalled');
          offset=Number(result.offset); receipt=result.receipt || receipt;
        }
        item.media_receipt=receipt; item.media_pending=false; delete item.media_base64;
      } catch(error) {
        delete item.media_base64; item.media_pending=true;
        pendingMediaMessages.set(String(item.id), {chat_id:payload.chat_id,sender:item.sender || '',sender_phone:item.sender_phone || '',sender_id:item.sender_id || '',from_me:!!item.from_me,body:item.body || '',type:item.type,timestamp:item.timestamp,attempts:0,next_at:Date.now()+10000});
        console.warn('Вложение будет загружено повторно:',error.message || error);
      }
    }
  }
  const response = await fetch(`${API_URL}${endpoint}`, {
    method: 'POST',
    signal: AbortSignal.timeout(30000),
    headers: {
      'Content-Type': 'application/json',
      'X-Webhook-Token': WEBHOOK_TOKEN,
    },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  if (endpoint === '/api/chat-messages-sync' && Array.isArray(data.local_media_recovered)) {
    const until = Date.now() + 60 * 60 * 1000;
    for (const recoveredId of data.local_media_recovered) {
      const id = String(recoveredId || '').trim();
      if (!id) continue;
      locallyRecoveredMediaIds.set(id, until);
      pendingMediaMessages.delete(id);
      outgoingMediaProbe.delete(id);
      rememberInternalOutgoingMessageId(id, 60 * 60 * 1000);
      console.log(`Локальное clipboard-вложение восстановлено без повторного скачивания WhatsApp: ${id}`);
    }
  }
  return data;
}

function serializedId(value) {
  if (typeof value === 'string') return value;
  if (value && typeof value._serialized === 'string') return value._serialized;
  // Recent WhatsApp Web builds may rename _serialized to $1.
  if (value && typeof value.$1 === 'string') return value.$1;
  // Some internal Wid objects expose only user/server.
  if (value && value.user && value.server) return `${value.user}@${value.server}`;
  return '';
}

// WhatsApp Web 2.3000+ sometimes exposes Wid.$1 instead of Wid._serialized.
// whatsapp-web.js still reads only _serialized inside Message.downloadMedia(),
// so copy the canonical value onto the live object before calling the library.
// Keep this best-effort because some internal Wid objects are frozen proxies.
function normalizeMessageIdObject(message) {
  if (!message) return '';
  const id = message.id || (message._data && message._data.id);
  const sid = serializedId(id);
  if (!sid || !id || typeof id !== 'object') return sid;
  try { if (typeof id._serialized !== 'string') id._serialized = sid; } catch (_) {}
  try {
    if (message._data && message._data.id && typeof message._data.id === 'object' &&
        typeof message._data.id._serialized !== 'string') {
      message._data.id._serialized = sid;
    }
  } catch (_) {}
  return sid;
}

async function resolveDirectPhoneId(value) {
  const directId = String(value || '').trim();
  if (directId.endsWith('@c.us')) return directId;
  if (lidToPhone.has(directId)) return lidToPhone.get(directId);
  if (directId.endsWith('@lid') && typeof client.getContactLidAndPhone === 'function') {
    try {
      const matches = await Promise.race([client.getContactLidAndPhone([directId]), new Promise((resolve)=>setTimeout(()=>resolve([]),2500))]);
      const row = matches && matches[0];
      const resolved = row ? serializedId(row.pn) : '';
      if (resolved.endsWith('@c.us')) { lidToPhone.set(directId, resolved); return resolved; }
    } catch (error) { console.warn('Не удалось сопоставить WhatsApp LID с номером:', error.message || error); }
  }
  return '';
}

async function resolvePhoneId(message, from) {
  const candidates = [
    from,
    serializedId(message._data && message._data.from),
    serializedId(message._data && message._data.author),
    serializedId(message._data && message._data.id && message._data.id.remote),
  ];
  const phoneId = candidates.find((value) => value.endsWith('@c.us'));
  if (phoneId) return phoneId;

  // Новые версии WhatsApp иногда отдают приватный LID вместо номера.
  return await resolveDirectPhoneId(from);
}

const client = new Client({
  authStrategy: new LocalAuth({
    clientId: 'ticket-prototype',
    dataPath: process.env.QUEUE_WHATSAPP_AUTH_DIR || '/var/lib/edinaya-ochered/whatsapp-auth',
  }),
  authTimeoutMs: 180000,
  puppeteer: {
    headless: true,
    protocolTimeout: 180000,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  },
});

let outboundTimer = null;
let outboundBusy = false;
let heartbeatTimer = null;
let groupSyncTimer = null;
let performanceTimer = null;
let highMemorySamples = 0;
const PERF_CONTACT_COOLDOWN_MS = Math.max(15000, Number(process.env.QUEUE_CONTACT_SYNC_COOLDOWN_MS || 60000));
const PERF_RECOVERY_INTERVAL_MS = Math.max(900, Number(process.env.QUEUE_RECOVERY_INTERVAL_MS || 1500));
const PERF_RAM_RESTART_MB = Math.max(0, Number(process.env.QUEUE_CONNECTOR_RAM_RESTART_MB || 0));
let readyFallbackTimer = null;
let readyFallbackStartedAt = 0;
let connectorReadyAt = 0;
let connectorOperational = false;
let shutdownStarted = false;
let heartbeatErrorReported = false;
let lastGroupRefreshRequest = '';
let lastRequestedGroupId = '';
let lastGroupParticipantSyncAt = 0;
let lastContactDiscoveryAt = 0;
let lastProfileSyncChatId = '';
let lastProfileSyncAt = 0;
let lastRecentPollAt = 0;
let lastPresenceSyncAt = 0;
let lastPresenceSignature = '';
let presenceSyncBusy = false;
let fastInboundTimer = null;
let contactDiscoveryBusy = false;
let recentPollBusy = false;
let recentOutgoingBusy = false;
let pendingMediaBusy = false;
let fastSyncBusy = false;
let participantSyncBusy = false;
let groupIdentityRepairBusy = false;
const groupIdentityRepairAt = new Map();
let profileSyncBusy = false;
const lidToPhone = new Map();
const reconciledInboundIds = new Map();
// 1.00.2: separate "currently being handled" from "successfully handled".
// Previously an incoming ID was marked reconciled before the async group path
// finished; one transient WhatsApp/Puppeteer error could therefore suppress all
// recovery attempts until the user opened the Groups page.
const inboundProcessingIds = new Map();
const participantDisplayById = new Map();
const ownMentionIds = new Set();
const knownChats = new Map();
// Human-readable names reported by WhatsApp. Kept separately so an outgoing
// system/employee message can never rename the remote contact to "Система".
const contactDisplayById = new Map();
const pendingMediaMessages = new Map();
const locallyRecoveredMediaIds = new Map();
// QUEUE_OUTGOING_MEDIA_PROBE_3_3_63
const outgoingMediaProbe = new Map();
let outgoingMediaProbeBusy = false;
const messageAcknowledgements = new Map();
const persistedAcknowledgements = new Map();
// ID исходящих сообщений, которые отправил сам коннектор. Они уже сохраняются
// отдельной логикой и не должны повторно определяться как ручные сообщения,
// отправленные сотрудником с рабочего телефона или WhatsApp Web.
const internalOutgoingMessageIds = new Map();
const internalOutgoingStanzaIds = new Map();
// ID ручных исходящих, уже синхронизированных в «Единую очередь». Карта нужна
// не только для события message_create, но и для резервной проверки коллекции
// WhatsApp Web, если браузер пропустил live-событие.
const reconciledOutgoingIds = new Map();
// Отдельно отмечаем ручные исходящие, у которых бинарное медиа уже сохранено.
const reconciledOutgoingMediaIds = new Map();
// WhatsApp обычно помечает пересланные сообщения через isForwarded/forwardingScore.
// Для сообщений, пересланных прямо из «Единой очереди», дополнительно запоминаем
// точный ID результата forward(), чтобы метка «Переслано» не потерялась даже
// если текущая версия WhatsApp Web временно не отдала флаг в live-событии.
const forwardedMessageIds = new Map();
// Recently checked saved messages for WhatsApp reply metadata. This lets a
// newly installed patch repair recent replies that were saved by an older build
// without quote information, without importing any unknown chat history.
const quoteProbeAt = new Map();
let quoteRepairBusy = false;
// Keep exact Message objects for recent live/outgoing messages. Actions such as
// delete/forward are much more reliable when they operate on the same object
// WhatsApp Web just emitted instead of trying to reconstruct it later from an
// @c.us id while the current browser session internally uses @lid.
const recentMessageObjects = new Map();
const chatIdAliases = new Map();
const seenSentAt = new Map();
let markReadBusy = false;
const RECENT_MESSAGE_CACHE_TTL_MS = 45 * 60 * 1000;

function rememberChatAlias(canonicalChatId, rawChatId) {
  const canonical = String(canonicalChatId || '').trim();
  const raw = String(rawChatId || '').trim();
  if (!canonical || !raw || canonical === 'status@broadcast' || raw === 'status@broadcast') return;
  const values = chatIdAliases.get(canonical) || new Set();
  values.add(canonical);
  values.add(raw);
  chatIdAliases.set(canonical, values);
  // Also make reverse lookups possible when a later event arrives under @lid.
  const reverse = chatIdAliases.get(raw) || new Set();
  reverse.add(raw);
  reverse.add(canonical);
  chatIdAliases.set(raw, reverse);
}

function rememberMessageObject(message, canonicalChatId = '') {
  if (!message) return '';
  const messageId = serializedId(message.id);
  const now = Date.now();
  if (messageId) recentMessageObjects.set(messageId, { message, at: now });
  const rawIds = [
    serializedId(message.from),
    serializedId(message.to),
    serializedId(message._data && message._data.from),
    serializedId(message._data && message._data.to),
    serializedId(message.id && message.id.remote),
    serializedId(message._data && message._data.id && message._data.id.remote),
  ].filter(Boolean);
  for (const raw of rawIds) rememberChatAlias(canonicalChatId || raw, raw);
  if (canonicalChatId) rememberChatAlias(canonicalChatId, canonicalChatId);
  if (recentMessageObjects.size > 700) {
    const cutoff = now - RECENT_MESSAGE_CACHE_TTL_MS;
    for (const [id, entry] of recentMessageObjects) {
      if (Number(entry && entry.at || 0) < cutoff) recentMessageObjects.delete(id);
    }
    while (recentMessageObjects.size > 600) {
      const first = recentMessageObjects.keys().next().value;
      if (!first) break;
      recentMessageObjects.delete(first);
    }
  }
  return messageId;
}

function cachedMessageObject(messageId) {
  const safe = String(messageId || '').trim();
  if (!safe) return null;
  const entry = recentMessageObjects.get(safe);
  if (!entry) return null;
  if (Date.now() - Number(entry.at || 0) > RECENT_MESSAGE_CACHE_TTL_MS) {
    recentMessageObjects.delete(safe);
    return null;
  }
  return entry.message || null;
}

function rememberForwardedMessage(message, ttlMs = 60 * 60 * 1000) {
  const messageId = serializedId(message && message.id);
  if (messageId) forwardedMessageIds.set(messageId, Date.now() + ttlMs);
  return messageId;
}

function isRememberedForwarded(messageId) {
  const safe = String(messageId || '').trim();
  const until = Number(forwardedMessageIds.get(safe) || 0);
  if (!until) return false;
  if (Date.now() <= until) return true;
  forwardedMessageIds.delete(safe);
  return false;
}

function internalMessageStanza(value, message = null) {
  const direct = String(
    (message && message.id && message.id.id) ||
    (message && message._data && message._data.id && message._data.id.id) || ''
  ).trim();
  if (direct) return direct;
  const safe = String(value || '').trim();
  const match = safe.match(/^(?:true|false)_.+_([A-Za-z0-9-]{8,})$/);
  return match ? String(match[1] || '') : (safe.includes('@') ? '' : safe);
}

function rememberInternalOutgoingMessage(message, ttlMs = 2 * 60 * 60 * 1000) {
  const messageId = normalizeMessageIdObject(message) || serializedId(message && message.id);
  const until = Date.now() + ttlMs;
  if (messageId) internalOutgoingMessageIds.set(messageId, until);
  const stanza = internalMessageStanza(messageId, message);
  if (stanza) internalOutgoingStanzaIds.set(stanza, until);
  return messageId;
}

function isRememberedInternalOutgoing(messageId) {
  const safe = String(messageId || '').trim();
  const now = Date.now();
  const until = Number(internalOutgoingMessageIds.get(safe) || 0);
  if (until) {
    if (now <= until) return true;
    internalOutgoingMessageIds.delete(safe);
  }
  const stanza = internalMessageStanza(safe);
  const stanzaUntil = Number(internalOutgoingStanzaIds.get(stanza) || 0);
  if (stanzaUntil) {
    if (now <= stanzaUntil) return true;
    internalOutgoingStanzaIds.delete(stanza);
  }
  return false;
}

function cleanupOutgoingTracking(now = Date.now()) {
  for (const [id, until] of internalOutgoingMessageIds) {
    if (Number(until || 0) < now) internalOutgoingMessageIds.delete(id);
  }
  for (const [id, until] of internalOutgoingStanzaIds) {
    if (Number(until || 0) < now) internalOutgoingStanzaIds.delete(id);
  }
  for (const [id, at] of reconciledOutgoingIds) {
    if (now - Number(at || 0) > 60 * 60 * 1000) reconciledOutgoingIds.delete(id);
  }
  for (const [id, until] of forwardedMessageIds) {
    if (Number(until || 0) < now) forwardedMessageIds.delete(id);
  }
}

// Защита от шквала сообщений в личных чатах. Группы сюда не попадают:
// там автоответы и так полностью отключены.
const FLOOD_WINDOW_MS = 5000;
const FLOOD_MESSAGE_LIMIT = 3;
const FLOOD_QUIET_MS = 5000;
const FLOOD_NOTICE_COOLDOWN_MS = 60000;
const FLOOD_NOTICE_TEXT = `Это автоматическая система регистрации заявок.

Вы отправили несколько сообщений подряд, поэтому автоматические ответы временно приостановлены, чтобы не создавать лишнюю переписку.

Пожалуйста, дождитесь ответа системы. После выбора темы заполните полученный шаблон целиком и отправьте данные одним сообщением. Не отправляйте строки шаблона по отдельности.

Чтобы начать заново, отправьте 0.`;
const inboundFloodState = new Map();
// Повтор одинакового автоответа не отправляется снова сразу. Это особенно
// важно для людей, которые пишут несколько произвольных сообщений вместо
// выбора пункта меню.
const autoReplyCooldowns = new Map();
// Отдельная защита от гонки: одно входящее событие иногда приходит почти
// одновременно через обычный event и резервную синхронизацию. Пока первый
// автоответ ещё отправляется, второй обработчик не должен отправить тот же
// текст повторно. Короткий exact-cooldown дополнительно ловит повтор сразу
// после завершения первой отправки.
const autoReplyInflight = new Set();
const autoReplyExactCooldowns = new Map();
const AUTO_REPLY_EXACT_COOLDOWN_MS = 8000;
const AUTO_REPLY_MENU_COOLDOWN_MS = 45000;
const AUTO_REPLY_HINT_COOLDOWN_MS = 15000;
// Помечаем сообщения, которые отправила сама система. Событие message_create
// также приходит на исходящие сообщения, и без этой метки автоответ системы
// ошибочно включал бы «ручной диалог» на 30 минут.
const internalOutgoingUntil = new Map();
let activeInternalOutgoingFingerprint = null;
function markInternalOutgoing(chatId, milliseconds = 8000) {
  const safe = String(chatId || '').trim();
  if (safe) internalOutgoingUntil.set(safe, Date.now() + milliseconds);
}
function isInternalOutgoing(chatId) {
  const safe = String(chatId || '').trim();
  const until = Number(internalOutgoingUntil.get(safe) || 0);
  if (!until) return false;
  if (Date.now() <= until) return true;
  internalOutgoingUntil.delete(safe);
  return false;
}
function markActiveInternalOutgoing(queued, aliases = [], milliseconds = 35000) {
  activeInternalOutgoingFingerprint = {
    queue_id: Number((queued && queued.id) || 0),
    // QUEUE_3_3_95_AUTHORITATIVE_CHAT_ID
    // The web UI queued the message for this exact chat. WhatsApp may echo the
    // same private chat as @lid, but that transport alias must never create a
    // second card in our UI.
    chat_id: String((queued && queued.chat_id) || '').trim(),
    body: String((queued && queued.body) || '').trim(),
    media_name: String((queued && queued.media_name) || '').trim(),
    media_mime: String((queued && queued.media_mime) || '').trim(),
    actor: String((queued && queued.actor) || 'Вы').trim(),
    reply_to_key: String((queued && queued.reply_to_key) || '').trim(),
    quoted_body: String((queued && queued.reply_preview_body) || '').trim().slice(0, 1200),
    quoted_sender: String((queued && queued.reply_preview_sender) || '').trim().slice(0, 100),
    has_media: Boolean(queued && queued.media_path),
    aliases: Array.from(new Set((aliases || []).map((value) => String(value || '').trim()).filter(Boolean))),
    until: Date.now() + milliseconds,
  };
}
function matchesActiveInternalOutgoing(message, rawChatId = '') {
  const marker = activeInternalOutgoingFingerprint;
  if (!marker || Date.now() > Number(marker.until || 0)) {
    activeInternalOutgoingFingerprint = null;
    return false;
  }
  const raw = String(rawChatId || '').trim();
  const liveBody = String(incomingText(message) || '').trim();
  const expectedBody = String(marker.body || '').trim();
  const liveName = String((message && message._data && message._data.filename) || '').trim();

  // WhatsApp Web often emits message_create for a pasted screenshot before
  // type/hasMedia/filename are populated. Do NOT reject that early event merely
  // because it temporarily looks like a plain chat message. During one outbound
  // queue send we already know the recipient aliases and caption, which are the
  // reliable identity signals at this stage.
  if (expectedBody && liveBody && liveBody !== expectedBody) return false;
  if (marker.media_name && liveName && liveName !== marker.media_name && !liveName.startsWith('clipboard-')) return false;
  if (raw && marker.aliases.includes(raw)) return true;
  if (expectedBody && liveBody === expectedBody) return true;
  if (marker.has_media && !expectedBody && !liveBody) return true;
  return false;
}

// Входящие звонки от обычных пользователей всегда отклоняются. Чтобы один
// человек не мог заставить систему заспамить его автоответами повторными
// звонками, уведомление отправляется не чаще одного раза в минуту.
const CALL_REJECT_NOTICE_COOLDOWN_MS = 60000;
const CALL_REJECT_NOTICE_TEXT = `Звонки для обычных пользователей отключены.

Пожалуйста, напишите сообщение в этот чат. Это автоматическая система регистрации заявок, поэтому выберите нужную тему и следуйте подсказке системы.`;
const callRejectNoticeAt = new Map();

// Резервный контроль звонков. В некоторых текущих сборках WhatsApp Web
// событие whatsapp-web.js не приходит, поэтому дополнительно наблюдаем
// внутреннюю WAWebCallCollection напрямую.
let callPollTimer = null;
let callPollBusy = false;
let callPollBaselineReady = false;
let callPollModuleWarningShown = false;
const callPollKnownIds = new Set();
const processedIncomingCalls = new Map();
const CALL_POLL_INTERVAL_MS = 700;
const CALL_DEDUP_TTL_MS = 10 * 60 * 1000;

function isLiveInboundMessage(message) {
  const timestamp = Number((message && message.timestamp) || 0);
  const now = Math.floor(Date.now() / 1000);
  if (!connectorReadyAt || !timestamp) return false;
  return timestamp >= connectorReadyAt && timestamp <= now + 30;
}

function messageText(message) {
  const body = incomingText(message);
  if (body) return body;
  const labels = {
    image: '[Фото]',
    video: '[Видео]',
    audio: '[Аудио]',
    ptt: '[Голосовое сообщение]',
    document: '[Документ]',
    sticker: '[Стикер]',
  };
  return labels[String((message && message.type) || '')] || '[Сообщение]';
}

function incomingText(message) {
  const selected = String(
    (message && (message.selectedRowId || message.selectedButtonId)) || ''
  ).trim();
  if (/^[1-9]$/.test(selected)) return selected;
  const raw=message?._data || {};
  const media=['image','video','audio','ptt','document','sticker'].includes(String(message?.type || ''));
  const body=String((message && message.body) || '').trim();
  if (media && (raw.caption || raw.mediaData?.caption)) return String(raw.caption || raw.mediaData.caption).trim();
  // Thumbnails sometimes occupy body in internal media models. Never expose them as text.
  if (media && (body.startsWith('data:') || (body.length>200 && /^[A-Za-z0-9+/=\s]+$/.test(body)))) return '';
  return body;
}

function incomingMenuChoice(message) {
  const selected = String(
    (message && (message.selectedRowId || message.selectedButtonId)) || ''
  ).trim();
  const dynamicChoice = selected.match(/^menu_(\d{1,2})$/);
  return MENU_CHOICE_NUMBERS[selected] || (dynamicChoice ? dynamicChoice[1] : '') || (/^\d{1,2}$/.test(selected) ? selected : '');
}

function liveMessageItem(message, fromMe = false, bodyOverride = '', notify = false) {
  const messageId = serializedId(message && message.id);
  const eventAck = Number((message && message.ack) || 0);
  const rememberedAck = Number(messageAcknowledgements.get(messageId) || 0);
  return {
    id: messageId,
    from_me: Boolean(fromMe),
    body: String(bodyOverride || messageText(message)).trim(),
    type: String((message && message.type) || 'chat'),
    timestamp: Number((message && message.timestamp) || Math.floor(Date.now() / 1000)),
    ack: Math.max(0, eventAck, rememberedAck),
    notify: Boolean(notify),
    edited: Boolean(message && (message.latestEditSenderTimestampMs || message._data?.latestEditSenderTimestampMs)),
    edit_timestamp: Number(message?.latestEditSenderTimestampMs || message?._data?.latestEditSenderTimestampMs || 0),
    forwarded: Boolean(
      (message && message.isForwarded) ||
      (message && message._data && message._data.isForwarded) ||
      Number((message && message.forwardingScore) || (message && message._data && message._data.forwardingScore) || 0) > 0 ||
      isRememberedForwarded(messageId)
    ),
  };
}

async function directQuoteSnapshotById(messageId) {
  const mid = String(messageId || '').trim();
  if (!mid || !client || !client.pupPage) return null;
  try {
    return await Promise.race([
      client.pupPage.evaluate(async (wanted) => {
        const widText = (value) => {
          if (!value) return '';
          if (typeof value === 'string') return value;
          if (typeof value._serialized === 'string') return value._serialized;
          if (typeof value.$1 === 'string') return value.$1;
          if (value.user && value.server) return `${value.user}@${value.server}`;
          return '';
        };
        const readBody = (value) => String(
          (value && (value.body || value.caption || value.text || value.pollName || value.eventName)) ||
          (value && value.msg && (value.msg.body || value.msg.caption || value.msg.text)) || ''
        ).trim();
        let Msg = null;
        try { Msg = window.require('WAWebCollections').Msg; } catch (_) {}
        if (!Msg) return null;
        let msg = null;
        try { msg = Msg.get(wanted) || null; } catch (_) {}
        if (!msg && typeof Msg.getMessagesById === 'function') {
          try { msg = (await Msg.getMessagesById([wanted]))?.messages?.[0] || null; } catch (_) {}
        }
        if (!msg) {
          let models = [];
          try { models = typeof Msg.getModelsArray === 'function' ? Msg.getModelsArray() : (Array.isArray(Msg.models) ? Msg.models : []); } catch (_) {}
          msg = (Array.isArray(models) ? models : []).find((m) => widText(m && m.id) === wanted) || null;
        }
        if (!msg) return null;
        const raw = msg._data || msg;
        const context = raw.contextInfo || raw.context || msg.contextInfo || msg.context || {};
        let quoted = raw.quotedMsg || raw.quotedMessage || msg.quotedMsg || msg.quotedMessage || context.quotedMessage || context.quotedMsg || null;
        let quoteId = widText(quoted && quoted.id) || String(
          raw.quotedStanzaID || raw.quotedMessageId || raw.replyToMsgId ||
          msg.quotedStanzaID || msg.quotedMessageId || msg.replyToMsgId ||
          context.stanzaId || context.quotedStanzaID || ''
        ).trim();
        const participant = widText(raw.quotedParticipant || msg.quotedParticipant || context.participant || context.quotedParticipant);
        const hasQuote = Boolean(quoted || quoteId || participant);
        if (!hasQuote) return null;

        // If only the quoted stanza id is present, try to resolve the actual
        // message model so the UI can show its text and sender immediately.
        if (!quoted && quoteId) {
          try { quoted = Msg.get(quoteId) || null; } catch (_) {}
          if (!quoted && typeof Msg.getMessagesById === 'function') {
            try { quoted = (await Msg.getMessagesById([quoteId]))?.messages?.[0] || null; } catch (_) {}
          }
          if (!quoted) {
            let models = [];
            try { models = typeof Msg.getModelsArray === 'function' ? Msg.getModelsArray() : (Array.isArray(Msg.models) ? Msg.models : []); } catch (_) {}
            quoted = (Array.isArray(models) ? models : []).find((m) => {
              const id = widText(m && m.id);
              if (id === quoteId) return true;
              return Boolean(quoteId && id && (id.endsWith(quoteId) || quoteId.endsWith(id)));
            }) || null;
          }
        }
        if (quoted && !quoteId) quoteId = widText(quoted.id);
        const body = readBody(quoted) || (quoted && (quoted.hasMedia || quoted.directPath || quoted.mediaKey) ? `[${String(quoted.type || 'Вложение')}]` : '');
        const quotedFromMe = Boolean(quoted && ((quoted.id && quoted.id.fromMe) || quoted.fromMe));
        const sender = quotedFromMe
          ? 'Вы'
          : String((quoted && (quoted.notifyName || quoted.pushname || quoted.senderName)) || '').trim() || 'Пользователь';
        return {
          has_quote: true,
          quoted_message_key: quoteId,
          quoted_body: body || 'Сообщение',
          quoted_sender: sender,
        };
      }, mid),
      new Promise((resolve) => setTimeout(() => resolve(null), 2500)),
    ]);
  } catch (_) {
    return null;
  }
}

async function attachQuoteToItem(message, item) {
  if (!message || !item) return item;
  const raw = message._data || {};
  const context = raw.contextInfo || raw.context || {};
  const rawQuoted = raw.quotedMsg || raw.quotedMessage || context.quotedMessage || context.quotedMsg || null;
  const quoteKeyFallback =
    serializedId(rawQuoted && rawQuoted.id) ||
    String(raw.quotedStanzaID || raw.quotedMessageId || raw.replyToMsgId || context.stanzaId || context.quotedStanzaID || '').trim();
  const hasQuote = Boolean(
    message.hasQuotedMsg || rawQuoted || quoteKeyFallback || raw.quotedParticipant || context.participant
  );
  if (!hasQuote && !item.from_me) return item;

  let quoted = null;
  if (typeof message.getQuotedMessage === 'function') {
    try {
      // Do not wrap whatsapp-web.js page methods in Promise.race. When the timer
      // wins Puppeteer can later report "Protocol error ... Promise was collected".
      quoted = await message.getQuotedMessage();
    } catch (_) {}
  }
  if (quoted) {
    rememberMessageObject(quoted);
    item.quoted_message_key = serializedId(quoted.id) || quoteKeyFallback;
    item.quoted_body = String(messageText(quoted) || '').trim().slice(0, 1200);
    if (!item.quoted_body && quoted.hasMedia) item.quoted_body = `[${String(quoted.type || 'Вложение')}]`;
    if (quoted.fromMe) {
      item.quoted_sender = 'Вы';
    } else {
      let senderName = '';
      try {
        const contact = typeof quoted.getContact === 'function' ? await quoted.getContact() : null;
        senderName = String(contact && (contact.pushname || contact.name || contact.shortName) || '').trim();
      } catch (_) {}
      item.quoted_sender = senderName || 'Пользователь';
    }
    return item;
  }

  // Fallback for builds where getQuotedMessage() cannot recreate the object but
  // WhatsApp already included the quoted stanza in message._data.
  if (quoteKeyFallback) item.quoted_message_key = quoteKeyFallback;
  if (rawQuoted) {
    item.quoted_body = String(
      rawQuoted.body || rawQuoted.caption || rawQuoted.text ||
      (rawQuoted.msg && (rawQuoted.msg.body || rawQuoted.msg.caption)) || ''
    ).trim().slice(0, 1200);
    const quotedFromMe = Boolean(
      rawQuoted.fromMe || (rawQuoted.id && rawQuoted.id.fromMe)
    );
    item.quoted_sender = quotedFromMe
      ? 'Вы'
      : String(rawQuoted.notifyName || rawQuoted.pushname || rawQuoted.senderName || '').trim() || 'Пользователь';
  }
  if (item.from_me && (!item.quoted_message_key || !item.quoted_body || item.quoted_body === 'Сообщение')) {
    const direct = await directQuoteSnapshotById(serializedId(message && message.id));
    if (direct && direct.has_quote) {
      if (direct.quoted_message_key) item.quoted_message_key = String(direct.quoted_message_key || '').trim();
      if (direct.quoted_body) item.quoted_body = String(direct.quoted_body || '').trim().slice(0, 1200);
      if (direct.quoted_sender) item.quoted_sender = String(direct.quoted_sender || '').trim().slice(0, 100);
    }
  }
  if (item.quoted_message_key && !item.quoted_body) item.quoted_body = 'Сообщение';
  if (item.quoted_message_key && !item.quoted_sender) item.quoted_sender = 'Сообщение';
  return item;
}

function messageMentionIds(message) {
  const values = [];
  const sources = [
    message && message.mentionedIds,
    message && message._data && message._data.mentionedJidList,
  ];
  for (const source of sources) {
    if (!Array.isArray(source)) continue;
    for (const value of source) {
      const id = serializedId(value);
      if (id && !values.includes(id)) values.push(id);
    }
  }
  return values;
}


function prettyPhoneFromId(value) {
  const id = String(value || '');
  if (id.includes('@') && !id.endsWith('@c.us')) return '';
  const digits = id.split('@')[0].replace(/\D/g, '');
  if (!digits) return '';
  if (digits.length === 11 && digits.startsWith('7')) {
    return `+7 ${digits.slice(1, 4)} ${digits.slice(4, 7)}-${digits.slice(7, 9)}-${digits.slice(9, 11)}`;
  }
  return `+${digits}`;
}

function cleanContactDisplayName(value) {
  const name = String(value || '').trim();
  if (!name) return '';
  const folded = name.toLocaleLowerCase('ru');
  if (['система', 'system', 'рабочий whatsapp', 'участник', 'участник группы', 'пользователь whatsapp', 'direct'].includes(folded)) return '';
  if (/^[+\d\s().-]+$/.test(name)) return '';
  return name;
}

async function resolveParticipantIdentity(rawId, fallbackName = '', contactHint = null) {
  const raw = String(rawId || '').trim();
  let contact = contactHint || null;
  let contactId = serializedId(contact && contact.id) || raw;
  let phoneId = contactId.endsWith('@c.us') ? contactId : (lidToPhone.get(contactId) || '');
  if (!phoneId && raw.endsWith('@c.us')) phoneId = raw;
  if (!phoneId && contactId.endsWith('@lid')) {
    phoneId = await resolveDirectPhoneId(contactId).catch(() => '');
  }
  if (!phoneId && raw.endsWith('@lid')) {
    phoneId = await resolveDirectPhoneId(raw).catch(() => '');
  }

  if (!contact) {
    const lookupId = phoneId || contactId || raw;
    if (lookupId) {
      try {
        contact = await Promise.race([
          client.getContactById(lookupId),
          new Promise((resolve) => setTimeout(() => resolve(null), 1000)),
        ]);
      } catch (_) {}
    }
  }

  contactId = serializedId(contact && contact.id) || contactId || raw;
  if (!phoneId && contactId.endsWith('@c.us')) phoneId = contactId;
  if (!phoneId && contactId.endsWith('@lid')) {
    phoneId = await resolveDirectPhoneId(contactId).catch(() => '');
  }

  let formattedPhone = prettyPhoneFromId(phoneId);
  if (!formattedPhone && contact && typeof contact.getFormattedNumber === 'function') {
    try {
      formattedPhone = String(await Promise.race([
        contact.getFormattedNumber(),
        new Promise((resolve) => setTimeout(() => resolve(''), 800)),
      ]) || '').trim();
    } catch (_) {}
  }

  const ownId = serializedId(client.info && client.info.wid);
  const ownDigits = ownId.endsWith('@c.us') ? ownId.split('@')[0] : '';
  const isMe = Boolean(contact && contact.isMe) || raw === ownId || contactId === ownId ||
    Boolean(phoneId && ownDigits && phoneId.split('@')[0] === ownDigits);
  const cachedName = participantDisplayById.get(raw) || participantDisplayById.get(contactId) ||
    participantDisplayById.get(phoneId) || contactDisplayById.get(raw) ||
    contactDisplayById.get(contactId) || contactDisplayById.get(phoneId) || '';
  const contactName = cleanContactDisplayName(contact && (contact.pushname || contact.name || contact.shortName));
  const hintedName = cleanContactDisplayName(fallbackName);
  const name = isMe ? 'Рабочий WhatsApp' : (contactName || hintedName || cachedName || formattedPhone || 'Участник группы');

  for (const id of [raw, contactId, phoneId]) {
    if (!id) continue;
    if (name) participantDisplayById.set(id, name);
    if (name) contactDisplayById.set(id, name);
  }
  if (raw.endsWith('@lid') && phoneId) lidToPhone.set(raw, phoneId);
  if (contactId.endsWith('@lid') && phoneId) lidToPhone.set(contactId, phoneId);

  // Queue profile photos for group senders/participants. The avatar worker is
  // bounded and cached, so this does not block message processing.
  avatarSync.enqueue([raw, contactId, phoneId].filter(Boolean));

  return {
    id: raw || contactId || phoneId,
    resolved_id: phoneId || contactId || raw,
    name,
    phone: formattedPhone,
    is_me: isMe,
  };
}

async function resolveGroupSenderInfo(message, fallbackName = '') {
  const author = serializedId(message && message.author) ||
    serializedId(message && message._data && message._data.author) ||
    serializedId(message && message._data && message._data.participant) ||
    serializedId(message && message._data && message._data.id && message._data.id.participant);
  let contact = null;
  if (message && typeof message.getContact === 'function') {
    try {
      contact = await Promise.race([
        message.getContact(),
        new Promise((resolve) => setTimeout(() => resolve(null), 900)),
      ]);
    } catch (_) {}
  }
  return resolveParticipantIdentity(author || serializedId(contact && contact.id), fallbackName, contact);
}

async function resolveMentionPresentation(message, sourceBody = '') {
  let body = String(sourceBody || '').trim();
  const ids = messageMentionIds(message);
  if (!ids.length) return { body, mentions: [] };

  let contacts = [];
  if (message && typeof message.getMentions === 'function') {
    try {
      contacts = await Promise.race([
        message.getMentions(),
        new Promise((resolve) => setTimeout(() => resolve([]), 1200)),
      ]);
    } catch (_) {
      contacts = [];
    }
  }

  const result = [];
  const ownId = serializedId(client.info && client.info.wid);
  const ownDigits = ownId.endsWith('@c.us') ? ownId.split('@')[0] : '';
  for (let index = 0; index < ids.length; index += 1) {
    const id = ids[index];
    const contact = Array.isArray(contacts) ? contacts[index] : null;
    const resolvedId = serializedId(contact && contact.id) || id;
    let phoneId = resolvedId.endsWith('@c.us') ? resolvedId : (lidToPhone.get(resolvedId) || '');
    if (!phoneId && resolvedId.endsWith('@lid')) {
      phoneId = await resolveDirectPhoneId(resolvedId).catch(() => '');
    }
    const isOwn = Boolean(contact && contact.isMe) ||
      resolvedId === ownId || ownMentionIds.has(id) || ownMentionIds.has(resolvedId) ||
      Boolean(phoneId && ownDigits && phoneId.split('@')[0] === ownDigits);
    const knownName = participantDisplayById.get(id) || participantDisplayById.get(resolvedId) || '';
    const fallback = prettyPhoneFromId(phoneId) || 'Участник';
    const name = isOwn
      ? 'Рабочий WhatsApp'
      : String((contact && (contact.pushname || contact.name || contact.shortName)) || knownName || fallback).trim();
    result.push({ id, resolved_id: phoneId || resolvedId, name });

    const candidates = new Set([
      id.split('@')[0],
      resolvedId.split('@')[0],
      phoneId ? phoneId.split('@')[0] : '',
    ]);
    for (const candidate of candidates) {
      if (!candidate) continue;
      body = body.replace(new RegExp(`@${candidate}(?!\\d)`, 'g'), `@${name}`);
    }
  }
  return { body, mentions: result };
}

async function downloadMediaDirectById(messageId) {
  const mid = String(messageId || '').trim();
  if (!mid) return null;
  try {
    return await Promise.race([
      client.pupPage.evaluate(async (msgId) => {
        try {
          const Collections = window.require('WAWebCollections');
          const Msg = Collections && Collections.Msg;
          if (!Msg) return null;
          const idCandidates = [String(msgId)];
          // For LID messages the browser model may require the structured Wid
          // object. Rebuild it from the stable serialized form as well.
          const parsed = String(msgId).match(/^(true|false)_(.+)_([A-Za-z0-9-]{8,})$/);
          if (parsed) {
            idCandidates.push({
              fromMe: parsed[1] === 'true',
              remote: parsed[2],
              id: parsed[3],
              $1: String(msgId),
              _serialized: String(msgId),
            });
            idCandidates.push(parsed[3]);
          }
          let msg = null;
          for (const candidate of idCandidates) {
            try { msg = typeof Msg.get === 'function' ? Msg.get(candidate) : null; } catch (_) {}
            if (msg) break;
          }
          if (!msg && typeof Msg.getMessagesById === 'function') {
            for (const candidate of idCandidates) {
              try {
                msg = (await Msg.getMessagesById([candidate]))?.messages?.[0] || null;
              } catch (_) {}
              if (msg) break;
            }
          }
          if (!msg) {
            let models = [];
            try {
              models = typeof Msg.getModelsArray === 'function' ? Msg.getModelsArray() :
                (Array.isArray(Msg.models) ? Msg.models : (Array.isArray(Msg._models) ? Msg._models : []));
            } catch (_) {}
            const wanted = String(msgId);
            msg = (Array.isArray(models) ? models : []).find((candidate) => {
              const id = candidate && candidate.id;
              return Boolean(id && (id._serialized === wanted || id.$1 === wanted ||
                (id.fromMe !== undefined && id.remote && id.id &&
                 `${Boolean(id.fromMe)}_${id.remote}_${id.id}` === wanted)));
            }) || null;
          }
          if (!msg) return null;

          // WhatsApp часто создаёт событие раньше, чем файл полностью готов.
          // Просим Web-клиент принудительно подготовить медиа, как это делает
          // whatsapp-web.js внутри Message.downloadMedia().
          if (msg.mediaData && msg.mediaData.mediaStage !== 'RESOLVED') {
            try {
              await Promise.race([
                msg.downloadMedia({ downloadEvenIfExpensive: true, rmrReason: 1 }),
                new Promise((resolve) => setTimeout(resolve, 3500)),
              ]);
            } catch (_) {}
          }
          // Даже при REUPLOADING не выходим сразу: directPath/mediaKey уже могут
          // быть доступны и файл можно забрать напрямую. Если ещё рано, очередь
          // догрузки повторит попытку через несколько секунд.

          // На новых сборках библиотеки уже может быть готовый resolveMediaBlob.
          if (window.WWebJS && typeof window.WWebJS.resolveMediaBlob === 'function') {
            try {
              const resolved = await window.WWebJS.resolveMediaBlob(msgId);
              if (resolved && resolved.blob) {
                const data = await window.WWebJS.arrayBufferToBase64Async(await resolved.blob.arrayBuffer());
                return {
                  data,
                  mimetype: resolved.mimetype || msg.mimetype || '',
                  filename: resolved.filename || msg.filename || '',
                  filesize: resolved.filesize || msg.size || 0,
                };
              }
            } catch (_) {}
          }

          const directPath = msg.directPath || (msg.mediaData && msg.mediaData.directPath) || '';
          const mediaKey = msg.mediaKey || (msg.mediaData && msg.mediaData.mediaKey);
          if (!directPath || !mediaKey) return null;
          const mockQpl = {
            addAnnotations() { return this; },
            addPoint() { return this; },
            end() { return this; },
          };
          const managerModule = window.require('WAWebDownloadManager');
          const manager = managerModule && (managerModule.downloadManager || managerModule);
          if (!manager || typeof manager.downloadAndMaybeDecrypt !== 'function') return null;
          const decryptedMedia = await manager.downloadAndMaybeDecrypt({
            directPath,
            encFilehash: msg.encFilehash || (msg.mediaData && msg.mediaData.encFilehash),
            filehash: msg.filehash || (msg.mediaData && msg.mediaData.filehash),
            mediaKey,
            mediaKeyTimestamp: msg.mediaKeyTimestamp || (msg.mediaData && msg.mediaData.mediaKeyTimestamp),
            type: msg.type,
            signal: new AbortController().signal,
            downloadQpl: mockQpl,
          });
          if (!decryptedMedia) return null;
          const data = await window.WWebJS.arrayBufferToBase64Async(decryptedMedia);
          return {
            data,
            mimetype: msg.mimetype || (msg.mediaData && msg.mediaData.mimetype) || '',
            filename: msg.filename || (msg.mediaData && msg.mediaData.filename) || '',
            filesize: msg.size || (msg.mediaData && msg.mediaData.size) || 0,
          };
        } catch (_) {
          return null;
        }
      }, mid),
      new Promise((resolve) => setTimeout(() => resolve(null), 8000)),
    ]);
  } catch (_) {
    return null;
  }
}

function applyMediaResult(item, media, message) {
  if (!media || !media.data) return false;
  const bytes = Buffer.byteLength(String(media.data), 'base64');
  item.media_name = String(media.filename || `WhatsApp ${(message && message.type) || 'media'}`).slice(0, 180);
  item.media_mime = String(media.mimetype || '').slice(0, 120);
  if (!bytes) return false;
  if (bytes > 512 * 1024 * 1024) {
    item.media_too_large = true;
    console.warn(`Вложение WhatsApp слишком большое для панели: ${Math.round(bytes / 1024 / 1024)} МБ`);
    return true;
  }
  item.media_base64 = String(media.data);
  item.media_pending = false;
  return true;
}

async function attachMediaToItem(message, item) {
  if (!message) return item;
  // Make whatsapp-web.js compatible with the current Wid shape before its
  // own downloadMedia() implementation reads message.id._serialized.
  const normalizedMessageId = normalizeMessageIdObject(message);
  const raw = message._data || {};
  const mediaExpected = Boolean(
    message.hasMedia || raw.directPath || raw.mediaKey || raw.mediaData ||
    ['image', 'video', 'audio', 'ptt', 'document', 'sticker'].includes(String(message.type || ''))
  );
  if (!mediaExpected) return item;

  const mid = normalizedMessageId || serializedId(message && message.id);
  if (mid) {
    const recoveredUntil = Number(locallyRecoveredMediaIds.get(String(mid)) || 0);
    if (recoveredUntil > Date.now()) {
      item.media_pending = false;
      item.media_name = String(item.media_name || `WhatsApp ${message.type || 'media'}`).slice(0, 180);
      return item;
    }
    if (recoveredUntil) locallyRecoveredMediaIds.delete(String(mid));
  }
  let lastError = null;

  // Сначала используем официальный Message.downloadMedia(). Даём ему больше
  // времени, чем раньше: на реальном рабочем WhatsApp фото нередко готовится
  // несколько секунд после появления самого сообщения.
  if (typeof message.downloadMedia === 'function') {
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      try {
        const media = await Promise.race([
          message.downloadMedia(),
          new Promise((resolve) => setTimeout(() => resolve(null), 5000)),
        ]);
        if (applyMediaResult(item, media, message)) return item;
      } catch (error) {
        lastError = error;
      }
      if (typeof message.reload === 'function') {
        try {
          await Promise.race([
            message.reload(),
            new Promise((resolve) => setTimeout(() => resolve(null), 1500)),
          ]);
        } catch (_) {}
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }

  // Если объект Message в библиотеке устарел или hasMedia=false, забираем файл
  // прямо из внутреннего Msg WhatsApp Web по ID. Это главный резерв для фото.
  if (mid) {
    const direct = await downloadMediaDirectById(mid);
    if (applyMediaResult(item, direct, message)) return item;
  }

  item.media_name = String(item.media_name || `WhatsApp ${message.type || 'media'}`).slice(0, 180);
  item.media_pending = true;
  console.warn(
    `Вложение WhatsApp поставлено на догрузку: ${mid || message.type || 'media'}`,
    lastError && (lastError.message || lastError) || ''
  );
  return item;
}

async function mentionsConnectedAccount(message) {
  const ownId = serializedId(client.info && client.info.wid);
  const ownUser = ownId ? ownId.split('@')[0] : '';
  const mentionedIds = messageMentionIds(message);

  // Только реальные WhatsApp mention IDs. Обычный текст с символом @ не считается
  // упоминанием и не должен создавать уведомление группы.
  if (ownId && mentionedIds.includes(ownId)) return true;
  if (mentionedIds.some((id) => ownMentionIds.has(id))) return true;
  if (
    ownUser &&
    mentionedIds.some((id) => id.endsWith('@c.us') && id.split('@')[0] === ownUser)
  ) {
    return true;
  }

  // В multi-device реальный mention может прийти как @lid. getMentions()
  // используем только когда WhatsApp действительно прислал список mention IDs.
  if (mentionedIds.length && message && typeof message.getMentions === 'function') {
    try {
      const contacts = await message.getMentions();
      if (
        Array.isArray(contacts) &&
        contacts.some((contact) => {
          if (contact && contact.isMe) return true;
          const contactId = serializedId(contact && contact.id);
          return Boolean(contactId && (contactId === ownId || ownMentionIds.has(contactId)));
        })
      ) {
        return true;
      }
    } catch (error) {
      console.warn('Не удалось проверить реальное упоминание через getMentions:', error.message || error);
    }
  }

  // Никаких текстовых fallback-проверок: уведомление «Упоминание в группе»
  // разрешено только когда WhatsApp реально передал mention ID / getMentions().
  return false;
}

async function publishLiveMessage(chatId, sender, message, fromMe = false, bodyOverride = '', notify = false, manualContact = false, senderPhone = '', senderId = '') {
  const safeChatId = String(chatId || '').trim();
  if (!safeChatId) return;
  rememberMessageObject(message, safeChatId);
  const rawBody = String(bodyOverride || messageText(message)).trim();
  const mentionView = await resolveMentionPresentation(message, rawBody);
  const item = liveMessageItem(message, fromMe, mentionView.body, notify);
  item.sender = String(sender || '');
  item.sender_phone = String(senderPhone || '');
  item.sender_id = String(senderId || '');
  item.mentions = mentionView.mentions;
  // Preserve WhatsApp reply context for both incoming replies and messages
  // written from the work phone/WhatsApp Web. Earlier builds had the resolver
  // but never called it, so WhatsApp showed the quote while our UI did not.
  await attachQuoteToItem(message, item);
  try { await attachMediaToItem(message, item); }
  catch (error) {
    item.media_pending = true;
    console.warn('Не удалось загрузить отдельное вложение:', error.message || error);
  }
  if (item.media_pending && item.id) {
    pendingMediaMessages.set(String(item.id), {
      chat_id: safeChatId,
      sender: String(sender || ''),
      sender_phone: String(senderPhone || ''),
      sender_id: String(senderId || ''),
      from_me: Boolean(fromMe),
      notify: Boolean(notify),
      body: String(item.body || ''),
      type: String(item.type || (message && message.type) || 'media'),
      timestamp: Number(item.timestamp || Math.floor(Date.now() / 1000)),
      attempts: 0,
      next_at: Date.now() + 1800,
    });
  }
  if (safeChatId.endsWith('@g.us')) {
    await postJson('/api/chat-messages-sync', {
      chat_id: safeChatId,
      append: true,
      messages: [item],
    });
    return item;
  }
  const previous = knownChats.get(safeChatId) || {};
  const whatsappName = cleanContactDisplayName(contactDisplayById.get(safeChatId));
  const inboundName = fromMe ? '' : cleanContactDisplayName(sender);
  const previousName = cleanContactDisplayName(previous.name);
  knownChats.set(safeChatId, {
    id: safeChatId,
    // Outgoing system/employee messages must not overwrite the remote contact name.
    name: whatsappName || inboundName || previousName || 'Пользователь WhatsApp',
    last_message: item.body,
    timestamp: item.timestamp,
    last_from_me: Boolean(fromMe),
    unread_count: fromMe || manualContact ? Number(previous.unread_count || 0) : Number(previous.unread_count || 0) + 1,
  });
  const chats = [...knownChats.values()]
    .sort((left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0))
    .slice(0, 100);
  await Promise.all([
    postJson('/api/chat-list-sync', { connected: true, chats }),
    postJson('/api/chat-messages-sync', {
      chat_id: safeChatId,
      append: true,
      messages: [item],
    }),
  ]);
  return item;
}

async function markChatRead(chatId) {
  const safeChatId = String(chatId || '').trim();
  if (!safeChatId || safeChatId.endsWith('@g.us') || typeof client.sendSeen !== 'function') return;
  const previous = knownChats.get(safeChatId);
  // No need to call into WhatsApp Web every two seconds for an already-read chat.
  if (previous && Number(previous.unread_count || 0) <= 0) return;
  const now = Date.now();
  if (markReadBusy || now - Number(seenSentAt.get(safeChatId) || 0) < 6000) return;
  markReadBusy = true;
  seenSentAt.set(safeChatId, now);
  try {
    await client.sendSeen(safeChatId);
    if (previous) {
      knownChats.set(safeChatId, { ...previous, unread_count: 0 });
      const chats = [...knownChats.values()]
        .sort((left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0))
        .slice(0, 100);
      await postJson('/api/chat-list-sync', { connected: true, chats });
    }
  } catch (error) {
    console.warn('Не удалось отметить сообщение прочитанным. Заявка продолжает обрабатываться:', error.message || error);
  } finally {
    markReadBusy = false;
  }
}

async function repairStoredGroupMessageIdentities(groupId, requestedIds = []) {
  const safeGroupId = String(groupId || '').trim();
  if (!safeGroupId.endsWith('@g.us') || groupIdentityRepairBusy || !connectorOperational) return;
  const now = Date.now();
  if (now - Number(groupIdentityRepairAt.get(safeGroupId) || 0) < 8000) return;
  groupIdentityRepairAt.set(safeGroupId, now);
  groupIdentityRepairBusy = true;
  try {
    const wanted = [...new Set((Array.isArray(requestedIds) ? requestedIds : [])
      .map((value) => String(value || '').trim()).filter(Boolean))].slice(0, 30);
    const wantedSet = new Set(wanted);
    const messagesById = new Map();

    // 3.3.37: сервер сообщает ID только тех сохранённых сообщений, где имя,
    // номер или sender_id ещё не определены. Получаем именно эти сообщения у
    // WhatsApp и никогда не импортируем постороннюю историю.
    if (wanted.length && typeof client.getMessageById === 'function') {
      for (let i = 0; i < wanted.length; i += 6) {
        const found = await Promise.all(wanted.slice(i, i + 6).map(async (id) => {
          try {
            return await Promise.race([
              client.getMessageById(id),
              new Promise((resolve) => setTimeout(() => resolve(null), 1400)),
            ]);
          } catch (_) {
            return null;
          }
        }));
        for (const message of found) {
          const id = serializedId(message && message.id);
          if (id && wantedSet.has(id)) messagesById.set(id, message);
        }
      }
    }

    // Если getMessageById не вернул часть моделей, читаем небольшой хвост
    // выбранной группы. Фильтр wantedSet гарантирует update-only поведение.
    if (wanted.length && messagesById.size < wanted.length) {
      try {
        const chat = await Promise.race([
          client.getChatById(safeGroupId),
          new Promise((resolve) => setTimeout(() => resolve(null), 2200)),
        ]);
        if (chat && typeof chat.fetchMessages === 'function') {
          const recent = await Promise.race([
            chat.fetchMessages({ limit: 120 }),
            new Promise((resolve) => setTimeout(() => resolve([]), 5000)),
          ]);
          for (const message of (Array.isArray(recent) ? recent : [])) {
            const id = serializedId(message && message.id);
            if (id && wantedSet.has(id)) messagesById.set(id, message);
          }
        }
      } catch (_) {}
    }

    // Резерв для новых live-сообщений: используем модели, которые WhatsApp Web
    // уже держит в памяти. Это также помогает сразу после получения сообщения.
    if (!wanted.length) {
      const rows = await Promise.race([
        client.pupPage.evaluate((gid) => {
          const widText = (value) => {
            if (!value) return '';
            if (typeof value === 'string') return value;
            if (typeof value._serialized === 'string') return value._serialized;
            if (typeof value.$1 === 'string') return value.$1;
            if (value.user && value.server) return `${value.user}@${value.server}`;
            return '';
          };
          const Msg = window.require('WAWebCollections').Msg;
          let models = [];
          try {
            if (Msg && typeof Msg.getModelsArray === 'function') models = Msg.getModelsArray() || [];
            else if (Msg && Array.isArray(Msg.models)) models = Msg.models;
          } catch (_) {}
          return (Array.isArray(models) ? models : []).slice(-500).map((m) => ({
            id: widText(m && m.id),
            remote: widText((m && m.id && m.id.remote) || (m && m.from) || (m && m.to)),
            from_me: Boolean(m && m.id && m.id.fromMe),
          })).filter((row) => row.id && row.remote === gid && !row.from_me).slice(-80);
        }, safeGroupId),
        new Promise((resolve) => setTimeout(() => resolve([]), 1500)),
      ]);
      for (const row of (Array.isArray(rows) ? rows : [])) {
        try {
          const message = typeof client.getMessageById === 'function'
            ? await Promise.race([client.getMessageById(String(row.id)), new Promise((resolve) => setTimeout(() => resolve(null), 800))])
            : null;
          if (message) messagesById.set(String(row.id), message);
        } catch (_) {}
      }
    }

    if (!messagesById.size) return;

    const identities = [];
    for (const [mid, message] of messagesById.entries()) {
      if (!message || message.fromMe) continue;
      const identity = await resolveGroupSenderInfo(message, '').catch(() => null);
      if (!identity) continue;
      const phoneId = String(identity.resolved_id || '').endsWith('@c.us')
        ? String(identity.resolved_id)
        : (lidToPhone.get(String(identity.id || '')) || '');
      const phone = String(identity.phone || prettyPhoneFromId(phoneId) || '').trim();
      let sender = String(identity.name || '').trim();
      const folded = sender.toLocaleLowerCase('ru');
      if (!sender || ['участник', 'участник группы', 'пользователь whatsapp', 'direct'].includes(folded)) {
        sender = phone || 'Участник группы';
      }
      identities.push({
        id: mid,
        sender,
        sender_phone: phone,
        sender_id: String(identity.id || identity.resolved_id || phoneId || '').trim(),
      });
    }
    if (identities.length) {
      const result = await postJson('/api/group-message-identities-sync', {
        chat_id: safeGroupId,
        identities,
      });
      if (Number(result && result.messages || 0) > 0) {
        console.log(`Данные авторов сообщений группы обновлены: ${safeGroupId} · ${result.messages}`);
      }
    }
  } catch (error) {
    console.warn(`Не удалось догрузить авторов сообщений группы ${safeGroupId}:`, error.message || error);
  } finally {
    groupIdentityRepairBusy = false;
  }
}

async function syncGroupParticipants(groupId) {
  if (participantSyncBusy) return;
  participantSyncBusy = true;
  const safeGroupId = String(groupId || '').trim();
  if (!safeGroupId.endsWith('@g.us')) {
    participantSyncBusy = false;
    return;
  }

  try {
    let rows = await Promise.race([
      client.pupPage.evaluate(async (gid) => {
        const widText = (value) => {
          if (!value) return '';
          if (typeof value === 'string') return value;
          if (typeof value._serialized === 'string') return value._serialized;
          if (typeof value.$1 === 'string') return value.$1;
          if (value.user && value.server) return `${value.user}@${value.server}`;
          return '';
        };
        const modelsFrom = (collection) => {
          if (!collection) return [];
          try { if (Array.isArray(collection)) return collection; } catch (_) {}
          try { if (typeof collection.getModelsArray === 'function') return collection.getModelsArray() || []; } catch (_) {}
          try { if (typeof collection.toArray === 'function') return collection.toArray() || []; } catch (_) {}
          try { if (Array.isArray(collection.models)) return collection.models; } catch (_) {}
          try {
            if (collection._models && typeof collection._models === 'object') {
              return Array.isArray(collection._models) ? collection._models : Object.values(collection._models);
            }
          } catch (_) {}
          try {
            if (typeof collection.serialize === 'function') {
              const serialized = collection.serialize();
              if (Array.isArray(serialized)) return serialized;
              if (serialized && typeof serialized === 'object') return Object.values(serialized);
            }
          } catch (_) {}
          try {
            if (typeof collection === 'object') {
              const values = Object.values(collection).filter((v) => v && typeof v === 'object');
              if (values.length) return values;
            }
          } catch (_) {}
          return [];
        };
        const participantRow = (p) => {
          const id = widText(
            (p && p.id) ||
            (p && p.wid) ||
            (p && p.userWid) ||
            (p && p.contact && p.contact.id)
          );
          return {
            id,
            is_admin: Boolean(p && (
              p.isAdmin === true || p.isSuperAdmin === true ||
              (typeof p.isAdmin === 'function' && p.isAdmin()) ||
              (typeof p.isSuperAdmin === 'function' && p.isSuperAdmin())
            )),
          };
        };

        let chat = null;
        // Самый надёжный путь в новых сборках WhatsApp Web: получить реальную
        // модель чата через WWebJS.getChat без сериализации в Node.js.
        try {
          if (window.WWebJS && typeof window.WWebJS.getChat === 'function') {
            chat = await window.WWebJS.getChat(gid, { getAsModel: false });
          }
        } catch (_) {}

        const Collections = window.require('WAWebCollections');
        const WidFactory = window.require('WAWebWidFactory');
        const wid = WidFactory.createWid(gid);
        if (!chat) {
          try { chat = Collections.Chat.get(wid) || Collections.Chat.get(gid) || null; } catch (_) {}
        }
        if (!chat && Collections.Chat && typeof Collections.Chat.find === 'function') {
          try { chat = await Collections.Chat.find(wid); } catch (_) {}
        }

        // Просим WhatsApp обновить метаданные группы перед чтением участников.
        try {
          const query = window.require('WAWebGroupQueryJob');
          if (query && typeof query.queryAndUpdateGroupMetadataById === 'function') {
            await Promise.race([
              query.queryAndUpdateGroupMetadataById({ id: gid }),
              new Promise((resolve) => setTimeout(resolve, 2500)),
            ]);
          }
        } catch (_) {}

        try {
          if (window.WWebJS && typeof window.WWebJS.getChat === 'function') {
            const refreshed = await window.WWebJS.getChat(gid, { getAsModel: false });
            if (refreshed) chat = refreshed;
          }
        } catch (_) {}
        if (!chat) {
          try { chat = Collections.Chat.get(wid) || Collections.Chat.get(gid) || null; } catch (_) {}
        }
        if (!chat || !chat.groupMetadata) return [];

        const list = modelsFrom(chat.groupMetadata.participants);
        return list.slice(0, 500).map(participantRow).filter((p) => p.id);
      }, safeGroupId),
      new Promise((resolve) => setTimeout(() => resolve([]), 6500)),
    ]);

    rows = Array.isArray(rows) ? rows : [];

    // Последний резерв через публичный объект GroupChat. Он медленнее, но
    // полезен, если внутренняя коллекция изменила форму после обновления Web.
    if (!rows.length) {
      try {
        const chat = await Promise.race([
          client.getChatById(safeGroupId),
          new Promise((resolve) => setTimeout(() => resolve(null), 4500)),
        ]);
        const participants = chat && Array.isArray(chat.participants) ? chat.participants : [];
        rows = participants.map((p) => ({
          id: serializedId(p && p.id),
          is_admin: Boolean(p && (p.isAdmin || p.isSuperAdmin)),
        })).filter((p) => p.id);
      } catch (_) {}
    }

    const sourceRows = rows;
    if (!sourceRows.length) {
      await postJson('/api/group-participants-sync', { chat_id: safeGroupId, participants: [] });
      console.warn(`Участники группы пока не получены: ${safeGroupId}`);
      return;
    }

    const rawLids = sourceRows.map((p) => String(p.id || '')).filter((id) => id.endsWith('@lid'));
    if (rawLids.length && typeof client.getContactLidAndPhone === 'function') {
      for (let i = 0; i < rawLids.length; i += 60) {
        try {
          const mapped = await Promise.race([
            client.getContactLidAndPhone(rawLids.slice(i, i + 60)),
            new Promise((resolve) => setTimeout(() => resolve([]), 2500)),
          ]);
          for (const row of (Array.isArray(mapped) ? mapped : [])) {
            const lid = serializedId(row && row.lid);
            const pn = serializedId(row && row.pn);
            if (lid && pn.endsWith('@c.us')) lidToPhone.set(lid, pn);
          }
        } catch (_) {}
      }
    }

    const ownId = serializedId(client.info && client.info.wid);
    const ownDigits = ownId.endsWith('@c.us') ? ownId.split('@')[0] : '';
    const quick = sourceRows.map((p) => {
      const raw = String(p.id || '');
      const phoneId = raw.endsWith('@c.us') ? raw : (lidToPhone.get(raw) || '');
      const me = raw === ownId || Boolean(phoneId && ownDigits && phoneId.split('@')[0] === ownDigits);
      if (me) {
        ownMentionIds.add(raw);
        if (phoneId) ownMentionIds.add(phoneId);
      }
      const phone = prettyPhoneFromId(phoneId);
      const known = participantDisplayById.get(raw) || participantDisplayById.get(phoneId) || contactDisplayById.get(raw) || contactDisplayById.get(phoneId) || '';
      const name = me ? 'Рабочий WhatsApp' : (known || phone || 'Участник');
      participantDisplayById.set(raw, name);
      if (phoneId) participantDisplayById.set(phoneId, name);
      return { mention_id: raw, resolved_id: phoneId || raw, name, phone, is_admin: Boolean(p.is_admin), is_me: me };
    });
    await postJson('/api/group-participants-sync', { chat_id: safeGroupId, participants: quick });
    const quickAvatarIds = [...new Set(quick.flatMap((participant) => [participant.mention_id, participant.resolved_id]).filter(Boolean))];
    avatarSync.enqueue(quickAvatarIds);
    avatarSync.sync(client, postJson, quickAvatarIds.slice(0, 3)).catch(() => {});
    console.log(`Участники группы загружены: ${safeGroupId} · ${quick.length}`);

    // Имена догружаем небольшими порциями, чтобы не подвешивать QR-коннектор.
    const enriched = [];
    for (let i = 0; i < sourceRows.length; i += 12) {
      const part = await Promise.all(sourceRows.slice(i, i + 12).map(async (p) => {
        const raw = String(p.id || '');
        if (!raw) return null;
        const phoneId = raw.endsWith('@c.us') ? raw : (lidToPhone.get(raw) || '');
        let c = null;
        try {
          c = await Promise.race([
            client.getContactById(phoneId || raw),
            new Promise((resolve) => setTimeout(() => resolve(null), 1000)),
          ]);
        } catch (_) {}
        const me = Boolean(c && c.isMe) || raw === ownId || Boolean(phoneId && ownDigits && phoneId.split('@')[0] === ownDigits);
        if (me) {
          ownMentionIds.add(raw);
          if (phoneId) ownMentionIds.add(phoneId);
        }
        let phone = prettyPhoneFromId(phoneId);
        if (!phone && c && typeof c.getFormattedNumber === 'function') {
          try {
            phone = String(await Promise.race([
              c.getFormattedNumber(),
              new Promise((resolve) => setTimeout(() => resolve(''), 700)),
            ]) || '').trim();
          } catch (_) {}
        }
        const name = me
          ? 'Рабочий WhatsApp'
          : String((c && (c.pushname || c.name || c.shortName)) || participantDisplayById.get(raw) || participantDisplayById.get(phoneId) || contactDisplayById.get(raw) || contactDisplayById.get(phoneId) || phone || 'Участник');
        participantDisplayById.set(raw, name);
        contactDisplayById.set(raw, name);
        if (phoneId) {
          participantDisplayById.set(phoneId, name);
          contactDisplayById.set(phoneId, name);
        }
        return { mention_id: raw, resolved_id: phoneId || raw, name, phone, is_admin: Boolean(p.is_admin), is_me: me };
      }));
      enriched.push(...part.filter(Boolean));
      await postJson('/api/group-participants-sync', { chat_id: safeGroupId, participants: enriched });
    }
  } catch (error) {
    console.warn(`Не удалось получить участников группы ${safeGroupId}:`, error && error.stack ? error.stack.split('\n')[0] : (error.message || error));
  } finally {
    participantSyncBusy = false;
  }
}

async function syncContactDiscovery() {
  if (contactDiscoveryBusy) return;
  const now=Date.now(); if(now-lastContactDiscoveryAt<PERF_CONTACT_COOLDOWN_MS)return; lastContactDiscoveryAt=now; contactDiscoveryBusy=true;
  try {
    const contacts=await Promise.race([client.getContacts(),new Promise(r=>setTimeout(()=>r([]),7000))]);
    const users=(Array.isArray(contacts)?contacts:[]).filter(c=>c&&c.isUser&&!c.isGroup).slice(0,1000);
    const lids=users.map(c=>serializedId(c.id)).filter(id=>id.endsWith('@lid'));
    if(lids.length&&typeof client.getContactLidAndPhone==='function') for(let i=0;i<lids.length;i+=80){try{const rows=await Promise.race([client.getContactLidAndPhone(lids.slice(i,i+80)),new Promise(r=>setTimeout(()=>r([]),3500))]);for(const row of (Array.isArray(rows)?rows:[])){const lid=serializedId(row&&row.lid),pn=serializedId(row&&row.pn);if(lid&&pn.endsWith('@c.us'))lidToPhone.set(lid,pn);}}catch(_){}}
    const payload=users.map(c=>{
      const raw=serializedId(c.id),pn=raw.endsWith('@c.us')?raw:(lidToPhone.get(raw)||'');
      const chatId=pn||raw;
      const displayName=cleanContactDisplayName(c.pushname||c.name||c.shortName||'');
      if(chatId&&displayName) contactDisplayById.set(chatId,displayName);
      if(raw&&displayName) contactDisplayById.set(raw,displayName);
      return {chat_id:chatId,raw_id:raw,phone:prettyPhoneFromId(pn),name:displayName,saved:Boolean(c.isMyContact)};
    });
    await postJson('/api/contact-list-sync',{contacts:payload});
    avatarSync.enqueue(payload.map(c=>c.chat_id));
  } catch(error){console.warn('Поиск контактов WhatsApp временно недоступен:',error.message||error);}
  finally { contactDiscoveryBusy=false; }
}

async function syncSelectedContactProfile(chatId) {
  if (profileSyncBusy) return;
  const safe=String(chatId||'').trim(); if(!safe||safe.endsWith('@g.us'))return; const now=Date.now(); if(safe===lastProfileSyncChatId&&now-lastProfileSyncAt<30000)return; lastProfileSyncChatId=safe;lastProfileSyncAt=now; profileSyncBusy=true;
  try { const pn=safe.endsWith('@c.us')?safe:await resolveDirectPhoneId(safe); const contact=await Promise.race([client.getContactById(pn||safe),new Promise(r=>setTimeout(()=>r(null),2200))]); if(!contact)return; let pic='',about='',formatted=''; try{pic=await Promise.race([contact.getProfilePicUrl(),new Promise(r=>setTimeout(()=>r(''),1500))])||'';}catch(_){} try{about=await Promise.race([contact.getAbout(),new Promise(r=>setTimeout(()=>r(''),1500))])||'';}catch(_){} try{formatted=await Promise.race([contact.getFormattedNumber(),new Promise(r=>setTimeout(()=>r(''),1500))])||'';}catch(_){} const displayName=cleanContactDisplayName(contact.pushname||contact.name||contact.shortName||''); if(displayName){contactDisplayById.set(safe,displayName);if(pn)contactDisplayById.set(pn,displayName);} await postJson('/api/contact-profile-sync',{chat_id:safe,name:displayName,phone:formatted||prettyPhoneFromId(pn),about:String(about||''),profile_pic_url:String(pic||''),is_business:Boolean(contact.isBusiness)}); } catch(error){console.warn('Профиль WhatsApp не загрузился:',error.message||error);}
  finally { profileSyncBusy=false; }
}

async function recoverRawInboundCandidate(row) {
  const mid = String((row && row.id) || '').trim();
  const from = String((row && (row.from || row.remote)) || '').trim();
  if (!mid || !from || from.endsWith('@g.us')) return false;
  const phoneId = from.endsWith('@c.us') ? from : await resolveDirectPhoneId(from).catch(() => '');
  const canonicalChatId = phoneId || from;
  const phoneDigits = phoneId ? phoneId.split('@')[0] : '';
  const sender = String((row && row.sender) || '').trim() || phoneDigits || 'Пользователь WhatsApp';
  const type = String((row && row.type) || 'chat');
  const forwarded = Boolean(row && (row.isForwarded || row.forwarded || Number(row.forwardingScore || 0) > 0));
  const fallbackByType = { location: '[Геолокация]', vcard: '[Контакт]', multi_vcard: '[Контакты]' };
  const text = String((row && row.body) || '').trim() || (row && row.has_media ? messageText({ type }) : (fallbackByType[type] || ''));
  const timestamp = Number((row && row.timestamp) || Math.floor(Date.now() / 1000));
  if (!text && !row.has_media) return false;

  const previous = knownChats.get(canonicalChatId) || {};
  knownChats.set(canonicalChatId, {
    ...previous,
    id: canonicalChatId,
    name: sender,
    last_message: text || '[Вложение]',
    timestamp,
    last_from_me: false,
    unread_count: Number(previous.unread_count || 0) + 1,
  });
  const chats = [...knownChats.values()]
    .sort((left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0))
    .slice(0, 100);
  await Promise.all([
    postJson('/api/chat-list-sync', { connected: true, chats }),
    postJson('/api/chat-messages-sync', {
      chat_id: canonicalChatId,
      append: true,
      messages: [{ id: mid, from_me: false, body: text || '[Вложение]', type, timestamp, ack: 0, notify: false, forwarded }],
    }),
  ]);
  if (row && row.has_media) {
    pendingMediaMessages.set(mid, {
      chat_id: canonicalChatId,
      sender,
      sender_phone: '',
      sender_id: '',
      from_me: false,
      notify: false,
      forwarded,
      body: text || '[Вложение]',
      type,
      timestamp,
      attempts: 0,
      next_at: Date.now() + 1000,
    });
  }
  const result = await postJson('/api/whatsapp', {
    external_id: mid,
    sender,
    phone: phoneDigits ? `+${phoneDigits}` : '',
    chat_id: canonicalChatId,
    text,
    menu_choice: '',
    attachment_name: row && row.has_media ? `WhatsApp ${type}` : '',
    message_timestamp: timestamp,
    message_type: type,
  });
  if (!result || result.duplicate || result.manual_contact || result.silent || !result.reply) return true;
  await sendAutomaticReply([phoneId, from], result).catch(() => null);
  return true;
}


function rawMentionPresentation(body, mentionIds) {
  let text = String(body || '');
  const mentions = [];
  for (const rawId of (Array.isArray(mentionIds) ? mentionIds : [])) {
    const id = String(rawId || '').trim();
    if (!id) continue;
    const phoneId = id.endsWith('@c.us') ? id : (lidToPhone.get(id) || '');
    const ownId = serializedId(client.info && client.info.wid);
    const ownDigits = ownId.endsWith('@c.us') ? ownId.split('@')[0] : '';
    const isOwn = id === ownId || ownMentionIds.has(id) || Boolean(phoneId && ownDigits && phoneId.split('@')[0] === ownDigits);
    const name = isOwn
      ? 'Рабочий WhatsApp'
      : (participantDisplayById.get(id) || participantDisplayById.get(phoneId) || prettyPhoneFromId(phoneId) || 'Участник');
    mentions.push({ id, resolved_id: phoneId || id, name });
    const candidates = new Set([id.split('@')[0], phoneId ? phoneId.split('@')[0] : '']);
    for (const candidate of candidates) {
      if (!candidate) continue;
      text = text.replace(new RegExp(`@${candidate}(?!\\d)`, 'g'), `@${name}`);
    }
  }
  return { body: text, mentions };
}

async function recoverRawGroupCandidate(row) {
  const mid = String((row && row.id) || '').trim();
  const groupId = String((row && (row.from || row.remote)) || '').trim();
  if (!mid || !groupId.endsWith('@g.us')) return false;
  const author = String((row && row.author) || '').trim();
  const type = String((row && row.type) || 'chat');
  const timestamp = Number((row && row.timestamp) || Math.floor(Date.now() / 1000));
  const forwarded = Boolean(row && (row.isForwarded || row.forwarded || Number(row.forwardingScore || 0) > 0));
  const mentionIds = Array.isArray(row && row.mentions) ? row.mentions : [];
  const rawBody = String((row && row.body) || '').trim() || (row && row.has_media ? messageText({ type }) : '');
  const mentionView = rawMentionPresentation(rawBody, mentionIds);
  const identity = await resolveParticipantIdentity(author, String((row && row.sender) || '').trim());
  const phoneId = String(identity.resolved_id || '').endsWith('@c.us') ? String(identity.resolved_id) : '';
  const sender = identity.name || 'Участник группы';

  const ownId = serializedId(client.info && client.info.wid);
  const ownDigits = ownId.endsWith('@c.us') ? ownId.split('@')[0] : '';
  const mentionedUs = mentionIds.some((id) => {
    const safe = String(id || '');
    const pn = safe.endsWith('@c.us') ? safe : (lidToPhone.get(safe) || '');
    return safe === ownId || ownMentionIds.has(safe) || Boolean(pn && ownDigits && pn.split('@')[0] === ownDigits);
  });

  const item = {
    id: mid,
    from_me: false,
    sender,
    sender_phone: identity.phone || '',
    sender_id: identity.id || author || '',
    body: mentionView.body || (row && row.has_media ? messageText({ type }) : ''),
    type,
    timestamp,
    ack: 0,
    notify: mentionedUs,
    forwarded,
    mentions: mentionView.mentions,
  };
  await postJson('/api/chat-messages-sync', {
    chat_id: groupId,
    append: true,
    messages: [item],
  });

  if (row && row.has_media) {
    pendingMediaMessages.set(mid, {
      chat_id: groupId,
      sender,
      sender_phone: identity.phone || '',
      sender_id: identity.id || author || '',
      from_me: false,
      notify: mentionedUs,
      forwarded,
      body: item.body,
      type,
      timestamp,
      attempts: 0,
      next_at: Date.now() + 1000,
    });
  }
  if (mentionedUs) {
    console.log(`Резервная синхронизация нашла упоминание рабочего WhatsApp в группе: ${groupId}`);
  }
  return true;
}

async function reconcileRecentInbound() {
  if (recentPollBusy) return;
  const now = Date.now();
  if (now - lastRecentPollAt < 900) return;
  lastRecentPollAt = now;
  recentPollBusy = true;
  try {
    // client.getChats() сейчас может падать внутри сериализации WhatsApp Web с
    // короткой ошибкой "r". Для восстановления входящих читаем только ID
    // последних сообщений напрямую из WAWebCollections.Msg. Это существенно
    // легче и не зависит от сериализации всего списка чатов.
    const candidates = await client.pupPage.evaluate(() => {
        const widText = (value) => {
          if (!value) return '';
          if (typeof value === 'string') return value;
          if (typeof value._serialized === 'string') return value._serialized;
          if (typeof value.$1 === 'string') return value.$1;
          if (value.user && value.server) return `${value.user}@${value.server}`;
          return '';
        };
        const Msg = window.require('WAWebCollections').Msg;
        let models = [];
        if (Msg && typeof Msg.getModelsArray === 'function') models = Msg.getModelsArray();
        else if (Msg && Array.isArray(Msg.models)) models = Msg.models;
        const cutoff = Math.floor(Date.now() / 1000) - 900;
        return (Array.isArray(models) ? models : []).slice(-180).map((m) => ({
          id: widText(m && m.id),
          from: widText(m && m.from),
          remote: widText(m && m.id && m.id.remote),
          author: widText((m && m.author) || (m && m.participant) || (m && m.id && m.id.participant)),
          mentions: Array.isArray(m && m.mentionedJidList)
            ? m.mentionedJidList.map(widText).filter(Boolean)
            : [],
          from_me: Boolean(m && m.id && m.id.fromMe),
          type: String((m && m.type) || 'chat'),
          timestamp: Number((m && (m.t || m.timestamp)) || 0),
          body: String((m && (m.body || m.caption || m.pollName || m.eventName)) || ''),
          sender: String((m && (m.notifyName || m.pushname)) || ''),
          has_media: Boolean(m && (
            m.hasMedia || m.directPath || m.mediaKey || m.mediaData ||
            (m._data && (m._data.directPath || m._data.mediaKey || m._data.mediaData)) ||
            ['image', 'video', 'audio', 'ptt', 'document', 'sticker'].includes(String(m.type || ''))
          )),
          isForwarded: Boolean(m && (m.isForwarded || m.forwarded || (m._data && m._data.isForwarded))),
          forwarded: Boolean(m && (m.forwarded || m.isForwarded)),
          forwardingScore: Number((m && (m.forwardingScore || (m._data && m._data.forwardingScore))) || 0),
        })).filter((m) => m.id && !m.from_me && m.timestamp >= cutoff);
      });

    let recovered = 0;
    for (const row of (Array.isArray(candidates) ? candidates.slice(-40) : [])) {
      const mid = String(row.id || '');
      const from = String(row.from || row.remote || '');
      if (!mid || reconciledInboundIds.has(mid) || inboundProcessingIds.has(mid) || from === 'status@broadcast' || from.endsWith('@newsletter')) continue;
      if (!USER_MESSAGE_TYPES.has(String(row.type || ''))) continue;
      const isGroupMessage = from.endsWith('@g.us');
      let message = null;
      try {
        message = await client.getMessageById(mid);
        if (message) rememberMessageObject(message, from);
      } catch (_) {}
      if (message && !message.fromMe && isLiveInboundMessage(message)) {
        recovered += 1;
        // И личные, и групповые сообщения повторно подаются в единый обработчик.
        // Это важно: на текущем WhatsApp Web событие message иногда пропадает
        // именно в группах, хотя само сообщение уже есть во внутренней коллекции.
        client.emit('message', message);
        continue;
      }
      // Если библиотека не смогла собрать Message, всё равно сохраняем текст.
      // Для групп отдельно сохраняем автора, @упоминания и уведомление.
      try {
        const restored = isGroupMessage
          ? await recoverRawGroupCandidate(row)
          : await recoverRawInboundCandidate(row);
        if (restored) {
          reconciledInboundIds.set(mid, now);
          recovered += 1;
        }
      } catch (_) {}
    }
    if (recovered) console.log(`Резервная синхронизация восстановила входящих: ${recovered}`);
    for (const [id, t] of reconciledInboundIds) if (now - t > 3600000) reconciledInboundIds.delete(id);
    for (const [id, t] of inboundProcessingIds) if (now - t > 60000) inboundProcessingIds.delete(id);
  } catch (error) {
    console.warn('Резервная синхронизация входящих:', error && error.stack ? error.stack.split('\n')[0] : (error.message || error));
  } finally {
    recentPollBusy = false;
  }
}


async function reconcileRecentOutgoing() {
  // Резерв для случаев, когда WhatsApp Web не прислал message_create. Проверяем
  // только сообщения, созданные после запуска текущего коннектора, поэтому
  // старую переписку в систему не импортируем. Не допускаем параллельных циклов:
  // накопившиеся evaluate() заметно замедляли WhatsApp Web на слабом сервере.
  if (recentOutgoingBusy) return;
  recentOutgoingBusy = true;
  const now = Date.now();
  try {
    const candidates = await client.pupPage.evaluate((readyAt) => {
        const widText = (value) => {
          if (!value) return '';
          if (typeof value === 'string') return value;
          if (typeof value._serialized === 'string') return value._serialized;
          if (typeof value.$1 === 'string') return value.$1;
          if (value.user && value.server) return `${value.user}@${value.server}`;
          return '';
        };
        const Msg = window.require('WAWebCollections').Msg;
        let models = [];
        if (Msg && typeof Msg.getModelsArray === 'function') models = Msg.getModelsArray();
        else if (Msg && Array.isArray(Msg.models)) models = Msg.models;
        const quoteSnapshot = (m) => {
          if (!m) return { quoted_message_key: '', quoted_body: '', quoted_sender: '' };
          const raw = m._data || m;
          const context = raw.contextInfo || raw.context || m.contextInfo || m.context || {};
          const quoted = raw.quotedMsg || raw.quotedMessage || m.quotedMsg || m.quotedMessage || context.quotedMessage || context.quotedMsg || null;
          const quoteId = widText(quoted && quoted.id) || String(
            raw.quotedStanzaID || raw.quotedMessageId || raw.replyToMsgId ||
            m.quotedStanzaID || m.quotedMessageId || m.replyToMsgId ||
            context.stanzaId || context.quotedStanzaID || ''
          ).trim();
          if (!quoted && !quoteId && !context.participant && !raw.quotedParticipant) {
            return { quoted_message_key: '', quoted_body: '', quoted_sender: '' };
          }
          const body = String(
            (quoted && (quoted.body || quoted.caption || quoted.text || quoted.pollName || quoted.eventName)) ||
            (quoted && quoted.msg && (quoted.msg.body || quoted.msg.caption || quoted.msg.text)) || ''
          ).trim();
          const quotedFromMe = Boolean(quoted && ((quoted.id && quoted.id.fromMe) || quoted.fromMe));
          const sender = quotedFromMe
            ? 'Вы'
            : String((quoted && (quoted.notifyName || quoted.pushname || quoted.senderName)) || '').trim() || 'Пользователь';
          return {
            quoted_message_key: quoteId,
            quoted_body: body || (quoteId ? 'Сообщение' : ''),
            quoted_sender: quoteId || quoted ? sender : '',
          };
        };
        const cutoff = Math.max(Number(readyAt || 0), Math.floor(Date.now() / 1000) - 900);
        return (Array.isArray(models) ? models : []).slice(-600).map((m) => ({
          id: widText(m && m.id),
          remote: widText(m && m.id && m.id.remote),
          to: widText(m && m.to),
          from: widText(m && m.from),
          from_me: Boolean(m && m.id && m.id.fromMe),
          type: String((m && m.type) || 'chat'),
          timestamp: Number((m && (m.t || m.timestamp)) || 0),
          body: String((m && (m.body || m.caption || m.pollName || m.eventName)) || ''),
          has_media: Boolean(m && (
            m.hasMedia || m.directPath || m.mediaKey || m.mediaData ||
            m.mimetype || m.clientUrl || m.deprecatedMms3Url || m.filehash || m.encFilehash ||
            Number(m.size || m.mediaSize || 0) > 0 ||
            (m._data && (
              m._data.directPath || m._data.mediaKey || m._data.mediaData ||
              m._data.mimetype || m._data.clientUrl || m._data.deprecatedMms3Url ||
              m._data.filehash || m._data.encFilehash || Number(m._data.size || m._data.mediaSize || 0) > 0
            )) ||
            ['image','video','audio','ptt','document','sticker'].includes(String(m.type || ''))
          )),
          isForwarded: Boolean(m && (m.isForwarded || m.forwarded || (m._data && m._data.isForwarded))),
          forwarded: Boolean(m && (m.forwarded || m.isForwarded)),
          forwardingScore: Number((m && (m.forwardingScore || (m._data && m._data.forwardingScore))) || 0),
          ack: Math.max(0, Math.min(4, Number((m && (m.ack ?? m.__x_ack ?? (m._data && m._data.ack))) || 0))),
          ...quoteSnapshot(m),
        })).filter((m) => m.id && m.from_me && m.timestamp >= cutoff);
      }, connectorReadyAt);

    let recovered = 0;
    for (const row of (Array.isArray(candidates) ? candidates.slice(-50) : [])) {
      const mid = String(row.id || '').trim();
      if (!USER_MESSAGE_TYPES.has(String(row.type || ''))) continue;
      if (!mid) continue;

      // QUEUE_ACK_RECONCILE_3_3_73: message_ack can fire before the local row is
      // committed. In that case the first API update legitimately returns
      // updated=false and the UI can stay on «Отправляется». Read the current
      // ACK from WhatsApp's own message model on every bounded recovery pass
      // and retry persistence until the exact local message exists.
      const modelAck = Math.max(0, Math.min(4, Number(row.ack || 0)));
      const observedAck = Math.max(modelAck, Number(messageAcknowledgements.get(mid) || 0));
      const persistedAck = Number(persistedAcknowledgements.get(mid) || 0);
      if (observedAck > persistedAck) {
        const ackChatId = String(row.remote || row.to || row.from || '').trim();
        if (ackChatId) {
          try {
            const ackResult = await postJson('/api/chat-message-ack', {
              chat_id: ackChatId,
              message_id: mid,
              ack: observedAck,
            });
            if (ackResult && ackResult.updated) {
              persistedAcknowledgements.set(mid, observedAck);
              messageAcknowledgements.set(mid, Math.max(Number(messageAcknowledgements.get(mid) || 0), observedAck));
            }
          } catch (_) {}
        }
      }

      if (reconciledOutgoingIds.has(mid) || isRememberedInternalOutgoing(mid)) continue;
      let rawChatId = String(row.remote || row.to || '').trim();
      if (!rawChatId || rawChatId === 'status@broadcast' || rawChatId.endsWith('@newsletter')) continue;

      let message = null;
      try {
        message = await client.getMessageById(mid);
        if (message) rememberMessageObject(message, rawChatId);
      } catch (_) {}

      if (message && message.fromMe) {
        rawChatId = serializedId(message.to) || serializedId(message.from) || rawChatId;
        if (isRememberedInternalOutgoing(mid)) continue;
        scheduleOutgoingMediaProbe(message, rawChatId);
        if (rawChatId.endsWith('@g.us')) {
          const text = String(messageText(message) || '').trim();
          await publishLiveMessage(rawChatId, 'Рабочий WhatsApp', message, true, text, false, false).catch(() => {});
        } else {
          const phoneId = rawChatId.endsWith('@c.us') ? rawChatId : await resolveDirectPhoneId(rawChatId);
          const canonicalChatId = phoneId || rawChatId;
          const text = String(messageText(message) || '').trim();
          await publishLiveMessage(
            canonicalChatId,
            'Рабочий WhatsApp',
            message,
            true,
            text,
            false,
            false
          );
        }
        reconciledOutgoingIds.set(mid, now);
        recovered += 1;
        continue;
      }

      // Даже если whatsapp-web.js не смог собрать объект Message, текстовое
      // сообщение всё равно можно сохранить из внутренней коллекции.
      if (!USER_MESSAGE_TYPES.has(String(row.type || ''))) continue;
      if (!rawChatId.endsWith('@g.us')) {
        const phoneId = rawChatId.endsWith('@c.us') ? rawChatId : await resolveDirectPhoneId(rawChatId);
        rawChatId = phoneId || rawChatId;
      }
      // A phone screenshot can appear in Store.Msg a little earlier than its media
      // metadata. Saving/reconciling that empty provisional model loses the image
      // forever, because later sweeps skip the same message id. Leave it pending.
      const provisionalBody = String(row.body || '').trim();
      if (!row.has_media && !provisionalBody) {
        continue;
      }
      const item = {
        id: mid,
        from_me: true,
        sender: 'Рабочий WhatsApp',
        body: String(row.body || messageText({ type: row.type }) || '').trim(),
        type: String(row.type || 'chat'),
        timestamp: Number(row.timestamp || Math.floor(Date.now() / 1000)),
        ack: Math.max(1, Math.min(4, Number(row.ack || 0))),
        notify: false,
        forwarded: Boolean(
          row.isForwarded ||
          row.forwarded ||
          Number(row.forwardingScore || 0) > 0 ||
          isRememberedForwarded(mid)
        ),
        quoted_message_key: String(row.quoted_message_key || '').trim(),
        quoted_body: String(row.quoted_body || '').trim().slice(0, 1200),
        quoted_sender: String(row.quoted_sender || '').trim().slice(0, 100),
      };
      await postJson('/api/chat-messages-sync', {
        chat_id: rawChatId,
        append: true,
        messages: [item],
      });
      if (row.has_media) {
        console.log(`Исходящее медиа обнаружено резервной синхронизацией: ${mid}; type=${String(row.type || '')}`);
        if (!pendingMediaMessages.has(mid)) {
          pendingMediaMessages.set(mid, {
            chat_id: rawChatId,
            sender: 'Рабочий WhatsApp',
            sender_phone: '',
            sender_id: '',
            from_me: true,
            notify: false,
            forwarded: Boolean(item.forwarded),
            body: String(item.body || messageText({ type: item.type }) || '[Вложение]'),
            type: String(item.type || 'media'),
            timestamp: Number(item.timestamp || Math.floor(Date.now() / 1000)),
            attempts: 0,
            next_at: Date.now() + 800,
          });
        }
      } else {
        reconciledOutgoingIds.set(mid, now);
      }
      recovered += 1;
    }
    if (recovered) console.log(`Резервная синхронизация восстановила исходящих с рабочего WhatsApp: ${recovered}`);
    cleanupOutgoingTracking(now);
  } catch (error) {
    console.warn('Резервная синхронизация исходящих:', error && error.stack ? error.stack.split('\n')[0] : (error.message || error));
  } finally {
    recentOutgoingBusy = false;
  }
}


// QUEUE_OUTGOING_MEDIA_PROBE_3_3_63
function scheduleOutgoingMediaProbe(message, rawChatId) {
  const messageId = serializedId(message && message.id);
  const chatId = String(rawChatId || serializedId(message && message.to) || serializedId(message && message.from) || '').trim();
  if (!messageId || !chatId || isRememberedInternalOutgoing(messageId)) return;
  if (reconciledOutgoingMediaIds.has(messageId)) return;

  const previous = outgoingMediaProbe.get(messageId) || {};
  outgoingMediaProbe.set(messageId, {
    chat_id: chatId,
    body: String(messageText(message) || previous.body || '').trim(),
    type: String((message && message.type) || previous.type || 'chat'),
    timestamp: Number((message && message.timestamp) || previous.timestamp || Math.floor(Date.now() / 1000)),
    attempts: Number(previous.attempts || 0),
    next_at: Number(previous.next_at || (Date.now() + 1400)),
  });

  // Keep this queue bounded. It is only for very recent manual outgoing messages.
  if (outgoingMediaProbe.size > 80) {
    const oldest = [...outgoingMediaProbe.entries()]
      .sort((a, b) => Number(a[1]?.timestamp || 0) - Number(b[1]?.timestamp || 0))
      .slice(0, outgoingMediaProbe.size - 80);
    for (const [id] of oldest) outgoingMediaProbe.delete(id);
  }
}

async function outgoingMediaRawState(messageId) {
  const mid = String(messageId || '').trim();
  if (!mid || !client.pupPage) return null;
  try {
    return await Promise.race([
      client.pupPage.evaluate((msgId) => {
        try {
          const Collections = window.require('WAWebCollections');
          const Msg = Collections && Collections.Msg;
          let msg = Msg && typeof Msg.get === 'function' ? Msg.get(msgId) : null;
          if (!msg) return null;
          const data = msg._data || msg;
          return {
            type: String(msg.type || data.type || 'chat'),
            body: String(msg.body || msg.caption || data.body || data.caption || ''),
            has_media: Boolean(
              msg.hasMedia || msg.directPath || msg.mediaKey || msg.mediaData ||
              msg.mimetype || msg.clientUrl || msg.deprecatedMms3Url || msg.filehash || msg.encFilehash ||
              Number(msg.size || msg.mediaSize || 0) > 0 ||
              data.directPath || data.mediaKey || data.mediaData ||
              data.mimetype || data.clientUrl || data.deprecatedMms3Url || data.filehash || data.encFilehash ||
              Number(data.size || data.mediaSize || 0) > 0 ||
              ['image','video','audio','ptt','document','sticker'].includes(String(msg.type || data.type || ''))
            ),
            forwarded: Boolean(
              msg.isForwarded || msg.forwarded || data.isForwarded || data.forwarded ||
              Number(msg.forwardingScore || data.forwardingScore || 0) > 0
            ),
          };
        } catch (_) {
          return null;
        }
      }, mid),
      new Promise((resolve) => setTimeout(() => resolve(null), 1800)),
    ]);
  } catch (_) {
    return null;
  }
}

async function retryOutgoingMediaProbe() {
  if (outgoingMediaProbeBusy || !outgoingMediaProbe.size || shutdownStarted || !connectorOperational) return;
  outgoingMediaProbeBusy = true;
  try {
    const now = Date.now();
    const due = [...outgoingMediaProbe.entries()]
      .filter(([, meta]) => Number(meta.next_at || 0) <= now)
      .slice(0, 1);

    for (const [messageId, meta] of due) {
      try {
        if (isRememberedInternalOutgoing(messageId) || reconciledOutgoingMediaIds.has(messageId)) {
          outgoingMediaProbe.delete(messageId);
          continue;
        }

        let rawChatId = String(meta.chat_id || '').trim();
        let message = null;
        try {
          message = cachedMessageObject(messageId) || await Promise.race([
            client.getMessageById(messageId),
            new Promise((resolve) => setTimeout(() => resolve(null), 2600)),
          ]);
          if (message) {
            rawChatId = serializedId(message.to) || serializedId(message.from) || rawChatId;
            rememberMessageObject(message, rawChatId);
          }
        } catch (_) {}

        const rawState = await outgoingMediaRawState(messageId);
        const messageType = String((message && message.type) || (rawState && rawState.type) || meta.type || 'chat');
        const looksLikeMedia = Boolean(
          (message && (
            message.hasMedia || message._data?.directPath || message._data?.mediaKey || message._data?.mediaData ||
            ['image','video','audio','ptt','document','sticker'].includes(String(message.type || ''))
          )) ||
          (rawState && rawState.has_media) ||
          ['image','video','audio','ptt','document','sticker'].includes(messageType)
        );

        if (looksLikeMedia) {
          let destinationChatId = rawChatId;
          if (destinationChatId && !destinationChatId.endsWith('@g.us') && !destinationChatId.endsWith('@c.us')) {
            destinationChatId = await resolveDirectPhoneId(destinationChatId).catch(() => '') || destinationChatId;
          }
          if (!destinationChatId) {
            meta.attempts = Number(meta.attempts || 0) + 1;
            meta.next_at = now + 3500;
            outgoingMediaProbe.set(messageId, meta);
            continue;
          }

          const item = message
            ? liveMessageItem(message, true, String(messageText(message) || meta.body || rawState?.body || '').trim(), false)
            : {
                id: String(messageId),
                from_me: true,
                sender: 'Рабочий WhatsApp',
                body: String(meta.body || rawState?.body || '[Вложение]'),
                type: messageType,
                timestamp: Number(meta.timestamp || Math.floor(Date.now() / 1000)),
                ack: 1,
                notify: false,
                forwarded: Boolean(meta.forwarded || rawState?.forwarded),
              };
          item.sender = 'Рабочий WhatsApp';

          if (message) await attachMediaToItem(message, item);
          if (!item.media_base64 && !item.media_too_large) {
            const direct = await downloadMediaDirectById(messageId);
            applyMediaResult(item, direct, message || { type: messageType });
          }

          if (item.media_base64 || item.media_too_large) {
            await postJson('/api/chat-messages-sync', {
              chat_id: destinationChatId,
              append: true,
              messages: [item],
            });
            reconciledOutgoingMediaIds.set(messageId, Date.now());
            pendingMediaMessages.delete(messageId);
            outgoingMediaProbe.delete(messageId);
            console.log(`Исходящее медиа с телефона/WhatsApp Web догружено: ${messageId}`);
            continue;
          }

          // Media is confirmed but bytes are not ready yet. Reuse the normal
          // pending-media queue, while keeping this low-frequency probe alive.
          if (!pendingMediaMessages.has(messageId)) {
            pendingMediaMessages.set(messageId, {
              chat_id: destinationChatId,
              sender: 'Рабочий WhatsApp', sender_phone: '', sender_id: '',
              from_me: true, notify: false, forwarded: Boolean(item.forwarded || meta.forwarded || rawState?.forwarded),
              body: String(item.body || meta.body || '[Вложение]'),
              type: messageType,
              timestamp: Number(item.timestamp || meta.timestamp || Math.floor(Date.now() / 1000)),
              attempts: 0, next_at: Date.now() + 1800,
            });
          }
        }

        meta.attempts = Number(meta.attempts || 0) + 1;
        // 1.4s, then roughly 4s, 8s, 14s, 22s. Do not hammer Chromium.
        const delays = [2500, 4000, 6500, 9000, 12000, 15000];
        if (meta.attempts >= 18) {
          outgoingMediaProbe.delete(messageId);
        } else {
          meta.next_at = now + delays[Math.min(meta.attempts - 1, delays.length - 1)];
          outgoingMediaProbe.set(messageId, meta);
        }
      } catch (error) {
        meta.attempts = Number(meta.attempts || 0) + 1;
        if (meta.attempts >= 18) outgoingMediaProbe.delete(messageId);
        else {
          meta.next_at = Date.now() + Math.min(12000, 2500 + meta.attempts * 1800);
          outgoingMediaProbe.set(messageId, meta);
        }
        console.warn('Отложенная проверка исходящего медиа:', error.message || error);
      }
    }
  } finally {
    outgoingMediaProbeBusy = false;
  }
}

async function retryPendingMedia() {
  if (pendingMediaBusy || !pendingMediaMessages.size) return;
  pendingMediaBusy = true;
  try {
    const now = Date.now();
    const entries = [...pendingMediaMessages.entries()]
    .filter(([, meta]) => Number(meta.next_at || 0) <= now)
    .slice(0, 4);
  for (const [messageId, meta] of entries) {
    try {
      const locallyRecoveredUntil = Number(locallyRecoveredMediaIds.get(String(messageId)) || 0);
      if (locallyRecoveredUntil > Date.now()) {
        pendingMediaMessages.delete(messageId);
        outgoingMediaProbe.delete(messageId);
        continue;
      }
      if (locallyRecoveredUntil) locallyRecoveredMediaIds.delete(String(messageId));
      if (isRememberedInternalOutgoing(messageId)) {
        pendingMediaMessages.delete(messageId);
        outgoingMediaProbe.delete(messageId);
        continue;
      }
      let message = null;
      try {
        message = await Promise.race([
          client.getMessageById(messageId),
          new Promise((resolve) => setTimeout(() => resolve(null), 2500)),
        ]);
      } catch (_) {}

      const item = message
        ? liveMessageItem(message, Boolean(meta.from_me), messageText(message), Boolean(meta.notify))
        : {
            id: String(messageId),
            from_me: Boolean(meta.from_me),
            sender: String(meta.sender || ''),
            body: String(meta.body || '[Вложение]'),
            type: String(meta.type || 'media'),
            timestamp: Number(meta.timestamp || Math.floor(Date.now() / 1000)),
            ack: 0,
            notify: Boolean(meta.notify),
            forwarded: Boolean(meta.forwarded),
          };
      item.forwarded = Boolean(item.forwarded || meta.forwarded);
      item.sender = String(meta.sender || item.sender || '');
      item.sender_phone = String(meta.sender_phone || item.sender_phone || '');
      item.sender_id = String(meta.sender_id || item.sender_id || '');

      if (message) await attachMediaToItem(message, item);
      if (!item.media_base64) {
        const direct = await downloadMediaDirectById(messageId);
        applyMediaResult(item, direct, message || { type: meta.type || 'media' });
      }

      if (item.media_base64) {
        await postJson('/api/chat-messages-sync', {
          chat_id: String(meta.chat_id || ''),
          append: true,
          messages: [item],
        });
        if (item.media_pending) throw new Error("Передача вложения в панель не завершена");
        pendingMediaMessages.delete(messageId);
        if (meta.from_me) reconciledOutgoingIds.set(messageId, Date.now());
        console.log(`Вложение WhatsApp догружено и сохранено: ${messageId}`);
        continue;
      }

      meta.attempts = Number(meta.attempts || 0) + 1;
      meta.next_at = now + Math.min(12000, 1800 + meta.attempts * 700);
      if (meta.attempts >= 36) {
        pendingMediaMessages.delete(messageId);
        console.warn(`Не удалось скачать вложение после ${meta.attempts} попыток: ${messageId}`);
      } else {
        pendingMediaMessages.set(messageId, meta);
      }
    } catch (error) {
      meta.attempts = Number(meta.attempts || 0) + 1;
      meta.next_at = now + Math.min(12000, 1800 + meta.attempts * 700);
      if (meta.attempts >= 36) {
        pendingMediaMessages.delete(messageId);
        console.warn('Не удалось догрузить вложение WhatsApp:', error.message || error);
      } else {
        pendingMediaMessages.set(messageId, meta);
      }
    }
  }
  } finally {
    pendingMediaBusy = false;
  }
}


async function runFastSync() {
  if (fastSyncBusy || shutdownStarted || !connectorOperational) return;
  fastSyncBusy = true;
  try {
    // Serialize Puppeteer-heavy recovery tasks. Running them in parallel can
    // make Chromium collect an outstanding evaluation promise and produce
    // Runtime.callFunctionOn: Promise was collected.
    await reconcileRecentInbound();
    await reconcileRecentOutgoing();
    await retryPendingMedia();
    await retryOutgoingMediaProbe();
  } finally {
    fastSyncBusy = false;
  }
}


async function repairStoredMessageQuotes(chatId, messageIds) {
  const targetChatId = String(chatId || '').trim();
  const ids = Array.isArray(messageIds) ? messageIds.map((v) => String(v || '').trim()).filter(Boolean).slice(0, 20) : [];
  if (!targetChatId || !ids.length || quoteRepairBusy || typeof client.getMessageById !== 'function') return;
  quoteRepairBusy = true;
  try {
    const now = Date.now();
    for (const messageId of ids) {
      const checkedAt = Number(quoteProbeAt.get(messageId) || 0);
      if (checkedAt && now - checkedAt < 5 * 60 * 1000) continue;
      quoteProbeAt.set(messageId, now);
      let message = null;
      try {
        message = cachedMessageObject(messageId) || await client.getMessageById(messageId);
        if (message) rememberMessageObject(message, targetChatId);
      } catch (_) {}
      if (!message) continue;
      const item = liveMessageItem(message, Boolean(message.fromMe), String(messageText(message) || '').trim(), false);
      item.sender = '';
      item.sender_phone = '';
      item.sender_id = '';
      item.mentions = [];
      await attachQuoteToItem(message, item);
      if (!item.quoted_message_key) continue;
      await postJson('/api/chat-messages-sync', {
        chat_id: targetChatId,
        append: true,
        messages: [item],
      }).catch(() => {});
    }
    const cutoff = Date.now() - 30 * 60 * 1000;
    for (const [id, at] of quoteProbeAt) if (Number(at || 0) < cutoff) quoteProbeAt.delete(id);
  } finally {
    quoteRepairBusy = false;
  }
}

async function syncPresence(ids) {
  if (presenceSyncBusy) return;
  const cleanIds = [...new Set((Array.isArray(ids) ? ids : []).map((id) => String(id || '').trim()).filter((id) => id && !id.endsWith('@g.us')).slice(0, 16))];
  if (!cleanIds.length) return;
  const signature = cleanIds.join('|');
  const now = Date.now();
  if (signature === lastPresenceSignature && now - lastPresenceSyncAt < 7000) return;
  if (signature !== lastPresenceSignature && now - lastPresenceSyncAt < 1500) return;
  lastPresenceSignature = signature;
  lastPresenceSyncAt = now;
  presenceSyncBusy = true;
  try {
    const items = await Promise.race([
      client.pupPage.evaluate(async (inputIds) => {
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const timeout = async (promise, ms) => Promise.race([promise, sleep(ms).then(() => null)]);
        const widText = (value) => {
          if (!value) return '';
          if (typeof value === 'string') return value;
          if (typeof value._serialized === 'string') return value._serialized;
          if (value.user && value.server) return `${value.user}@${value.server}`;
          return '';
        };
        let Collections = null;
        let WidFactory = null;
        try { Collections = window.require('WAWebCollections'); } catch (_) {}
        try { WidFactory = window.require('WAWebWidFactory'); } catch (_) {}
        const readOne = async (chatId) => {
          let chat = null;
          try {
            if (window.WWebJS && typeof window.WWebJS.getChat === 'function') {
              chat = await timeout(window.WWebJS.getChat(chatId, { getAsModel: false }), 900);
            }
          } catch (_) {}
          if (!chat && Collections && Collections.Chat) {
            try {
              const wid = WidFactory && typeof WidFactory.createWid === 'function' ? WidFactory.createWid(chatId) : chatId;
              chat = Collections.Chat.get(wid) || Collections.Chat.get(chatId) || null;
              if (!chat && typeof Collections.Chat.find === 'function') chat = await timeout(Collections.Chat.find(wid), 900);
            } catch (_) {}
          }
          if (!chat) return { chat_id: chatId, known: false, online: false, state: '' };
          let presence = chat.presence || chat.__x_presence || null;
          let subscribed = false;
          try {
            if (presence && typeof presence.subscribe === 'function') {
              await timeout(Promise.resolve(presence.subscribe()), 1200);
              subscribed = true;
            }
          } catch (_) {}
          if (!subscribed) {
            try {
              const bridge = window.require('WAWebContactPresenceBridge');
              const fn = bridge && (bridge.subscribeUserPresence || bridge.subscribePresence);
              if (typeof fn === 'function') {
                const wid = WidFactory && typeof WidFactory.createWid === 'function' ? WidFactory.createWid(chatId) : chatId;
                await timeout(Promise.resolve(fn(wid)), 1200);
                subscribed = true;
              }
            } catch (_) {}
          }
          if (subscribed) await sleep(180);
          try {
            if (window.WWebJS && typeof window.WWebJS.getChat === 'function') {
              const refreshed = await timeout(window.WWebJS.getChat(chatId, { getAsModel: false }), 500);
              if (refreshed) chat = refreshed;
            }
          } catch (_) {}
          presence = chat.presence || chat.__x_presence || presence || null;
          const attrs = (presence && (presence.attributes || presence.__x_attributes)) || {};
          const chatstate = (presence && (presence.chatstate || presence.__x_chatstate)) || attrs.chatstate || {};
          const rawOnline = attrs.isOnline !== undefined ? attrs.isOnline : (presence && presence.isOnline !== undefined ? presence.isOnline : (presence && presence.__x_isOnline));
          const state = String((chatstate && (chatstate.type || chatstate.state || chatstate.chatstate)) || attrs.state || '').toLowerCase();
          const online = rawOnline === true || state === 'composing' || state === 'recording' || state === 'available';
          const known = online || state === 'unavailable';
          return { chat_id: chatId, known, online, state };
        };
        const result = [];
        for (let i = 0; i < inputIds.length; i += 4) {
          const part = await Promise.all(inputIds.slice(i, i + 4).map(readOne));
          result.push(...part);
        }
        return result;
      }, cleanIds),
      new Promise((resolve) => setTimeout(() => resolve([]), 6000)),
    ]);
    const rows = Array.isArray(items) ? items : [];
    if (rows.length) await postJson('/api/presence-sync', { items: rows });
  } catch (_) {
    // Presence is best-effort: privacy settings or WhatsApp Web updates can hide it.
  } finally {
    presenceSyncBusy = false;
  }
}

let connectionSyncBusy = false;
async function syncConnection() {
  if (connectionSyncBusy) return;
  connectionSyncBusy = true;
  try {
    await postJson('/api/chat-list-sync', { connected: true });
    const chatControl = await postJson('/api/chat-control', {});
    const requestedChatId = String((chatControl && chatControl.requested_chat_id) || '');
    for (const id of (chatControl.read_chat_ids || [])) markChatRead(id).catch(() => {});
    const presenceIds = Array.isArray(chatControl && chatControl.presence_ids) ? chatControl.presence_ids : (requestedChatId ? [requestedChatId] : []);
    syncPresence(presenceIds).catch(() => {});
    if (requestedChatId && !requestedChatId.endsWith('@g.us')) {
      // Only an actively visible chat may acknowledge reading.
      syncSelectedContactProfile(requestedChatId).catch(() => {});
    }
    syncContactDiscovery().catch(() => {});
    const requestedGroupId = String((chatControl && chatControl.requested_group_id) || '');
    avatarSync.sync(client,postJson,[requestedChatId,requestedGroupId].filter(Boolean)).catch(()=>{});
    const now = Date.now();
    if (requestedGroupId && (requestedGroupId !== lastRequestedGroupId || now - lastGroupParticipantSyncAt > 30000)) {
      lastRequestedGroupId = requestedGroupId;
      lastGroupParticipantSyncAt = now;
      syncGroupParticipants(requestedGroupId).catch(() => {});
    }
    if (requestedGroupId) {
      const unresolvedGroupIds = Array.isArray(chatControl && chatControl.unresolved_group_message_ids)
        ? chatControl.unresolved_group_message_ids
        : [];
      repairStoredGroupMessageIdentities(requestedGroupId, unresolvedGroupIds).catch(() => {});
    }
    const quoteProbeChatId = String((chatControl && chatControl.quote_probe_chat_id) || '');
    const quoteProbeMessageIds = Array.isArray(chatControl && chatControl.quote_probe_message_ids)
      ? chatControl.quote_probe_message_ids
      : [];
    repairStoredMessageQuotes(quoteProbeChatId, quoteProbeMessageIds).catch(() => {});
    const refreshState = await postJson('/api/group-refresh-check', {});
    const refreshRequest = String((refreshState && refreshState.request) || '');
    if (refreshRequest && refreshRequest !== lastGroupRefreshRequest) {
      lastGroupRefreshRequest = refreshRequest;
      await syncGroups();
    }
    heartbeatErrorReported = false;
  } catch (error) {
    if (!heartbeatErrorReported) {
      console.warn('Локальная панель пока недоступна:', error.message || error);
      heartbeatErrorReported = true;
    }
  } finally { connectionSyncBusy = false; }
}

async function publishConnectorState(status, qrDataUrl = '') {
  try {
    await postJson('/api/connector-state', {
      status,
      qr_data_url: qrDataUrl,
    });
  } catch (error) {
    console.warn('Не удалось обновить состояние коннектора в админке:', error.message || error);
  }
}

async function syncGroups() {
  const found = new Map();
  const diagnostics = {
    chats_ok: false,
    chats_total: 0,
    chats_groups: 0,
    contacts_ok: false,
    contacts_total: 0,
    contacts_groups: 0,
    enriched: 0,
    errors: [],
  };

  const addGroup = (item, source) => {
    if (!item) return false;
    const id = serializedId(item.id);
    if (!id.endsWith('@g.us')) return false;

    const previous = found.get(id) || {
      id,
      name: '',
      participant_count: 0,
      sources: [],
    };
    const candidateName = String(
      item.name || item.subject || item.pushname || item.shortName || ''
    ).trim();
    const participantCount = Array.isArray(item.participants)
      ? item.participants.length
      : Number(previous.participant_count || 0);

    found.set(id, {
      id,
      name: candidateName || previous.name || 'Группа WhatsApp',
      participant_count: Math.max(0, Number(participantCount || 0)),
      sources: [...new Set([...(previous.sources || []), source])],
    });
    return true;
  };

  // getChats() обычно даёт последние/текущие чаты, но не во всех сборках
  // WhatsApp Web туда попадают все группы. Поэтому это только первый источник.
  try {
    const chats = await client.getChats();
    diagnostics.chats_ok = true;
    diagnostics.chats_total = Array.isArray(chats) ? chats.length : 0;
    for (const chat of chats || []) {
      const id = serializedId(chat && chat.id);
      if (Boolean(chat && chat.isGroup) || id.endsWith('@g.us')) {
        if (addGroup(chat, 'chats')) diagnostics.chats_groups += 1;
      }
    }
  } catch (error) {
    diagnostics.errors.push(`getChats: ${error.message || error}`);
  }

  // В whatsapp-web.js группа также является Contact с isGroup=true. Этот
  // источник нужен для групп, которые не загружены в текущий список чатов.
  try {
    const contacts = await client.getContacts();
    diagnostics.contacts_ok = true;
    diagnostics.contacts_total = Array.isArray(contacts) ? contacts.length : 0;
    for (const contact of contacts || []) {
      const id = serializedId(contact && contact.id);
      if (Boolean(contact && contact.isGroup) || id.endsWith('@g.us')) {
        if (addGroup(contact, 'contacts')) diagnostics.contacts_groups += 1;
      }
    }
  } catch (error) {
    diagnostics.errors.push(`getContacts: ${error.message || error}`);
  }

  if (!diagnostics.chats_ok && !diagnostics.contacts_ok) {
    console.warn('Не удалось обновить список групп: оба источника WhatsApp недоступны.');
    if (diagnostics.errors.length) console.warn(diagnostics.errors.join(' | '));
    return;
  }

  // Если группа нашлась только как Contact, пытаемся получить её Chat, чтобы
  // подтянуть нормальное название и число участников. Ошибка одной группы не
  // прерывает весь список.
  const groupsToEnrich = [...found.values()]
    .filter((group) => !group.name || group.name === 'Группа WhatsApp' || !group.participant_count)
    .slice(0, 250);
  for (let index = 0; index < groupsToEnrich.length; index += 6) {
    const chunk = groupsToEnrich.slice(index, index + 6);
    await Promise.all(
      chunk.map(async (group) => {
        try {
          const chat = await client.getChatById(group.id);
          if (!chat) return;
          const current = found.get(group.id) || group;
          const name = String(chat.name || current.name || 'Группа WhatsApp').trim();
          const participantCount = Array.isArray(chat.participants)
            ? chat.participants.length
            : Number(current.participant_count || 0);
          found.set(group.id, {
            ...current,
            name,
            participant_count: Math.max(0, Number(participantCount || 0)),
          });
          diagnostics.enriched += 1;
        } catch (_) {
          // Группа всё равно останется в списке с данными из Contact/getChats.
        }
      })
    );
  }

  const groups = [...found.values()]
    .map((group) => ({
      id: group.id,
      name: String(group.name || 'Группа WhatsApp').trim(),
      participant_count: Math.max(0, Number(group.participant_count || 0)),
    }))
    .sort((left, right) => left.name.localeCompare(right.name, 'ru'));

  await postJson('/api/group-list-sync', { groups, diagnostics });
  avatarSync.enqueue(groups.map(g=>g.id));
  console.log(
    `Список групп обновлён: ${groups.length}. ` +
      `Через чаты: ${diagnostics.chats_groups}/${diagnostics.chats_total}; ` +
      `через контакты: ${diagnostics.contacts_groups}/${diagnostics.contacts_total}.`
  );
  if (diagnostics.errors.length) {
    console.warn('Часть способов поиска групп дала ошибку:', diagnostics.errors.join(' | '));
  }
}

function floodStateFor(chatId) {
  const key = String(chatId || '').trim();
  if (!key) return null;
  let state = inboundFloodState.get(key);
  if (!state) {
    state = {
      timestamps: [],
      active: false,
      lastMessageAt: 0,
      lastNoticeAt: 0,
      timer: null,
      recipients: [],
      sender: '',
    };
    inboundFloodState.set(key, state);
  }
  return state;
}

function scheduleFloodNotice(chatId) {
  const state = floodStateFor(chatId);
  if (!state) return;
  if (state.timer) clearTimeout(state.timer);
  state.timer = setTimeout(async () => {
    const current = inboundFloodState.get(chatId);
    if (!current || !current.active) return;
    const quietFor = Date.now() - Number(current.lastMessageAt || 0);
    if (quietFor < FLOOD_QUIET_MS) {
      scheduleFloodNotice(chatId);
      return;
    }

    current.active = false;
    current.timestamps = [];
    current.timer = null;

    // Не шлём одно и то же предупреждение чаще раза в минуту даже если
    // пользователь снова начинает флудить. Это снижает риск ответного спама.
    if (Date.now() - Number(current.lastNoticeAt || 0) < FLOOD_NOTICE_COOLDOWN_MS) {
      console.log(`Антиспам: повторное предупреждение для ${chatId} подавлено.`);
      return;
    }

    try {
      const sentMessage = await sendToUser(current.recipients, FLOOD_NOTICE_TEXT);
      current.lastNoticeAt = Date.now();
      await publishLiveMessage(
        chatId,
        current.sender || (knownChats.get(chatId) && knownChats.get(chatId).name) || chatId.split('@')[0],
        sentMessage,
        true,
        FLOOD_NOTICE_TEXT
      ).catch(() => {});
      await markChatRead(chatId);
      console.log(`Антиспам: пользователю ${chatId} отправлено одно предупреждение после паузы.`);
    } catch (error) {
      console.warn('Антиспам: не удалось отправить предупреждение:', error.message || error);
    }
  }, FLOOD_QUIET_MS);
}

function registerPersonalInbound(chatId, recipients, sender) {
  const state = floodStateFor(chatId);
  if (!state) return false;
  const now = Date.now();
  state.timestamps = state.timestamps.filter((timestamp) => now - timestamp <= FLOOD_WINDOW_MS);
  state.timestamps.push(now);
  state.lastMessageAt = now;
  state.recipients = [...new Set((recipients || []).map((value) => String(value || '').trim()).filter(Boolean))];
  state.sender = String(sender || state.sender || '');

  if (!state.active && state.timestamps.length < FLOOD_MESSAGE_LIMIT) {
    return false;
  }

  state.active = true;
  scheduleFloodNotice(chatId);
  return true;
}

async function sendToUser(recipients, content) {
  const targets = [...new Set(recipients.map(value => String(value || '').trim()).filter(Boolean))];
  if (!targets.length) throw new Error('WhatsApp-адрес пользователя не определён');
  // A transport exception can occur after delivery. Trying the same user via
  // the next PN/LID address would send the automatic reply twice.
  const outcome = await delivery.sendOnce({
    client, recipient:targets[0], aliases:targets, content, options:{}, begin:async()=>true,
  });
  if (outcome.status !== 'sent') throw new Error(outcome.error);
  rememberInternalOutgoingMessage(outcome.message);
  return outcome.message;
}

async function sendAutomaticReply(recipients, result) {
  if (!result || !result.reply) return null;
  const targets = [...new Set((recipients || []).map((value) => String(value || '').trim()).filter(Boolean))];
  const primaryTarget = targets[0] || 'unknown';
  const kind = result.menu_gate
    ? 'menu-gate'
    : result.menu_reminder
      ? 'menu-reminder'
    : (result.awaiting_category || result.main_menu || result.support_menu)
      ? 'menu'
      : result.awaiting_details
        ? `details:${String(result.category || 'general')}`
        : result.template_error_id
          ? 'template-error'
          : result.created
            ? 'accepted'
            : 'generic';
  const cooldown = result.force_menu
    ? 0
    : kind === 'menu'
      ? AUTO_REPLY_MENU_COOLDOWN_MS
    : kind === 'menu-gate'
      ? 5000
    : kind === 'menu-reminder'
      ? 8000
      : (kind.startsWith('details:') || kind === 'template-error')
        ? AUTO_REPLY_HINT_COOLDOWN_MS
        : 0;
  const cooldownKey = `${primaryTarget}|${kind}`;
  const now = Date.now();
  const lastSent = Number(autoReplyCooldowns.get(cooldownKey) || 0);
  if (cooldown && now - lastSent < cooldown) {
    result.reply_suppressed = true;
    console.log(`Повторный автоответ ${kind} для ${primaryTarget} подавлен.`);
    return null;
  }

  const exactText = `${String(result.preface || '').trim()}\n${String(result.reply || '').trim()}`.trim();
  const exactKey = `${primaryTarget}|${exactText}`;
  const exactLastSent = Number(autoReplyExactCooldowns.get(exactKey) || 0);
  if (exactLastSent && now - exactLastSent < AUTO_REPLY_EXACT_COOLDOWN_MS) {
    result.reply_suppressed = true;
    console.log(`Точный повтор автоответа для ${primaryTarget} подавлен.`);
    return null;
  }
  if (autoReplyInflight.has(exactKey)) {
    result.reply_suppressed = true;
    console.log(`Параллельный дубль автоответа для ${primaryTarget} подавлен.`);
    return null;
  }
  autoReplyInflight.add(exactKey);

  try {
    // Интерактивное меню отключено намеренно.
    // После команды 1 пользователь получает один обычный текстовый список.
    if (result.awaiting_category || result.main_menu || result.support_menu) {
      const plainMenuText = result.support_menu
        ? String(result.reply || 'Напишите свой вопрос одним сообщением.')
        : String(result.reply || MAIN_MENU_TEXT);

      const reminderText = result.preface ? String(result.preface).trim() : '';
      const fingerprint = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase('ru');
      // Всегда отправляем ОДИН WhatsApp message. Если preface действительно
      // отличается, он объединяется с основным ответом в одном пузыре. Это
      // исключает дубль даже когда сервер вернул визуально одинаковый текст
      // с разными переносами строк/пробелами.
      const distinctReminderText = reminderText && fingerprint(reminderText) !== fingerprint(plainMenuText)
        ? reminderText
        : '';
      const combinedText = distinctReminderText
        ? `${distinctReminderText}\n\n${plainMenuText}`
        : plainMenuText;
      const textMessage = await sendToUser(targets, combinedText);
      const textChatId = serializedId(textMessage && textMessage.to) || primaryTarget;
      await publishLiveMessage(textChatId, 'Система', textMessage, true, combinedText).catch(() => {});
      result.delivered_reply = combinedText;
      autoReplyCooldowns.set(cooldownKey, Date.now());
      autoReplyExactCooldowns.set(exactKey, Date.now());
      return textMessage;
    }

    const deliveredReply = String(result.reply);
    const textMessage = await sendToUser(targets, deliveredReply);
    const replyChatId = serializedId(textMessage && textMessage.to) || primaryTarget;
    await publishLiveMessage(replyChatId, 'Система', textMessage, true, deliveredReply).catch(() => {});
    result.delivered_reply = deliveredReply;
    if (cooldown) autoReplyCooldowns.set(cooldownKey, Date.now());
    autoReplyExactCooldowns.set(exactKey, Date.now());
    return textMessage;
  } finally {
    autoReplyInflight.delete(exactKey);
    const cleanupBefore = Date.now() - 5 * 60 * 1000;
    for (const [key, sentAt] of autoReplyExactCooldowns) {
      if (Number(sentAt || 0) < cleanupBefore) autoReplyExactCooldowns.delete(key);
    }
  }
}

function messageMatchesActionHint(message, hint) {
  if (!message || !hint || typeof hint !== 'object') return false;
  const expectedBody = String(hint.body || '').trim();
  const expectedMedia = String(hint.media_name || '').trim();
  const expectedTimestamp = Number(hint.timestamp || 0);
  const body = String(messageText(message) || '').trim();
  const mediaName = String((message._data && (message._data.filename || message._data.fileName)) || '').trim();
  if (expectedBody && body !== expectedBody) return false;
  if (!expectedBody && expectedMedia && mediaName && mediaName !== expectedMedia) return false;
  const ts = Number(message.timestamp || 0);
  if (expectedTimestamp && ts && Math.abs(ts - expectedTimestamp) > 10) return false;
  return Boolean(expectedBody || expectedMedia);
}

function actionMessageKeyParts(messageKey) {
  const wanted = String(messageKey || '').trim();
  if (!wanted) return { remote: '', stanza: '' };
  const first = wanted.indexOf('_');
  const last = wanted.lastIndexOf('_');
  if (first < 0 || last <= first) return { remote: '', stanza: '' };
  const middle = wanted.slice(first + 1, last);
  const split = middle.lastIndexOf('_');
  if (split <= 0 || split >= middle.length - 1) return { remote: '', stanza: '' };
  return {
    remote: middle.slice(0, split),
    stanza: middle.slice(split + 1),
  };
}

function actionMessageStanza(message) {
  if (!message) return '';
  const direct = String(
    (message.id && message.id.id) ||
    (message._data && message._data.id && message._data.id.id) ||
    ''
  ).trim();
  if (direct) return direct;
  return actionMessageKeyParts(serializedId(message.id)).stanza;
}

async function actionChatCandidates(chatId, messageKey = '') {
  const target = String(chatId || '').trim();
  const result = [];
  const push = (value) => {
    const safe = String(value || '').trim();
    if (safe && !result.includes(safe)) result.push(safe);
  };
  push(target);
  const keyParts = actionMessageKeyParts(messageKey);
  // The DB keeps the exact WhatsApp message key. In multi-device private chats
  // it often contains the real @lid even when our canonical chat id is @c.us.
  // Reuse that remote id so an older message can still be loaded reliably.
  push(keyParts.remote);
  for (const alias of (chatIdAliases.get(target) || [])) push(alias);
  for (const alias of (chatIdAliases.get(keyParts.remote) || [])) push(alias);
  if (target.endsWith('@c.us') && typeof client.getNumberId === 'function') {
    try {
      const wid = await client.getNumberId(target.split('@')[0]);
      push(serializedId(wid));
    } catch (_) {}
  }
  return result;
}

async function resolveMessageForAction(messageKey, chatId = '', actionHint = null, forceRefresh = false) {
  const wanted = String(messageKey || '').trim();
  if (!wanted) return null;

  if (!forceRefresh) {
    const cached = cachedMessageObject(wanted);
    if (cached) return cached;
  }

  if (typeof client.getMessageById === 'function') {
    try {
      // Important: do not race Puppeteer-backed promises against a timer. The
      // orphaned browser promise is what produced Runtime.callFunctionOn:
      // "Promise was collected" in the server log.
      const direct = await client.getMessageById(wanted);
      if (direct) {
        rememberMessageObject(direct, chatId);
        return direct;
      }
    } catch (_) {}
  }

  const aliases = await actionChatCandidates(chatId, wanted);
  for (const targetChatId of aliases) {
    if (!targetChatId || typeof client.getChatById !== 'function') continue;
    try {
      const chat = await client.getChatById(targetChatId);
      if (!chat || typeof chat.fetchMessages !== 'function') continue;
      const wantedParts = actionMessageKeyParts(wanted);
      let recent = await chat.fetchMessages({ limit: 300 });
      if (!Array.isArray(recent)) continue;
      for (const item of recent) rememberMessageObject(item, chatId || targetChatId);
      let exact = recent.find((item) => serializedId(item && item.id) === wanted);
      if (!exact && wantedParts.stanza) {
        exact = recent.find((item) => Boolean(item && item.fromMe) && actionMessageStanza(item) === wantedParts.stanza);
      }
      if (exact) return exact;

      // A busy support chat can push a 15-30 minute old message outside the
      // last 300 models. Only for an explicit action, load a deeper tail once.
      // This is intentionally not used by normal chat synchronisation.
      if (wantedParts.stanza || (actionHint && Number(actionHint.timestamp || 0))) {
        try {
          const deeper = await chat.fetchMessages({ limit: 1200 });
          if (Array.isArray(deeper) && deeper.length > recent.length) {
            recent = deeper;
            for (const item of recent) rememberMessageObject(item, chatId || targetChatId);
            exact = recent.find((item) => serializedId(item && item.id) === wanted);
            if (!exact && wantedParts.stanza) {
              exact = recent.find((item) => Boolean(item && item.fromMe) && actionMessageStanza(item) === wantedParts.stanza);
            }
            if (exact) return exact;
          }
        } catch (_) {}
      }

      if (actionHint && typeof actionHint === 'object') {
        const candidates = recent.filter((item) => Boolean(item && item.fromMe) && messageMatchesActionHint(item, actionHint));
        if (candidates.length) {
          const expectedTimestamp = Number(actionHint.timestamp || 0);
          candidates.sort((a, b) =>
            Math.abs(Number(a.timestamp || 0) - expectedTimestamp) -
            Math.abs(Number(b.timestamp || 0) - expectedTimestamp)
          );
          return candidates[0];
        }
      }
    } catch (_) {}
  }

  // Last fallback: scan exact Message objects captured by live/message_create.
  if (actionHint && typeof actionHint === 'object') {
    const candidates = [];
    for (const entry of recentMessageObjects.values()) {
      const message = entry && entry.message;
      if (message && message.fromMe && messageMatchesActionHint(message, actionHint)) candidates.push(message);
    }
    if (candidates.length) {
      const expectedTimestamp = Number(actionHint.timestamp || 0);
      candidates.sort((a, b) =>
        Math.abs(Number(a.timestamp || 0) - expectedTimestamp) -
        Math.abs(Number(b.timestamp || 0) - expectedTimestamp)
      );
      return candidates[0];
    }
  }
  return null;
}

async function resolveForwardTarget(targetChatId) {
  const raw = String(targetChatId || '').trim();
  if (!raw) return '';
  const aliases = await actionChatCandidates(raw);
  // Prefer an already observed @lid for private multi-device chats; groups stay @g.us.
  if (raw.endsWith('@g.us')) return raw;
  const lid = aliases.find((value) => value.endsWith('@lid'));
  return lid || aliases[0] || raw;
}

async function directDeleteInWhatsApp(messageKey, chatId, hint) {
  const aliases = await actionChatCandidates(chatId, messageKey);
  const payload = {
    body: String(hint && hint.body || '').trim(),
    media_name: String(hint && hint.media_name || '').trim(),
    timestamp: Number(hint && hint.timestamp || 0),
  };
  return await client.pupPage.evaluate(async (wanted, chatAliases, expected) => {
    const widText = (value) => {
      if (!value) return '';
      if (typeof value === 'string') return value;
      if (typeof value._serialized === 'string') return value._serialized;
      if (typeof value.$1 === 'string') return value.$1;
      if (value.user && value.server) return `${value.user}@${value.server}`;
      return '';
    };
    const errorText = (error) => {
      if (!error) return 'unknown error';
      const name = String(error.name || '').trim();
      const message = String(error.message || error || '').trim();
      return `${name ? name + ': ' : ''}${message}`.slice(0, 900);
    };
    const collections = window.require('WAWebCollections');
    const Msg = collections.Msg;
    const Chat = collections.Chat;
    let msg = Msg.get(wanted) || (await Msg.getMessagesById([wanted]))?.messages?.[0] || null;
    if (!msg) {
      let wantedStanza = '';
      try {
        const first = wanted.indexOf('_');
        const last = wanted.lastIndexOf('_');
        const middle = first >= 0 && last > first ? wanted.slice(first + 1, last) : '';
        const split = middle.lastIndexOf('_');
        wantedStanza = split > 0 ? middle.slice(split + 1) : '';
      } catch (_) {}
      if (wantedStanza) {
        let models = [];
        try {
          models = typeof Msg.getModelsArray === 'function' ? Msg.getModelsArray() : (Array.isArray(Msg.models) ? Msg.models : []);
        } catch (_) {}
        msg = (Array.isArray(models) ? models : []).slice(-2500).find((candidate) => {
          if (!candidate || !(candidate.id && candidate.id.fromMe)) return false;
          return String(candidate.id.id || '') === wantedStanza;
        }) || null;
      }
    }
    if (!msg && expected && (expected.body || expected.media_name)) {
      let models = [];
      try {
        models = typeof Msg.getModelsArray === 'function' ? Msg.getModelsArray() : (Array.isArray(Msg.models) ? Msg.models : []);
      } catch (_) {}
      const aliasSet = new Set(Array.isArray(chatAliases) ? chatAliases : []);
      const matches = (Array.isArray(models) ? models : []).slice(-2500).filter((candidate) => {
        if (!candidate || !(candidate.id && candidate.id.fromMe)) return false;
        const remote = widText((candidate.id && candidate.id.remote) || candidate.to || candidate.from);
        if (aliasSet.size && remote && !aliasSet.has(remote)) return false;
        const body = String(candidate.body || candidate.caption || '').trim();
        const filename = String(candidate.filename || candidate.fileName || '').trim();
        if (expected.body && body !== expected.body) return false;
        if (!expected.body && expected.media_name && filename && filename !== expected.media_name) return false;
        const ts = Number(candidate.t || candidate.timestamp || 0);
        if (expected.timestamp && ts && Math.abs(ts - expected.timestamp) > 12) return false;
        return true;
      });
      if (matches.length) {
        matches.sort((a, b) => Math.abs(Number(a.t || a.timestamp || 0) - Number(expected.timestamp || 0)) - Math.abs(Number(b.t || b.timestamp || 0) - Number(expected.timestamp || 0)));
        msg = matches[0];
      }
    }
    if (!msg) return { ok: false, reason: 'not_found' };

    // WhatsApp Web's local capability helper can return false for valid
    // outgoing multi-device messages (especially IDs whose remote is @lid).
    // Treat it as diagnostic only. Returning here used to prevent every
    // direct revoke fallback from running at all. The actual sendRevokeMsgs
    // call below is authoritative: if WhatsApp really rejects the revoke,
    // it will throw and we keep the concrete error in the journal.
    let canSender = false;
    let canAdmin = false;
    try {
      const capability = window.require('WAWebMsgActionCapability');
      canSender = Boolean(capability.canSenderRevokeMsg(msg));
      canAdmin = Boolean(capability.canAdminRevokeMsg(msg));
    } catch (_) {}
    const capabilityAllowed = canSender || canAdmin;

    // Old/stale multi-device messages may contain an @lid remote while the
    // active chat model lives under the canonical @c.us id. 3.3.80 only used
    // msg.id.remote, so both of its delete paths could still hit the same bad
    // chat model. Build several live chat candidates before revoking.
    const chats = [];
    const seenChats = new Set();
    const pushChat = (chat, label) => {
      if (!chat) return;
      const id = widText(chat.id || chat.__x_id || chat.wid) || label;
      const key = id || label;
      if (seenChats.has(key)) return;
      seenChats.add(key);
      chats.push({ chat, label: label || id || 'chat' });
    };
    try { pushChat(msg.chat, 'msg.chat'); } catch (_) {}
    try { pushChat(Chat.get(msg.id.remote), `remote:get:${widText(msg.id.remote)}`); } catch (_) {}
    try { pushChat(await Chat.find(msg.id.remote), `remote:find:${widText(msg.id.remote)}`); } catch (_) {}
    for (const alias of (Array.isArray(chatAliases) ? chatAliases : [])) {
      try { pushChat(Chat.get(alias), `alias:get:${alias}`); } catch (_) {}
      try { pushChat(await Chat.find(alias), `alias:find:${alias}`); } catch (_) {}
    }
    if (!chats.length) return { ok: false, reason: 'chat_not_found', id: widText(msg.id) };

    const { Cmd } = window.require('WAWebCmd');
    const errors = [];
    const preferredNew = Boolean(window.WWebJS && window.WWebJS.compareWwebVersions && window.WWebJS.compareWwebVersions(window.Debug.VERSION, '>=', '2.3000.0'));
    const signatures = preferredNew ? ['new', 'old'] : ['old', 'new'];

    for (const entry of chats) {
      for (const signature of signatures) {
        for (const clearMedia of [false, true]) {
          try {
            if (signature === 'new') {
              await Cmd.sendRevokeMsgs(entry.chat, { list: [msg], type: 'message' }, { clearMedia });
            } else {
              await Cmd.sendRevokeMsgs(entry.chat, [msg], {
                clearMedia,
                type: msg.id.fromMe ? 'Sender' : 'Admin',
              });
            }
            return {
              ok: true,
              id: widText(msg.id),
              chat: entry.label,
              signature,
              clear_media: clearMedia,
              web_version: String(window.Debug && window.Debug.VERSION || ''),
              capability_allowed: capabilityAllowed,
              capability_sender: canSender,
              capability_admin: canAdmin,
            };
          } catch (error) {
            errors.push(`${entry.label}/${signature}/clearMedia=${clearMedia}: ${errorText(error)}`);
          }
        }
      }
    }

    return {
      ok: false,
      reason: 'revoke_failed',
      id: widText(msg.id),
      remote: widText(msg.id && msg.id.remote),
      web_version: String(window.Debug && window.Debug.VERSION || ''),
      errors: errors.slice(0, 12),
      capability_allowed: capabilityAllowed,
      capability_sender: canSender,
      capability_admin: canAdmin,
    };
  }, String(messageKey || ''), aliases, payload);
}

async function directForwardInWhatsApp(messageKey, sourceChatId, targetChatId, hint) {
  const aliases = await actionChatCandidates(sourceChatId);
  const payload = {
    body: String(hint && hint.body || '').trim(),
    media_name: String(hint && hint.media_name || '').trim(),
    timestamp: Number(hint && hint.timestamp || 0),
  };
  return await client.pupPage.evaluate(async (wanted, sourceAliases, target, expected) => {
    const widText = (value) => {
      if (!value) return '';
      if (typeof value === 'string') return value;
      if (typeof value._serialized === 'string') return value._serialized;
      if (typeof value.$1 === 'string') return value.$1;
      if (value.user && value.server) return `${value.user}@${value.server}`;
      return '';
    };
    const Msg = window.require('WAWebCollections').Msg;
    let msg = Msg.get(wanted) || (await Msg.getMessagesById([wanted]))?.messages?.[0] || null;
    if (!msg && expected && (expected.body || expected.media_name)) {
      let models = [];
      try { models = typeof Msg.getModelsArray === 'function' ? Msg.getModelsArray() : (Array.isArray(Msg.models) ? Msg.models : []); } catch (_) {}
      const aliases = new Set(Array.isArray(sourceAliases) ? sourceAliases : []);
      const matches = (Array.isArray(models) ? models : []).slice(-700).filter((candidate) => {
        if (!candidate) return false;
        const remote = widText((candidate.id && candidate.id.remote) || candidate.to || candidate.from);
        if (aliases.size && remote && !aliases.has(remote)) return false;
        const body = String(candidate.body || candidate.caption || '').trim();
        const filename = String(candidate.filename || candidate.fileName || '').trim();
        if (expected.body && body !== expected.body) return false;
        if (!expected.body && expected.media_name && filename && filename !== expected.media_name) return false;
        const ts = Number(candidate.t || candidate.timestamp || 0);
        if (expected.timestamp && ts && Math.abs(ts - expected.timestamp) > 12) return false;
        return true;
      });
      if (matches.length) {
        matches.sort((a, b) => Math.abs(Number(a.t || a.timestamp || 0) - Number(expected.timestamp || 0)) - Math.abs(Number(b.t || b.timestamp || 0) - Number(expected.timestamp || 0)));
        msg = matches[0];
      }
    }
    if (!msg) return { ok: false, reason: 'not_found' };
    await window.WWebJS.forwardMessage(target, widText(msg.id));
    return { ok: true, id: widText(msg.id) };
  }, String(messageKey || ''), aliases, String(targetChatId || ''), payload);
}

async function deleteMessageReliably(messageKey, chatId, hint, initialMessage) {
  let directError = null;
  try {
    const direct = await directDeleteInWhatsApp(messageKey, chatId, hint);
    if (direct && direct.ok) return true;
    if (direct && direct.reason && direct.reason !== 'not_found') {
      const details = Array.isArray(direct.errors) && direct.errors.length ? ` | ${direct.errors.join(' || ')}` : '';
      console.warn(`Удаление WhatsApp: прямой revoke вернул ${direct.reason} для ${messageKey}; remote=${direct.remote || ''}; web=${direct.web_version || ''}; capability=${direct.capability_allowed === false ? 'false(advisory)' : String(direct.capability_allowed ?? '')}${details}`);
    }
    if (direct && direct.reason === 'revoke_failed' && Array.isArray(direct.errors) && direct.errors.length) {
      directError = new Error(`WhatsApp revoke failed: ${direct.errors[0]}`);
    }
  } catch (error) {
    directError = error;
  }

  let message = initialMessage;
  let lastError = directError;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (!message && attempt === 0) message = await resolveMessageForAction(messageKey, chatId, hint, false);
    if (!message && attempt === 1) message = await resolveMessageForAction(messageKey, chatId, hint, true);
    if (!message) continue;
    if (!message.fromMe) throw new Error('Удалять можно только сообщения рабочего аккаунта');
    if (typeof message.delete !== 'function') throw new Error('Эта версия WhatsApp Web не поддерживает удаление сообщения');
    try {
      const deleted = await message.delete(true, false);
      if (deleted === false) throw new Error('WhatsApp не разрешил удалить это сообщение для всех');
      return true;
    } catch (error) {
      lastError = error;
      message = null;
    }
  }
  if (lastError) throw lastError;
  throw new Error('Сообщение не найдено в активной сессии WhatsApp Web');
}

async function pollWhatsappAction() {
  let queued = null;
  try {
    const result = await postJson('/api/whatsapp-action/claim', {});
    queued = result && result.action;
    if (!queued) return;
    let actionHint = null;
    let forwardTargetRaw = '';
    if (queued.action_type === 'delete' && queued.body) {
      try { actionHint = JSON.parse(String(queued.body || '')); } catch (_) {}
    }
    if (queued.action_type === 'forward' && queued.body) {
      try {
        const parsed = JSON.parse(String(queued.body || ''));
        if (parsed && typeof parsed === 'object') {
          forwardTargetRaw = String(parsed.target_chat_id || '').trim();
          actionHint = parsed;
        }
      } catch (_) {
        forwardTargetRaw = String(queued.body || '').trim();
      }
    }
    let message = await resolveMessageForAction(String(queued.message_key || ''), String(queued.chat_id || ''), actionHint);
    if (queued.action_type === 'edit') {
      const aliases = await actionChatCandidates(String(queued.chat_id || ''));
      const messageKey = serializedId(message && message.id) || String(queued.message_key || '');
      const result = await client.pupPage.evaluate(delivery.editInPage, messageKey, String(queued.body || ''), aliases);
      if (!result?.ok) {
        if (result?.detail) console.warn('Диагностика редактирования WhatsApp:', result.detail);
        throw new Error(delivery.editError(result));
      }
      if (message) {
        message.body = String(queued.body || '');
        message.latestEditSenderTimestampMs = Date.now();
        rememberMessageObject(message, String(queued.chat_id || ''));
      }
    } else if (queued.action_type === 'react') {
      if (!message) message = await resolveMessageForAction(String(queued.message_key || ''), String(queued.chat_id || ''), null, true);
      if (!message) throw new Error('Сообщение для реакции не найдено в активной сессии WhatsApp Web');
      if (typeof message.react !== 'function') throw new Error('Текущая версия WhatsApp Web не поддерживает реакции');
      await message.react(String(queued.body || ''));
      rememberMessageObject(message, String(queued.chat_id || ''));
    } else if (queued.action_type === 'delete') {
      await deleteMessageReliably(
        String(queued.message_key || ''),
        String(queued.chat_id || ''),
        actionHint,
        message
      );
    } else if (queued.action_type === 'forward') {
      const targetRaw = forwardTargetRaw || String(queued.body || '').trim();
      const target = await resolveForwardTarget(targetRaw);
      if (!target) throw new Error('Не выбран чат для пересылки');
      let directResult = null;
      let directError = null;
      try {
        directResult = await directForwardInWhatsApp(
          String(queued.message_key || ''),
          String(queued.chat_id || ''),
          target,
          actionHint
        );
      } catch (error) {
        directError = error;
      }
      if (!(directResult && directResult.ok)) {
        if (!message) message = await resolveMessageForAction(String(queued.message_key || ''), String(queued.chat_id || ''), actionHint, true);
        if (!message) {
          if (directError) throw directError;
          throw new Error('Исходное сообщение не найдено в активной сессии WhatsApp Web');
        }
        if (typeof message.forward !== 'function') throw new Error('Текущая версия WhatsApp не поддерживает пересылку');
        try {
          await message.forward(target);
        } catch (firstError) {
          if (!targetRaw || targetRaw === target) throw firstError;
          await message.forward(targetRaw);
        }
      }
      // forward() in whatsapp-web.js returns void in current releases. The
      // resulting outgoing message is captured by message_create/recovery and
      // receives the normal «Переслано» marker there.
    } else {
      throw new Error('Неизвестное действие');
    }
    await postJson('/api/whatsapp-action/result', {
      action_id: queued.id,
      status: 'sent',
    });
    console.log(`Действие ${queued.action_type} для сообщения выполнено.`);
  } catch (error) {
    const rawReason = String((error && error.message) || error || 'Ошибка');
    const reason = rawReason.length < 4 ? delivery.editError({reason:'unavailable'}) : rawReason;
    if (queued && queued.id) {
      try {
        await postJson('/api/whatsapp-action/result', {
          action_id: queued.id,
          status: 'failed',
          error: reason,
        });
      } catch (_) {}
    }
    if (queued) console.warn('Не удалось изменить сообщение WhatsApp:', reason);
  }
}

async function pollOutbound() {
  if (outboundBusy) return;
  outboundBusy = true;
  let queued = null;
  let heartbeat = null;
  let delivered = false;
  let deliveredId = '';
  let releaseFile = null;
  try {
    // CONNECTED alone can precede reinjection of the WWebJS bridge.
    const bridgeReady = await client.pupPage.evaluate(() => {
      try { return Boolean(window.WWebJS?.sendMessage && window.WWebJS?.getChat && window.require('WAWebCollections')?.Msg); }
      catch (_) { return false; }
    }).catch(() => false);
    if (!bridgeReady) return;
    await pollWhatsappAction();
    const result = await postJson('/api/outbound/claim', {
      created_after: connectorReadyAt,
    });
    queued = result.message;
    if (!queued) return;
    heartbeat = setInterval(() => postJson('/api/outbound/heartbeat', {message_id: queued.id}).catch(() => {}), 15000);
    const phoneDigits = String(queued.phone || '').replace(/\D/g, '');
    const chatRecipient = String(queued.chat_id || '').trim();
    const phoneRecipient = phoneDigits ? `${phoneDigits}@c.us` : '';
    const recipient = phoneRecipient || chatRecipient;
    if (!recipient) throw new Error('WhatsApp-адрес пользователя не определён');
    const mentionIds = Array.isArray(queued.mentions)
      ? queued.mentions.map((value) => String(value || '').trim()).filter(Boolean)
      : [];
    const sendAliases = await actionChatCandidates(recipient);
    for (const alias of new Set([recipient, ...sendAliases])) markInternalOutgoing(alias, 20000);
    markActiveInternalOutgoing(queued, [recipient, ...sendAliases], 35000);
    const replyTo = String(queued.reply_to_key || '').trim();
    const options = {mentions:mentionIds, waitUntilMsgSent:false, ignoreQuoteErrors:false};
    let replyTargetObject = null;
    let replyProviderId = replyTo;
    if (replyTo) {
      replyTargetObject = cachedMessageObject(replyTo) || await resolveMessageForAction(replyTo, recipient, null).catch(() => null);
      const resolvedReplyId = serializedId(replyTargetObject && replyTargetObject.id);
      if (resolvedReplyId) replyProviderId = resolvedReplyId;
      options.quotedMessageId = replyProviderId;
    }
    let content = String(queued.body || '');
    if (queued.media_path) {
      const mediaPath = String(queued.media_path || '');
      if (!fs.existsSync(mediaPath)) throw new Error('Файл для отправки больше не найден на сервере');
      const info = {name:String(queued.media_name || 'Вложение'), mime:String(queued.media_mime || 'application/octet-stream'), size:fs.statSync(mediaPath).size};
      if (info.size>512*1024*1024) throw new Error('Файл превышает 512 МБ');
      if (info.size>largeMedia.threshold) {
        const prepared=await largeMedia.prepare(client,mediaPath,MessageMedia,info);
        content=prepared.content; releaseFile=prepared.dispose;
        options.sendMediaAsDocument=true;
      } else {
        content=new MessageMedia(info.mime,fs.readFileSync(mediaPath,{encoding:'base64'}),info.name,info.size);
        options.sendMediaAsDocument=!/^(image|video|audio)\//.test(info.mime);
      }
      if (String(queued.body || '').trim()) options.caption=String(queued.body);

    }
    const outcome = await delivery.sendOnce({
      client, recipient, content, options, aliases: sendAliases,
      begin: async () => Boolean((await postJson('/api/outbound/begin', {message_id:queued.id})).updated),
    });
    if (outcome.status !== 'sent') {
      await postJson('/api/outbound/result', {message_id:queued.id, status:outcome.status, error:outcome.error});
      console.warn(outcome.error, outcome.detail || '');
      return;
    }
    const sentMessage = outcome.message;
    delivered = true;
    deliveredId = serializedId(sentMessage && sentMessage.id);
    rememberMessageObject(sentMessage, recipient);
    const providerId = rememberInternalOutgoingMessage(sentMessage) || deliveredId || normalizeMessageIdObject(sentMessage);
    activeInternalOutgoingFingerprint = null;
    let quotedBody = String(queued.reply_preview_body || '').trim().slice(0, 1200);
    let quotedSender = String(queued.reply_preview_sender || '').trim().slice(0, 100);
    if (replyTo) {
      const quoted = replyTargetObject || cachedMessageObject(replyTo) || await resolveMessageForAction(replyTo, recipient, null).catch(() => null);
      if (quoted) {
        const liveQuotedBody = String(messageText(quoted) || '').trim().slice(0, 1200);
        if (liveQuotedBody) quotedBody = liveQuotedBody;
        if (!quotedBody && quoted.hasMedia) quotedBody = `[${String(quoted.type || 'Вложение')}]`;
        quotedSender = quoted.fromMe ? 'Вы' : (quotedSender || 'Пользователь');
        try {
          if (!quoted.fromMe && typeof quoted.getContact === 'function') {
            const contact = await quoted.getContact();
            quotedSender = String(contact && (contact.pushname || contact.name || contact.shortName) || '').trim() || quotedSender;
          }
        } catch (_) {}
      }
      if (!quotedBody) quotedBody = 'Сообщение';
      if (!quotedSender) quotedSender = 'Сообщение';
    }
    await postJson('/api/outbound/result', {
      message_id: queued.id,
      status: 'sent',
      provider_id: providerId,
      reply_to_key: replyTo,
      quoted_body: quotedBody,
      quoted_sender: quotedSender,
    });

    // QUEUE_3_3_70_AUTHORITATIVE_LOCAL_MEDIA_SYNC
    // The outbound-result endpoint preserves the original upload under the
    // queue id. Publish one exact provider-id observation that explicitly
    // references that queue id. This makes local history independent from the
    // fragile early WhatsApp media object used by Win+Shift+S clipboard sends.
    if (providerId && queued.media_path) {
      const mime = String(queued.media_mime || 'application/octet-stream');
      const localType = mime.startsWith('image/') ? 'image'
        : mime.startsWith('video/') ? 'video'
        : mime.startsWith('audio/') ? 'audio' : 'document';
      await postJson('/api/chat-messages-sync', {
        chat_id: String(queued.chat_id || recipient),
        append: true,
        messages: [{
          id: providerId,
          from_me: true,
          sender: String(queued.actor || 'Вы'),
          body: String(queued.body || ''),
          type: localType,
          timestamp: Number((sentMessage && sentMessage.timestamp) || Math.floor(Date.now() / 1000)),
          ack: Math.max(1, Math.min(4, Number((sentMessage && sentMessage.ack) || 0), Number(messageAcknowledgements.get(providerId) || 0))),
          notify: false,
          media_mime: mime,
          media_name: String(queued.media_name || 'Вложение'),
          outbound_message_id: Number(queued.id || 0),
          quoted_message_key: replyTo,
          quoted_body: quotedBody,
          quoted_sender: quotedSender,
        }],
      });
      console.log(`Локальная копия исходящего вложения #${queued.id} привязана к WhatsApp ID: ${providerId}`);
    }
    console.log(
      queued.ticket_id
        ? `Ответ по заявке #${queued.ticket_id} отправлен пользователю`
        : 'Сообщение из вкладки WhatsApp отправлено пользователю'
    );
    // /api/outbound/result persists this exact provider id in the DB. Publishing
    // it a second time here was the main source of duplicate bubbles. Only use
    // the live fallback if WhatsApp did not return a provider id at all.
    if (!providerId) {
      await publishLiveMessage(
        recipient,
        String(queued.actor || 'Вы'),
        sentMessage,
        true,
        String(queued.body || '')
      ).catch(() => {});
    }
  } catch (error) {
    const reason = error && error.message ? error.message : String(error);
    console.error('Не удалось отправить ответ из заявки:', reason);
    if (queued && queued.id) {
      try {
        await postJson('/api/outbound/result', {
          message_id: queued.id,
          status: delivered ? 'sent' : 'failed',
          provider_id: deliveredId,
          reply_to_key: queued.reply_to_key || '',
          error: delivered ? '' : reason,
        });
      } catch (ackError) {
        console.error('Не удалось сохранить результат отправки:', ackError.message || ackError);
      }
    }
  } finally {
    if (heartbeat) clearInterval(heartbeat);
    if (releaseFile) await releaseFile();
    activeInternalOutgoingFingerprint = null;
    outboundBusy = false;
  }
}

client.on('qr', async (qr) => {
  console.log('\nОткрой в телефоне: WhatsApp -> Связанные устройства -> Привязка устройства');
  console.log('Отсканируй QR-код ниже:\n');
  qrcode.generate(qr, { small: true });
  try {
    const qrDataUrl = await QRCode.toDataURL(qr, { width: 360, margin: 2 });
    await publishConnectorState('qr', qrDataUrl);
  } catch (error) {
    console.warn('Не удалось показать QR-код в админке:', error.message || error);
  }
});

function stopReadyFallback() {
  if (readyFallbackTimer) {
    clearInterval(readyFallbackTimer);
    readyFallbackTimer = null;
  }
}

function connectorServiceRssMb() {
  try {
    const raw = fs.readFileSync('/sys/fs/cgroup/system.slice/edinaya-ochered-whatsapp.service/memory.current', 'utf8').trim();
    const bytes = Number(raw || 0);
    if (Number.isFinite(bytes) && bytes > 0) return bytes / 1024 / 1024;
  } catch (_) {}
  return process.memoryUsage().rss / 1024 / 1024;
}

async function reportPerformance() {
  if (shutdownStarted) return;
  try {
    const memory = process.memoryUsage();
    const nodeRssMb = memory.rss / 1024 / 1024;
    const rssMb = connectorServiceRssMb();
    await postJson('/api/performance-telemetry', {
      rss_mb: Math.round(rssMb * 10) / 10,
      node_rss_mb: Math.round(nodeRssMb * 10) / 10,
      heap_mb: Math.round(memory.heapUsed / 1024 / 1024 * 10) / 10,
      external_mb: Math.round(memory.external / 1024 / 1024 * 10) / 10,
      pending_media: pendingMediaMessages.size,
      outgoing_probe: outgoingMediaProbe.size,
      fast_sync_busy: fastSyncBusy,
      pending_media_busy: pendingMediaBusy,
      contact_cooldown_ms: PERF_CONTACT_COOLDOWN_MS,
      recovery_interval_ms: PERF_RECOVERY_INTERVAL_MS,
      ram_restart_mb: PERF_RAM_RESTART_MB,
    });
    if (PERF_RAM_RESTART_MB > 0 && rssMb >= PERF_RAM_RESTART_MB) highMemorySamples += 1;
    else highMemorySamples = 0;
    // QUEUE_3_3_109_NO_MESSAGE_RESTART
    // 3.3.107 автоматически завершал процесс после трёх замеров выше RAM-порога.
    // На рабочих системах это выглядело как падение коннектора сразу после
    // входящего/исходящего сообщения, а systemd тут же поднимал его снова.
    // Оставляем телеметрию и предупреждение, но больше не завершаем процесс.
    if (highMemorySamples >= 3 && connectorOperational && !outboundBusy && !fastSyncBusy && !pendingMediaBusy) {
      console.warn(`Память WhatsApp-коннектора ${rssMb.toFixed(0)} МБ выше порога ${PERF_RAM_RESTART_MB} МБ. Автоперезапуск отключён, коннектор продолжает работу.`);
      highMemorySamples = 0;
    }
  } catch (_) {}
}

async function activateConnector(source = 'ready') {
  if (connectorOperational) return;
  connectorOperational = true;
  connectorReadyAt = Math.floor(Date.now() / 1000);
  stopReadyFallback();
  console.log(
    source === 'ready'
      ? '\nWhatsApp QR-коннектор готов.'
      : `\nWhatsApp подключён. Рабочий режим включён по состоянию ${source}, даже если событие ready не пришло.`
  );
  console.log('Заявки создаются только после выбора пункта цифрового меню.');
  console.log('Префикс не требуется.');
  console.log('Новые сообщения групп передаются во вкладку «Группы».');
  console.log('Ответы из карточек заявок отправляются через это подключение.');
  publishConnectorState('ready').catch((error) => {
    console.warn('Не удалось опубликовать состояние ready:', error.message || error);
  });
  // Сначала поднимаем лёгкие live-процессы, а полный обход групп запускаем
  // чуть позже. Так вкладки получают сохранённые данные и новые сообщения,
  // пока WhatsApp Web спокойно заканчивает инициализацию.
  syncConnection().catch(() => {});
  setTimeout(() => {
    syncContactDiscovery().catch(() => {});
    runFastSync().catch(() => {});
  }, 150);
  setTimeout(() => {
    syncGroups().catch((error) => {
      console.warn('Первичная синхронизация групп пока недоступна:', error.message || error);
    });
  }, 900);
  if (!outboundTimer) {
    outboundTimer = setInterval(pollOutbound, 800);
    setTimeout(() => { void pollOutbound(); }, 100);
  }
  if (!heartbeatTimer) {
    heartbeatTimer = setInterval(syncConnection, 2000);
  }
  if (!performanceTimer) {
    performanceTimer = setInterval(() => { void reportPerformance(); }, 15000);
    setTimeout(() => { void reportPerformance(); }, 1200);
  }
  if (!fastInboundTimer) {
    fastInboundTimer = setInterval(() => { void runFastSync(); }, PERF_RECOVERY_INTERVAL_MS);
    setTimeout(() => { void runFastSync(); }, 250);
    console.log(`Резервная синхронизация WhatsApp активна: ${PERF_RECOVERY_INTERVAL_MS} мс; новые события обрабатываются сразу.`);
  }
  if (!groupSyncTimer) {
    groupSyncTimer = setInterval(syncGroups, 5 * 60 * 1000);
  }
  startIncomingCallPolling();
}

async function checkReadyFallback() {
  if (connectorOperational || shutdownStarted) return;
  try {
    const state = await client.getState();
    const normalized = String(state || '').toUpperCase();
    if (normalized === 'CONNECTED') {
      console.warn('Событие ready не пришло, но WhatsApp уже сообщает CONNECTED. Включаю рабочий режим.');
      await activateConnector('CONNECTED');
      return;
    }
    const elapsed = Math.floor((Date.now() - readyFallbackStartedAt) / 1000);
    if (elapsed > 0 && elapsed % 30 < 6) {
      console.log(`Ожидание готовности WhatsApp: ${normalized || 'UNKNOWN'}, ${elapsed} сек.`);
    }
  } catch (error) {
    const elapsed = Math.floor((Date.now() - readyFallbackStartedAt) / 1000);
    if (elapsed > 0 && elapsed % 30 < 6) {
      console.warn('Проверка состояния WhatsApp во время запуска:', error.message || error);
    }
  }
}

function startReadyFallback() {
  stopReadyFallback();
  readyFallbackStartedAt = Date.now();
  readyFallbackTimer = setInterval(checkReadyFallback, 1500);
  setTimeout(checkReadyFallback, 600);
}

client.on('authenticated', () => {
  console.log('WhatsApp подтвердил подключение. Загружаю сессию...');
  publishConnectorState('connecting');
  startReadyFallback();
});

client.on('change_state', (state) => {
  const normalized = String(state || '').toUpperCase();
  console.log(`Состояние WhatsApp: ${normalized || 'UNKNOWN'}`);
  if (normalized === 'CONNECTED') {
    activateConnector('change_state=CONNECTED');
  }
});

client.on('ready', () => {
  activateConnector('ready');
});

client.on('auth_failure', (message) => {
  console.error('Ошибка авторизации WhatsApp:', message);
  publishConnectorState('offline');
});

client.on('disconnected', (reason) => {
  connectorOperational = false;
  stopReadyFallback();
  stopIncomingCallPolling();
  console.error('WhatsApp отключил сессию:', reason);
  console.error('Запусти коннектор снова и при необходимости отсканируй новый QR.');
  postJson('/api/chat-list-sync', { connected: false }).catch(() => {});
  publishConnectorState('offline');
});

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

    const replyResult = {
      reply: `${CALL_REJECT_NOTICE_TEXT}\n\n${START_MENU_TEXT}`,
      menu_gate: true,
    };
    await sendAutomaticReply([phoneId, callerId], replyResult);
    console.log(`После отклонения звонка отправлена инструкция открыть меню заявок: ${canonicalChatId}`);
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
  if (callPollBusy || shutdownStarted || !connectorOperational) return;
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

// QUEUE_3_3_116_REMOTE_DELETE_SYNC
// QUEUE_3_3_118_REMOTE_DELETE_ORIGINAL_KEY
async function mirrorWhatsAppDeletion(message, revokedMessage) {
  try {
    const serializeMessageKey = (value) => {
      if (!value) return '';
      const direct = serializedId(value);
      if (direct) return direct;
      if (typeof value === 'object') {
        const remote = serializedId(value.remote);
        const stanza = String(value.id || '').trim();
        if (remote && stanza) return `${Boolean(value.fromMe)}_${remote}_${stanza}`;
      }
      return '';
    };

    const messageKey = (item) => (item && (item.id || (item._data && item._data.id))) || null;
    const protocolKey = (message && (
      message.protocolMessageKey ||
      (message._data && message._data.protocolMessageKey) ||
      (message._data && message._data.protocolMessage && message._data.protocolMessage.key)
    )) || null;

    const revokedKey = messageKey(revokedMessage);
    const currentKey = messageKey(message);
    const originalKey = revokedKey || protocolKey || currentKey;
    const messageId = serializeMessageKey(originalKey);
    const currentMessageId = serializeMessageKey(currentKey);
    const protocolMessageId = serializeMessageKey(protocolKey);

    if (!messageId) {
      console.warn('WhatsApp remote-delete: не удалось определить ID исходного сообщения');
      return;
    }

    const fromMeFromSerialized = (value) => {
      if (String(value || '').startsWith('true_')) return true;
      if (String(value || '').startsWith('false_')) return false;
      return null;
    };
    const originalFromKey = originalKey && typeof originalKey === 'object' && typeof originalKey.fromMe === 'boolean'
      ? Boolean(originalKey.fromMe)
      : fromMeFromSerialized(messageId);
    const fromMe = originalFromKey !== null
      ? originalFromKey
      : Boolean(
          revokedMessage && (revokedMessage.fromMe || revokedMessage.id?.fromMe || revokedMessage._data?.id?.fromMe)
        );

    const timestamp = Number(
      (revokedMessage && revokedMessage.timestamp) ||
      (message && message.timestamp) ||
      Math.floor(Date.now() / 1000)
    );
    const type = String((message && message.type) || 'revoked');

    const remoteFromKey = (key, serialized) => {
      if (key && typeof key === 'object') {
        const remote = serializedId(key.remote);
        if (remote) return remote;
      }
      const text = String(serialized || '');
      const first = text.indexOf('_');
      const last = text.lastIndexOf('_');
      if (first >= 0 && last > first) return text.slice(first + 1, last);
      return '';
    };

    let rawChatId = remoteFromKey(originalKey, messageId);
    const candidates = [revokedMessage, message].filter(Boolean);
    if (!rawChatId) {
      const readId = (value) => {
        if (!value) return '';
        if (typeof value === 'string') return value;
        return serializedId(value);
      };
      for (const item of candidates) {
        const raw = (item && item._data) || {};
        const remote = readId(item?.id?.remote) || readId(raw?.id?.remote);
        const from = readId(item?.from) || readId(raw?.from);
        const to = readId(item?.to) || readId(raw?.to);
        const preferred = fromMe ? (to || remote || from) : (from || remote || to);
        if (preferred && (preferred.endsWith('@g.us') || preferred.endsWith('@c.us') || preferred.endsWith('@lid'))) {
          rawChatId = preferred;
          break;
        }
      }
    }
    if (!rawChatId) {
      console.warn(`WhatsApp remote-delete: не удалось определить чат для ${messageId}`);
      return;
    }

    let canonicalChatId = rawChatId;
    if (!rawChatId.endsWith('@g.us')) {
      canonicalChatId = (await resolveDirectPhoneId(rawChatId)) || rawChatId;
    }

    // Cancel late hydration for both the original message and the technical
    // revoke event so deleted media cannot come back after sync.
    const cleanupIds = new Set([messageId, currentMessageId, protocolMessageId].filter(Boolean));
    for (const id of cleanupIds) {
      pendingMediaMessages.delete(id);
      outgoingMediaProbe.delete(id);
      locallyRecoveredMediaIds.delete(id);
      recentMessageObjects.delete(id);
    }

    await postJson('/api/chat-messages-sync', {
      chat_id: canonicalChatId,
      source_chat_id: rawChatId,
      append: true,
      remote_delete: true,
      messages: [{
        id: messageId,
        from_me: Boolean(fromMe),
        body: '',
        type,
        timestamp,
        ack: 0,
        notify: false,
        media_mime: '',
        media_name: '',
        transcript: '',
        mentions: [],
        quoted_message_key: '',
        quoted_body: '',
        quoted_sender: '',
        forwarded: false,
        edited: false,
        edit_timestamp: 0,
        deleted: true,
        reactions: {},
      }],
    });
    // QUEUE_1_00_1_DELETE_PREVIEW_SYNC
    // Keep the chat-list preview in lockstep with the message tombstone.
    // Only replace the preview when the revoked message is still the latest
    // known message, so deleting an older message cannot hide a newer one.
    const previousChat = knownChats.get(canonicalChatId);
    if (previousChat && Number(timestamp || 0) >= Number(previousChat.timestamp || 0)) {
      knownChats.set(canonicalChatId, {
        ...previousChat,
        id: canonicalChatId,
        last_message: 'Сообщение удалено',
        timestamp: Number(timestamp || previousChat.timestamp || 0),
        last_from_me: Boolean(fromMe),
      });
      const chats = [...knownChats.values()]
        .sort((left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0))
        .slice(0, 100);
      await postJson('/api/chat-list-sync', { connected: true, chats });
    }

    console.log(
      `Удаление WhatsApp зеркально применено в Единой очереди: ${canonicalChatId} | original=${messageId} | current=${currentMessageId || '-'} | protocol=${protocolMessageId || '-'}`
    );
  } catch (error) {
    console.warn('Не удалось зеркально применить удаление WhatsApp:', error.message || error);
  }
}

client.on('message_revoke_everyone', (message, revokedMessage) => {
  void mirrorWhatsAppDeletion(message, revokedMessage);
});

client.on('call', (call) => { void handleIncomingCall(call, 'event:call'); });
client.on('incoming_call', (call) => { void handleIncomingCall(call, 'event:incoming_call'); });

client.on('message_reaction', async (reaction) => {
  try {
    const messageRef = reaction && (reaction.msgId || reaction.messageId || reaction.parentMsgId);
    const messageId = serializedId(messageRef);
    if (!messageId) return;
    let rawChatId = serializedId(messageRef && messageRef.remote);
    let message = cachedMessageObject(messageId);
    if (!message && typeof client.getMessageById === 'function') {
      try { message = await client.getMessageById(messageId); } catch (_) {}
    }
    if (message) {
      rememberMessageObject(message, rawChatId || '');
      rawChatId = rawChatId || serializedId(message.to) || serializedId(message.from) || serializedId(message.id && message.id.remote);
    }
    if (!rawChatId) return;
    let canonicalChatId = rawChatId;
    if (!rawChatId.endsWith('@g.us') && !rawChatId.endsWith('@c.us')) {
      canonicalChatId = await resolveDirectPhoneId(rawChatId).catch(() => '') || rawChatId;
    }
    const senderId = serializedId(reaction && (reaction.senderId || reaction.sender || reaction.author));
    const ownId = serializedId(client.info && client.info.wid);
    const ownDigits = ownId.endsWith('@c.us') ? ownId.split('@')[0] : '';
    const senderDigits = senderId.endsWith('@c.us') ? senderId.split('@')[0] : '';
    const fromMe = Boolean(
      senderId && (
        senderId === ownId ||
        ownMentionIds.has(senderId) ||
        (ownDigits && senderDigits && ownDigits === senderDigits)
      )
    );
    const emoji = String(reaction && (reaction.reaction ?? reaction.emoji) || '').slice(0, 32);
    await postJson('/api/chat-reaction-sync', {
      chat_id: canonicalChatId,
      message_id: messageId,
      sender_id: senderId || (fromMe ? ownId : 'unknown'),
      from_me: fromMe,
      emoji,
    });
  } catch (error) {
    console.warn('Не удалось синхронизировать реакцию WhatsApp:', error.message || error);
  }
});

client.on('message_ack', (message, ack) => {
  const messageId = serializedId(message && message.id);
  const chatId = serializedId(message && message.to) || serializedId(message && message.from);
  if (!messageId || !chatId) return;
  const safeAck = Math.max(0, Math.min(4, Number(ack) || 0));
  messageAcknowledgements.set(
    messageId,
    Math.max(Number(messageAcknowledgements.get(messageId) || 0), safeAck)
  );
  postJson('/api/chat-message-ack', {
    chat_id: chatId,
    message_id: messageId,
    ack: safeAck,
  }).then((result) => {
    if (result && result.updated) {
      persistedAcknowledgements.set(
        messageId,
        Math.max(Number(persistedAcknowledgements.get(messageId) || 0), safeAck)
      );
    }
  }).catch((error) => {
    console.warn('Не удалось обновить отметку доставки сообщения:', error.message || error);
  });
});

client.on('message_create', async (message) => {
  try {
    if (!message || !message.fromMe || !isLiveInboundMessage(message)) return;
    if (!USER_MESSAGE_TYPES.has(String(message.type || ''))) return;
    const rawChatId = serializedId(message.to) || serializedId(message.from);
    if (!rawChatId || rawChatId === 'status@broadcast' || rawChatId.endsWith('@newsletter')) return;

    // message_create fires before client.sendMessage() returns. For messages
    // sent by the Queue UI we therefore do not yet know the provider id here.
    // Recognise the in-flight chat marker first, then immediately bind the exact
    // WhatsApp id/stanza to the internal send. Otherwise the same screenshot is
    // imported as a "manual" message and its media is endlessly re-downloaded.
    const outgoingEventId = normalizeMessageIdObject(message) || serializedId(message && message.id);
    let internalChatEvent = isRememberedInternalOutgoing(outgoingEventId) || isInternalOutgoing(rawChatId) || matchesActiveInternalOutgoing(message, rawChatId);
    let canonicalForInternal = rawChatId;
    if (!internalChatEvent && !rawChatId.endsWith('@g.us') && !rawChatId.endsWith('@c.us')) {
      const resolvedInternalChat = await resolveDirectPhoneId(rawChatId).catch(() => '');
      if (resolvedInternalChat) {
        canonicalForInternal = resolvedInternalChat;
        internalChatEvent = isInternalOutgoing(resolvedInternalChat);
      }
    }
    if (internalChatEvent) {
      const marker = activeInternalOutgoingFingerprint && Date.now() <= Number(activeInternalOutgoingFingerprint.until || 0)
        ? {...activeInternalOutgoingFingerprint} : null;
      const exactId = rememberInternalOutgoingMessage(message) || outgoingEventId;
      rememberMessageObject(message, canonicalForInternal || rawChatId);
      if (exactId) {
        reconciledOutgoingIds.set(exactId, Date.now());
        pendingMediaMessages.delete(exactId);
        outgoingMediaProbe.delete(exactId);
      }

      // QUEUE_3_3_94_CLIPBOARD_EARLY_LOCAL_ECHO
      // This is intentionally independent from message.hasMedia/downloadMedia().
      // Win+Shift+S -> Ctrl+V is uploaded by our own web UI, therefore the
      // outbound queue is the authoritative source of the image bytes.
      if (exactId && marker && marker.has_media && Number(marker.queue_id || 0) > 0) {
        const aliases = Array.isArray(marker.aliases) ? marker.aliases : [];
        const authoritativeChatId = String(marker.chat_id || '').trim();
        const targetChatId = authoritativeChatId || String(canonicalForInternal || rawChatId || '').trim();
        const markerChatMatches = aliases.includes(String(rawChatId || '')) || aliases.includes(String(canonicalForInternal || '')) || authoritativeChatId === String(rawChatId || '') || authoritativeChatId === String(canonicalForInternal || '');
        const liveBody = String(incomingText(message) || '').trim();
        const markerBody = String(marker.body || '').trim();
        const bodyCompatible = !markerBody || !liveBody || markerBody === liveBody;
        if (targetChatId && markerChatMatches && bodyCompatible) {
          const mime = String(marker.media_mime || 'application/octet-stream');
          const localType = mime.startsWith('image/') ? 'image'
            : mime.startsWith('video/') ? 'video'
            : mime.startsWith('audio/') ? 'audio' : 'document';
          try {
            const localResult = await postJson('/api/chat-messages-sync', {
              chat_id: targetChatId,
              // Tell the server which WhatsApp transport alias produced the
              // event. The server persists a safe @lid -> @c.us mapping and
              // merges any already-created duplicate card.
              source_chat_id: String(rawChatId || ''),
              append: true,
              messages: [{
                id: exactId,
                from_me: true,
                sender: String(marker.actor || 'Вы'),
                body: markerBody,
                type: localType,
                timestamp: Number(message.timestamp || Math.floor(Date.now() / 1000)),
                ack: 1,
                notify: false,
                media_mime: mime,
                media_name: String(marker.media_name || 'Вложение'),
                outbound_message_id: Number(marker.queue_id || 0),
                quoted_message_key: String(marker.reply_to_key || ''),
                quoted_body: String(marker.quoted_body || ''),
                quoted_sender: String(marker.quoted_sender || ''),
              }],
            });
            if (localResult && Array.isArray(localResult.local_media_recovered) && localResult.local_media_recovered.includes(exactId)) {
              console.log(`Локальный скриншот #${marker.queue_id} восстановлен из outbound-хранилища: ${exactId}`);
            } else {
              console.log(`Локальное вложение #${marker.queue_id} синхронизировано по раннему WhatsApp ID: ${exactId}`);
            }
          } catch (error) {
            console.warn(`Не удалось локально восстановить исходящий скриншот #${marker.queue_id}:`, error.message || error);
          }
        }
      }
      return;
    }
    scheduleOutgoingMediaProbe(message, rawChatId);

    if (rawChatId.endsWith('@g.us')) {
      const text = String(messageText(message) || '').trim();
      await publishLiveMessage(rawChatId, 'Рабочий WhatsApp', message, true, text, false, false).catch(() => {});
      return;
    }

    const phoneId = rawChatId.endsWith('@c.us') ? rawChatId : await resolveDirectPhoneId(rawChatId);
    const canonicalChatId = phoneId || rawChatId;
    const text = String(messageText(message) || '').trim();
    await publishLiveMessage(
      canonicalChatId,
      'Рабочий WhatsApp',
      message,
      true,
      text,
      false,
      false
    );
    if (outgoingEventId) reconciledOutgoingIds.set(outgoingEventId, Date.now());
    console.log(`Исходящее сообщение с рабочего WhatsApp сохранено в системе: ${canonicalChatId}`);
  } catch (error) {
    console.warn('Не удалось обработать ручное исходящее сообщение WhatsApp:', error.message || error);
  }
});

client.on('message', async (message) => {
  let templateErrorId = 0;
  let stage = 'проверка сообщения';
  const incomingEventId = serializedId(message && message.id);
  let handledSuccessfully = false;
  if (incomingEventId) {
    if (reconciledInboundIds.has(incomingEventId)) return;
    const startedAt = Number(inboundProcessingIds.get(incomingEventId) || 0);
    if (startedAt && Date.now() - startedAt < 60000) return;
    inboundProcessingIds.set(incomingEventId, Date.now());
  }
  try {
    const from = String(message.from || '');
    if (from.endsWith('@g.us')) {
      if (
        message.fromMe ||
        !USER_MESSAGE_TYPES.has(String(message.type || '')) ||
        !isLiveInboundMessage(message)
      ) {
        return;
      }
      const groupText = incomingText(message);
      const fallbackSender = String((message._data && message._data.notifyName) || '').trim();
      const groupSenderInfo = await resolveGroupSenderInfo(message, fallbackSender);
      const groupSender = groupSenderInfo.name || fallbackSender || 'Участник группы';
      const mentionedUs = await mentionsConnectedAccount(message);
      // В группах система НИКОГДА не запускает обработчик заявок и не отправляет
      // автоответы. Сообщение сохраняется в переписку. Начиная с 1.00.2 глобальный
      // центр уведомлений показывает все непрочитанные сообщения незаглушённых
      // групп, а реальное @упоминание дополнительно помечается как упоминание.
      await publishLiveMessage(
        from,
        groupSender,
        message,
        false,
        groupText,
        mentionedUs,
        false,
        groupSenderInfo.phone || '',
        groupSenderInfo.id || groupSenderInfo.resolved_id || ''
      );
      handledSuccessfully = true;
      if (mentionedUs) {
        console.log(`Упоминание рабочего WhatsApp в группе: ${from} | ${groupSender}`);
      }
      return;
    }
    if (
      message.fromMe ||
      !from ||
      from === 'status@broadcast' ||
      from.endsWith('@newsletter')
    ) {
      return;
    }
    if (!USER_MESSAGE_TYPES.has(String(message.type || ''))) {
      console.log(`Служебное событие WhatsApp пропущено без ответа: ${message.type || 'unknown'}`);
      return;
    }
    if (!isLiveInboundMessage(message)) {
      console.log('Старое событие WhatsApp пропущено без ответа.');
      return;
    }

    // getChat() и getContact() намеренно не вызываются. После обновлений
    // WhatsApp Web эти дополнительные запросы иногда падают с ошибкой "r".
    // Всё необходимое для заявки уже есть в событии входящего сообщения.
    stage = 'определение номера пользователя';
    const phoneId = await resolvePhoneId(message, from);
    const phoneDigits = phoneId ? phoneId.split('@')[0] : '';
    // Для личного чата всегда используем номерной @c.us, если WhatsApp смог
    // сопоставить приватный @lid с телефоном. Так один человек не появляется
    // в списке дважды: отдельно под именем и отдельно под номером.
    const canonicalChatId = phoneId || from;
    const sender =
      String((message._data && message._data.notifyName) || '').trim() ||
      phoneDigits ||
      'Пользователь WhatsApp';
    const attachmentName = message.hasMedia
      ? `WhatsApp ${message.type || 'media'}`
      : '';
    const text = incomingText(message);

    // Политику контакта узнаём ДО публикации сообщения. Раньше коннектор
    // возвращался сразу после этой проверки, из-за чего у добавленного через
    // админку пользователя сообщение могло потеряться для панели. Теперь
    // ручной контакт всё равно проходит через серверный /api/whatsapp, где
    // сообщение надёжно сохраняется, но заявки и автоответы не создаются.
    let contactPolicy = null;
    let contactPolicyChecked = false;
    try {
      contactPolicy = await postJson('/api/contact-policy', {
        chat_id: canonicalChatId,
        phone: phoneDigits ? `+${phoneDigits}` : '',
      });
      contactPolicyChecked = true;
    } catch (error) {
      console.warn('Не удалось заранее проверить политику контакта. Сервер проверит её при обработке сообщения:', error.message || error);
    }
    const isManualContact = Boolean(contactPolicy && contactPolicy.manual_contact);

    // Любое входящее сообщение сначала сохраняем в чат, включая медиа. Для
    // контактов администратора сервер затем только отключает автоматику; повторный
    // upsert по тому же message id не удваивает уведомление.
    const savedInboundItem = await publishLiveMessage(canonicalChatId, sender, message, false, text, false, isManualContact).catch((error) => {
      console.warn('Не удалось сразу показать входящее сообщение в панели:', error.message || error);
      return null;
    });

    // Не блокируем несколько коротких сообщений подряд. Ранее антиспам мог
    // мешать обычному живому общению и сам присылал лишнее предупреждение.
    // Повторные автоматические меню/подсказки теперь ограничиваются cooldown в
    // sendAutomaticReply, поэтому сообщения пользователя всегда доходят до
    // серверной логики и не теряются.

    stage = 'передача сообщения в локальную систему';
    const result = await postJson('/api/whatsapp', {
      external_id: serializedId(message && message.id),
      sender,
      phone: phoneDigits ? `+${phoneDigits}` : '',
      chat_id: canonicalChatId,
      text,
      menu_choice: incomingMenuChoice(message),
      attachment_name: String((savedInboundItem && savedInboundItem.media_name) || attachmentName || ''),
      media_base64: String((savedInboundItem && savedInboundItem.media_base64) || ''),
      media_receipt: String((savedInboundItem && savedInboundItem.media_receipt) || ''),
      media_mime: String((savedInboundItem && savedInboundItem.media_mime) || ''),
      media_name: String((savedInboundItem && savedInboundItem.media_name) || attachmentName || ''),
      message_timestamp: Number(message.timestamp || Math.floor(Date.now() / 1000)),
      message_type: String(message.type || 'chat'),
    });
    handledSuccessfully = true;

    if (result.duplicate) {
      return;
    }
    if (result.manual_contact) {
      const displayName = String(result.name || (contactPolicy && contactPolicy.name) || sender || canonicalChatId);
      console.log(`Личное сообщение от контакта администратора сохранено без автоответа: ${displayName}`);
      return;
    }

    // Receiving a message is not evidence that an employee has read it.
    if (result.silent) {
      console.log(`Автоответчик временно молчит для живого диалога: ${sender}`);
      return;
    }
    if (result.ignored) {
      const preview = String(message.body || '').trim().replace(/\s+/g, ' ').slice(0, 90);
      console.log(`Пропущено сообщение от ${sender}: ${result.reason || 'не распознано'}${preview ? ` | ${preview}` : ''}`);
      const menuResult = {
        ...result,
        reply: result.reply || MAIN_MENU_TEXT,
        awaiting_category: true,
      };
      await sendAutomaticReply([phoneId, from], menuResult);
      return;
    }
    if (result.created) {
      console.log(`Создана заявка #${result.ticket_id}: ${result.title || result.category || 'рабочий запрос'} | ${sender}`);
      if (result.reply) {
        stage = 'отправка подтверждения пользователю';
        await sendAutomaticReply([phoneId, from], result);
        console.log(`Подтверждение по заявке #${result.ticket_id} отправлено пользователю: ${sender}`);
      }
      return;
    }
    if (result.linked) {
      console.log(`Уточнение связано с заявкой #${result.ticket_id}: ${sender}`);
      const linkedReply = `Уточнение добавлено к запросу №${result.ticket_id}.\n\n${MAIN_MENU_TEXT}`;
      await sendAutomaticReply(
        [phoneId, from],
        { reply: linkedReply, awaiting_category: true }
      );
      return;
    }
    if (!result.reply) {
      return;
    }

    templateErrorId = Number(result.template_error_id || 0);
    stage = 'отправка автоматического ответа пользователю';
    const replyMessage = await sendAutomaticReply([phoneId, from], result);
    if (templateErrorId) {
      stage = 'сохранение результата отправки';
      await postJson('/api/template-error-delivery', {
        template_error_id: templateErrorId,
        status: 'sent',
        provider_id:
          serializedId(replyMessage && replyMessage.id),
      });
      console.log(`Отправлен шаблон неполного БИН: ${sender}`);
    } else {
      console.log(`Отправлена просьба уточнить обращение: ${sender}`);
    }
  } catch (error) {
    const reason = error && error.message ? error.message : String(error);
    console.error(`Не удалось обработать сообщение (${stage}):`, reason);
    if (process.env.DEBUG_WHATSAPP === '1' && error && error.stack) {
      console.error(error.stack);
    }
    if (templateErrorId) {
      try {
        await postJson('/api/template-error-delivery', {
          template_error_id: templateErrorId,
          status: 'failed',
          error: String(error.message || error),
        });
      } catch (ackError) {
        console.error('Не удалось сохранить ошибку отправки:', ackError.message || ackError);
      }
    }
  } finally {
    if (incomingEventId) {
      inboundProcessingIds.delete(incomingEventId);
      if (handledSuccessfully) reconciledInboundIds.set(incomingEventId, Date.now());
    }
  }
});

async function gracefulShutdown(signal) {
  if (shutdownStarted) return;
  shutdownStarted = true;
  connectorOperational = false;
  console.log(`\nОстанавливаю QR-коннектор (${signal})...`);
  if (outboundTimer) clearInterval(outboundTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (fastInboundTimer) clearInterval(fastInboundTimer);
  if (groupSyncTimer) clearInterval(groupSyncTimer);
  stopReadyFallback();
  stopIncomingCallPolling();

  const timeout = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  try {
    await Promise.race([publishConnectorState('offline'), timeout(3000)]);
  } catch (error) {
    console.warn('Не удалось сообщить сайту об остановке:', error.message || error);
  }
  try {
    await Promise.race([client.destroy(), timeout(15000)]);
  } catch (error) {
    console.warn('WhatsApp Web завершён принудительно:', error.message || error);
  }
  process.exit(0);
}

process.on('SIGINT', () => { void gracefulShutdown('SIGINT'); });
process.on('SIGTERM', () => { void gracefulShutdown('SIGTERM'); });

console.log('Запускаю WhatsApp Web. Восстанавливаю сохранённую сессию...');
publishConnectorState('connecting').catch(() => {});
client.initialize().catch((error) => {
  console.error('Не удалось запустить WhatsApp Web:', error.message || error);
  process.exitCode = 1;
});
