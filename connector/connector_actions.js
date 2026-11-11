'use strict';
// Действия с уже существующими сообщениями по команде сервера: удаление и пересылка.

module.exports = {
  actionChatCandidates, pollWhatsappAction, resolveMessageForAction,
};

const delivery = require('./queue_delivery');
const {MessageMedia} = require('whatsapp-web.js');
const {
  cachedMessageObject, chatIdAliases, client, incomingText, messageText, normalizeMessageIdObject,
  postJson, recentMessageObjects, rememberForwardedMessage, rememberInternalOutgoingMessage,
  rememberMessageObject, serializedId,
} = require('./connector_common');
const sending = require('./connector_send');
const receiving = require('./connector_receive');

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
    // active chat model lives under the canonical @c.us id. Using only
    // msg.id.remote, both delete paths could still hit the same bad
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
    if (!window.require('WAWebChatForwardMessage')?.forwardMessages) return { ok: false, reason: 'forward_unavailable' };
    await window.WWebJS.forwardMessage(target, widText(msg.id));
    return { ok: true, id: widText(msg.id) };
  }, String(messageKey || ''), aliases, String(targetChatId || ''), payload);
}

const RESEND_DOWNLOAD_MS = 20000; // a video needs longer than the 8 seconds given to background downloads
const RESEND_DOWNLOAD_ATTEMPTS = 2;
const RESEND_DOWNLOAD_PAUSE_MS = 2000;

// Message.downloadMedia() fails with a short error on current WhatsApp Web, so the
// direct download goes first, as attachMediaToItem() ends up doing. The direct
// download refuses to start while another one is running, hence the second attempt.
async function downloadForResend(message) {
  const id = normalizeMessageIdObject(message);
  const media = await delivery.retried(async () => {
    const found = await receiving.downloadMediaDirectById(id, RESEND_DOWNLOAD_MS)
      || await Promise.resolve().then(() => message.downloadMedia()).catch(() => null);
    return found && found.data ? found : null;
  }, RESEND_DOWNLOAD_ATTEMPTS, RESEND_DOWNLOAD_PAUSE_MS);
  if (!media) return null;
  return new MessageMedia(media.mimetype, media.data, media.filename || null, media.filesize || undefined);
}

const RESEND_BODY = {location: '[Геолокация]', vcard: '[Контакт]'};

async function forwardByResend(message, target, actor) {
  const text = String(incomingText(message) || '').trim();
  const type = String(message.type || '');
  const hasMedia = Boolean(message.hasMedia) || ['image', 'video', 'audio', 'ptt', 'document', 'sticker'].includes(type);
  const special = type in RESEND_BODY; // sent as its own kind of message, without text to compare
  const since = sending.sendStartSeconds();
  let media = null;
  const outcome = await delivery.resendAsForward({
    client, type, hasMedia, target, text, location: message.location,
    download: async () => (media = await downloadForResend(message)),
    confirm: () => sending.confirmSentMessage([target], special ? '' : text, hasMedia || special, since),
  });
  if (outcome.status !== 'sent') throw new Error(outcome.error);
  rememberForwardedMessage(outcome.message);
  const item = delivery.forwardedCopyItem({
    id: normalizeMessageIdObject(outcome.message) || serializedId(outcome.message.id),
    actor, body: RESEND_BODY[type] || text || (hasMedia ? messageText({type, mimetype: media && media.mimetype}) : ''),
    type: hasMedia || special ? type : 'chat', timestamp: outcome.message.timestamp, media,
  });
  const saved = await delivery.savedAfterSend(
    () => postJson('/api/chat-messages-sync', {chat_id: target, append: true, messages: [item]}),
    (error) => console.warn('Пересланная копия отправлена, но не сохранена в истории:', (error && error.message) || error),
  );
  // Unsaved, the copy stays visible to the reserve sync, which saves it later.
  if (saved) rememberInternalOutgoingMessage(outcome.message);
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
        if ((directResult && directResult.reason === 'forward_unavailable') || delivery.isForwardUnavailable(directError)) {
          await forwardByResend(message, target, String(queued.actor || 'Вы'));
        } else {
          try {
            await message.forward(target);
          } catch (firstError) {
            if (delivery.isForwardUnavailable(firstError)) {
              await forwardByResend(message, target, String(queued.actor || 'Вы'));
            } else {
              if (!targetRaw || targetRaw === target) throw firstError;
              await message.forward(targetRaw);
            }
          }
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
    const shortReason = queued && queued.action_type === 'forward'
      ? 'Не удалось переслать сообщение в WhatsApp' : delivery.editError({reason:'unavailable'});
    const reason = rawReason.length < 4 ? shortReason : rawReason;
    if (queued && queued.id) {
      try {
        await postJson('/api/whatsapp-action/result', {
          action_id: queued.id,
          status: 'failed',
          error: reason,
        });
      } catch (_) {}
    }
    if (queued) console.warn(`Не удалось выполнить действие ${queued.action_type} в WhatsApp:`, reason, rawReason === reason ? '' : `(${rawReason})`);
  }
}
