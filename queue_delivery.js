'use strict';
// Outgoing WhatsApp delivery with at-most-once semantics.
//
// begin() tells the server the message is about to leave (status "sending").
// After that a send is attempted once: an exception may happen after WhatsApp
// already delivered the message, so it is reported as "uncertain" and never
// retried automatically. Only errors that WhatsApp Web raises before sending
// anything (an invalid or unknown address) move on to the next alias.

const PRE_SEND_ERROR = /(invalid wid|wid error|no lid for user|not a valid|isn't on whatsapp|not registered)/i;

function errorText(error) {
  return String((error && error.message) || error || 'Неизвестная ошибка WhatsApp').slice(0, 300);
}

// Resolves with the first truthy result of read() or with null after timeoutMs.
async function waitForValue(read, timeoutMs, intervalMs = 50) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await read();
    if (value) return value;
    if (Date.now() >= deadline) return null;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

// Finds our message among recent rows of the WhatsApp message store. A sent
// message may be stored under the @lid of a chat we addressed as @c.us, so a row
// from an unknown @lid still counts. Two equal candidates cannot be told apart:
// the answer is null and the send stays uncertain.
function findSentMessage(rows, {text, hasMedia = false, since, aliases, isKnown}) {
  const found = (rows || []).filter((row) => row && row.id && row.fromMe
    && Number(row.timestamp || 0) >= since
    && (row.type !== 'chat') === hasMedia
    && (text ? row.text === text : hasMedia)
    && !isKnown(row));
  const exact = found.filter((row) => aliases.has(row.remote));
  const pool = exact.length ? exact : found.filter((row) => String(row.remote || '').endsWith('@lid'));
  return pool.length === 1 ? pool[0] : null;
}

// confirm(target) is asked when whatsapp-web.js returned nothing: the message may
// already be in the chat. It only looks for the message and never sends again.
async function sendOnce({client, recipient, aliases = [], content, options = {}, begin, confirm}) {
  const targets = [...new Set([recipient, ...aliases].map((value) => String(value || '').trim()).filter(Boolean))];
  if (!targets.length) return {status: 'failed', error: 'Не указан получатель'};
  let started = false;
  try {
    started = Boolean(await begin());
  } catch (error) {
    return {status: 'failed', error: `Сервер не подтвердил начало отправки: ${errorText(error)}`};
  }
  if (!started) return {status: 'failed', error: 'Сообщение больше не ожидает отправки'};

  let lastError = null;
  for (const target of targets) {
    try {
      const returned = await client.sendMessage(target, content, options);
      const message = returned || (confirm ? await confirm(target).catch(() => null) : null);
      if (!message) return {status: 'uncertain', error: 'WhatsApp не вернул отправленное сообщение. Проверьте переписку'};
      return {status: 'sent', message, recipient: target};
    } catch (error) {
      lastError = error;
      if (!PRE_SEND_ERROR.test(errorText(error))) {
        return {
          status: 'uncertain',
          error: 'Нет подтверждения WhatsApp: сообщение могло уйти. Проверьте переписку',
          detail: errorText(error),
        };
      }
    }
  }
  return {status: 'failed', error: 'WhatsApp не принял ни один адрес получателя', detail: errorText(lastError)};
}

// whatsapp-web.js forwards through a WhatsApp Web module that now loads only when
// a person opens the forward dialog. This error means nothing was sent.
const FORWARD_UNAVAILABLE = /reading 'forwardMessages'|WAWebChatForwardMessage/;

function isForwardUnavailable(error) {
  return FORWARD_UNAVAILABLE.test(errorText(error));
}

// Sends a copy of the message marked as forwarded. Use it only when
// isForwardUnavailable(): after any other error the original may have gone out.
async function resendAsForward({client, type, hasMedia, download, target, text, confirm}) {
  const options = {extra: {isForwarded: true, forwardingScore: 1}};
  let content = text;
  if (hasMedia) {
    content = await download();
    if (!content) throw new Error('Не удалось загрузить вложение для пересылки');
    if (text) options.caption = text;
    if (type === 'document') options.sendMediaAsDocument = true;
  }
  if (!content) throw new Error('В сообщении нет текста для пересылки');
  return sendOnce({client, recipient: target, content, options, begin: async () => true, confirm});
}

// Runs inside the WhatsApp Web page (client.pupPage.evaluate), so it may only use
// browser globals. Mirrors whatsapp-web.js Message.edit() with feature checks.
async function editInPage(messageKey, body, aliases) {
  try {
    const store = window.Store || {};
    const collection = store.Msg || (window.require && window.require('WAWebCollections').Msg);
    if (!collection || !window.WWebJS || typeof window.WWebJS.editMessage !== 'function') {
      return {ok: false, reason: 'unavailable'};
    }
    let msg = collection.get(messageKey);
    if (!msg && Array.isArray(aliases)) {
      const id = String(messageKey || '').split('_').pop();
      msg = collection.getModelsArray().find((item) => item && item.id && item.id.fromMe && item.id.id === id) || null;
    }
    if (!msg) return {ok: false, reason: 'not_found'};
    const checks = store.MsgActionChecks;
    if (checks && !(checks.canEditText(msg) || checks.canEditCaption(msg))) return {ok: false, reason: 'not_editable'};
    await window.WWebJS.editMessage(msg, String(body || ''), {});
    return {ok: true};
  } catch (error) {
    return {ok: false, reason: 'error', detail: String((error && error.message) || error).slice(0, 300)};
  }
}

function editError(result) {
  const reasons = {
    unavailable: 'Редактирование сейчас недоступно в WhatsApp Web',
    not_found: 'Сообщение не найдено в WhatsApp Web',
    not_editable: 'WhatsApp больше не позволяет изменить это сообщение',
  };
  return reasons[result && result.reason] || 'Не удалось изменить сообщение в WhatsApp';
}

module.exports = {isForwardUnavailable, resendAsForward, waitForValue, findSentMessage, sendOnce, editInPage, editError, PRE_SEND_ERROR};
