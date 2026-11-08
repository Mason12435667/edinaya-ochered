'use strict';
// Приём: обрабатывает события WhatsApp о сообщениях и звонках, передаёт входящие на сервер,
// скачивает медиа, восстанавливает пропущенные входящие. При подключении файла регистрирует обработчики.

module.exports = {
  applyMediaResult, attachMediaToItem, attachQuoteToItem, downloadMediaDirectById, liveMessageItem,
  markChatRead, publishLiveMessage, reconcileRecentInbound, retryPendingMedia,
  startIncomingCallPolling, stopIncomingCallPolling,
};

const {
  runtime, LANGUAGE_SELECTION_TEXT, STARTUP_BACKFILL_BATCH, STARTUP_BACKFILL_MODEL_LIMIT,
  USER_MESSAGE_TYPES, backgroundWorkAllowed, cachedMessageObject, callRejectNoticeAt,
  cleanContactDisplayName, client, contactDisplayById, inboundFloodState, inboundSpool, incomingText,
  isLiveInboundMessage, isRememberedForwarded, isRememberedInternalOutgoing, knownChats, lidToPhone,
  locallyRecoveredMediaIds, mergeKnownDirectChatAlias138, messageAcknowledgements, messageMentionIds,
  messageText, normalizeMessageIdObject, noteBrowserPressure, noteHotInboundPriority,
  outgoingMediaProbe, ownMentionIds, participantDisplayById, pendingMediaMessages,
  persistedAcknowledgements, postJson, prettyPhoneFromId, recentMessageObjects, reconciledOutgoingIds,
  rememberInternalOutgoingMessage, rememberMessageObject, resolveDirectPhoneId,
  resolveGroupSenderInfo, resolveMentionPresentation, resolveParticipantIdentity, resolvePhoneId,
  safeWhatsAppBody, serializedId,
} = require('./connector_common');
const sending = require('./connector_send');
const syncing = require('./connector_sync');

// Keep old WhatsApp list/button row ids compatible with the v3 numbering. If a
// user taps an old list message after the update, removed topics are folded into
// the new generic item instead of selecting an unrelated category.
const MENU_CHOICE_NUMBERS = {
  // Old WhatsApp row IDs must REOPEN the current configurable menu,
  // not submit a removed category.
  topic_database: '0',
  topic_mobile: '0',
  topic_general: '0',
  topic_support: '0',
  support_text: '0',
  active_tickets: '8',
};
let lastRecentPollAt = 0;
let recentPollBusy = false;
const STARTUP_BACKFILL_LOOKBACK_SECONDS = 30 * 60;
const STARTUP_AUTOMATION_MAX_AGE_SECONDS = 120;
const reconciledInboundIds = new Map();
const seenSentAt = new Map();
let markReadBusy = false;

// Защита от шквала сообщений в личных чатах. Группы сюда не попадают:
// там автоответы и так полностью отключены.
const FLOOD_WINDOW_MS = 5000;
const FLOOD_MESSAGE_LIMIT = 3;
const FLOOD_QUIET_MS = 5000;
const FLOOD_NOTICE_COOLDOWN_MS = 60000;
const FLOOD_NOTICE_TEXT = `🛑 Пожалуйста, отправляйте всю информацию **одним сообщением**.
(Голосовые звонки бот не принимает, пишите текстом).
Чтобы вернуться в меню, отправьте 0.

🛑 Барлық ақпаратты **бір хабарламамен** жіберіңіз.
(Бот дауыстық қоңырауларды қабылдамайды, мәтінмен жазыңыз).
Мәзірге оралу үшін 0 жіберіңіз.`;

// Входящие звонки от обычных пользователей всегда отклоняются. Чтобы один
// человек не мог заставить систему заспамить его автоответами повторными
// звонками, уведомление отправляется не чаще одного раза в минуту.
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

function incomingMenuChoice(message) {
  const selected = String(
    (message && (message.selectedRowId || message.selectedButtonId)) || ''
  ).trim();
  const dynamicChoice = selected.match(/^menu_(\d{1,2})$/);
  const selectedChoice = MENU_CHOICE_NUMBERS[selected] ||
    (dynamicChoice ? dynamicChoice[1] : '') ||
    (/^[0-9]$/.test(selected) ? selected : '');
  if (selectedChoice) return selectedChoice;

  // Users usually type menu digits as ordinary WhatsApp text, not as an
  // interactive row/button event. Treat 0..8 as explicit navigation.
  const typed = String(incomingText(message) || '').trim();
  return /^[0-9]$/.test(typed) ? typed : '';
}

function isDraftFinishText138(value) {
  const normalized = String(value || '')
    .toLocaleLowerCase('ru-RU')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[.,!?:;—\-]+$/g, '')
    .trim();
  return new Set([
    'готово', 'готов',
    'вся инфа', 'вся информация', 'это вся инфа', 'это вся информация',
    'все данные', 'это все данные', 'это всё', 'это все',
    'всё что есть', 'все что есть', 'больше данных нет',
    'данных больше нет', 'другой информации нет', 'больше информации нет',
    'больше ничего нет', 'это вся имеющаяся информация',
    'создать заявку', 'создай заявку', 'отправить заявку', 'отправь заявку',
    'завершить заявку',
    'дайын', 'бар ақпарат осы', 'осы барлық ақпарат', 'басқа ақпарат жоқ',
    'басқа мәлімет жоқ', 'бар мәлімет осы', 'өтінімді құру', 'өтінімді жіберу',
  ]).has(normalized);
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

// .116: one in-page decrypt evaluation at a time.  A timed-out Puppeteer
// promise is NOT cancelled by Promise.race; never accumulate orphaned decoders.
let directMediaEvaluation = null;
// The official WWebJS media downloader can also outlive a Promise.race timeout.
// Bound its actual in-flight promises rather than just waiting callers.
let officialMediaDownloads = 0;
const directMediaReasonLoggedAt = new Map();
function reportDirectMediaFailure(reason) {
  const label = String(reason || 'unknown').replace(/[\r\n]/g, ' ').slice(0, 110);
  const now = Date.now();
  if (now - Number(directMediaReasonLoggedAt.get(label) || 0) < 60000) return;
  directMediaReasonLoggedAt.set(label, now);
  if (directMediaReasonLoggedAt.size > 100) for (const [key, at] of directMediaReasonLoggedAt) {
    if (now - at > 3600000) directMediaReasonLoggedAt.delete(key);
  }
  console.warn('[media116] direct recovery:', label);
}
async function downloadMediaDirectById(messageId) {
  const mid = String(messageId || '').trim();
  if (!mid || directMediaEvaluation || runtime.shutdownStarted || !client.pupPage) return null;
  let evaluation = null;
  try {
    evaluation = client.pupPage.evaluate(async (msgId) => {
        let phase = 'collection';
        try {
          let Msg = null;
          try { Msg = window.require('WAWebCollections')?.Msg || null; } catch (_) {}
          if (!Msg) Msg = (window.Store && (window.Store.Msg || window.Store.Messages)) || null;
          if (!Msg) return {__media_error:'collection-unavailable'};
          const idCandidates = [String(msgId)];
          // For LID messages the browser model may require the structured Wid
          // object. Rebuild it from the stable serialized form as well.
          // false_<chat@g.us>_<stanza>_<sender@lid> is a four-part key.
          // Old three-part parser failed on all group messages with participants.
          const parsed = String(msgId).match(/^(true|false)_([^_]+)_([^_]+)(?:_(.+))?$/);
          if (parsed) {
            idCandidates.push({
              fromMe: parsed[1] === 'true', remote: parsed[2], id: parsed[3],
              participant: parsed[4] || undefined,
              $1: String(msgId), _serialized: String(msgId),
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
          if (!msg) return {__media_error:'message-model-not-in-browser-cache'};

          // WhatsApp часто создаёт событие раньше, чем файл полностью готов.
          // Просим Web-клиент принудительно подготовить медиа, как это делает
          // whatsapp-web.js внутри Message.downloadMedia().
          if (msg.mediaData && msg.mediaData.mediaStage !== 'RESOLVED' && typeof msg.downloadMedia === 'function') {
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

          // Official WhatsApp Web download helper may be present on newer builds.
          // It returns an actual media payload, not the low-resolution body thumbnail.
          if (window.WWebJS && typeof window.WWebJS.downloadMedia === 'function') {
            try {
              const original = await window.WWebJS.downloadMedia(msg);
              if (original && typeof original.data === 'string' && original.data.length > 100) {
                return {
                  data: original.data, mimetype: original.mimetype || msg.mimetype || '',
                  filename: original.filename || msg.filename || '', filesize: original.filesize || msg.size || 0,
                };
              }
              if (typeof original === 'string' && original.length > 100) {
                return {data:original, mimetype:msg.mimetype || '',filename:msg.filename || '',filesize:msg.size || 0};
              }
            } catch (_) {}
          }

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
          if (!directPath || !mediaKey) return {__media_error:'original-url-or-key-not-ready'};
          const mockQpl = {
            addAnnotations() { return this; },
            addPoint() { return this; },
            end() { return this; },
          };
          phase = 'download-manager';
          let managerModule = null;
          try { managerModule = window.require('WAWebDownloadManager'); } catch (_) {}
          if (!managerModule) managerModule = window.Store && window.Store.DownloadManager;
          const manager = managerModule && (managerModule.downloadManager || managerModule);
          if (!manager || typeof manager.downloadAndMaybeDecrypt !== 'function')
            return {__media_error:'download-manager-unavailable'};
          const declaredMime = String(msg.mimetype || (msg.mediaData && msg.mediaData.mimetype) || '').trim().toLowerCase();
          const mediaType = String(msg.type || '').toLowerCase();
          const fallbackMimes = mediaType === 'ptt'
            ? ['audio/ogg; codecs=opus', 'audio/ogg']
            : mediaType === 'audio'
              ? ['audio/ogg; codecs=opus', 'audio/ogg', 'audio/mpeg', 'audio/mp4']
              : mediaType === 'image'
                ? ['image/jpeg', 'image/png', 'image/webp']
                : mediaType === 'video'
                  ? ['video/mp4', 'video/webm']
                  : mediaType === 'sticker' ? ['image/webp', 'image/png'] : [];
          const mimeCandidates = declaredMime && declaredMime !== 'application/octet-stream'
            ? [declaredMime, ...fallbackMimes]
            : fallbackMimes.length ? fallbackMimes : ['application/octet-stream'];
          let decryptedMedia = null;
          let lastMimeError = null;
          for (const candidateMime of [...new Set(mimeCandidates)]) {
            try {
              decryptedMedia = await manager.downloadAndMaybeDecrypt({
                directPath,
                encFilehash: msg.encFilehash || (msg.mediaData && msg.mediaData.encFilehash),
                filehash: msg.filehash || (msg.mediaData && msg.mediaData.filehash),
                mediaKey,
                mediaKeyTimestamp: msg.mediaKeyTimestamp || (msg.mediaData && msg.mediaData.mediaKeyTimestamp),
                type: msg.type,
                mimetype: candidateMime,
                signal: new AbortController().signal,
                downloadQpl: mockQpl,
              });
              if (decryptedMedia) break;
            } catch (error) {
              lastMimeError = error;
              if (!/unexpected mimetype/i.test(String(error && (error.message || error.name) || ''))) throw error;
            }
          }
          if (!decryptedMedia && lastMimeError) throw lastMimeError;
          if (!decryptedMedia) return {__media_error:'decrypt-returned-empty'};
          const data = await window.WWebJS.arrayBufferToBase64Async(decryptedMedia);
          return {
            data,
            mimetype: msg.mimetype || (msg.mediaData && msg.mediaData.mimetype) || '',
            filename: msg.filename || (msg.mediaData && msg.mediaData.filename) || '',
            filesize: msg.size || (msg.mediaData && msg.mediaData.size) || 0,
          };
        } catch (error) {
          // Do not print message IDs or decrypted bytes to service logs.
          return {__media_error:phase + ':' + String(error && (error.message || error.name) || 'failure').slice(0, 70)};
        }
      }, mid);
    directMediaEvaluation = evaluation;
    // An outstanding Chromium evaluation remains guarded after timeout until
    // it actually settles; subsequent media retries use the official API only.
    evaluation.then(() => {
      if (directMediaEvaluation === evaluation) directMediaEvaluation = null;
    }, () => {
      if (directMediaEvaluation === evaluation) directMediaEvaluation = null;
    });
    const result = await Promise.race([
      evaluation,
      new Promise((resolve) => setTimeout(() => resolve({__media_error:'direct-evaluation-timeout'}), 8000)),
    ]);
    if (result && result.__media_error) {
      reportDirectMediaFailure(result.__media_error);
      return null;
    }
    return result || null;
  } catch (error) {
    reportDirectMediaFailure('puppeteer:' + String(error && (error.message || error.name) || 'failure'));
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
  let officialTimedOut = false;
  if (typeof message.downloadMedia === 'function') {
    if (officialMediaDownloads >= 2) {
      officialTimedOut = true;
    } else {
      officialMediaDownloads += 1;
      const official = Promise.resolve().then(() => message.downloadMedia());
      const release = () => { officialMediaDownloads = Math.max(0, officialMediaDownloads - 1); };
      official.then(release, release);
      try {
        const timeoutMarker = {__media_timeout:true};
        const media = await Promise.race([
          official,
          new Promise(resolve => setTimeout(() => resolve(timeoutMarker), 6500)),
        ]);
        if (media === timeoutMarker) {
          officialTimedOut = true;
          reportDirectMediaFailure('official-evaluation-timeout');
        } else if (applyMediaResult(item, media, message)) {
          return item;
        }
      } catch (error) {
        lastError = error;
        reportDirectMediaFailure('official:' + String(error && (error.message || error.name) || 'failure'));
      }
    }
  }
  // A direct download is serialized and attempted once per cycle. If the official
  // Chromium call is still running, defer to the retry queue instead of piling on.
  if (!officialTimedOut && mid) {
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

async function publishLiveMessage(chatId, sender, message, fromMe = false, bodyOverride = '', notify = false, manualContact = false, senderPhone = '', senderId = '', deferMedia = false) {
  const requestedChatId = String(chatId || '').trim();
  if (!requestedChatId) return;
  const raw = (message && message._data) || {};
  const eventRemote = String(
    fromMe
      ? (serializedId(message && message.to) || serializedId(raw.to) || serializedId(message && message.id && message.id.remote) || serializedId(raw.id && raw.id.remote))
      : (serializedId(message && message.from) || serializedId(raw.from) || serializedId(message && message.id && message.id.remote) || serializedId(raw.id && raw.id.remote))
  ).trim();
  let safeChatId = requestedChatId;
  if (!safeChatId.endsWith('@g.us') && eventRemote && !eventRemote.endsWith('@g.us')) {
    safeChatId = mergeKnownDirectChatAlias138(safeChatId, eventRemote) || safeChatId;
  }
  const sourceChatId138 = [requestedChatId, eventRemote].find((value) => String(value || '').endsWith('@lid')) || eventRemote || requestedChatId;
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

  // QUEUE_FAST_INBOUND_20260930
  // Never make a fresh inbound message wait 6-15 seconds for Chromium media
  // decryption. Put the text/caption in the Queue immediately and let the
  // existing bounded media-recovery worker attach the file afterwards.
  const rawMedia = (message && message._data) || {};
  const mediaExpected = Boolean(
    message && (
      message.hasMedia || rawMedia.directPath || rawMedia.mediaKey || rawMedia.mediaData ||
      message.mimetype || rawMedia.mimetype || rawMedia.clientUrl || rawMedia.deprecatedMms3Url ||
      ['image','video','audio','ptt','document','sticker'].includes(String(message.type || ''))
    )
  );
  if (deferMedia && mediaExpected) {
    item.media_name = String(
      (message && message.filename) || rawMedia.filename || `WhatsApp ${String((message && message.type) || 'media')}`
    ).slice(0, 180);
    item.media_mime = String((message && message.mimetype) || rawMedia.mimetype || '').slice(0, 120);
    item.media_pending = true;
  } else {
    try { await attachMediaToItem(message, item); }
    catch (error) {
      item.media_pending = true;
      console.warn('Не удалось загрузить отдельное вложение:', error.message || error);
    }
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
      source_chat_id: sourceChatId138,
      append: true,
      messages: [item],
    }),
  ]);
  return item;
}

async function markChatRead(chatId) {
  const safeChatId = String(chatId || '').trim();
  if (!backgroundWorkAllowed()) return;
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
    noteBrowserPressure(error, 'sendSeen');
    console.warn('Не удалось отметить сообщение прочитанным. Заявка продолжает обрабатываться:', error.message || error);
  } finally {
    markReadBusy = false;
  }
}

async function recoverRawInboundCandidate(row, allowAutomation = true) {
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
  const text = safeWhatsAppBody(row && row.body) || (row && row.has_media ? messageText({ type, mimetype: row.mimetype }) : (fallbackByType[type] || ''));
  const timestamp = Number((row && row.timestamp) || Math.floor(Date.now() / 1000));
  if (!text && !row.has_media) return false;

  const effectiveChatId138 = mergeKnownDirectChatAlias138(canonicalChatId, from) || canonicalChatId;
  const previous = knownChats.get(effectiveChatId138) || {};
  knownChats.set(effectiveChatId138, {
    ...previous,
    id: effectiveChatId138,
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
      chat_id: effectiveChatId138,
      source_chat_id: from,
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
  // Startup history older than the short catch-up window is restored only to
  // the chat UI. It must not replay old menus/language prompts after a restart.
  if (!allowAutomation) return true;

  // Keep menu/category semantics even when the live WhatsApp event was missed.
  const recoveredMenuChoice = /^[0-8]$/.test(String(text || '').trim())
    ? String(text || '').trim()
    : '';
  const recoveredDraftFinish138 = isDraftFinishText138(text);
  const result = await postJson('/api/whatsapp', {
    external_id: mid,
    sender,
    phone: phoneDigits ? `+${phoneDigits}` : '',
    chat_id: canonicalChatId,
    text,
    menu_choice: recoveredMenuChoice,
    draft_finish: recoveredDraftFinish138,
    attachment_name: row && row.has_media ? `WhatsApp ${type}` : '',
    message_timestamp: timestamp,
    message_type: type,
  });
  if (result) {
    result._source_message_id138 = mid;
    result._source_menu_choice138 = recoveredMenuChoice;
    result._source_finish_command138 = recoveredDraftFinish138;
  }
  if (result && !result.duplicate) sending.cancelDialogHint138([phoneId, from]);
  if (!result || result.duplicate || result.manual_contact || result.silent || !result.reply) return true;
  await sending.sendAutomaticReply([phoneId, from], result).catch(() => null);
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
  const rawBody = safeWhatsAppBody(row && row.body) || (row && row.has_media ? messageText({ type, mimetype: row.mimetype }) : '');
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
  const startupBackfill = now < runtime.startupInboundRecoveryUntil;
  const lookbackSeconds = startupBackfill ? STARTUP_BACKFILL_LOOKBACK_SECONDS : 900;
  const modelLimit = startupBackfill ? STARTUP_BACKFILL_MODEL_LIMIT : 180;
  try {
    // client.getChats() сейчас может падать внутри сериализации WhatsApp Web с
    // короткой ошибкой "r". Для восстановления входящих читаем только ID
    // последних сообщений напрямую из WAWebCollections.Msg. Это существенно
    // легче и не зависит от сериализации всего списка чатов.
    const candidates = await client.pupPage.evaluate(({ lookbackSeconds, modelLimit }) => {
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
        const cutoff = Math.floor(Date.now() / 1000) - lookbackSeconds;
        return (Array.isArray(models) ? models : []).slice(-modelLimit).map((m) => ({
          id: widText(m && m.id),
          from: widText(m && m.from),
          remote: widText(m && m.id && m.id.remote),
          author: widText((m && m.author) || (m && m.participant) || (m && m.id && m.id.participant)),
          mentions: Array.isArray(m && m.mentionedJidList)
            ? m.mentionedJidList.map(widText).filter(Boolean)
            : [],
          from_me: Boolean(m && m.id && m.id.fromMe),
          type: String((m && m.type) || 'chat'),
          mimetype: String((m && (m.mimetype || (m.mediaData && m.mediaData.mimetype))) || ''),
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
      }, { lookbackSeconds, modelLimit });

    let recovered = 0;
    const unreconciled = (Array.isArray(candidates) ? candidates : [])
      .filter((row) => {
        const from = String(row && (row.from || row.remote) || '');
        return row && row.id && !reconciledInboundIds.has(String(row.id)) &&
          USER_MESSAGE_TYPES.has(String(row.type || '')) && from !== 'status@broadcast' && !from.endsWith('@newsletter');
      });
    const batch = startupBackfill ? unreconciled.slice(-STARTUP_BACKFILL_BATCH) : unreconciled.slice(-40);
    for (const row of batch) {
      const mid = String(row.id || '');
      const from = String(row.from || row.remote || '');
      if (!mid || reconciledInboundIds.has(mid) || from === 'status@broadcast' || from.endsWith('@newsletter')) continue;
      if (!USER_MESSAGE_TYPES.has(String(row.type || ''))) continue;
      const isGroupMessage = from.endsWith('@g.us');
      let message = null;
      try {
        message = await client.getMessageById(mid);
        if (message) rememberMessageObject(message, from);
      } catch (_) {}
      if (message && !message.fromMe && isLiveInboundMessage(message)) {
        reconciledInboundIds.set(mid, now);
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
        const rowTimestamp = Number(row && row.timestamp || 0);
        const allowStartupAutomation = !startupBackfill ||
          (rowTimestamp > 0 && rowTimestamp >= runtime.connectorReadyAt - STARTUP_AUTOMATION_MAX_AGE_SECONDS);
        const restored = isGroupMessage
          ? await recoverRawGroupCandidate(row)
          : await recoverRawInboundCandidate(row, allowStartupAutomation);
        if (restored) {
          reconciledInboundIds.set(mid, now);
          recovered += 1;
        }
      } catch (_) {}
    }
    if (startupBackfill) {
      const remaining = unreconciled.filter((row) => !reconciledInboundIds.has(String(row.id))).length;
      const ceiling = runtime.startupInboundRecoveryStartedAt + 5 * 60 * 1000;
      runtime.startupInboundRecoveryUntil = remaining > 0 && Date.now() < ceiling
        ? Math.min(ceiling, Date.now() + 60 * 1000)
        : 0;
    }
    if (recovered) console.log(`Резервная синхронизация восстановила входящих: ${recovered}`);
    for (const [id, t] of reconciledInboundIds) if (now - t > 3600000) reconciledInboundIds.delete(id);
  } catch (error) {
    console.warn('Резервная синхронизация входящих:', error && error.stack ? error.stack.split('\n')[0] : (error.message || error));
  } finally {
    recentPollBusy = false;
  }
}

async function retryPendingMedia() {
  if (runtime.pendingMediaBusy || !pendingMediaMessages.size || !backgroundWorkAllowed()) return;
  runtime.pendingMediaBusy = true;
  try {
    const now = Date.now();
    const entries = [...pendingMediaMessages.entries()]
      .filter(([, meta]) => Number(meta.next_at || 0) <= now)
      .slice(0, 1);

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
            new Promise((resolve) => setTimeout(() => resolve(null), 1800)),
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

        if (message) {
          await attachMediaToItem(message, item);
        } else {
          const direct = await downloadMediaDirectById(messageId);
          applyMediaResult(item, direct, { type: meta.type || 'media' });
        }

        if (item.media_base64) {
          await postJson('/api/chat-messages-sync', {
            chat_id: String(meta.chat_id || ''),
            append: true,
            messages: [item],
          });
          if (item.media_pending) throw new Error('Передача вложения в панель не завершена');
          pendingMediaMessages.delete(messageId);
          if (meta.from_me) reconciledOutgoingIds.set(messageId, Date.now());
          console.log(`Вложение WhatsApp догружено и сохранено: ${messageId}`);
          continue;
        }

        meta.attempts = Number(meta.attempts || 0) + 1;
        meta.next_at = now + Math.min(90000, 6000 + meta.attempts * 9000);
        if (meta.attempts >= 12) {
          pendingMediaMessages.delete(messageId);
          console.warn(`Не удалось скачать вложение после ${meta.attempts} попыток: ${messageId}`);
        } else {
          pendingMediaMessages.set(messageId, meta);
        }
      } catch (error) {
        meta.attempts = Number(meta.attempts || 0) + 1;
        meta.next_at = Date.now() + Math.min(90000, 6000 + meta.attempts * 9000);
        noteBrowserPressure(error, 'media recovery');
        if (meta.attempts >= 12) {
          pendingMediaMessages.delete(messageId);
          console.warn('Не удалось догрузить вложение WhatsApp:', error.message || error);
        } else {
          pendingMediaMessages.set(messageId, meta);
        }
      }
    }
  } finally {
    runtime.pendingMediaBusy = false;
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
      const sentMessage = await sending.sendToUser(current.recipients, FLOOD_NOTICE_TEXT);
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
    const automaticReplyEcho = sending.matchesPendingAutoReplyEcho138(message, rawChatId);
    let internalChatEvent = automaticReplyEcho || isRememberedInternalOutgoing(outgoingEventId) || sending.isInternalOutgoing(rawChatId) || sending.matchesActiveInternalOutgoing(message, rawChatId);
    let canonicalForInternal = rawChatId;
    if (!internalChatEvent && !rawChatId.endsWith('@g.us') && !rawChatId.endsWith('@c.us')) {
      const resolvedInternalChat = await resolveDirectPhoneId(rawChatId).catch(() => '');
      if (resolvedInternalChat) {
        canonicalForInternal = resolvedInternalChat;
        internalChatEvent = sending.isInternalOutgoing(resolvedInternalChat);
      }
    }
    if (internalChatEvent) {
      const marker = runtime.activeInternalOutgoingFingerprint && Date.now() <= Number(runtime.activeInternalOutgoingFingerprint.until || 0)
        ? {...runtime.activeInternalOutgoingFingerprint} : null;
      const exactId = rememberInternalOutgoingMessage(message) || outgoingEventId;
      rememberMessageObject(message, canonicalForInternal || rawChatId);
      if (exactId) {
        reconciledOutgoingIds.set(exactId, Date.now());
        pendingMediaMessages.delete(exactId);
        outgoingMediaProbe.delete(exactId);
      }

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
    syncing.scheduleOutgoingMediaProbe(message, rawChatId);

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
  try {
    const from = String(message.from || '');
    const incomingEventId = serializedId(message && message.id);
    if (incomingEventId) reconciledInboundIds.set(incomingEventId, Date.now());
    if (from.endsWith('@g.us')) {
      if (
        message.fromMe ||
        !USER_MESSAGE_TYPES.has(String(message.type || '')) ||
        !isLiveInboundMessage(message)
      ) {
        return;
      }
      noteHotInboundPriority();
      const groupText = incomingText(message);
      const fallbackSender = String((message._data && message._data.notifyName) || '').trim();
      const groupSenderInfo = await resolveGroupSenderInfo(message, fallbackSender);
      const groupSender = groupSenderInfo.name || fallbackSender || 'Участник группы';
      const mentionedUs = await mentionsConnectedAccount(message);
      // В группах система НИКОГДА не запускает обработчик заявок и не отправляет
      // автоответы. Сообщение сохраняется в переписку, а уведомление создаётся
      // только если участник явно упомянул подключённый рабочий WhatsApp через @.
      await publishLiveMessage(
        from,
        groupSender,
        message,
        false,
        groupText,
        mentionedUs,
        false,
        groupSenderInfo.phone || '',
        groupSenderInfo.id || groupSenderInfo.resolved_id || '',
        true
      );
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
    noteHotInboundPriority();

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

    // Persist text and identifiers before the first request to the application.
    inboundSpool.enqueue({external_id:serializedId(message && message.id),sender,
      phone:phoneDigits ? `+${phoneDigits}` : '',chat_id:canonicalChatId,text,
      menu_choice:incomingMenuChoice(message),attachment_name:attachmentName,
      message_timestamp:Number(message.timestamp || Math.floor(Date.now()/1000)),message_type:String(message.type || 'chat')});

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
    if (contactPolicy && contactPolicy.bot_reset_token) {
      sending.clearBotRuntimeForUser138(canonicalChatId, phoneId || from, contactPolicy.bot_reset_token);
    }
    const isManualContact = Boolean(contactPolicy && contactPolicy.manual_contact);

    // The panel/UI copy and bot decision no longer wait for each other.
    // Media is already deferred by publishLiveMessage(), so /api/whatsapp can
    // start immediately with text + lightweight attachment metadata.
    const liveStartedAt = Date.now();
    const panelSyncPromise = publishLiveMessage(
      canonicalChatId, sender, message, false, text, false, isManualContact, '', '', true
    ).then((item) => {
      const panelMs = Date.now() - liveStartedAt;
      if (panelMs > 1000) {
        console.log(`Fast Reply WhatsApp→панель: ${panelMs} мс | ${canonicalChatId}`);
      }
      return item;
    }).catch((error) => {
      console.warn('Не удалось сразу показать входящее сообщение в панели:', error.message || error);
      return null;
    });

    // Не блокируем несколько коротких сообщений подряд. Повторные автоматические
    // меню/подсказки ограничиваются strict-cooldown в sendAutomaticReply.
    stage = 'передача сообщения в локальную систему';
    const apiStartedAt = Date.now();
    const rawMedia = (message && message._data) || {};
    const fastMediaMime = String((message && message.mimetype) || rawMedia.mimetype || '').slice(0, 120);
    const draftFinish138 = isDraftFinishText138(text);
    const result = await postJson('/api/whatsapp', {
      external_id: serializedId(message && message.id),
      sender,
      phone: phoneDigits ? `+${phoneDigits}` : '',
      chat_id: canonicalChatId,
      text,
      menu_choice: incomingMenuChoice(message),
      draft_finish: draftFinish138,
      attachment_name: String(attachmentName || ''),
      media_base64: '',
      media_receipt: '',
      media_mime: fastMediaMime,
      media_name: String(attachmentName || ''),
      message_timestamp: Number(message.timestamp || Math.floor(Date.now() / 1000)),
      message_type: String(message.type || 'chat'),
    });
    result._source_message_id138 = incomingEventId;
    result._source_menu_choice138 = incomingMenuChoice(message);
    result._source_finish_command138 = draftFinish138;

    const apiMs = Date.now() - apiStartedAt;
    if (apiMs > 1000) {
      console.log(`Fast Reply WhatsApp→бот: обработчик ${apiMs} мс | ${canonicalChatId}`);
    }
    // Keep the UI sync alive, but never make the user's auto-reply wait for it.
    void panelSyncPromise;

    if (!result.duplicate) sending.cancelDialogHint138([phoneId, from]);
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
      if (result.reply) await sending.sendAutomaticReply([phoneId, from], result);
      return;
    }
    if (result.created) {
      console.log(`Создана заявка #${result.ticket_id}: ${result.title || result.category || 'рабочий запрос'} | ${sender}`);
      if (result.reply) {
        stage = 'отправка подтверждения пользователю';
        const confirmationMessage = await sending.sendAutomaticReply([phoneId, from], result);
        if (confirmationMessage) {
          console.log(`Подтверждение по заявке #${result.ticket_id} отправлено пользователю: ${sender}`);
        }
      }
      return;
    }
    if (result.linked) {
      console.log(`Уточнение связано с заявкой #${result.ticket_id}: ${sender}`);
      if (result.reply) await sending.sendAutomaticReply([phoneId, from], result);
      return;
    }
    if (!result.reply) {
      return;
    }

    templateErrorId = Number(result.template_error_id || 0);
    stage = 'отправка автоматического ответа пользователю';
    const replyMessage = await sending.sendAutomaticReply([phoneId, from], result);
    if (templateErrorId && replyMessage) {
      stage = 'сохранение результата отправки';
      await postJson('/api/template-error-delivery', {
        template_error_id: templateErrorId,
        status: 'sent',
        provider_id:
          serializedId(replyMessage && replyMessage.id),
      });
      console.log(`Отправлен запрос недостающих данных: ${sender}`);
    } else if (!templateErrorId && replyMessage) {
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
  }
});
