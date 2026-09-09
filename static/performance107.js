(() => {
  'use strict';
  const VERSION = '3.3.107';
  document.documentElement.classList.add('uq-performance-107');

  // Friendly error layer: preserve original API contracts, only improve generic messages.
  const errorMessage = (raw) => {
    const text = String(raw || '').trim();
    const low = text.toLowerCase();
    if (/target closed|detached frame|protocol error/.test(low)) return 'WhatsApp Web перезапустил внутреннюю страницу. Коннектор восстановит соединение автоматически';
    if (/timeout|timed out|истекло время/.test(low)) return 'Истекло время ожидания. Повторите действие через несколько секунд';
    if (/database is locked|database is busy|база занята/.test(low)) return 'База данных занята другой операцией. Повторите через несколько секунд';
    if (/too large|413|превышает лимит|слишком большой/.test(low)) return 'Файл превышает допустимый размер';
    if (/whatsapp|connector|коннектор/.test(low)) return 'WhatsApp временно недоступен. Проверьте индикатор подключения и повторите действие';
    if (/failed to fetch|networkerror|network error|connection/.test(low)) return 'Нет связи с сервером. Проверьте сеть и повторите действие';
    return text || 'Неизвестная ошибка. Технические детали сохранены в журнале';
  };
  window.queueFriendlyError = errorMessage;

  // Lazy media: browser-native lazy images and metadata-only audio/video until playback.
  const tuneMedia = (root = document) => {
    root.querySelectorAll?.('img[src*="/api/chat-media"], img[data-media-preview]').forEach((img) => {
      img.loading = 'lazy'; img.decoding = 'async'; img.fetchPriority = 'low';
    });
    root.querySelectorAll?.('video, audio').forEach((media) => {
      if (!media.autoplay && !media.dataset.uqPreload107) {
        media.preload = 'metadata'; media.dataset.uqPreload107 = '1';
      }
    });
  };
  tuneMedia();
  new MutationObserver((mutations) => {
    for (const m of mutations) for (const node of m.addedNodes) if (node.nodeType === 1) tuneMedia(node);
  }).observe(document.documentElement, {childList:true, subtree:true});

  // PC-only build: intentionally does not add phone-specific navigation or layouts,
  // touch-only chat modes, or viewport overrides. Existing desktop UI remains authoritative.

  window.UQPerformance107 = {version: VERSION, errorMessage};
})();

// 3.3.107 reliability/performance diagnostics on the existing admin page.
(() => {
  if (location.pathname !== '/admin/reliability') return;
  const mount = document.querySelector('.reliability-grid');
  if (!mount) return;
  const render = async () => {
    try {
      const response = await fetch('/api/performance-state', {cache:'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      let box = document.querySelector('[data-performance107-cards]');
      if (!box) {
        box = document.createElement('section'); box.className = 'reliability-grid uq-performance-cards-107'; box.dataset.performance107Cards = '1';
        mount.insertAdjacentElement('afterend', box);
      }
      const c = data.connector || {}, sql = data.sql || {}, voice = data.voice_queue || {};
      box.innerHTML = `
        <article class="panel reliability-card"><span>RAM WhatsApp</span><strong>${Number(c.rss_mb||0).toFixed(0)} МБ</strong><small>автопорог: ${c.ram_restart_mb ? c.ram_restart_mb+' МБ' : 'выключен'}</small></article>
        <article class="panel reliability-card"><span>RAM сайта</span><strong>${Number(data.server_rss_mb||0).toFixed(0)} МБ</strong><small>Python-процесс</small></article>
        <article class="panel reliability-card"><span>Медиа-очередь</span><strong>${Number(c.pending_media||0)}</strong><small>${Number(data.media_workers||0)} обработчика · chunk ${Math.round(Number(data.chunk_size||0)/1024)} КБ</small></article>
        <article class="panel reliability-card"><span>SQLite</span><strong class="${sql.ok===false?'system-bad':'system-ok'}">${sql.ok===false?'Проверить':'OK'}</strong><small>индексов ${Number(sql.indexes||0)} · медленных ${Array.isArray(sql.slow)?sql.slow.length:0}</small></article>
        <article class="panel reliability-card"><span>Голосовые</span><strong>${Number(voice.queued||0)} в очереди</strong><small>1 ограниченный worker · готовые не пересчитываются</small></article>`;
    } catch (_) {}
  };
  render(); setInterval(render, 15000);
})();

// Show a concise human explanation for failed interactive API calls without changing their response body.
(() => {
  const originalFetch = window.fetch;
  if (!originalFetch || originalFetch.__uq107) return;
  const recent = new Map();
  const toast = (text) => {
    const message = window.queueFriendlyError(text);
    const now = Date.now(); if (now - Number(recent.get(message)||0) < 5000) return; recent.set(message, now);
    const stack = document.querySelector('[data-toast-stack]');
    if (!stack) return;
    const el = document.createElement('div'); el.className = 'toast system-toast uq-friendly-error-107';
    el.innerHTML = `<strong>Не удалось выполнить действие</strong><span></span>`; el.querySelector('span').textContent = message;
    stack.appendChild(el); setTimeout(() => el.remove(), 6500);
  };
  const quiet = /\/api\/(notifications|chat-state|group-state|performance-state|admin\/connector-state)/;
  const wrapped = async (...args) => {
    try {
      const response = await originalFetch(...args);
      const url = String(args[0]?.url || args[0] || '');
      if (!response.ok && !quiet.test(url)) {
        const ct = response.headers.get('content-type') || '';
        if (ct.includes('json')) response.clone().json().then((x) => { if (x?.error) toast(x.error); }).catch(() => {});
      }
      return response;
    } catch (error) {
      const url = String(args[0]?.url || args[0] || '');
      if (!quiet.test(url)) toast(error?.message || error);
      throw error;
    }
  };
  wrapped.__uq107 = true; window.fetch = wrapped;
})();
