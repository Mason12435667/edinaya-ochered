// Колокольчик уведомлений и всплывающие сообщения о новых событиях.
window.QueueNotificationCenter = (() => {
  const notificationCenter = document.querySelector("[data-notification-center]");
  const notificationPopover = document.querySelector("[data-notification-popover]");
  const notificationCount = document.querySelector("[data-notification-count]");
  const notificationItems = document.querySelector("[data-notification-items]");
  const toastStack = document.querySelector("[data-toast-stack]");
  function startNotifications() {
    if (!notificationCenter || !notificationPopover || !notificationCount || !notificationItems) return;

    const loudSoundKey = "queue-loud-notification-sound-v14";
    let loudSoundEnabled = true;
    try { loudSoundEnabled = localStorage.getItem(loudSoundKey) !== "0"; } catch (_) {}
    let noticeAudioContext = null;
    let noticeAudioUnlocked = false;

    function ensureNoticeAudio(unlockOnly = false) {
      if (!loudSoundEnabled) return null;
      try {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) return null;
        if (!noticeAudioContext) noticeAudioContext = new AudioContextClass();
        if (noticeAudioContext.state === "suspended") {
          const resumed = noticeAudioContext.resume();
          if (resumed && typeof resumed.catch === "function") resumed.catch(() => {});
        }
        noticeAudioUnlocked = noticeAudioContext.state === "running" || noticeAudioUnlocked;
        return unlockOnly ? noticeAudioContext : (noticeAudioUnlocked ? noticeAudioContext : null);
      } catch (_) { return null; }
    }

    const unlockNoticeAudio = () => {
      const ctx = ensureNoticeAudio(true);
      if (!ctx) return;
      const mark = () => { noticeAudioUnlocked = ctx.state === "running"; };
      if (ctx.state === "running") mark();
      else if (ctx.resume) Promise.resolve(ctx.resume()).then(mark).catch(() => {});
    };
    document.addEventListener("pointerdown", unlockNoticeAudio, {capture:true, once:true});
    document.addEventListener("keydown", unlockNoticeAudio, {capture:true, once:true});

    function playTriggerNotice(item = {}) {
      if (!loudSoundEnabled) return;
      const ctx = ensureNoticeAudio(false);
      if (!ctx) {
        try { window.QueueUI?.playNotice?.(); } catch (_) {}
        return;
      }
      try {
        const now = ctx.currentTime + 0.015;
        const compressor = ctx.createDynamicsCompressor();
        compressor.threshold.value = -24;
        compressor.knee.value = 10;
        compressor.ratio.value = 12;
        compressor.attack.value = 0.003;
        compressor.release.value = 0.22;
        compressor.connect(ctx.destination);

        const urgent = ["system","sla"].includes(String(item.kind || "").toLowerCase());
        const tones = urgent
          ? [[880,0.00,0.13],[1320,0.16,0.13],[880,0.34,0.13],[1568,0.50,0.20],[1175,0.82,0.15],[1568,1.00,0.22]]
          : [[880,0.00,0.13],[1320,0.16,0.13],[1568,0.34,0.20],[1175,0.62,0.14]];

        tones.forEach(([frequency, offset, duration]) => {
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = "square";
          osc.frequency.setValueAtTime(frequency, now + offset);
          gain.gain.setValueAtTime(0.0001, now + offset);
          gain.gain.exponentialRampToValueAtTime(0.34, now + offset + 0.012);
          gain.gain.setValueAtTime(0.34, now + offset + Math.max(0.025, duration - 0.035));
          gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + duration);
          osc.connect(gain);
          gain.connect(compressor);
          osc.start(now + offset);
          osc.stop(now + offset + duration + 0.02);
        });
      } catch (_) {
        try { window.QueueUI?.playNotice?.(); } catch (_) {}
      }
    }

    const loudSoundButton = document.createElement("button");
    loudSoundButton.type = "button";
    loudSoundButton.className = "notification-native-toggle";
    const syncLoudSoundButton = () => {
      loudSoundButton.textContent = loudSoundEnabled ? "Громкий сигнал: включён" : "Громкий сигнал: выключен";
    };
    loudSoundButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      loudSoundEnabled = !loudSoundEnabled;
      try { localStorage.setItem(loudSoundKey, loudSoundEnabled ? "1" : "0"); } catch (_) {}
      syncLoudSoundButton();
      if (loudSoundEnabled) {
        unlockNoticeAudio();
        window.setTimeout(() => playTriggerNotice({kind:"info"}), 80);
      }
    });
    notificationPopover.append(loudSoundButton);
    syncLoudSoundButton();

    let loading = false;
    const seenKey = "queue-toast-seen-v331";
    const initKey = "queue-toast-initialized-v331";
    let seen = new Set();
    let initialized = false;
    try {
      seen = new Set(JSON.parse(localStorage.getItem(seenKey) || "[]"));
      initialized = localStorage.getItem(initKey) === "1";
    } catch (_) {}

    let noticeDatabase;
    function claimNotice(id) {
      if (!window.indexedDB) return Promise.resolve(true);
      noticeDatabase ||= new Promise((resolve, reject) => {
        const request=indexedDB.open("queue-notifications",1);
        request.onupgradeneeded=()=>request.result.createObjectStore("shown");
        request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
      });
      return noticeDatabase.then(db=>new Promise(resolve=>{
        const transaction=db.transaction("shown","readwrite");
        const request=transaction.objectStore("shown").add(Date.now(),id);
        request.onerror=event=>{event.preventDefault();event.stopPropagation();};
        let added=false;request.onsuccess=()=>{added=true;};
        transaction.oncomplete=()=>resolve(added);transaction.onabort=()=>resolve(false);
      })).catch(()=>false);
    }

    function saveSeen() {
      try { localStorage.setItem(seenKey, JSON.stringify([...seen].slice(-120))); } catch (_) {}
    }

    function absoluteAssetUrl(value, fallback = "") {
      const raw = String(value || fallback || "").trim();
      if (!raw) return "";
      try { return new URL(raw, window.location.origin).href; } catch (_) { return ""; }
    }

    function showNativeNotice(item) {
      if (!("Notification" in window) || Notification.permission !== "granted") return;
      if (!document.hidden && document.hasFocus()) return;
      const title = String(item.title || "Единая очередь");
      const body = [String(item.source || "").trim(), String(item.detail || "").trim()].filter(Boolean).join("\n");
      const icon = absoluteAssetUrl(item.avatar_url, "/static/favicon.png?v=3.3.52");
      const image = absoluteAssetUrl(item.media_url);
      try {
        const notification = new Notification(title, {
          body,
          icon,
          badge: absoluteAssetUrl("/static/favicon.png?v=3.3.52"),
          image: image || undefined,
          tag: `queue-${String(item.id || Date.now())}`,
          renotify: true,
          silent: false,
        });
        notification.onclick = () => {
          try { window.focus(); } catch (_) {}
          const href = String(item.href || "#");
          if (href && href !== "#") window.location.href = href;
          notification.close();
        };
        window.setTimeout(() => notification.close(), item.kind === "system" ? 16000 : 12000);
      } catch (_) {}
    }

    function showToast(item) {
      if (!toastStack || !item || !item.id) return;
      const kind = String(item.kind || "info");
      const iconMap = {
        ticket: "🎫",
        tickets: "🎫",
        contact: "💬",
        contacts: "💬",
        group: "@",
        groups: "@",
        support: "🛟",
        system: "⚠",
        sla: "⏱",
        info: "🔔",
      };
      const ttl = kind === "system" || kind === "sla" ? 13000 : 9500;
      const card = document.createElement("div");
      card.className = `toast-card toast-${kind}`;
      const link = document.createElement("a");
      link.className = "toast-link";
      link.href = String(item.href || "#");
      const icon = document.createElement("span");
      icon.className = "toast-icon";
      const avatarUrl = String(item.avatar_url || "").trim();
      if (avatarUrl) {
        const avatar = document.createElement("img");
        avatar.src = avatarUrl;
        avatar.alt = "";
        avatar.loading = "eager";
        avatar.addEventListener("error", () => { avatar.remove(); icon.textContent = iconMap[kind] || iconMap.info; });
        icon.append(avatar);
      } else {
        icon.textContent = iconMap[kind] || iconMap.info;
      }
      const content = document.createElement("div");
      content.className = "toast-content";
      const meta = document.createElement("div");
      meta.className = "toast-meta";
      const source = document.createElement("small");
      source.textContent = String(item.source || "Единая очередь");
      const badge = document.createElement("b");
      badge.className = "toast-kind-badge";
      badge.textContent = kind === "group" || kind === "groups" ? "Упоминание" : kind === "contact" || kind === "contacts" ? "Сообщение" : kind === "ticket" || kind === "tickets" ? "Заявка" : kind === "support" ? "Поддержка" : kind === "sla" ? "SLA" : kind === "system" ? "Система" : "Событие";
      meta.append(source, badge);
      const title = document.createElement("strong");
      title.textContent = String(item.title || "Новое событие");
      const detail = document.createElement("span");
      detail.textContent = String(item.detail || "");
      content.append(meta, title, detail);
      const mediaUrl = String(item.media_url || "").trim();
      if (mediaUrl) {
        const thumb = document.createElement("img");
        thumb.className = "toast-media-preview";
        thumb.src = mediaUrl;
        thumb.alt = "Фото из сообщения";
        thumb.loading = "eager";
        thumb.addEventListener("error", () => thumb.remove());
        content.append(thumb);
      }
      link.append(icon, content);
      const close = document.createElement("button");
      close.type = "button";
      close.className = "toast-close";
      close.setAttribute("aria-label", "Закрыть уведомление");
      close.textContent = "×";
      const progress = document.createElement("div");
      progress.className = "toast-progress";
      progress.style.animationDuration = `${ttl}ms`;
      const remove = () => { card.classList.add("toast-leave"); window.setTimeout(() => card.remove(), 200); };
      close.addEventListener("click", remove);
      card.append(link, close, progress);
      toastStack.prepend(card);
      playTriggerNotice(item);
      showNativeNotice(item);
      while (toastStack.children.length > 4) toastStack.lastElementChild.remove();
      window.setTimeout(remove, ttl);
    }


    const nativeNoticeButton = document.createElement("button");
    nativeNoticeButton.type = "button";
    nativeNoticeButton.className = "notification-native-toggle";
    function syncNativeNoticeButton() {
      if (!("Notification" in window)) {
        nativeNoticeButton.textContent = "Уведомления браузера недоступны";
        nativeNoticeButton.disabled = true;
        return;
      }
      nativeNoticeButton.disabled = Notification.permission === "denied";
      nativeNoticeButton.textContent = Notification.permission === "granted"
        ? "Уведомления браузера: включены"
        : Notification.permission === "denied"
          ? "Уведомления браузера заблокированы"
          : "Включить уведомления браузера";
    }
    nativeNoticeButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!("Notification" in window) || Notification.permission === "denied") return;
      try {
        const permission = await Notification.requestPermission();
        if (permission === "granted") {
          unlockNoticeAudio();
          playTriggerNotice({kind:"info"});
          const test = new Notification("Единая очередь", {
            body: "Уведомления браузера включены. Новые события будут видны даже в другой вкладке.",
            icon: absoluteAssetUrl("/static/favicon.png?v=3.3.52"),
            badge: absoluteAssetUrl("/static/favicon.png?v=3.3.52"),
            tag: "queue-notification-test",
            renotify: true,
            silent: false,
          });
          window.setTimeout(() => test.close(), 6000);
        }
      } catch (_) {}
      syncNativeNoticeButton();
    });
    notificationPopover.append(nativeNoticeButton);
    syncNativeNoticeButton();

    notificationCenter.addEventListener("click", () => {
      notificationPopover.hidden = !notificationPopover.hidden;
    });
    document.addEventListener("click", (event) => {
      if (!notificationPopover.contains(event.target) && !notificationCenter.contains(event.target)) {
        notificationPopover.hidden = true;
      }
    });

    let queueSiteFailureCount = 0;
    let queueSiteFailureNotified = false;
    async function refresh() {
      if (loading) return;
      loading = true;
      try {
        const response = await fetch("/api/notifications", {cache: "no-store"});
        if (!response.ok) throw new Error("Нет связи");
        const state = await response.json();
        queueSiteFailureCount = 0;
        queueSiteFailureNotified = false;
        const counts = state.counts || {};
        const total = Number(state.total || 0);
        notificationCount.textContent = String(total);
        notificationCount.hidden = total <= 0;
        notificationCenter.classList.toggle("has-unread", total > 0);
        notificationItems.replaceChildren();
        const groupChatId = String(state.group_chat_id || "");
        const groupHref = groupChatId ? `/groups?chat_id=${encodeURIComponent(groupChatId)}` : "/groups";
        const contactChatId = String(state.contact_chat_id || "");
        const contactHref = contactChatId ? `/whatsapp?chat_id=${encodeURIComponent(contactChatId)}` : "/whatsapp";
        const rows = [
          ["Новые заявки", Number(counts.tickets || 0), "/"],
          ["Новые сообщения WhatsApp", Number(counts.contacts || 0), contactHref],
          ["@ Упоминания в группах", Number(counts.groups || 0), groupHref],
          ["Вопросы в поддержку", Number(counts.support || 0), "/?category=support&status=new"],
          ["Напоминания", Number(counts.reminders || 0), "/reminders"],
          ["Системные предупреждения", Number(counts.system || 0), "/admin/system"],
        ];
        rows.forEach(([label, amount, href]) => {
          const link = document.createElement("a");
          link.href = href;
          link.className = "notification-item";
          link.textContent = `${label}: ${amount}`;
          notificationItems.append(link);
        });

        // Show active system/SLA problems with explicit actions.
        // "Проверить" re-runs live diagnostics, "Открыть" takes the employee to
        // the relevant screen, and "Убрать" dismisses the current occurrence.
        const allNotificationEvents = Array.isArray(state.events) ? state.events : [];
        const systemEvents = [];
        const systemEventIds = new Set();
        for (const item of allNotificationEvents) {
          if (!item) continue;
          const kind = String(item.kind || "").toLowerCase();
          const eventId = String(item.id || item.event_id || "").trim();
          const isSystem = kind === "system" || kind === "sla" || eventId.startsWith("system:") || eventId.startsWith("reliability:") || eventId.startsWith("sla:");
          if (!isSystem) continue;
          const dedupeKey = eventId || `${kind}:${String(item.title || "")}:${String(item.detail || "")}`;
          if (systemEventIds.has(dedupeKey)) continue;
          systemEventIds.add(dedupeKey);
          systemEvents.push(item);
        }
        const postWarningAction = async (action, eventId = "") => {
          const response = await fetch("/api/system-warning-action", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({action, event_id:eventId}),
          });
          let payload = {};
          try { payload = await response.json(); } catch (_) {}
          if (!response.ok || payload.error) throw new Error(String(payload.error || "Не удалось выполнить действие"));
          return payload;
        };
        if (systemEvents.length) {
          const details = document.createElement("section");
          details.className = "notification-system-events";
          details.setAttribute("aria-label", "Активные системные предупреждения");
          const headingRow = document.createElement("div");
          headingRow.className = "notification-system-events-heading";
          const heading = document.createElement("div");
          heading.className = "notification-system-events-title";
          heading.textContent = "Что требует внимания";
          const headingActions = document.createElement("div");
          headingActions.className = "notification-system-heading-actions";
          const checkAll = document.createElement("button");
          checkAll.type = "button";
          checkAll.className = "notification-system-mini-action";
          checkAll.textContent = "Проверить все";
          checkAll.addEventListener("click", async (event) => {
            event.preventDefault(); event.stopPropagation();
            checkAll.disabled = true; checkAll.textContent = "Проверяю…";
            try { await postWarningAction("check_all"); await refresh(); }
            catch (error) { window.QueueUI?.toast?.(error.message || "Не удалось проверить предупреждения", "error"); }
            finally { checkAll.disabled = false; checkAll.textContent = "Проверить все"; }
          });
          const dismissAll = document.createElement("button");
          dismissAll.type = "button";
          dismissAll.className = "notification-system-mini-action danger";
          dismissAll.textContent = "Убрать все";
          dismissAll.addEventListener("click", async (event) => {
            event.preventDefault(); event.stopPropagation();
            dismissAll.disabled = true;
            try { await postWarningAction("dismiss_all"); await refresh(); }
            catch (error) { window.QueueUI?.toast?.(error.message || "Не удалось убрать предупреждения", "error"); }
            finally { dismissAll.disabled = false; }
          });
          headingActions.append(checkAll, dismissAll);
          headingRow.append(heading, headingActions);
          details.append(headingRow);
          systemEvents.slice(0, 8).forEach((item) => {
            const kind = String(item.kind || "").toLowerCase();
            const eventId = String(item.id || item.event_id || "").trim();
            const level = String(item.level || "warning").toLowerCase();
            const card = document.createElement("div");
            card.className = `notification-system-event ${level === "critical" ? "critical" : "warning"}`;
            let href = String(item.href || "").trim();
            if (!href && eventId.startsWith("sla:")) {
              const ticketId = eventId.split(":")[1] || "";
              href = /^\d+$/.test(ticketId) ? `/ticket?id=${encodeURIComponent(ticketId)}` : "/admin/system";
            }
            href = href || "/admin/system";
            const icon = document.createElement("span");
            icon.className = "notification-system-event-icon";
            icon.textContent = level === "critical" ? "●" : kind === "sla" ? "◷" : "!";
            const body = document.createElement("span");
            body.className = "notification-system-event-body";
            const title = document.createElement("strong");
            title.textContent = String(item.title || "Системное предупреждение");
            const detail = document.createElement("span");
            detail.textContent = String(item.detail || "Откройте раздел для подробностей");
            body.append(title, detail);
            const meta = document.createElement("small");
            const ts = Number(item.timestamp || 0);
            const timeText = ts > 0 ? new Date(ts * 1000).toLocaleString("ru-RU", {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"}) : "";
            meta.textContent = [kind === "sla" ? "SLA" : "Система", timeText].filter(Boolean).join(" · ");
            body.append(meta);
            const actions = document.createElement("div");
            actions.className = "notification-system-event-actions";
            const open = document.createElement("a");
            open.className = "notification-system-action";
            open.href = href;
            open.textContent = kind === "sla" ? "К заявке" : "Открыть";
            const check = document.createElement("button");
            check.type = "button";
            check.className = "notification-system-action";
            check.textContent = "Проверить";
            check.addEventListener("click", async (event) => {
              event.preventDefault(); event.stopPropagation();
              check.disabled = true; check.textContent = "Проверяю…";
              try { await postWarningAction("check", eventId); await refresh(); }
              catch (error) { window.QueueUI?.toast?.(error.message || "Не удалось проверить предупреждение", "error"); }
              finally { check.disabled = false; check.textContent = "Проверить"; }
            });
            const dismiss = document.createElement("button");
            dismiss.type = "button";
            dismiss.className = "notification-system-action danger";
            dismiss.textContent = "Убрать";
            dismiss.title = "Скрыть текущее предупреждение. Если проблема исчезнет и возникнет снова, уведомление появится заново.";
            dismiss.addEventListener("click", async (event) => {
              event.preventDefault(); event.stopPropagation();
              dismiss.disabled = true;
              try { await postWarningAction("dismiss", eventId); await refresh(); }
              catch (error) { window.QueueUI?.toast?.(error.message || "Не удалось убрать предупреждение", "error"); dismiss.disabled = false; }
            });
            actions.append(open, check, dismiss);
            card.append(icon, body, actions);
            details.append(card);
          });
          const systemCount = Number(counts.system || 0);
          if (systemCount > systemEvents.slice(0, 8).length) {
            const more = document.createElement("a");
            more.className = "notification-system-more";
            more.href = "/admin/system";
            more.textContent = `Ещё ${systemCount - systemEvents.slice(0, 8).length} предупрежд.`;
            details.append(more);
          }
          notificationItems.append(details);
        }

        const notifyOnce = async () => {
          try { seen = new Set(JSON.parse(localStorage.getItem(seenKey) || "[]")); initialized = localStorage.getItem(initKey) === "1"; } catch (_) {}
        const events = Array.isArray(state.events) ? state.events : [];
        const currentIds = new Set(events.map((item) => String(item.id || item.event_id || "")).filter(Boolean));
        if (!currentIds.has("system:whatsapp-offline")) seen.delete("system:whatsapp-offline");
        if (!initialized) {
          for (const item of events) { const id = String(item && (item.id || item.event_id) || ""); if (id) { seen.add(id); await claimNotice(id); } }
          initialized = true;
          try { localStorage.setItem(initKey, "1"); } catch (_) {}
          saveSeen();
        } else {
          for (const item of [...events].reverse()) {
            const id = String(item && (item.id || item.event_id) || "");
            if (!id || seen.has(id)) continue;
            seen.add(id);
            if (await claimNotice(id)) showToast(item);
          }
          saveSeen();
        }
        };
        if (navigator.locks) await navigator.locks.request("queue-notifications-356", notifyOnce);
        else {
          // Older browsers use one elected tab; system Notification tag also replaces duplicates.
          const leaseKey = "queue-notice-lease";
          const tabId = window.queueNoticeTabId ||= Math.random().toString(36).slice(2);
          let lease = {}; try { lease = JSON.parse(localStorage.getItem(leaseKey) || "{}"); } catch (_) {}
          if (!lease.until || lease.until < Date.now() || lease.id === tabId) {
            localStorage.setItem(leaseKey, JSON.stringify({id:tabId, until:Date.now()+6000})); await notifyOnce();
          }
        }
      } catch (error) {
        notificationItems.textContent = "Не удалось обновить уведомления";
        queueSiteFailureCount += 1;
        if (queueSiteFailureCount >= 2 && !queueSiteFailureNotified) {
          queueSiteFailureNotified = true;
          showToast({id:"system:site-unavailable",kind:"system",title:"Связь с Единой очередью потеряна",detail:"Сайт или сервер временно недоступен. Проверяется автоматически.",source:"Система",href:"/admin/system"});
        }
      } finally {
        loading = false;
      }
    }
    refresh();
    document.addEventListener("visibilitychange", () => { void refresh(); });
    window.addEventListener("focus", () => { void refresh(); });
    window.addEventListener("queue-realtime", () => { void refresh(); });
    window.setInterval(() => { void refresh(); }, 15000);
  }

  return {start: startNotifications};
})();
