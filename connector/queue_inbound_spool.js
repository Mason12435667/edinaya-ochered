'use strict';
// Надёжная очередь входящих сообщений WhatsApp.
//
// Сообщение записывается на диск до первого запроса к приложению и удаляется только после его
// ответа. Если приложение недоступно или коннектор перезапустился, drain() доставляет остаток.
// Сервер отбрасывает повторы по external_id, поэтому двойная доставка безопасна.

const fs = require('fs');
const path = require('path');

const MIN_AGE_MS = 30 * 1000; // leave fresh messages to the live path

function fileName(externalId) {
  const safe = String(externalId || '').replace(/[^A-Za-z0-9_.@-]/g, '_').slice(0, 180);
  return safe ? `${safe}.json` : '';
}

class InboundSpool {
  constructor(directory) {
    this.directory = directory;
    this.inFlight = new Set();
    fs.mkdirSync(directory, {recursive: true});
  }

  pathFor(payload) {
    const name = fileName(payload && payload.external_id);
    return name ? path.join(this.directory, name) : '';
  }

  enqueue(payload) {
    const target = this.pathFor(payload);
    if (!target) return false;
    const temporary = `${target}.${process.pid}.tmp`;
    const descriptor = fs.openSync(temporary, 'w');
    try {
      fs.writeFileSync(descriptor, JSON.stringify({saved_at: Date.now(), payload}));
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
    if (target) fs.rmSync(target, {force: true});
  }

  // Live path: keep the spooled copy until the application has answered.
  async submit(payload, send) {
    const target = this.pathFor(payload);
    if (target) this.enqueue(payload); // the live payload is the most complete version
    this.inFlight.add(target);
    try {
      const result = await send(payload);
      this.remove(payload);
      return result;
    } catch (error) {
      // A 4xx answer will not get better on retry; keep only retryable failures.
      const status = Number(error && error.httpStatus || 0);
      if (status && status < 500) this.remove(payload);
      throw error;
    } finally {
      this.inFlight.delete(target);
    }
  }

  pending() {
    const now = Date.now();
    const items = [];
    for (const name of fs.readdirSync(this.directory)) {
      if (!name.endsWith('.json')) continue;
      const file = path.join(this.directory, name);
      if (this.inFlight.has(file)) continue;
      try {
        const record = JSON.parse(fs.readFileSync(file, 'utf8'));
        if (now - Number(record.saved_at || 0) >= MIN_AGE_MS) items.push(record);
      } catch (_) {
        fs.rmSync(file, {force: true}); // half-written or corrupt record
      }
    }
    return items.sort((a, b) => Number(a.saved_at) - Number(b.saved_at)).map((record) => record.payload);
  }

  // Recovery path: deliver old records in arrival order; stop at the first
  // retryable failure so the order is kept for the next attempt.
  async drain(send, after = async () => {}) {
    let delivered = 0;
    for (const payload of this.pending()) {
      const result = await this.submit(payload, send);
      delivered += 1;
      if (result) await after(payload, result);
    }
    return delivered;
  }
}

module.exports = {InboundSpool, MIN_AGE_MS};
