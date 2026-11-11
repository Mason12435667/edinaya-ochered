'use strict';
// Приём: обрабатывает события WhatsApp о сообщениях, передаёт входящие на сервер, публикует
// сообщения в панель и защищает от шквала сообщений. При подключении файла регистрирует обработчики.

module.exports = {
  attachQuoteToItem, isDraftFinishText138, liveMessageItem, markChatRead, publishLiveMessage,
};

const {
  runtime, backgroundWorkAllowed, cachedMessageObject, cleanContactDisplayName, client,
  contactDisplayById, inboundFloodState, inboundSpool, incomingText, isLiveInboundMessage,
  isRememberedForwarded, isRememberedInternalOutgoing, knownChats, locallyRecoveredMediaIds,
  mergeKnownDirectChatAlias138, messageAcknowledgements, messageMentionIds, messageText,
  normalizeMessageIdObject, noteBrowserPressure, noteHotInboundPriority, outgoingMediaProbe,
  ownMentionIds, pendingMediaMessages, persistedAcknowledgements, postJson, recentMessageObjects,
  reconciledInboundIds, reconciledOutgoingIds, rememberInternalOutgoingMessage,
  rememberMessageObject, resolveDirectPhoneId, resolveGroupSenderInfo, resolveMentionPresentation,
  resolvePhoneId, serializedId, USER_MESSAGE_TYPES,
} = require('./connector_common');
const sending = require('./connector_send');
const downloading = require('./connector_media');

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
    try { await downloading.attachMediaToItem(message, item); }
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
