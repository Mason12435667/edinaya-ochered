'use strict';
// Надёжная очередь входящих сообщений WhatsApp.
//
// Сообщение записывается на диск до первого запроса к приложению и удаляется только после его
// ответа. Если приложение недоступно или коннектор перезапустился, drain() доставляет остаток.
// Сервер отбрасывает повторы по external_id, поэтому двойная доставка безопасна.
// Внутри одного чата сообщения уходят на сервер по одному и в порядке поступления.

const fs = require('fs');
const path = require('path');

const MIN_AGE_MS = 30 * 1000; // leave fresh messages to the live path

// Сервер отверг само сообщение, повтор не поможет. Остальные ошибки могут пройти
// (нет связи, 5xx, неверный токен, ограничение частоты), и сообщение остаётся в очереди.
const FINAL_STATUSES = new Set([400, 413, 422]);

function isFinalRejection(error) {
  return FINAL_STATUSES.has(Number(error && error.httpStatus));
}

function fileName(externalId) {
  const safe = String(externalId || '').replace(/[^A-Za-z0-9_.@-]/g, '_').slice(0, 180);
  return safe ? `${safe}.json` : '';
}

const byArrival = (a, b) => Number(a.record.saved_at) - Number(b.record.saved_at);

class InboundSpool {
  constructor(directory) {
    this.directory = directory;
    this.inFlight = new Set();
    this.failed = new Set(); // записи, которые живой путь не смог доставить
    this.chatLines = new Map();
    this.lastSavedAt = 0;
    fs.mkdirSync(directory, {recursive: true});
  }

  pathFor(payload) {
    const name = fileName(payload && payload.external_id);
    return name ? path.join(this.directory, name) : '';
  }

  savedAtOf(file) {
    try {
      return Number(JSON.parse(fs.readFileSync(file, 'utf8')).saved_at) || 0;
    } catch (_) {
      return 0;
    }
  }

  // Строго возрастает: у двух сообщений одной миллисекунды порядок тоже определён.
  nextSavedAt() {
    this.lastSavedAt = Math.max(Date.now(), this.lastSavedAt + 1);
    return this.lastSavedAt;
  }

  // Перезапись обновляет содержимое, но место сообщения в очереди остаётся прежним.
  enqueue(payload) {
    const target = this.pathFor(payload);
    if (!target) return false;
    const savedAt = this.savedAtOf(target) || this.nextSavedAt();
    const temporary = `${target}.${process.pid}.tmp`;
    const descriptor = fs.openSync(temporary, 'w');
    try {
      fs.writeFileSync(descriptor, JSON.stringify({saved_at: savedAt, payload}));
      fs.fsyncSync(descriptor);
    } finally {
      fs.closeSync(descriptor);
    }
    fs.renameSync(temporary, target);
    this.syncDirectory();
    return true;
  }

  // Makes the rename itself survive a power cut. Windows refuses fsync on a
  // directory (EPERM); there the flushed file above is all we can do.
  syncDirectory() {
    if (process.platform === 'win32') return;
    const descriptor = fs.openSync(this.directory, 'r');
    try {
      fs.fsyncSync(descriptor);
    } finally {
      fs.closeSync(descriptor);
    }
  }

  remove(payload) {
    const target = this.pathFor(payload);
    if (!target) return;
    fs.rmSync(target, {force: true});
    this.failed.delete(target);
  }

  // Следующая задача чата начинается только после завершения предыдущей, даже неудачного.
  inChatOrder(payload, task) {
    const chat = payload && payload.chat_id;
    if (!chat) return task();
    const line = (this.chatLines.get(chat) || Promise.resolve()).then(task);
    const tail = line.catch(() => {});
    this.chatLines.set(chat, tail);
    tail.then(() => {
      if (this.chatLines.get(chat) === tail) this.chatLines.delete(chat);
    });
    return line;
  }

  // Keep the spooled copy until the application has answered.
  async deliver(payload, send) {
    const target = this.pathFor(payload);
    this.inFlight.add(target);
    try {
      const result = await send(payload);
      this.remove(payload);
      return result;
    } catch (error) {
      if (isFinalRejection(error)) this.remove(payload);
      throw error;
    } finally {
      this.inFlight.delete(target);
    }
  }

  // Сообщения чата, которые застряли раньше этого, уходят первыми. Если они не проходят,
  // новое сообщение остаётся в очереди за ними.
  async deliverOlder(payload, send, after) {
    if (!payload || !payload.chat_id) return;
    for (const older of this.olderInChat(payload, this.savedAtOf(this.pathFor(payload)))) {
      let result;
      try {
        result = await this.deliver(older, send);
      } catch (error) {
        if (isFinalRejection(error)) continue;
        throw error;
      }
      try {
        if (result) await after(older, result);
      } catch (error) {
        console.warn('Не удалось ответить на восстановленное сообщение:', (error && error.message) || error);
      }
    }
  }

  // Live path.
  async submit(payload, send, after = async () => {}) {
    const target = this.pathFor(payload);
    if (target) this.enqueue(payload); // the live payload is the most complete version
    this.inFlight.add(target);
    try {
      return await this.inChatOrder(payload, async () => {
        await this.deliverOlder(payload, send, after);
        return this.deliver(payload, send);
      });
    } catch (error) {
      if (target && fs.existsSync(target)) this.failed.add(target);
      throw error;
    } finally {
      this.inFlight.delete(target);
    }
  }

  records() {
    const items = [];
    for (const name of fs.readdirSync(this.directory)) {
      if (!name.endsWith('.json')) continue;
      const file = path.join(this.directory, name);
      if (this.inFlight.has(file)) continue;
      try {
        items.push({file, record: JSON.parse(fs.readFileSync(file, 'utf8'))});
      } catch (_) {
        fs.rmSync(file, {force: true}); // half-written or corrupt record
      }
    }
    return items;
  }

  pending() {
    const now = Date.now();
    return this.records()
      .filter(({record}) => now - Number(record.saved_at || 0) >= MIN_AGE_MS)
      .sort(byArrival)
      .map(({record}) => record.payload);
  }

  // Свежие записи без неудачной попытки ещё обрабатывает живой путь, их не трогаем.
  olderInChat(payload, savedAt) {
    const now = Date.now();
    return this.records()
      .filter(({file, record}) => record.payload && record.payload.chat_id === payload.chat_id
        && Number(record.saved_at) < savedAt
        && (now - Number(record.saved_at || 0) >= MIN_AGE_MS || this.failed.has(file)))
      .sort(byArrival)
      .map(({record}) => record.payload);
  }

  // Recovery path: deliver old records in arrival order; stop at the first
  // retryable failure so the order is kept for the next attempt.
  async drain(send, after = async () => {}) {
    let delivered = 0;
    for (const payload of this.pending()) {
      await this.inChatOrder(payload, async () => {
        if (!fs.existsSync(this.pathFor(payload))) return; // уже доставлено живым путём
        const result = await this.deliver(payload, send);
        delivered += 1;
        if (result) await after(payload, result);
      });
    }
    return delivered;
  }
}

module.exports = {InboundSpool, MIN_AGE_MS, isFinalRejection};
