'use strict';
// Отправка: забирает исходящие из очереди сервера, отправляет их в WhatsApp, подтверждает результат
// и шлёт автоматические ответы. Отправка повторно не выполняется, если результат неизвестен.

module.exports = {
  cancelDialogHint138, clearBotRuntimeForUser138, confirmSentMessage, isInternalOutgoing,
  matchesActiveInternalOutgoing, matchesPendingAutoReplyEcho138, pollOutbound, sendAutomaticReply,
  sendStartSeconds, sendToUser,
};

const fs = require('fs');
const largeMedia = require('./queue_large_media');
const delivery = require('./queue_delivery');
const path = require('path');
const {MessageMedia} = require('whatsapp-web.js');
const {
  runtime, cachedMessageObject, callRejectNoticeAt, chatIdAliases, client, inboundFloodState,
  incomingText, isBridgePressureError, lidToPhone, mediaDebug, messageAcknowledgements, messageText,
  normalizeMessageIdObject, postJson, rememberInternalOutgoingMessage, rememberMessageObject,
  resolveDirectPhoneId, serializedId,
} = require('./connector_common');
const receiving = require('./connector_receive');
const whatsappActions = require('./connector_actions');
const lifecycle = require('./connector_lifecycle');

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

// One inbound WhatsApp message may reach us through both the live event and
// reserve recovery. Track the source message id so it can never produce two
// automatic replies. Separately track the body/recipient of an auto-reply
// before client.sendMessage() returns, because message_create may fire first.
const autoReplySourceInflight138 = new Set();
const autoReplySourceSent138 = new Map();
const pendingAutoReplyEchoes138 = new Map();
const AUTO_REPLY_SOURCE_TTL_MS = 10 * 60 * 1000;
const AUTO_REPLY_ECHO_TTL_MS = 20000;

function normalizeAutoReplyBody138(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function rememberPendingAutoReplyEcho138(recipients, content) {
  const aliases = new Set();
  for (const raw of (recipients || [])) {
    const value = String(raw || '').trim();
    if (!value) continue;
    aliases.add(value);
    const known = chatIdAliases.get(value);
    if (known) for (const alias of known) aliases.add(String(alias || '').trim());
  }
  const body = normalizeAutoReplyBody138(content);
  const token = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  pendingAutoReplyEchoes138.set(token, {
    aliases,
    body,
    until: Date.now() + AUTO_REPLY_ECHO_TTL_MS,
  });
  return token;
}

function forgetPendingAutoReplyEcho138(token) {
  if (token) pendingAutoReplyEchoes138.delete(token);
}

function matchesPendingAutoReplyEcho138(message, rawChatId = '') {
  const now = Date.now();
  const raw = String(rawChatId || '').trim();
  const body = normalizeAutoReplyBody138(incomingText(message));
  for (const [token, entry] of pendingAutoReplyEchoes138) {
    if (!entry || now > Number(entry.until || 0)) {
      pendingAutoReplyEchoes138.delete(token);
      continue;
    }
    if (!entry.aliases || !entry.aliases.has(raw)) continue;
    if (entry.body && body && entry.body !== body) continue;
    if (entry.body && !body) continue;
    pendingAutoReplyEchoes138.delete(token);
    return true;
  }
  return false;
}

function cleanupAutoReplySourceIds138(now = Date.now()) {
  for (const [key, at] of autoReplySourceSent138) {
    if (now - Number(at || 0) > AUTO_REPLY_SOURCE_TTL_MS) autoReplySourceSent138.delete(key);
  }
}

// The bot must be strict about the current dialog state, but it must not
// answer every separate user message with the same prompt.
const AUTO_REPLY_EXACT_COOLDOWN_MS = 60000;
const AUTO_REPLY_MENU_COOLDOWN_MS = 60000;
const AUTO_REPLY_HINT_COOLDOWN_MS = 45000;
const AUTO_REPLY_GATE_COOLDOWN_MS = 30000;
const AUTO_REPLY_PROFILE_COOLDOWN_MS = 30000;
// Помечаем сообщения, которые отправила сама система. Событие message_create
// также приходит на исходящие сообщения, и без этой метки автоответ системы
// ошибочно включал бы «ручной диалог» на 30 минут.
const internalOutgoingUntil = new Map();
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
  runtime.activeInternalOutgoingFingerprint = {
    queue_id: Number((queued && queued.id) || 0),
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
  const marker = runtime.activeInternalOutgoingFingerprint;
  if (!marker || Date.now() > Number(marker.until || 0)) {
    runtime.activeInternalOutgoingFingerprint = null;
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

const SENT_LOOKUP_TIMEOUT_MS = 4000;
const SENT_LOOKUP_INTERVAL_MS = 250;
// Ids this lookup already gave to a send. Not isRememberedInternalOutgoing():
// message_create marks our own message as internal before the lookup runs, and
// the reserve sync skips internal messages, so nothing would ever save it.
const confirmedSentIds = new Set();

function readOwnMessagesSince(since) {
  return client.pupPage.evaluate((cutoff) => {
    const widText = (value) => {
      if (!value) return '';
      if (typeof value === 'string') return value;
      if (typeof value._serialized === 'string') return value._serialized;
      if (typeof value.$1 === 'string') return value.$1;
      return value.user && value.server ? `${value.user}@${value.server}` : '';
    };
    // a message sent a moment ago is among the newest; the whole collection can be large
    return window.require('WAWebCollections').Msg.getModelsArray().slice(-600)
      .filter((m) => m && m.id && m.id.fromMe && Number(m.t || 0) >= cutoff)
      .map((m) => ({
        id: widText(m.id),
        remote: widText(m.id.remote),
        fromMe: true,
        timestamp: Number(m.t || 0),
        type: String(m.type || 'chat'),
        text: String((m.type === 'chat' ? m.body : m.caption) || ''),
      }));
  }, since);
}

// Current WhatsApp Web stores a sent message under the @lid of a chat we addressed
// as @c.us, so client.sendMessage() cannot find it and returns nothing. Without
// the message its id is unknown and the site never shows it. Look it up in the
// message store, the same way reconcileRecentOutgoing() does. This only reads.
async function confirmSentMessage(recipients, body, hasMedia, since) {
  const aliases = new Set(recipients);
  for (const recipient of recipients) {
    for (const alias of (chatIdAliases.get(recipient) || [])) aliases.add(alias);
  }
  const text = normalizeAutoReplyBody138(body);
  let own = [];
  const row = await delivery.waitForValue(async () => delivery.findSentMessage(
    await Promise.all((own = await readOwnMessagesSince(since)).map(async (item) => ({
      ...item, text: normalizeAutoReplyBody138(item.text), phone: await resolveDirectPhoneId(item.remote),
    }))),
    {text, hasMedia, since, aliases, isKnown: (item) => confirmedSentIds.has(item.id)},
  ), SENT_LOOKUP_TIMEOUT_MS, SENT_LOOKUP_INTERVAL_MS);
  if (!row) {
    console.warn(`Отправка не подтверждена: своих сообщений с начала отправки ${own.length}, адреса: ${[...aliases].length}`);
    return null;
  }
  confirmedSentIds.add(row.id);
  setTimeout(() => confirmedSentIds.delete(row.id), 2 * 60 * 1000); // rows older than `since` never match
  const message = await client.getMessageById(row.id).catch(() => null);
  return message || {id: {_serialized: row.id, remote: row.remote, fromMe: true}, fromMe: true, timestamp: row.timestamp};
}

function sendStartSeconds() {
  return Math.floor(Date.now() / 1000) - 5; // tolerate a small clock difference
}

async function sendToUser(recipients, content) {
  const targets = [...new Set(recipients.map(value => String(value || '').trim()).filter(Boolean))];
  if (!targets.length) throw new Error('WhatsApp-адрес пользователя не определён');

  // message_create can arrive BEFORE delivery.sendOnce() returns. Register the
  // exact automatic reply first so that its WhatsApp echo is never mistaken for
  // a manual employee message (which could otherwise silence the bot later).
  const echoToken = rememberPendingAutoReplyEcho138(targets, content);
  let outcome = null;
  try {
    // A transport exception can occur after delivery. Trying the same user via
    // the next PN/LID address would send the automatic reply twice.
    const since = sendStartSeconds();
    outcome = await delivery.sendOnce({
      client, recipient:targets[0], aliases:targets, content, options:{}, begin:async()=>true,
      confirm: () => confirmSentMessage(targets, content, false, since),
    });
    if (outcome.status !== 'sent') throw new Error(outcome.error);
    rememberInternalOutgoingMessage(outcome.message);
    return outcome.message;
  } catch (error) {
    if (isBridgePressureError(error) && runtime.connectorOperational && !runtime.shutdownStarted) {
      void lifecycle.restartAfterStuckBridge('Отправка автоответа потеряла рабочий WhatsApp bridge');
    }
    throw error;
  } finally {
    // If message_create already fired it removed the marker. If it has not, the
    // exact provider id is remembered above, so the marker is no longer needed.
    forgetPendingAutoReplyEcho138(echoToken);
  }
}

const dialogHintTimers138 = new Map();
function cancelDialogHint138(recipients) {
  for (const target of (recipients || [])) {
    const pending = dialogHintTimers138.get(String(target || ''));
    if (!pending) continue;
    clearTimeout(pending.timer);
    for (const alias of pending.aliases) if (dialogHintTimers138.get(alias) === pending) dialogHintTimers138.delete(alias);
    pending.resolve(null);
  }
}

const botResetTokenByChat138 = new Map();

function botResetAliases138(chatId, phoneId = '') {
  const aliases = new Set();
  const add = (value) => {
    const safe = String(value || '').trim();
    if (safe) aliases.add(safe);
  };
  add(chatId);
  add(phoneId);
  const phoneDigits = String(phoneId || chatId || '').replace(/\D/g, '');
  if (phoneDigits) add(`${phoneDigits}@c.us`);

  // Expand PN/LID aliases already learned by this connector.
  for (let pass = 0; pass < 3; pass += 1) {
    for (const value of [...aliases]) {
      const known = chatIdAliases.get(value);
      if (known) for (const alias of known) add(alias);
      const mapped = lidToPhone.get(value);
      if (mapped) add(mapped);
    }
    for (const [lid, pn] of lidToPhone) {
      if (aliases.has(lid) || aliases.has(pn)) {
        add(lid);
        add(pn);
      }
    }
  }
  return [...aliases];
}

function clearBotRuntimeForUser138(chatId, phoneId, resetToken) {
  const token = String(resetToken || '').trim();
  if (!token) return false;

  const tokenKey = String(chatId || phoneId || '').trim();
  if (!tokenKey) return false;
  if (botResetTokenByChat138.get(tokenKey) === token) return false;
  botResetTokenByChat138.set(tokenKey, token);

  const aliases = botResetAliases138(chatId, phoneId);
  cancelDialogHint138(aliases);

  for (const alias of aliases) {
    inboundFloodState.delete(alias);
    callRejectNoticeAt.delete(alias);
    internalOutgoingUntil.delete(alias);
  }

  const belongsToUser = (key) => aliases.some((alias) => String(key || '').startsWith(`${alias}|`));
  for (const key of [...autoReplyCooldowns.keys()]) {
    if (belongsToUser(key)) autoReplyCooldowns.delete(key);
  }
  for (const key of [...autoReplyExactCooldowns.keys()]) {
    if (belongsToUser(key)) autoReplyExactCooldowns.delete(key);
  }
  for (const key of [...autoReplyInflight]) {
    if (belongsToUser(key)) autoReplyInflight.delete(key);
  }

  console.log(`Hard reset бота применён в коннекторе: ${tokenKey}; aliases=${aliases.length}`);
  return true;
}
async function sendAutomaticReply(recipients, result) {
  cancelDialogHint138(recipients);
  if (!result || !result.reply) return null;

  // A deliberate menu/category transition must feel immediate. Do not hold it
  // behind the burst debounce used for free-form detail messages.
  const navigationChoice138 = String(result._source_menu_choice138 || '').trim();
  if (/^[0-8]$/.test(navigationChoice138) || result.language_required || result._source_finish_command138) {
    return sendAutomaticReplyNow138(recipients, result);
  }

  if (result.awaiting_details || result.menu_reminder) {
    return new Promise((resolve, reject) => {
      const aliases = [...new Set((recipients || []).map(String).filter(Boolean))];
      const pending = { aliases, resolve, timer: null };
      pending.timer = setTimeout(() => {
        for (const alias of aliases) if (dialogHintTimers138.get(alias) === pending) dialogHintTimers138.delete(alias);
        sendAutomaticReplyNow138(recipients, {...result, _dialog_debounced138: true}).then(resolve, reject);
      }, 750);
      for (const alias of aliases) dialogHintTimers138.set(alias, pending);
    });
  }
  return sendAutomaticReplyNow138(recipients, result);
}
async function sendAutomaticReplyNow138(recipients, result) {
  if (!result || !result.reply) return null;
  const targets = [...new Set((recipients || []).map((value) => String(value || '').trim()).filter(Boolean))];
  const primaryTarget = targets[0] || 'unknown';

  const sourceMessageId = String(result._source_message_id138 || '').trim();
  const sourceKey = sourceMessageId ? `${primaryTarget}|${sourceMessageId}` : '';
  cleanupAutoReplySourceIds138();
  if (sourceKey && (autoReplySourceSent138.has(sourceKey) || autoReplySourceInflight138.has(sourceKey))) {
    result.reply_suppressed = true;
    console.log(`Дубль автоответа по тому же входящему сообщению для ${primaryTarget} подавлен.`);
    return null;
  }

  // Menu navigation, a language digit and the finish command are explicit user
  // actions. They must get one response for every distinct inbound message, even
  // when the reply text is identical to something sent a few seconds ago.
  const priorityInteraction = delivery.isDeliberateStep(result._source_menu_choice138, result._source_finish_command138);

  const missingSignature = Array.isArray(result.missing_fields)
    ? result.missing_fields.map((value) => String(value || '').trim()).filter(Boolean).sort().join(',')
    : '';
  const kind = result.language_required
    ? 'language'
    : result.profile_required
      ? 'profile'
      : result.menu_gate
        ? 'menu-gate'
        : result.menu_reminder
          ? 'menu-reminder'
          : (result.awaiting_category || result.main_menu || result.support_menu)
            ? 'menu'
            : result.awaiting_details
              ? `details:${String(result.category || 'general')}:${missingSignature || 'prompt'}`
              : result.template_error_id
                ? 'template-error'
                : result.created
                  ? 'accepted'
                  : 'generic';

  // force_menu means an explicit transition/opening of the menu. It may bypass
  // cooldown, except menu_reminder: an invalid message while the menu is already
  // open must NOT cause the same menu to be sent on every user message.
  const explicitMenuRefresh = Boolean(result.force_menu && !result.menu_reminder);
  const cooldown = priorityInteraction
    ? 0
    : explicitMenuRefresh
      ? 0
      : kind === 'language' || kind === 'profile'
        ? AUTO_REPLY_PROFILE_COOLDOWN_MS
        : kind === 'menu'
          ? AUTO_REPLY_MENU_COOLDOWN_MS
          : kind === 'menu-gate'
            ? AUTO_REPLY_GATE_COOLDOWN_MS
            : kind === 'menu-reminder'
              ? AUTO_REPLY_MENU_COOLDOWN_MS
              : (kind.startsWith('details:') || kind === 'template-error')
                ? AUTO_REPLY_HINT_COOLDOWN_MS
                : 0;
  const cooldownKey = `${primaryTarget}|${kind}`;
  const now = Date.now();
  const lastSent = Number(autoReplyCooldowns.get(cooldownKey) || 0);
  if (cooldown && now - lastSent < cooldown) {
    result.reply_suppressed = true;
    const left = Math.max(1, Math.ceil((cooldown - (now - lastSent)) / 1000));
    console.log(`Строгий автоответ: повтор ${kind} для ${primaryTarget} подавлен ещё на ${left} сек.`);
    return null;
  }

  const exactText = `${String(result.preface || '').trim()}\n${String(result.reply || '').trim()}`.trim();
  const exactKey = `${primaryTarget}|${exactText}`;
  const exactLastSent = Number(autoReplyExactCooldowns.get(exactKey) || 0);
  if (!priorityInteraction && exactLastSent && now - exactLastSent < AUTO_REPLY_EXACT_COOLDOWN_MS) {
    result.reply_suppressed = true;
    console.log(`Точный повтор автоответа для ${primaryTarget} подавлен.`);
    return null;
  }
  if (!priorityInteraction && autoReplyInflight.has(exactKey)) {
    result.reply_suppressed = true;
    console.log(`Параллельный дубль автоответа для ${primaryTarget} подавлен.`);
    return null;
  }
  if (!priorityInteraction) autoReplyInflight.add(exactKey);
  if (sourceKey) autoReplySourceInflight138.add(sourceKey);

  try {
    // Интерактивное меню отключено намеренно.
    // После команды 1 пользователь получает один обычный текстовый список.
    if (result.awaiting_category || result.main_menu || result.support_menu) {
      const plainMenuText = String(result.reply || '').trim();
      if (!plainMenuText) return null;

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
      const textChatId = delivery.replyChatId(primaryTarget, serializedId(textMessage && textMessage.to));
      await receiving.publishLiveMessage(textChatId, 'Система', textMessage, true, combinedText).catch(() => {});
      result.delivered_reply = combinedText;
      autoReplyCooldowns.set(cooldownKey, Date.now());
      autoReplyExactCooldowns.set(exactKey, Date.now());
      if (sourceKey) autoReplySourceSent138.set(sourceKey, Date.now());
      return textMessage;
    }

    const deliveredReply = String(result.reply);
    const textMessage = await sendToUser(targets, deliveredReply);
    const replyChatId = delivery.replyChatId(primaryTarget, serializedId(textMessage && textMessage.to));
    await receiving.publishLiveMessage(replyChatId, 'Система', textMessage, true, deliveredReply).catch(() => {});
    result.delivered_reply = deliveredReply;
    if (cooldown) autoReplyCooldowns.set(cooldownKey, Date.now());
    autoReplyExactCooldowns.set(exactKey, Date.now());
    if (sourceKey) autoReplySourceSent138.set(sourceKey, Date.now());
    return textMessage;
  } finally {
    if (!priorityInteraction) autoReplyInflight.delete(exactKey);
    if (sourceKey) autoReplySourceInflight138.delete(sourceKey);
    const cleanupBefore = Date.now() - 5 * 60 * 1000;
    for (const [key, sentAt] of autoReplyExactCooldowns) {
      if (Number(sentAt || 0) < cleanupBefore) autoReplyExactCooldowns.delete(key);
    }
  }
}

async function pollOutbound() {
  if (runtime.outboundBusy) return;
  runtime.outboundBusy = true;
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
    await whatsappActions.pollWhatsappAction();
    const result = await postJson('/api/outbound/claim', {
      created_after: runtime.connectorReadyAt,
      wait_ms: 18000,
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
    const sendAliases = await whatsappActions.actionChatCandidates(recipient);
    for (const alias of new Set([recipient, ...sendAliases])) markInternalOutgoing(alias, 20000);
    markActiveInternalOutgoing(queued, [recipient, ...sendAliases], 35000);
    const replyTo = String(queued.reply_to_key || '').trim();
    const options = {mentions:mentionIds, waitUntilMsgSent:false, ignoreQuoteErrors:false};
    let replyTargetObject = null;
    let replyProviderId = replyTo;
    if (replyTo) {
      replyTargetObject = cachedMessageObject(replyTo) || await whatsappActions.resolveMessageForAction(replyTo, recipient, null).catch(() => null);
      const resolvedReplyId = serializedId(replyTargetObject && replyTargetObject.id);
      if (resolvedReplyId) replyProviderId = resolvedReplyId;
      options.quotedMessageId = replyProviderId;
    }
    let content = String(queued.body || '');
    if (queued.media_path) {
      const mediaPath = path.resolve(String(queued.media_path || ''));
      if (!fs.existsSync(mediaPath)) {
        mediaDebug(`missing queue=${queued.id} path=${mediaPath}`);
        throw new Error('Файл для отправки больше не найден на сервере');
      }
      const info = {name:String(queued.media_name || 'Вложение'), mime:String(queued.media_mime || 'application/octet-stream'), size:fs.statSync(mediaPath).size};
      mediaDebug(`send queue=${queued.id} name=${info.name} mime=${info.mime} size=${info.size} recipient=${recipient}`);
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
    const since = sendStartSeconds();
    const outcome = await delivery.sendOnce({
      client, recipient, content, options, aliases: sendAliases,
      begin: async () => Boolean((await postJson('/api/outbound/begin', {message_id:queued.id})).updated),
      confirm: () => confirmSentMessage([recipient, ...sendAliases], String(queued.body || ''), Boolean(queued.media_path), since),
    });
    if (outcome.status !== 'sent') {
      await postJson('/api/outbound/result', {message_id:queued.id, status:outcome.status, error:outcome.error});
      if (queued && queued.media_path) mediaDebug(`result queue=${queued.id} status=${outcome.status} error=${outcome.error || ''} detail=${outcome.detail || ''}`);
      console.warn(outcome.error, outcome.detail || '');
      return;
    }
    if (queued && queued.media_path) mediaDebug(`result queue=${queued.id} status=sent provider=${serializedId(outcome.message && outcome.message.id) || ''}`);
    const sentMessage = outcome.message;
    delivered = true;
    deliveredId = serializedId(sentMessage && sentMessage.id);
    rememberMessageObject(sentMessage, recipient);
    const providerId = rememberInternalOutgoingMessage(sentMessage) || deliveredId || normalizeMessageIdObject(sentMessage);
    runtime.activeInternalOutgoingFingerprint = null;
    let quotedBody = String(queued.reply_preview_body || '').trim().slice(0, 1200);
    let quotedSender = String(queued.reply_preview_sender || '').trim().slice(0, 100);
    if (replyTo) {
      const quoted = replyTargetObject || cachedMessageObject(replyTo) || await whatsappActions.resolveMessageForAction(replyTo, recipient, null).catch(() => null);
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
      await receiving.publishLiveMessage(
        recipient,
        String(queued.actor || 'Вы'),
        sentMessage,
        true,
        String(queued.body || '')
      ).catch(() => {});
    }
  } catch (error) {
    const reason = error && error.message ? error.message : String(error);
    if (queued && queued.media_path) mediaDebug(`exception queue=${queued.id} reason=${reason}`);
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
    runtime.activeInternalOutgoingFingerprint = null;
    runtime.outboundBusy = false;
  }
}
