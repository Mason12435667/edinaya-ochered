'use strict';
// Отправка в WhatsApp не более одного раза.
//
// begin() сообщает серверу, что сообщение уходит (статус «sending»). После этого отправка
// выполняется один раз: исключение может возникнуть уже после доставки, поэтому результат
// отмечается как «uncertain» и автоматически не повторяется. К следующему адресу переходят
// только при ошибках, которые WhatsApp Web выдаёт до отправки (неверный или неизвестный адрес).

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
// also counts when its @lid resolves to one of our addresses (row.phone). A row
// that cannot be tied to this chat never counts: it may be another person's. Two
// equal candidates cannot be told apart. In both cases the send stays uncertain.
function findSentMessage(rows, {text, hasMedia = false, since, aliases, isKnown}) {
  const found = (rows || []).filter((row) => row && row.id && row.fromMe
    && Number(row.timestamp || 0) >= since
    && (row.type !== 'chat') === hasMedia
    && (text ? row.text === text : hasMedia)
    && !isKnown(row));
  const exact = found.filter((row) => aliases.has(row.remote));
  const pool = exact.length ? exact : found.filter((row) => aliases.has(row.phone));
  return pool.length === 1 ? pool[0] : null;
}

// The chat an automatic reply is saved under. WhatsApp Web may report the sent
// message under an @lid the server has never seen. Saved there, the reply lands in
// a separate chat and the user's own chat looks unanswered.
function replyChatId(addressed, sentTo) {
  if (String(addressed || '').endsWith('@c.us')) return addressed;
  return sentTo || addressed;
}

// A menu digit or the finish command is a deliberate step and always gets its
// answer. Free text is not: until a language is chosen, every message would
// otherwise get the same greeting again.
function isDeliberateStep(menuChoice, finishCommand) {
  return /^[0-8]$/.test(String(menuChoice || '').trim()) || Boolean(finishCommand);
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
    if (type === 'ptt') options.sendAudioAsVoice = true;
    if (type === 'sticker') options.sendMediaAsSticker = true;
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

module.exports = {replyChatId, isDeliberateStep, isForwardUnavailable, resendAsForward, waitForValue, findSentMessage, sendOnce, editInPage, editError, PRE_SEND_ERROR};
