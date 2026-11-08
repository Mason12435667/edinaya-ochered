'use strict';
// Общая основа коннектора: настройки, клиент WhatsApp, запросы к серверу и память о сообщениях и чатах.
// Этот файл не зависит от остальных частей коннектора. Изменяемое состояние, которым пользуются
// несколько частей, лежит в объекте runtime.

const fs = require('fs');
const path = require('path');
const {Client, LocalAuth} = require('whatsapp-web.js');
const {InboundSpool, isFinalRejection} = require('./queue_inbound_spool');



// Состояние, которым пользуются несколько частей коннектора.
const runtime = {
  outboundBusy: false,
  connectorReadyAt: 0,
  connectorOperational: false,
  shutdownStarted: false,
  // Recover recent inbound media that was missed while the WhatsApp connector was offline.
  startupInboundRecoveryUntil: 0,
  startupInboundRecoveryStartedAt: 0,
  pendingMediaBusy: false,
  fastSyncBusy: false,
  hotInboundUntil: 0,
  browserPressureUntil: 0,
  startupLiveOnlyUntil: 0,
  activeInternalOutgoingFingerprint: null,
};
const queueLogDir = String(process.env.QUEUE_LOG_DIR || '').trim();
function mediaDebug(message) {
  const line = `[${new Date().toISOString()}] ${String(message || '')}`;
  try {
    if (queueLogDir) { fs.mkdirSync(queueLogDir, {recursive:true}); fs.appendFileSync(path.join(queueLogDir, 'whatsapp-media.log'), line + "\n", 'utf8'); }
  } catch (_) {}
  console.log('[MEDIA]', String(message || ''));
}

const ROOT = path.join(__dirname, '..');
const inboundSpool = new InboundSpool(path.join(process.env.QUEUE_DATA_DIR || (process.platform === 'win32' ? path.join(process.env.LOCALAPPDATA || ROOT,'QueueLocal') : '/var/lib/edinaya-ochered'), 'inbound-spool'));

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
  // Legacy connector fallback only; configured menu comes from Python.
  { id: 'menu_0', title: '0. Открыть актуальное меню' },
  { id: 'active_tickets', title: '8. Мои активные заявки' },
];

const SUPPORT_ROWS = [
  { id: 'menu_0', title: '0. Открыть актуальное меню' },
];

const LANGUAGE_SELECTION_TEXT = `Здравствуйте! / Сәлеметсіз бе!
Выберите язык обслуживания / Қызмет көрсету тілін таңдаңыз (1-2):
1️⃣ — Русский
2️⃣ — Қазақша`;

// Тексты меню формирует Python-сервис с учётом сохранённого языка пользователя.
// Эта константа оставлена только как безопасный резерв для редких ошибок коннектора.
const MAIN_MENU_TEXT = LANGUAGE_SELECTION_TEXT;

async function postJson(endpoint, payload) {
  if(endpoint==='/api/whatsapp' && payload && payload.external_id)
    return inboundSpool.submit(payload, data=>postJsonWithRetry(endpoint,data), replyToRecovered);
  return postJsonWithRetry(endpoint,payload);
}

// Ответ бота на сообщение, доставленное из очереди, а не живым путём.
async function replyToRecovered(payload, result) {
  if(result.reply && !result.duplicate && !result.silent && !result.manual_contact)
    await postJsonOnce('/api/inbound-recovery-reply',{external_id:payload.external_id,chat_id:payload.chat_id,reply:result.reply});
}

async function postJsonWithRetry(endpoint, payload) {
  const retryInbound = endpoint === '/api/whatsapp' && Boolean(payload && payload.external_id);
  for (let attempt = 0; ; attempt++) {
    try { return await postJsonOnce(endpoint, payload); }
    catch (error) {
      if (!retryInbound || attempt >= 3 || isFinalRejection(error)) throw error;
      await new Promise(resolve => setTimeout(resolve, [500, 1500, 4000][attempt]));
    }
  }
}

async function postJsonOnce(endpoint, payload) {
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
        pendingMediaMessages.set(String(item.id), {chat_id:payload.chat_id,sender:item.sender || '',sender_phone:item.sender_phone || '',sender_id:item.sender_id || '',from_me:!!item.from_me,body:item.body || '',type:item.type,timestamp:item.timestamp,attempts:0,next_at:Date.now()+7000});
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
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.httpStatus = response.status;
    throw error;
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
      const matches = await Promise.race([client.getContactLidAndPhone([directId]), new Promise((resolve)=>setTimeout(()=>resolve([]),900))]);
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

function resolveBrowserExecutable() {
  const explicit = String(process.env.QUEUE_CHROME_PATH || '').trim();
  if (explicit && fs.existsSync(explicit)) return explicit;

  if (process.platform !== 'win32') return '';

  const candidates = [];
  const programFiles = String(process.env.ProgramFiles || '').trim();
  const programFilesX86 = String(process.env['ProgramFiles(x86)'] || '').trim();
  const localAppData = String(process.env.LOCALAPPDATA || '').trim();
  if (programFiles) {
    candidates.push(path.join(programFiles, 'Microsoft', 'Edge', 'Application', 'msedge.exe'));
    candidates.push(path.join(programFiles, 'Google', 'Chrome', 'Application', 'chrome.exe'));
  }
  if (programFilesX86) {
    candidates.push(path.join(programFilesX86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'));
    candidates.push(path.join(programFilesX86, 'Google', 'Chrome', 'Application', 'chrome.exe'));
  }
  if (localAppData) {
    candidates.push(path.join(localAppData, 'Google', 'Chrome', 'Application', 'chrome.exe'));
    candidates.push(path.join(localAppData, 'Yandex', 'YandexBrowser', 'Application', 'browser.exe'));
  }
  return candidates.find((candidate) => candidate && fs.existsSync(candidate)) || '';
}

const browserExecutablePath = resolveBrowserExecutable();
const puppeteerOptions = {
  headless: true,
  protocolTimeout: 30000,
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
};
if (browserExecutablePath) {
  puppeteerOptions.executablePath = browserExecutablePath;
  console.log(`Browser for WhatsApp Web: ${browserExecutablePath}`);
}

const client = new Client({
  authStrategy: new LocalAuth({
    clientId: String(process.env.QUEUE_WHATSAPP_CLIENT_ID || 'ticket-prototype').trim() || 'ticket-prototype',
    dataPath: process.env.QUEUE_WHATSAPP_AUTH_DIR || '/var/lib/edinaya-ochered/whatsapp-auth',
  }),
  authTimeoutMs: 180000,
  // whatsapp-web.js otherwise forces its old default Chrome UA. Let the real
  // bundled Chromium advertise its own current UA so WhatsApp Web does not
  // classify this Chrome 146 session as an obsolete Chrome build.
  userAgent: false,
  puppeteer: puppeteerOptions,
});
const PERF_CONTACT_COOLDOWN_MS = Math.max(60000, Number(process.env.QUEUE_CONTACT_SYNC_COOLDOWN_MS || 300000));

// Fresh inbound/category traffic always wins over cosmetic/background work.
const HOT_INBOUND_GRACE_MS = 10000;
const STARTUP_BACKFILL_MODEL_LIMIT = 600;
const STARTUP_BACKFILL_BATCH = 12;

function noteHotInboundPriority() {
  runtime.hotInboundUntil = Math.max(runtime.hotInboundUntil, Date.now() + HOT_INBOUND_GRACE_MS);
}

function isHotInboundPriority() {
  return Date.now() < runtime.hotInboundUntil;
}

function isBridgePressureError(error) {
  const text = String(error && (error.message || error.stack) || error || '');
  return /Runtime\.callFunctionOn.*timed out|Protocol error|detached Frame|Execution context was destroyed|Target closed/i.test(text);
}

function noteBrowserPressure(error, source = 'background') {
  if (!isBridgePressureError(error)) return false;
  runtime.browserPressureUntil = Math.max(runtime.browserPressureUntil, Date.now() + 60000);
  console.warn(`Фоновая нагрузка WhatsApp приостановлена на 60 сек: ${source}.`);
  return true;
}

function backgroundWorkAllowed() {
  return Date.now() >= runtime.startupLiveOnlyUntil &&
    !isHotInboundPriority() &&
    Date.now() >= runtime.browserPressureUntil;
}
const lidToPhone = new Map();
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
// WhatsApp обычно помечает пересланные сообщения через isForwarded/forwardingScore.
// Для сообщений, пересланных прямо из «Единой очереди», дополнительно запоминаем
// точный ID результата forward(), чтобы метка «Переслано» не потерялась даже
// если текущая версия WhatsApp Web временно не отдала флаг в live-событии.
const forwardedMessageIds = new Map();
// Keep exact Message objects for recent live/outgoing messages. Actions such as
// delete/forward are much more reliable when they operate on the same object
// WhatsApp Web just emitted instead of trying to reconstruct it later from an
// @c.us id while the current browser session internally uses @lid.
const recentMessageObjects = new Map();
const chatIdAliases = new Map();
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

// WhatsApp may expose the first message of a brand-new contact as @lid and the
// bot reply a moment later as @c.us. Merge those transport aliases immediately
// so one person can never become two cards in the left chat list.
function mergeKnownDirectChatAlias138(canonicalChatId, rawChatId) {
  let canonical = String(canonicalChatId || '').trim();
  const raw = String(rawChatId || '').trim();
  if (!canonical || !raw || canonical.endsWith('@g.us') || raw.endsWith('@g.us')) return canonical || raw;

  // Prefer a phone-based @c.us whenever either side already knows it.
  if (!canonical.endsWith('@c.us') && raw.endsWith('@c.us')) canonical = raw;
  const aliases = new Set([canonical, raw]);
  const remembered = chatIdAliases.get(canonical);
  if (remembered) for (const value of remembered) aliases.add(String(value || '').trim());
  const rawRemembered = chatIdAliases.get(raw);
  if (rawRemembered) for (const value of rawRemembered) aliases.add(String(value || '').trim());

  for (const alias of aliases) {
    if (alias) rememberChatAlias(canonical, alias);
  }

  let merged = null;
  for (const alias of aliases) {
    if (!alias) continue;
    const row = knownChats.get(alias);
    if (!row) continue;
    if (!merged) {
      merged = {...row};
      continue;
    }
    const mergedTs = Number(merged.timestamp || 0);
    const rowTs = Number(row.timestamp || 0);
    const newer = rowTs >= mergedTs ? row : merged;
    const older = rowTs >= mergedTs ? merged : row;
    const newerName = cleanContactDisplayName(newer.name);
    const olderName = cleanContactDisplayName(older.name);
    merged = {
      ...older,
      ...newer,
      id: canonical,
      name: newerName || olderName || 'Пользователь WhatsApp',
      unread_count: Math.max(Number(older.unread_count || 0), Number(newer.unread_count || 0)),
    };
  }

  // Carry the useful contact display name to the canonical key as well.
  for (const alias of aliases) {
    const display = cleanContactDisplayName(contactDisplayById.get(alias));
    if (display && !cleanContactDisplayName(contactDisplayById.get(canonical))) {
      contactDisplayById.set(canonical, display);
    }
  }

  for (const alias of aliases) {
    if (alias && alias !== canonical) knownChats.delete(alias);
  }
  if (merged) knownChats.set(canonical, {...merged, id: canonical});
  return canonical;
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
  return rememberInternalOutgoingMessageId(messageId, ttlMs, message);
}

function rememberInternalOutgoingMessageId(messageId, ttlMs, message = null) {
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
const inboundFloodState = new Map();
const callRejectNoticeAt = new Map();

function isLiveInboundMessage(message) {
  const timestamp = Number((message && message.timestamp) || 0);
  const now = Math.floor(Date.now() / 1000);
  if (!runtime.connectorReadyAt || !timestamp) return false;
  return timestamp >= runtime.connectorReadyAt && timestamp <= now + 30;
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
  const raw = message?._data || {};
  const mime = String(message?.mimetype || raw.mimetype || raw.mediaData?.mimetype || '').toLowerCase();
  if (mime.startsWith('image/')) return labels.image;
  if (mime.startsWith('video/')) return labels.video;
  if (mime.startsWith('audio/')) return String(message?.type || raw.type) === 'ptt' ? labels.ptt : labels.audio;
  return labels[String((message && message.type) || '')] || '[Сообщение]';
}

function safeWhatsAppBody(value) {
  const body = String(value || '').trim();
  if (!body) return '';
  if (/^data:[^,]{1,160};base64,/i.test(body)) return '';
  const compact = body.replace(/\s+/g, '');
  // WA Web can expose an encrypted/encoded media payload as `body` before its
  // message type and media flags have finished loading. Never render such blobs as chat text.
  if (compact.length > 512 && /^[A-Za-z0-9+/]*={0,2}$/.test(compact)) return '';
  return body;
}

function incomingText(message) {
  const selected = String(
    (message && (message.selectedRowId || message.selectedButtonId)) || ''
  ).trim();
  if (/^[1-9]$/.test(selected)) return selected;
  const raw=message?._data || {};
  const media=['image','video','audio','ptt','document','sticker'].includes(String(message?.type || ''));
  const hasMediaMetadata = Boolean(
    message?.hasMedia || raw.hasMedia || raw.directPath || raw.mediaKey || raw.mediaData ||
    message?.mimetype || raw.mimetype || raw.clientUrl || raw.deprecatedMms3Url || raw.filehash || raw.encFilehash
  );
  const body=String((message && message.body) || '').trim();
  if ((media || hasMediaMetadata) && (raw.caption || raw.mediaData?.caption)) return String(raw.caption || raw.mediaData.caption).trim();
  return safeWhatsAppBody(body);
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

  // Profile photos are intentionally NOT bulk-queued from participant resolution.
  // The active chat/group avatar is still requested by syncConnection().
  // This keeps message synchronization ahead of cosmetic avatar work.

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

module.exports = {
  replyToRecovered,
  LANGUAGE_SELECTION_TEXT, PERF_CONTACT_COOLDOWN_MS, STARTUP_BACKFILL_BATCH,
  STARTUP_BACKFILL_MODEL_LIMIT, USER_MESSAGE_TYPES, backgroundWorkAllowed, cachedMessageObject,
  callRejectNoticeAt, chatIdAliases, cleanContactDisplayName, cleanupOutgoingTracking, client,
  contactDisplayById, inboundFloodState, inboundSpool, incomingText, isBridgePressureError,
  isLiveInboundMessage, isRememberedForwarded, isRememberedInternalOutgoing, knownChats, lidToPhone,
  locallyRecoveredMediaIds, mediaDebug, mergeKnownDirectChatAlias138, messageAcknowledgements,
  messageMentionIds, messageText, normalizeMessageIdObject, noteBrowserPressure,
  noteHotInboundPriority, outgoingMediaProbe, ownMentionIds, participantDisplayById,
  pendingMediaMessages, persistedAcknowledgements, postJson, postJsonOnce, postJsonWithRetry,
  prettyPhoneFromId, recentMessageObjects, reconciledOutgoingIds, rememberForwardedMessage,
  rememberInternalOutgoingMessage, rememberMessageObject, resolveDirectPhoneId,
  resolveGroupSenderInfo, resolveMentionPresentation, resolveParticipantIdentity, resolvePhoneId,
  runtime, safeWhatsAppBody, serializedId,
};
