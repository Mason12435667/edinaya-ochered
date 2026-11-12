'use strict';
// Восстановление: находит в WhatsApp Web входящие, по которым не пришло событие, и передаёт их
// на сервер.

module.exports = {
  reconcileRecentInbound,
};

const {
  runtime, client, isLiveInboundMessage, knownChats, lidToPhone, mergeKnownDirectChatAlias138,
  messageText, ownMentionIds, participantDisplayById, pendingMediaMessages, postJson,
  prettyPhoneFromId, reconciledInboundIds, rememberMessageObject, resolveDirectPhoneId,
  resolveParticipantIdentity, safeWhatsAppBody, serializedId, STARTUP_BACKFILL_BATCH,
  STARTUP_BACKFILL_MODEL_LIMIT, USER_MESSAGE_TYPES,
} = require('./connector_common');
const sending = require('./connector_send');
const receiving = require('./connector_receive');

let lastRecentPollAt = 0;
let recentPollBusy = false;
const STARTUP_BACKFILL_LOOKBACK_SECONDS = 30 * 60;
const STARTUP_AUTOMATION_MAX_AGE_SECONDS = 120;
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
  const recoveredDraftFinish138 = receiving.isDraftFinishText138(text);
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

