(() => {
  'use strict';

  function setTheme(theme) {
    const value = theme === 'pink' ? 'pink' : (theme === 'dark' ? 'dark' : 'light');
    document.documentElement.dataset.theme = value;
    try { localStorage.setItem('queue-theme', value); } catch (_) {}
    document.querySelectorAll('[data-auth-theme-toggle]').forEach((button) => {
      button.textContent = value === 'pink' ? 'Розовая тема 🌸' : (value === 'dark' ? 'Светлая тема' : 'Тёмная тема');
    });
    return value;
  }

  async function savePreference(key, value) {
    const userId = Number(document.body?.dataset?.authUserId || 0);
    if (!userId) return;
    try {
      const body = new URLSearchParams({[key]: String(value)});
      await fetch('/api/account-preferences', {
        method: 'POST',
        headers: {'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8','X-Requested-With':'fetch'},
        body: body.toString(),
      });
    } catch (_) {}
  }

  document.querySelectorAll('[data-auth-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const value = setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
      savePreference('theme', value);
    });
  });
  if (document.querySelector('[data-auth-theme-toggle]')) setTheme(document.documentElement.dataset.theme || 'dark');

  // Main application theme button is owned by app.js; persist its final value to the account.
  document.addEventListener('click', (event) => {
    const button = event.target?.closest?.('[data-theme-toggle]');
    if (!button) return;
    window.setTimeout(() => savePreference('theme', setTheme(document.documentElement.dataset.theme || 'dark')), 80);
  });

  // Favorites/bookmarks have been restored. Remove only the unwanted auto-voice
  // toolbar control; never touch the manually controlled voice player or WhatsApp.
  function removeAutoVoiceControl(root=document) {
    const candidates = [];
    if (root.matches?.('button, [role="button"]')) candidates.push(root);
    root.querySelectorAll?.('button, [role="button"]').forEach((node) => candidates.push(node));
    for (const node of candidates) {
      const label = String(node.textContent || node.getAttribute('aria-label') || node.title || '').replace(/\s+/g, ' ').trim();
      if (/^авто[\s-]*голос(?:\s|$|[:—–-])/i.test(label)) node.remove();
    }
  }
  removeAutoVoiceControl();
  new MutationObserver((mutations) => {
    for (const mutation of mutations) for (const node of mutation.addedNodes || []) if (node.nodeType === 1) removeAutoVoiceControl(node);
  }).observe(document.documentElement, {subtree: true, childList: true});

  // 1.00.6.26: remove redundant conversation actions while keeping their backend intact.
  const hiddenConversationLabels = new Set(['Непрочитано', 'История', 'Сводка']);
  function removeCompactConversationActions(root=document) {
    const nodes = [];
    if (root?.matches?.('button, a, [role="button"]')) nodes.push(root);
    root.querySelectorAll?.('button, a, [role="button"]').forEach((node) => nodes.push(node));
    for (const node of nodes) {
      if (!node.closest?.('[data-conversation-page]')) continue;
      const label = String(node.textContent || '').replace(/\s+/g, ' ').trim();
      if (hiddenConversationLabels.has(label)) node.remove();
    }
  }
  removeCompactConversationActions();
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes || []) {
        if (node.nodeType === 1) removeCompactConversationActions(node);
      }
    }
  }).observe(document.documentElement, {subtree:true, childList:true});



  // 1.00.6.26: unified validation for authentication forms.
  function ensureFieldError(input, message) {
    if (!input) return;
    let node = input.parentElement?.querySelector?.('.auth-field-error');
    if (!node) {
      node = document.createElement('small');
      node.className = 'auth-field-error';
      input.parentElement?.appendChild(node);
    }
    node.textContent = message || '';
    input.classList.toggle('auth-input-invalid', Boolean(message));
    node.hidden = !message;
  }

  function validateAuthForm(form) {
    let valid = true;
    form.querySelectorAll('input[required]').forEach((input) => {
      const value = String(input.value || '').trim();
      let message = '';
      if (!value) message = 'Заполните это поле';
      else if (input.name === 'username' && !/^[A-Za-z0-9_.-]{3,32}$/.test(value)) message = 'Логин: 3–32 символа, латиница, цифры, . _ -';
      else if ((input.type === 'password' || /password/i.test(input.name)) && input.minLength > 0 && value.length < input.minLength) message = `Минимум ${input.minLength} символов`;
      ensureFieldError(input, message);
      if (message) valid = false;
    });
    for (const [a,b] of [['password','password2'], ['new_password','new_password2']]) {
      const first = form.querySelector(`[name="${a}"]`);
      const second = form.querySelector(`[name="${b}"]`);
      if (first && second && String(first.value || '') !== String(second.value || '')) {
        ensureFieldError(second, 'Пароли не совпадают');
        valid = false;
      }
    }
    return valid;
  }

  document.querySelectorAll('form.auth-form, form.auth-profile-password, form.auth-create-user').forEach((form) => {
    form.setAttribute('novalidate', 'novalidate');
    form.addEventListener('input', (event) => {
      if (event.target instanceof HTMLInputElement) ensureFieldError(event.target, '');
    });
    form.addEventListener('submit', (event) => {
      if (!validateAuthForm(form)) {
        event.preventDefault();
        form.querySelector('.auth-input-invalid')?.focus();
      }
    });
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    document.querySelectorAll('.auth-profile-menu[open]').forEach((details) => details.removeAttribute('open'));
  });


  const shell = document.querySelector('[data-conversation-page]');
  if (!shell) return;

  let lastPayload = null;
  let usersCache = null;
  let currentUserId = 0;
  let isAdmin = false;
  let activeShift = '';
  const transferChoices = new Map();
  let bannerSignature = '';

  async function loadUsers() {
    if (usersCache) return usersCache;
    try {
      const response = await fetch('/api/conversation-users', {cache: 'no-store'});
      if (!response.ok) return [];
      const data = await response.json();
      activeShift = String(data.active_shift_employee || "").trim().toLocaleLowerCase();
      usersCache = Array.isArray(data.users) ? data.users : [];
      currentUserId = Number(data.current_user_id || 0);
      isAdmin = Boolean(data.is_admin);
      return usersCache;
    } catch (_) {
      return [];
    }
  }

  function lockElements(readOnly, ownerName) {
    const targets = [
      ...shell.querySelectorAll('[data-chat-composer] textarea, [data-chat-composer] button[type="submit"], [data-media-input], [data-manual-mode-toggle], [data-group-mute-toggle], [data-forward-send]')
    ];
    targets.forEach((element) => {
      if (readOnly) {
        if (!element.disabled) element.dataset.lockDisabled = '1';
        element.disabled = true;
        element.setAttribute('aria-disabled', 'true');
      } else if (element.dataset.lockDisabled === '1') {
        element.disabled = false;
        element.removeAttribute('aria-disabled');
        delete element.dataset.lockDisabled;
      }
    });
    const composer = shell.querySelector('[data-chat-composer]');
    if (composer) composer.classList.toggle('is-read-only-lock', readOnly);
    const textarea = shell.querySelector('[data-chat-composer] textarea');
    if (textarea) {
      if (!textarea.dataset.normalPlaceholder) textarea.dataset.normalPlaceholder = textarea.placeholder || '';
      textarea.placeholder = readOnly ? `Только просмотр · ведёт ${ownerName || 'другой пользователь'}` : textarea.dataset.normalPlaceholder;
    }
  }

  function banner() {
    let node = shell.querySelector('[data-conversation-lock-banner]');
    if (node) return node;
    node = document.createElement('div');
    node.className = 'conversation-lock-banner';
    node.dataset.conversationLockBanner = '1';
    const header = shell.querySelector('.chat-main-head, .chat-header, [data-chat-title]')?.closest('.chat-main-head, .chat-header') || shell.querySelector('[data-chat-title]')?.parentElement?.parentElement;
    if (header && header.parentNode) header.parentNode.insertBefore(node, header.nextSibling);
    else shell.prepend(node);
    return node;
  }

  async function postLock(action, targetUserId = 0) {
    const payload = lastPayload || {};
    const chatId = String(payload.selected_chat_id || '');
    if (!chatId) return;
    const body = new URLSearchParams({chat_id: chatId, action});
    if (targetUserId) body.set('target_user_id', String(targetUserId));
    try {
      const response = await fetch('/conversation-lock', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Requested-With': 'fetch'},
        body: body.toString(),
      });
      const data = await response.json().catch(() => ({}));
      if (data.conversation_lock && chatId === String(lastPayload?.selected_chat_id || '') && chatId === String(shell.querySelector('[data-chat-id]')?.value || chatId)) {
        const next = {...payload, conversation_lock: data.conversation_lock};
        applyState(next);
      }
      if (!response.ok && data.message) window.alert(data.message);
    } catch (_) {}
  }

  async function renderActions(node, lock) {
    const actions = node.querySelector('[data-lock-actions]');
    if (!actions) return;
    actions.innerHTML = '';
    if (!lock.locked) return;
    const chatId = String(lastPayload?.selected_chat_id || '');
    const users = await loadUsers();
    if (!actions.isConnected || node.querySelector('[data-lock-actions]') !== actions || chatId !== String(lastPayload?.selected_chat_id || '')) return;
    if (lock.owned_by_me) {
      const release = document.createElement('button');
      release.type = 'button'; release.className = 'button compact ghost'; release.textContent = 'Освободить';
      release.addEventListener('click', () => postLock('release'));
      actions.appendChild(release);
      const others = users.filter((u) => Number(u.id) !== currentUserId);
      if (others.length) {
        const select = document.createElement('select'); select.className = 'lock-transfer-select';
        others.forEach((u) => { const option=document.createElement('option'); option.value=String(u.id); option.textContent=u.role === 'admin' ? `${u.display_name} · Администратор` : `${u.display_name} · Сотрудник`; select.appendChild(option); });
        const empty = document.createElement('option');
        empty.value = ''; empty.textContent = 'Выберите сотрудника';
        select.prepend(empty);
        const shiftUser = others.find(u => [u.employee_name, u.display_name, u.username].some(n => activeShift && String(n || '').trim().toLocaleLowerCase() === activeShift));
        const chosen = transferChoices.has(chatId) ? transferChoices.get(chatId) : String(shiftUser?.id || '');
        select.value = Array.from(select.options).some(o => o.value === chosen) ? chosen : '';
        select.addEventListener('change', () => { transferChoices.set(chatId, select.value); transfer.disabled = !select.value; });
        const transfer = document.createElement('button'); transfer.type='button'; transfer.className='button compact'; transfer.textContent='Передать';
        transfer.disabled = !select.value;
        transfer.addEventListener('click', () => { if (select.value) postLock('transfer', Number(select.value)); });
        actions.append(select, transfer);
      }
    } else if (isAdmin && currentUserId) {
      const take = document.createElement('button'); take.type='button'; take.className='button compact'; take.textContent='Передать мне';
      take.addEventListener('click', () => postLock('transfer', currentUserId));
      actions.appendChild(take);
    }
  }

  function applyState(payload) {
    if (!payload || typeof payload !== 'object') return;
    const selected = String(payload.selected_chat_id || '');
    const visibleChat = String(shell.querySelector('[data-chat-id]')?.value || '');
    if (selected && visibleChat && selected !== visibleChat) return;
    lastPayload = payload;
    const lock = payload.conversation_lock || {};
    const node = banner();
    if (!selected || !lock.locked) {
      node.hidden = true;
      bannerSignature = '';
      lockElements(false, '');
      return;
    }
    node.hidden = false;
    const owner = lock.owner || {};
    const ownerName = String(owner.display_name || owner.username || 'другой пользователь');
    const isGroup = selected.endsWith('@g.us');
    const readOnly = !Boolean(lock.owned_by_me);
    lockElements(readOnly, ownerName);
    const signature = JSON.stringify([selected, lock.locked, lock.owned_by_me, owner.id, ownerName]);
    if (signature === bannerSignature) return;
    bannerSignature = signature;
    node.classList.toggle('is-read-only', readOnly);
    node.innerHTML = `<div><strong>${readOnly ? 'Только просмотр' : 'Вы ведёте этот диалог'}</strong><span>${isGroup ? 'Группу' : 'Диалог'} сейчас ведёт <b data-lock-owner></b></span></div><div class="conversation-lock-actions" data-lock-actions></div>`;
    const ownerNode = node.querySelector('[data-lock-owner]');
    if (ownerNode) ownerNode.textContent = ownerName;
    lockElements(readOnly, ownerName);
    renderActions(node, lock);
  }

  async function releaseIfOwned(conversationId, keepalive = false) {
    const chatId = String(conversationId || '');
    const payload = lastPayload;
    const lock = payload?.conversation_lock || {};
    if (!chatId || chatId !== String(payload?.selected_chat_id || '') || !lock.locked || !lock.owned_by_me) return false;
    const body = new URLSearchParams({chat_id: chatId, action: 'release'});
    try {
      const response = await fetch('/conversation-lock', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Requested-With': 'fetch'},
        body: body.toString(),
        keepalive,
      });
      const data = await response.json().catch(() => ({}));
      const visibleChat = String(shell.querySelector('[data-chat-id]')?.value || '');
      if (data.conversation_lock && chatId === String(lastPayload?.selected_chat_id || '') && chatId === visibleChat) {
        applyState({...lastPayload, conversation_lock: data.conversation_lock});
      }
      return Boolean(response.ok && data.updated);
    } catch (_) {
      return false;
    }
  }
  window.QueueConversationLock = Object.freeze({releaseIfOwned});
  window.addEventListener('pagehide', () => { void releaseIfOwned(lastPayload?.selected_chat_id || '', true); });

  // The chat poll dispatches only after its request sequence and selected-chat checks pass.
  // Applying snapshots at the fetch layer lets old overlapping requests undo the current lock UI.
  window.addEventListener('queue-chat-state-accepted', (event) => applyState(event.detail));

  loadUsers();
})();


// 1.00.6.26 fix2: keep native tool containers intact and expose secondary actions through safe proxy buttons.
(() => {
  'use strict';
  const overflowLabels = ['Теги / заметка', 'Выключить автоответчик', 'Включить автоответчик', 'Экспорт', 'Обращения', 'Заявка'];

  function plainLabel(node) {
    return String(node?.textContent || '').replace(/[🔎🔕🔔📎⋯…]/g, '').replace(/\s+/g, ' ').trim();
  }

  function ensureOverflow(actions) {
    let details = actions.querySelector(':scope > details.chat-toolbar-overflow');
    if (details) return details;
    details = document.createElement('details');
    details.className = 'chat-toolbar-overflow';
    const summary = document.createElement('summary');
    summary.className = 'button compact ghost chat-toolbar-more';
    summary.title = 'Ещё действия';
    summary.setAttribute('aria-label', 'Ещё действия');
    summary.textContent = '⋯';
    const menu = document.createElement('div');
    menu.className = 'chat-toolbar-overflow-menu';
    details.append(summary, menu);
    actions.appendChild(details);
    return details;
  }

  function sourceButtons(actions) {
    const result = [];
    actions.querySelectorAll(':scope > .productivity-chat-tools button, :scope > .workflow-chat-tools button, :scope > [data-manual-mode-toggle]').forEach((node) => {
      if (overflowLabels.includes(plainLabel(node))) result.push(node);
    });
    return result;
  }

  function rebuildOverflow(actions) {
    if (!actions || actions.dataset.toolbar623FixBusy === '1') return;
    actions.dataset.toolbar623FixBusy = '1';
    try {
      const details = ensureOverflow(actions);
      const menu = details.querySelector('.chat-toolbar-overflow-menu');
      const sources = sourceButtons(actions);
      const signature = sources.map((node) => `${plainLabel(node)}:${node.disabled ? 1 : 0}`).join('|');
      if (menu.dataset.sourceSignature !== signature) {
        menu.replaceChildren();
        for (const source of sources) {
          source.classList.add('toolbar-overflow-source');
          const proxy = document.createElement('button');
          proxy.type = 'button';
          proxy.className = 'button compact ghost chat-toolbar-overflow-proxy';
          proxy.textContent = String(source.textContent || '').trim();
          proxy.disabled = Boolean(source.disabled);
          proxy.addEventListener('click', () => {
            if (source.disabled) return;
            source.click();
            details.open = false;
          });
          menu.appendChild(proxy);
        }
        menu.dataset.sourceSignature = signature;
      }
      details.hidden = sources.length === 0;
    } finally {
      delete actions.dataset.toolbar623FixBusy;
    }
  }

  function scan(root=document) {
    const actions = [];
    if (root?.matches?.('.chat-header-actions')) actions.push(root);
    root.querySelectorAll?.('.chat-header-actions').forEach((node) => actions.push(node));
    actions.forEach(rebuildOverflow);
  }

  scan();
  let timer = 0;
  new MutationObserver((mutations) => {
    if (!mutations.some((m) => m.addedNodes?.length || m.removedNodes?.length || m.type === 'attributes')) return;
    clearTimeout(timer);
    timer = window.setTimeout(() => scan(), 80);
  }).observe(document.documentElement, {subtree:true, childList:true, attributes:true, attributeFilter:['disabled']});
})();


/* 1.00.6.26: simplify the conversation sidebar filters.
   WhatsApp opens on "Личные"; Groups opens on "Группы".
   "Все" and "Закреплённые" are intentionally removed. */
(() => {
  'use strict';

  const FILTER_LABELS_TO_REMOVE = new Set(['Все', 'Закреплённые', 'Нужен ответ']);

  function normalizeLabel(node) {
    return String(node?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function getConversationShell() {
    return document.querySelector('[data-conversation-page]');
  }

  function desiredDefaultFilter(shell) {
    if (!shell) return '';
    if (shell.hasAttribute('data-whatsapp-page')) return 'Личные';
    const endpoint = String(shell.getAttribute('data-state-endpoint') || '');
    if (endpoint.includes('/api/group-state')) return 'Группы';
    return '';
  }

  function sidebarFilterButtons(shell) {
    const sidebar = shell?.querySelector('.chat-sidebar');
    if (!sidebar) return [];
    return [...sidebar.querySelectorAll('button, a, [role="button"]')].filter((node) => {
      const label = normalizeLabel(node);
      return ['Все', 'Личные', 'Группы', 'Непрочитанные', 'Закреплённые', 'Нужен ответ'].includes(label);
    });
  }

  function looksActive(node) {
    if (!node) return false;
    if (node.getAttribute('aria-pressed') === 'true' || node.getAttribute('aria-selected') === 'true') return true;
    const classes = String(node.className || '');
    return /(^|\s)(active|is-active|selected|is-selected)(\s|$)/i.test(classes);
  }

  function applySidebarDefaultFilter() {
    const shell = getConversationShell();
    if (!shell) return;
    const desired = desiredDefaultFilter(shell);
    if (!desired) return;

    const buttons = sidebarFilterButtons(shell);
    if (!buttons.length) return;

    for (const button of buttons) {
      if (FILTER_LABELS_TO_REMOVE.has(normalizeLabel(button))) button.remove();
    }

    const currentButtons = sidebarFilterButtons(shell);
    const target = currentButtons.find((button) => normalizeLabel(button) === desired);
    if (!target) return;

    // Click only when this exact rendered control has not been initialized yet.
    // Clicking the native control keeps the real filter state in sync with the highlight.
    if (target.dataset.queueDefaultFilterApplied === '1') return;
    target.dataset.queueDefaultFilterApplied = '1';
    if (!looksActive(target)) target.click();
  }

  function scheduleApply() {
    window.clearTimeout(scheduleApply._timer);
    scheduleApply._timer = window.setTimeout(applySidebarDefaultFilter, 30);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scheduleApply, {once: true});
  } else {
    scheduleApply();
  }

  const observer = new MutationObserver(scheduleApply);
  observer.observe(document.documentElement, {subtree: true, childList: true});
})();


/* 1.00.6.26 fix1: visibly highlight the currently selected sidebar category. */
(() => {
  'use strict';

  const KNOWN_FILTERS = new Set(['Личные', 'Группы', 'Непрочитанные']);
  let activeLabel = '';

  function labelOf(node) {
    return String(node?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function shell() {
    return document.querySelector('[data-conversation-page]');
  }

  function defaultLabel(root) {
    if (!root) return '';
    if (root.hasAttribute('data-whatsapp-page')) return 'Личные';
    const endpoint = String(root.getAttribute('data-state-endpoint') || '');
    if (endpoint.includes('/api/group-state')) return 'Группы';
    return '';
  }

  function buttons(root) {
    const sidebar = root?.querySelector('.chat-sidebar');
    if (!sidebar) return [];
    return [...sidebar.querySelectorAll('button, a, [role="button"]')]
      .filter((node) => KNOWN_FILTERS.has(labelOf(node)));
  }

  function paint() {
    const root = shell();
    if (!root) return;
    if (!activeLabel) activeLabel = defaultLabel(root);
    for (const button of buttons(root)) {
      const active = labelOf(button) === activeLabel;
      button.classList.toggle('queue-filter-active', active);
      button.setAttribute('aria-current', active ? 'true' : 'false');
    }
  }

  document.addEventListener('click', (event) => {
    const button = event.target?.closest?.('.chat-sidebar button, .chat-sidebar a, .chat-sidebar [role="button"]');
    if (!button) return;
    const label = labelOf(button);
    if (!KNOWN_FILTERS.has(label)) return;
    activeLabel = label;
    window.requestAnimationFrame(paint);
    window.setTimeout(paint, 60);
  }, true);

  function boot() {
    const root = shell();
    if (!root) return;
    if (!activeLabel) activeLabel = defaultLabel(root);
    paint();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else boot();

  let timer = 0;
  new MutationObserver(() => {
    window.clearTimeout(timer);
    timer = window.setTimeout(paint, 25);
  }).observe(document.documentElement, {subtree:true, childList:true});
})();
/* QUEUE_1_00_6_127_EXACT_UI: keep bookmarks functional, hide selected controls and voice transcription. */
(() => {
  "use strict";
  function clean(root=document) {
    const buttons=[];
    if (root?.matches?.('button, a, [role="button"]')) buttons.push(root);
    root.querySelectorAll?.('button, a, [role="button"]').forEach(node=>buttons.push(node));
    for (const node of buttons) {
      const label=String(node.textContent||'').replace(/\s+/g,' ').trim().replace(/^[^\p{L}\p{N}]+/u,'').trim();
      if (label==='Закладки' || label.startsWith('Авто-голос')) node.remove();
    }
    const transcripts=[];
    if (root?.matches?.('.voice-transcribe, .voice-transcribe-error, details.chat-file-text')) transcripts.push(root);
    root.querySelectorAll?.('.voice-transcribe, .voice-transcribe-error, details.chat-file-text').forEach(node=>transcripts.push(node));
    for (const node of transcripts) {
      if (node.matches('details.chat-file-text') && String(node.querySelector('summary')?.textContent||'').trim()!=='Расшифровка') continue;
      node.remove();
    }
  }
  clean();
  new MutationObserver(mutations=>{
    for (const mutation of mutations) for (const node of mutation.addedNodes||[]) if (node.nodeType===1) clean(node);
  }).observe(document.documentElement,{subtree:true,childList:true});
})();
