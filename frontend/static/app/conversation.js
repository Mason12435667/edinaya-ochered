// Страница чата и группы: список диалогов, переписка, отправка, действия с сообщениями.
window.QueueConversation = (() => {
  const {queueAlert, queueConfirm, queuePrompt, postForm} = window.QueueCommon;
  const menu = document.getElementById("ticket-context-menu");
  const conversationPage = document.querySelector("[data-conversation-page]");
  function formatChatTime(timestamp) {
    if (!timestamp) return "";
    return new Intl.DateTimeFormat("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(Number(timestamp) * 1000));
  }

  function chatFallbackName() {
    return "Пользователь WhatsApp";
  }

  function cleanWhatsAppName(value) {
    const name = String(value || "").trim();
    if (!name) return "";
    if (["система", "system", "рабочий whatsapp"].includes(name.toLocaleLowerCase("ru"))) return "";
    if (/^[+\d\s().-]+$/.test(name)) return "";
    return name;
  }

  function avatarLetters(name, chatId) {
    const words = String(name || chatFallbackName(chatId))
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    return (words.slice(0, 2).map((word) => Array.from(word)[0]).join("") || "W").toLocaleUpperCase("ru");
  }

  function startConversationPage() {
    if (!conversationPage) return;
    // Части страницы из соседних файлов получают общее через page: элементы, функции и
    // состояние, которое меняют несколько частей (page.selectedChatId и другие).
    const page = {
      avatarLetters, conversationPage, formatChatTime, loadChatState, placeholder, queueAlert,
      queueMessageAction, selectedChatName, startReply, toggleForward,
    };
    Object.assign(
      page, window.QueueChatHeaderBot(page), window.QueueChatMessageMenu(page), window.QueueChatMessages(page),
      window.QueueChatComposer(page),
    );
    const chatList = conversationPage.querySelector("[data-chat-list]");
    const chatSearch = conversationPage.querySelector("[data-chat-search]");
    const chatMessages = page.chatMessages = conversationPage.querySelector("[data-chat-messages]");
    const chatTitle = conversationPage.querySelector("[data-chat-title]");
    const contactPresence = conversationPage.querySelector("[data-contact-presence]");
    const chatIdInput = conversationPage.querySelector("[data-chat-id]");
    const composer = page.composer = conversationPage.querySelector("[data-chat-composer]");
    const refreshButton = conversationPage.querySelector("[data-chat-refresh]");
    const connection = document.getElementById("whatsapp-connection");
    page.manualModeButton = conversationPage.querySelector("[data-manual-mode-toggle]");
    let manualModeState = conversationPage.querySelector("[data-manual-mode-state]");
    page.botResetButton = conversationPage.querySelector("[data-bot-reset]");
    const profileButton = conversationPage.querySelector("[data-profile-toggle]");
    // This control belongs only to personal WhatsApp dialogs.
    // Never attach it to the Groups sidebar / "Обновить" button.
    const botRestartPersonalPage = location.pathname === "/whatsapp";
    const botRestartHeaderActions = page.botRestartHeaderActions = conversationPage.querySelector(".chat-header-actions");
    if (!page.manualModeButton && botRestartPersonalPage) {
      page.manualModeButton = document.createElement("button");
      page.manualModeButton.type = "button";
      page.manualModeButton.className = "button compact secondary";
      page.manualModeButton.dataset.manualModeToggle = "1";
      if (profileButton && profileButton.parentElement) {
        profileButton.insertAdjacentElement("afterend", page.manualModeButton);
      } else if (botRestartHeaderActions) {
        botRestartHeaderActions.append(page.manualModeButton);
      }
    }
    if (!page.botResetButton && botRestartPersonalPage) {
      page.botResetButton = document.createElement("button");
      page.botResetButton.type = "button";
      page.botResetButton.className = "button compact secondary toolbar-overflow-source";
      page.botResetButton.dataset.botReset = "1";
      page.botResetButton.textContent = "↻ Перезагрузить бота";
    }
    if (!manualModeState && botRestartPersonalPage) {
      manualModeState = document.createElement("small");
      manualModeState.dataset.manualModeState = "1";
      manualModeState.className = "manual-mode-state";
      const chatHeader = conversationPage.querySelector(".chat-header");
      const titleBlock = chatTitle?.parentElement;
      if (titleBlock && chatHeader && chatHeader.contains(titleBlock)) {
        titleBlock.append(manualModeState);
      } else if (page.manualModeButton?.parentElement) {
        page.manualModeButton.parentElement.insertAdjacentElement("afterend", manualModeState);
      }
    }

    // CSP-safe version:
    // - never writes element.style (strict style-src blocks inline styles);
    // - keeps the real v15 controls hidden in their original DOM location;
    // - creates menu proxy buttons which call the REAL control's .click();
    // - removes duplicate old auto-reply rows from the existing "..." menu.
    page.botManualContactV17 = false;
    if (botRestartPersonalPage) {
      const titleBlock = chatTitle?.parentElement;
      if (manualModeState && titleBlock && manualModeState.parentElement !== titleBlock) titleBlock.append(manualModeState);
      page.parkHeaderBotControlsV17();

      document.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("button, summary, a, [role='button']") : null;
        if (!target) return;
        const label = page.normalizeActionLabelV17(target.textContent || target.getAttribute("aria-label") || target.title);
        if (label === "..." || label === "…" || label.includes("ещё") || label.includes("дополн")) {
          page.scheduleOverflowSyncV17();
        }
      }, true);

      if (!document.documentElement.dataset.botOverflowObserverV19) {
        document.documentElement.dataset.botOverflowObserverV19 = "1";
        let observerTimerV19 = 0;
        const observerV19 = new MutationObserver(() => {
          window.clearTimeout(observerTimerV19);
          observerTimerV19 = window.setTimeout(() => {
            const panel = page.findExistingOverflowPanelV17();
            if (panel) page.syncOverflowBotActionsV17();
          }, 40);
        });
        observerV19.observe(document.body, {
          childList: true,
          subtree: true,
          attributes: true,
          attributeFilter: ["hidden", "class", "aria-expanded"]
        });
      }
    }
    const profileSummary = conversationPage.querySelector("[data-contact-profile-summary]");
    const participantsList = conversationPage.querySelector("[data-group-participants]");
    const participantsCount = conversationPage.querySelector("[data-participants-count]");
    const groupMuteButton = conversationPage.querySelector("[data-group-mute-toggle]");
    const mentionsInput = conversationPage.querySelector("[data-mentions-input]");
    const replyInput = page.replyInput = conversationPage.querySelector("[data-reply-to]");
    const replyPreview = conversationPage.querySelector("[data-reply-preview]");
    const replySender = conversationPage.querySelector("[data-reply-sender]");
    const replyBody = conversationPage.querySelector("[data-reply-body]");
    const replyCancel = conversationPage.querySelector("[data-reply-cancel]");
    const mediaInput = page.mediaInput = conversationPage.querySelector("[data-media-input]");
    const mediaName = page.mediaName = conversationPage.querySelector("[data-media-name]");
    const emojiToggle = conversationPage.querySelector("[data-emoji-toggle]");
    const emojiPicker = conversationPage.querySelector("[data-emoji-picker]");
    let forwardToolbar = conversationPage.querySelector("[data-forward-toolbar]");
    if (!forwardToolbar) forwardToolbar = document.createElement("div");
    forwardToolbar.dataset.forwardToolbar = "1";
    forwardToolbar.classList.add("chat-forward-toolbar-repair");
    // Rebuild the selection toolbar instead of reusing possibly stale or
    // template-hidden controls. Keep its location directly between messages
    // and the composer so the send controls cannot be clipped by the scroller.
    forwardToolbar.replaceChildren();
    const forwardCount = document.createElement("span");
    forwardCount.dataset.forwardCount = "1";
    const forwardTarget = document.createElement("select");
    forwardTarget.dataset.forwardTarget = "1";
    forwardTarget.setAttribute("aria-label", "Чат для пересылки");
    const forwardCancel = document.createElement("button");
    forwardCancel.type = "button";
    forwardCancel.dataset.forwardCancel = "1";
    forwardCancel.className = "button compact ghost";
    forwardCancel.textContent = "Отмена";
    const forwardSend = document.createElement("button");
    forwardSend.type = "button";
    forwardSend.dataset.forwardSend = "1";
    forwardSend.className = "button compact primary";
    forwardSend.textContent = "Переслать";
    forwardToolbar.append(forwardCount, forwardTarget, forwardCancel, forwardSend);
    if (chatMessages?.parentElement) chatMessages.insertAdjacentElement("afterend", forwardToolbar);
    else if (composer?.parentElement) composer.parentElement.insertBefore(forwardToolbar, composer);
    else conversationPage.append(forwardToolbar);
    const stateEndpoint = conversationPage.dataset.stateEndpoint || "/api/chat-state";
    const sendEndpoint = conversationPage.dataset.sendEndpoint || "/chat-send";
    const mediaSendEndpoint = conversationPage.dataset.mediaSendEndpoint || "/chat-media-send";
    const forwardEndpoint = conversationPage.dataset.forwardEndpoint || "/chat-forward";
    const emptyLabel = conversationPage.dataset.listEmpty || "Личные чаты пока не загружены";
    const selectLabel = conversationPage.dataset.selectTitle || "Выберите пользователя";
    page.selectedChatId = conversationPage.dataset.initialChatId || "";
    let chats = [];
    let lastChatsSignature = "";
    let stateSequence = 0;
    let forwardTargets = [];
    let forwardOptionsSignature = "";
    let forwardBusy = false;
    page.participants = [];
    let loading = false;
    page.lastMessageSignature = "";
    page.manualModeActive = false;
    let groupMuted = false;
    const PROFILE_OPEN_STORAGE_KEY = "queue-whatsapp-open-profile";
    let profileOpenChatId = "";
    try { profileOpenChatId = sessionStorage.getItem(PROFILE_OPEN_STORAGE_KEY) || ""; } catch (_) {}
    let profileOpen = Boolean(page.selectedChatId && profileOpenChatId === page.selectedChatId);
    let currentProfile = {};
    const selectedMentionIds = new Set();
    const selectedForForward = page.selectedForForward = new Set();
    const expandedLongMessages = page.expandedLongMessages = new Set();
    let replyToMessage = null;
    page.historyCursor = "";
    page.historyHasMore = false;
    page.olderMessages = [];
    page.latestMessages = [];
    const reactionValues = page.reactionValues = ["👍","❤️","😂","😮","😢","🙏","🔥","🎉","✅","👏","🤔","👀","😡","💯","👌","😁","😍","🥳"];
    const mentionPicker = document.createElement("div");
    mentionPicker.className = "mention-picker";
    mentionPicker.hidden = true;
    if (composer) composer.append(mentionPicker);

    function placeholder(container, text) {
      if (!container) return;
      container.replaceChildren();
      const element = document.createElement("p");
      element.className = "chat-placeholder";
      element.textContent = text;
      container.append(element);
    }

    function selectedChatName() {
      const current = chats.find((chat) => chat.id === page.selectedChatId);
      return current ? (cleanWhatsAppName(current.name) || chatFallbackName(page.selectedChatId)) : chatFallbackName(page.selectedChatId);
    }

    window.QueueClampFloatingPanel = page.clampFloatingPanel;

    function selectChat(chatId) {
      if (composer.dataset.sending === "1") return;
      try { sessionStorage.removeItem(`queue-manual-unread:${chatId}`); } catch (_) {}
      page.saveDraft();
      const changed = page.selectedChatId !== chatId;
      if (changed && page.selectedChatId) void window.QueueConversationLock?.releaseIfOwned?.(page.selectedChatId);
      page.selectedChatId = chatId;
      if (changed) {
        profileOpen = false;
        profileOpenChatId = "";
        try { sessionStorage.removeItem(PROFILE_OPEN_STORAGE_KEY); } catch (_) {}
        currentProfile = {};
        if (profileSummary) { profileSummary.hidden = true; profileSummary.replaceChildren(); }
        selectedMentionIds.clear();
        selectedForForward.clear();
        replyToMessage = null;
        if (replyInput) replyInput.value = "";
        if (replyPreview) replyPreview.hidden = true;
        if (mentionsInput) mentionsInput.value = "";
        page.historyCursor = "";
        page.historyHasMore = false;
        page.olderMessages = [];
        page.latestMessages = [];
        page.clearAttachment();
        page.restoreDraft();
      }
      page.lastMessageSignature = "";
      updateForwardToolbar();
      chatIdInput.value = chatId;
      chatTitle.textContent = selectedChatName();
      const url = new URL(window.location.href);
      url.searchParams.set("chat_id", chatId);
      url.searchParams.delete("message_id");
      delete conversationPage.dataset.jumpLoaded;
      delete conversationPage.dataset.jumpScrolled;
      window.history.replaceState({}, "", url);
      renderChats();
      if (changed) placeholder(chatMessages, "Подгружаю последние сообщения...");
      loadChatState(true);
    }

    const supportsReplyQueue = false; // obsolete reply queue removed
    let needsReplyOnly = supportsReplyQueue && sessionStorage.getItem("queue-needs-reply:" + location.pathname) === "1";
    const needsReplyButton = document.createElement("button");
    needsReplyButton.type = "button"; needsReplyButton.className = "button compact ghost reply-queue-toggle";
    if (supportsReplyQueue) {
      chatSearch.after(needsReplyButton);
      chatSearch.closest(".chat-sidebar")?.classList.add("has-reply-queue");
    }
    needsReplyButton.addEventListener("click", () => {
      if (!supportsReplyQueue) return;
      needsReplyOnly = !needsReplyOnly;
      sessionStorage.setItem("queue-needs-reply:" + location.pathname, needsReplyOnly ? "1" : "0");
      lastChatsSignature = ""; renderChats();
    });
    function replyWaitTimestamp(chat) {
      if (!chat || String(chat.id || "").endsWith("@g.us") || !chat.needs_reply) return 0;
      const value = Number(chat.waiting_since || 0);
      return Number.isFinite(value) && value > 0 ? value : 0;
    }
    // Bookmarks open through /bookmarks (?bookmarks=1); the sidebar has no button for them.
    page.refreshMessageBookmarks(true).catch(() => {});
    if (new URLSearchParams(location.search).get('bookmarks') === '1') {
      page.showMessageBookmarks();
    }

    // QUEUE_FAVORITES_3_3_79
    async function toggleChatFavorite(chat, nextFavorite) {
      if (!chat || !chat.id) return;
      const body = new URLSearchParams({chat_id: String(chat.id), favorite: nextFavorite ? "1" : "0"});
      const response = await fetch("/chat-favorite", {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body,
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.updated) throw new Error("Не удалось изменить избранное");
      chat.favorite = Boolean(result.favorite);
      lastChatsSignature = "";
      renderChats();
      window.QueueUI?.toast?.(chat.favorite ? "Чат добавлен в избранное" : "Чат убран из избранного");
    }

    function renderChats() {
      const waitingChats = chats.filter(c => replyWaitTimestamp(c) > 0);
      needsReplyButton.textContent = `Нужен ответ (${waitingChats.length})`;
      needsReplyButton.setAttribute("aria-pressed", String(needsReplyOnly));
      const query = String(chatSearch.value || "").trim().toLocaleLowerCase("ru");
      let filtered = chats.filter((chat) => {
        const haystack = `${chat.name || ""} ${chat.id || ""} ${chat.last_message || ""}`.toLocaleLowerCase("ru");
        return (!needsReplyOnly || chat.needs_reply) && (!query || haystack.includes(query));
      });
      // Starred chats stay at the top, recent chats follow in timestamp order.
      filtered.sort((left, right) => Number(Boolean(right.favorite)) - Number(Boolean(left.favorite))
        || Number(right.timestamp || 0) - Number(left.timestamp || 0));
      const chatsSignature = page.selectedChatId + query + needsReplyOnly + JSON.stringify(filtered);
      if (lastChatsSignature === chatsSignature) return;
      lastChatsSignature = chatsSignature;
      chatList.replaceChildren();
      if (!filtered.length) {
        placeholder(chatList, query ? "Ничего не найдено" : emptyLabel);
        return;
      }
      filtered.forEach((chat) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `chat-list-item${chat.id === page.selectedChatId ? " active" : ""}`;
        button.addEventListener("click", () => selectChat(chat.id));

        const avatar = document.createElement("span");
        avatar.className = `chat-avatar${String(chat.id || "").endsWith("@g.us") ? " group" : ""}`;
        avatar.textContent = avatarLetters(chat.name, chat.id);
        if (chat.avatar_url) {
          const img = document.createElement("img");
          img.src = chat.avatar_url; img.alt = ""; img.loading = "lazy";
          img.addEventListener("error", () => img.remove());
          avatar.append(img);
        }

        const content = document.createElement("span");
        content.className = "chat-list-content";
        const top = document.createElement("span");
        top.className = "chat-list-top";
        const name = document.createElement("strong");
        name.textContent = cleanWhatsAppName(chat.name) || chatFallbackName(chat.id);
        const favoriteToggle = document.createElement("span");
        favoriteToggle.className = `chat-favorite-toggle${chat.favorite ? " active" : ""}`;
        favoriteToggle.textContent = chat.favorite ? "★" : "☆";
        favoriteToggle.setAttribute("role", "button");
        favoriteToggle.tabIndex = 0;
        favoriteToggle.title = chat.favorite ? "Убрать из избранного" : "Добавить в избранное";
        favoriteToggle.setAttribute("aria-label", favoriteToggle.title);
        const changeFavorite = async (event) => {
          event.preventDefault();
          event.stopPropagation();
          if (favoriteToggle.dataset.busy === "1") return;
          favoriteToggle.dataset.busy = "1";
          try { await toggleChatFavorite(chat, !Boolean(chat.favorite)); }
          catch (error) { window.QueueUI?.toast?.(error?.message || "Не удалось изменить избранное"); }
          finally { favoriteToggle.dataset.busy = "0"; }
        };
        favoriteToggle.addEventListener("pointerdown", (event) => event.stopPropagation());
        favoriteToggle.addEventListener("click", changeFavorite);
        favoriteToggle.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") changeFavorite(event);
        });
        if (String(chat.id || "").endsWith("@g.us") && chat.muted) {
          const mutedMark = document.createElement("span");
          mutedMark.className = "group-muted-mark";
          mutedMark.textContent = " 🔕";
          mutedMark.title = "Уведомления этой группы заглушены";
          name.append(mutedMark);
        }
        const presence = chat && chat.presence && typeof chat.presence === "object" ? chat.presence : {};
        if (!String(chat.id || "").endsWith("@g.us") && presence.known) {
          const dot = document.createElement("i");
          dot.className = `presence-dot ${presence.online ? "online" : "offline"}`;
          dot.title = presence.online ? "Онлайн" : "Не в сети";
          name.append(" ", dot);
        }
        const time = document.createElement("time");
        time.textContent = formatChatTime(chat.timestamp);
        top.append(name, favoriteToggle, time);
        const bottom = document.createElement("span");
        bottom.className = "chat-list-bottom";
        const preview = document.createElement("span");
        const previewText = chat.last_message || "Нет текстовых сообщений";
        preview.textContent = chat.last_from_me ? `Вы: ${previewText}` : (String(chat.id).endsWith("@g.us") && chat.last_sender ? `${chat.last_sender}: ${previewText}` : previewText);
        if (!chat.last_from_me && String(chat.id).endsWith("@g.us") && chat.last_sender_avatar_url) {
          const senderPic = document.createElement("img"); senderPic.className = "preview-author-avatar";
          senderPic.src = chat.last_sender_avatar_url; senderPic.alt = ""; senderPic.loading = "lazy";
          senderPic.addEventListener("error", () => senderPic.remove()); bottom.append(senderPic);
        }
        if (chat.last_from_me) {
          preview.classList.add("chat-preview-outgoing");
          const ack = Number(chat.last_ack || 0);
          const ticks = document.createElement("span");
          ticks.className = `chat-preview-ack${ack >= 3 ? " read" : ack <= 0 ? " pending" : ""}`;
          ticks.textContent = ack >= 2 ? "✓✓" : ack >= 1 ? "✓" : "◷";
          ticks.title = ack >= 3 ? "Прочитано" : ack >= 2 ? "Доставлено" : ack >= 1 ? "Отправлено" : "Отправляется";
          bottom.append(ticks);
        }
        bottom.append(preview);
        if (Number(chat.unread_count) > 0) {
          const unread = document.createElement("b");
          const isGroupMention = String(chat.id || "").endsWith("@g.us") && Boolean(chat.mentioned !== false);
          unread.className = isGroupMention ? "chat-mention" : "chat-unread";
          unread.textContent = isGroupMention ? "@" : String(chat.unread_count);
          if (isGroupMention) unread.title = "Вас упомянули в группе";
          bottom.append(unread);
        }
        content.append(top, bottom);
        button.append(avatar, content);
        chatList.append(button);
      });
    }

    function clearReply() {
      replyToMessage = null;
      if (replyInput) replyInput.value = "";
      if (replyPreview) replyPreview.hidden = true;
    }

    function startReply(message) {
      if (!message || !message.id) return;
      replyToMessage = message;
      if (replyInput) replyInput.value = message.id;
      if (replyPreview) replyPreview.hidden = false;
      if (replySender) replySender.textContent = `Ответ на сообщение · ${message.from_me ? "Вы" : (message.sender || selectedChatName())}`;
      if (replyBody) replyBody.textContent = String(message.body || message.media_name || "Вложение").slice(0, 180);
      const textarea = composer && composer.querySelector("textarea[name='message']");
      if (textarea) textarea.focus();
    }

    function updateForwardToolbar() {
      if (!forwardToolbar || !forwardCount || !forwardTarget) return;
      const count = selectedForForward.size;
      const visible = count > 0;
      forwardToolbar.hidden = !visible;
      forwardToolbar.style.setProperty("display", visible ? "flex" : "none", "important");
      forwardToolbar.style.setProperty("visibility", visible ? "visible" : "hidden", "important");
      [forwardCount, forwardTarget, forwardCancel, forwardSend].forEach((node) => {
        node.hidden = !visible;
        node.style.setProperty("display", visible ? "inline-flex" : "none", "important");
        node.style.setProperty("visibility", visible ? "visible" : "hidden", "important");
        node.style.setProperty("opacity", visible ? "1" : "0", "important");
      });
      forwardCount.textContent = `${count} ${count === 1 ? "сообщение выбрано" : "сообщения выбрано"}`;
      const targets = (forwardTargets.length ? forwardTargets : chats).filter(chat => String(chat?.id || '').trim());
      const optionsSignature = JSON.stringify(targets.map(chat => [String(chat.id), chat.name]));
      if (optionsSignature !== forwardOptionsSignature) {
        const previous = forwardTarget.value;
        forwardOptionsSignature = optionsSignature;
        const options = [new Option(targets.length ? 'Выберите чат для пересылки' : 'Нет доступных чатов', '')];
        targets.forEach(chat => options.push(new Option(cleanWhatsAppName(chat.name) || chatFallbackName(chat.id), String(chat.id))));
        forwardTarget.replaceChildren(...options);
        forwardTarget.value = options.some(o => o.value === previous) ? previous : '';
      }
      forwardSend.disabled = forwardBusy || !forwardTarget.value || forwardSend.dataset.lockDisabled === '1';
    }

    function toggleForward(message) {
      if (!message || !message.id) return;
      if (selectedForForward.has(message.id)) {
        selectedForForward.delete(message.id);
      } else {
        if (page.isMentionOnlyMessage(message)) {
          queueAlert("Нельзя пересылать сообщение, состоящее только из тега пользователя");
          return;
        }
        if (selectedForForward.size < 20) selectedForForward.add(message.id);
      }
      updateForwardToolbar();
      page.lastMessageSignature = "";
      loadChatState(true);
    }

    async function sendForwardSelection() {
      if (forwardBusy) return;
      if (!page.selectedChatId) {
        queueAlert("Сначала откройте чат с сообщением для пересылки");
        return;
      }
      if (!selectedForForward.size) {
        queueAlert("Сначала выберите сообщение для пересылки");
        return;
      }
      if (!forwardTarget || !forwardTarget.value) {
        queueAlert("Выберите чат назначения в списке перед пересылкой");
        forwardTarget?.focus();
        return;
      }
      forwardBusy = true;
      if (forwardSend) forwardSend.disabled = true;
      try {
        const response = await fetch(forwardEndpoint, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            source_chat_id: page.selectedChatId,
            target_chat_id: forwardTarget.value,
            message_ids: [...selectedForForward],
          }),
        });
        const result = await response.json();
        if (!response.ok || !result.queued) throw new Error(result.error || "Не удалось поставить пересылку в очередь");
        const selectedIds = [...selectedForForward];
        const actionIds = Array.isArray(result.action_ids) ? result.action_ids : [];
        if (actionIds.length) {
          const outcomes = await Promise.allSettled(actionIds.map((id) => waitForMessageAction(id)));
          const actionMessageIds = Array.isArray(result.action_message_ids) ? result.action_message_ids.map(String) : [];
          const queuedMessageIds = new Set(actionMessageIds);
          const failed = outcomes
            .map((outcome, index) => ({outcome, messageId: actionMessageIds[index] || selectedIds[index] || ""}))
            .filter(({outcome}) => outcome.status === "rejected");
          const notQueued = selectedIds.filter((id) => !queuedMessageIds.has(String(id)));
          selectedForForward.clear();
          [...failed.map((item) => item.messageId), ...notQueued].filter(Boolean).forEach((id) => selectedForForward.add(id));
          updateForwardToolbar();
          if (failed.length || notQueued.length) {
            const firstError = failed[0]?.outcome.reason;
            const detail = firstError?.message || firstError || "Сообщение отсутствует в очереди WhatsApp";
            throw new Error(`${failed.length + notQueued.length} сообщений не переслано. Ошибка: ${detail}`);
          }
        } else {
          selectedForForward.clear();
          updateForwardToolbar();
        }
        window.QueueUI?.toast?.(`Переслано сообщений: ${Number(result.count || actionIds.length || 0)}`);
      } catch (error) {
        queueAlert(error.message || "Не удалось переслать сообщения");
      } finally {
        forwardBusy = false;
        if (forwardSend) forwardSend.disabled = !forwardTarget.value || forwardSend.dataset.lockDisabled === "1";
      }
    }

    async function waitForMessageAction(actionId) {
      for (let attempt = 0; attempt < 18; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, attempt < 4 ? 500 : 900));
        const response = await fetch(`/api/chat-action-status?id=${encodeURIComponent(actionId)}`, {cache: "no-store"});
        if (!response.ok) continue;
        const result = await response.json();
        const state = result && result.action ? String(result.action.status || "") : "";
        if (state === "sent") return result.action;
        if (state === "failed") throw new Error(String(result.action.error || "WhatsApp не выполнил действие"));
      }
      throw new Error("WhatsApp не подтвердил действие вовремя. Проверьте чат и повторите");
    }

    async function queueMessageAction(message, action, actionBody = "") {
      if (!message || !message.id || !page.selectedChatId) return;
      let body = action === "react" ? String(actionBody || "") : "";
      if (action === "edit") {
        const editedBody = await queuePrompt("Введите новый текст сообщения", message.body || "", "Изменить сообщение");
        if (editedBody === null) return;
        body = String(editedBody || "");
        if (!body.trim() || body.trim() === String(message.body || "").trim()) return;
      } else if (action === "delete") {
        if (!(await queueConfirm("Удалить это сообщение в WhatsApp для всех? Это действие синхронизируется с WhatsApp.", "Удаление сообщения"))) return;
      }
      try {
        const response = await postForm("/chat-action", {
          chat_id: page.selectedChatId,
          message_id: message.id,
          action,
          message: body,
        });
        const result = await response.json();
        if (!response.ok || !result.queued || !result.action_id) throw new Error(result.error || "Действие не поставлено в очередь");
        await waitForMessageAction(result.action_id);
        page.lastMessageSignature = "";
        await loadChatState(true);
      } catch (error) {
        queueAlert(error.message || "Не удалось изменить сообщение");
      }
    }

    function participantLabel(participant) {
      return String(participant.name || participant.phone || "Участник");
    }

    function insertMention(participant, textarea, start, end) {
      const mentionId = String(participant.mention_id || "");
      const mentionUser = mentionId.split("@")[0];
      if (!mentionUser || !textarea) return;
      selectedMentionIds.add(mentionId);
      if (mentionsInput) mentionsInput.value = [...selectedMentionIds].join(",");
      const before = textarea.value.slice(0, start);
      const after = textarea.value.slice(end);
      const replacement = `@${mentionUser} `;
      textarea.value = before + replacement + after;
      const caret = before.length + replacement.length;
      textarea.focus();
      textarea.setSelectionRange(caret, caret);
      mentionPicker.hidden = true;
    }

    function updateMentionPicker(textarea) {
      if (!textarea || !page.selectedChatId.endsWith("@g.us") || !page.participants.length) {
        mentionPicker.hidden = true;
        return;
      }
      const cursor = textarea.selectionStart ?? textarea.value.length;
      const before = textarea.value.slice(0, cursor);
      const match = before.match(/(^|\s)@([^\s@]*)$/);
      if (!match) {
        mentionPicker.hidden = true;
        return;
      }
      const query = String(match[2] || "").toLocaleLowerCase("ru");
      const start = cursor - match[2].length - 1;
      const visible = page.participants.filter((participant) => {
        const haystack = `${participantLabel(participant)} ${participant.phone || ""}`.toLocaleLowerCase("ru");
        return (!needsReplyOnly || chat.needs_reply) && (!query || haystack.includes(query));
      }).slice(0, 10);
      mentionPicker.replaceChildren();
      if (!visible.length) {
        mentionPicker.hidden = true;
        return;
      }
      visible.forEach((participant) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "mention-option";
        const name = document.createElement("strong");
        name.textContent = participantLabel(participant) + (participant.is_admin ? " · админ" : "");
        const phone = document.createElement("small");
        phone.textContent = participant.phone || "Номер WhatsApp пока не определён";
        option.append(name, phone);
        option.addEventListener("mousedown", (event) => {
          event.preventDefault();
          insertMention(participant, textarea, start, cursor);
        });
        mentionPicker.append(option);
      });
      mentionPicker.hidden = false;
    }

    function renderParticipants(items) {
      page.participants = Array.isArray(items) ? items : [];
      if (!participantsList) return;
      participantsList.replaceChildren();
      if (participantsCount) participantsCount.textContent = String(page.participants.length);
      if (!page.participants.length) {
        const note = document.createElement("span");
        note.className = "muted";
        note.textContent = page.selectedChatId ? "Участники загружаются..." : "Выберите группу";
        participantsList.append(note);
        return;
      }
      page.participants.forEach((participant) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `participant-chip${participant.is_me ? " is-me" : ""}`;
        const avatar = document.createElement("span");
        avatar.className = "participant-avatar";
        avatar.textContent = avatarLetters(participantLabel(participant), participant.resolved_id || participant.mention_id);
        if (participant.avatar_url) {
          const img = document.createElement("img");
          img.src = participant.avatar_url;
          img.alt = "";
          img.loading = "lazy";
          img.addEventListener("error", () => img.remove());
          avatar.append(img);
        }
        const details = document.createElement("span");
        details.className = "participant-details";
        const name = document.createElement("strong");
        name.textContent = `${participantLabel(participant)}${participant.is_admin ? " · админ" : ""}`;
        const presence = document.createElement("small");
        const participantPresence = participant && participant.presence && typeof participant.presence === "object" ? participant.presence : {};
        presence.className = `participant-presence ${participantPresence.known ? (participantPresence.online ? "online" : "offline") : "unknown"}`;
        presence.textContent = participantPresence.known ? (participantPresence.online ? "Онлайн" : "Не в сети") : "Статус недоступен";
        presence.title = participantPresence.known ? (participantPresence.online ? "Пользователь сейчас онлайн" : "Пользователь сейчас не отображается онлайн") : "WhatsApp не передал статус. Он может быть скрыт настройками приватности.";
        const phone = document.createElement("small");
        phone.className = "participant-phone";
        phone.textContent = participant.phone || "Номер WhatsApp пока не определён";
        phone.hidden = true;
        details.append(name, presence, phone);
        button.append(avatar, details);
        const phoneDigits = String(participant.phone || "").replace(/\D/g, "");
        const resolvedDirect = String(participant.resolved_id || "").trim();
        const mentionDirect = String(participant.mention_id || "").trim();
        const directChatId = resolvedDirect.endsWith("@c.us") ? resolvedDirect : phoneDigits ? `${phoneDigits}@c.us` : resolvedDirect || mentionDirect;
        if (!participant.is_me && directChatId && !directChatId.endsWith("@g.us")) {
          button.classList.add("can-open-dm");
          button.title = "Открыть личный чат";
          button.addEventListener("click", () => {
            window.location.href = `/whatsapp?chat_id=${encodeURIComponent(directChatId)}`;
          });
        } else {
          button.title = "Нажмите, чтобы посмотреть номер";
          button.addEventListener("click", () => { phone.hidden = !phone.hidden; });
        }
        participantsList.append(button);
      });
    }

    function renderManualMode(mode, manualContact = false) {
      const groupConversation = location.pathname === "/groups" || String(page.selectedChatId || "").endsWith("@g.us");
      if (groupConversation) {
        page.botManualContactV17 = false;
        if (page.manualModeButton) page.manualModeButton.hidden = true;
        if (page.botResetButton) page.botResetButton.hidden = true;
        if (manualModeState) manualModeState.hidden = true;
        page.manualModeActive = false;
        page.parkHeaderBotControlsV17();
        page.syncOverflowBotActionsV17();
        return;
      }
      if (!page.manualModeButton || !manualModeState) return;
      page.manualModeActive = Boolean(mode && typeof mode === "object" && Object.keys(mode).length);
      page.botManualContactV17 = Boolean(manualContact);
      if (manualContact) {
        page.manualModeButton.hidden = true;
        if (page.botResetButton) page.botResetButton.hidden = true;
        manualModeState.hidden = false;
        manualModeState.textContent = "Контакт из админки · автоответы отключены";
        manualModeState.className = "manual-mode-state";
        page.parkHeaderBotControlsV17();
        page.syncOverflowBotActionsV17();
        return;
      }
      page.manualModeButton.hidden = false;
      if (page.botResetButton) {
        page.botResetButton.hidden = false;
        page.botResetButton.disabled = !page.selectedChatId;
        page.botResetButton.textContent = "↻ Перезагрузить бота";
        page.botResetButton.title = "Очистить текущий сценарий бота только для этого пользователя и начать с главного меню.";
      }
      manualModeState.hidden = false;
      page.manualModeButton.disabled = !page.selectedChatId;
      page.manualModeButton.textContent = page.manualModeActive ? "▶ Включить автоответы" : "⏸ Отключить автоответы";
      page.manualModeButton.title = page.manualModeActive
        ? "Возобновить автоматические ответы для этого пользователя без сброса его текущего сценария."
        : "Остановить автоматические ответы только для этого пользователя. Сообщения продолжат приходить в систему.";
      const actor = String((mode && mode.actor) || "").trim();
      manualModeState.textContent = !page.selectedChatId
        ? ""
        : page.manualModeActive
          ? `Автоответы отключены${actor ? ` · ${actor}` : ""}`
          : "Автоответчик работает для обычного пользователя";
      manualModeState.className = `manual-mode-state${page.manualModeActive ? " active" : ""}`;
      page.parkHeaderBotControlsV17();
      page.syncOverflowBotActionsV17();
    }

    function drawProfileSummary() {
      if (!profileSummary) return;
      if (!profileOpen || profileOpenChatId !== page.selectedChatId) {
        profileSummary.hidden = true;
        profileSummary.replaceChildren();
        return;
      }
      profileSummary.hidden = false;
      profileSummary.replaceChildren();
      const data = currentProfile && typeof currentProfile === "object" ? currentProfile : {};
      const pic = String(data.profile_pic_url || "");
      if (pic) {
        const img = document.createElement("img");
        img.src = pic;
        img.alt = "Фото профиля";
        img.className = "contact-profile-avatar";
        profileSummary.append(img);
      }
      const details = document.createElement("span");
      details.className = "contact-profile-details";
      const name = cleanWhatsAppName(data.name) || cleanWhatsAppName(selectedChatName()) || "Пользователь WhatsApp";
      const nameLine = document.createElement("strong");
      nameLine.textContent = name;
      details.append(nameLine);

      const rawPhone = String(data.phone || "").trim() || (() => {
        const direct = String(page.selectedChatId || "").replace(/@(?:c\.us|lid)$/i, "").replace(/\D/g, "");
        return direct ? `+${direct}` : "";
      })();
      if (rawPhone) {
        const digits = rawPhone.replace(/\D/g, "");
        let prettyPhone = rawPhone;
        if (digits.length === 11 && digits.startsWith("7")) {
          prettyPhone = `+7 (${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7, 9)}-${digits.slice(9, 11)}`;
        } else if (digits.length === 12 && digits.startsWith("998")) {
          prettyPhone = `+998 (${digits.slice(3, 5)}) ${digits.slice(5, 8)}-${digits.slice(8, 10)}-${digits.slice(10, 12)}`;
        } else if (digits) {
          prettyPhone = `+${digits}`;
        }
        const phoneLine = document.createElement("small");
        phoneLine.className = "contact-profile-phone";
        phoneLine.textContent = prettyPhone;
        details.append(phoneLine);
      }

      const about = String(data.about || "").trim();
      if (about) {
        const aboutLine = document.createElement("small");
        aboutLine.className = "contact-profile-about";
        aboutLine.textContent = about;
        details.append(aboutLine);
      }
      profileSummary.append(details);
    }

    // QUEUE_GROUP_MUTE_3_3_73
    function renderGroupMute(state) {
      if (!groupMuteButton) return;
      const isGroup = Boolean(page.selectedChatId && page.selectedChatId.endsWith("@g.us"));
      groupMuteButton.hidden = !isGroup;
      if (!isGroup) return;
      const selected = chats.find((chat) => String(chat.id || "") === page.selectedChatId) || {};
      groupMuted = Boolean(state && Object.prototype.hasOwnProperty.call(state, "selected_muted") ? state.selected_muted : selected.muted);
      groupMuteButton.textContent = groupMuted ? "🔔 Включить уведомления" : "🔕 Заглушить";
      groupMuteButton.title = groupMuted
        ? "Размутить группу: реальные @упоминания снова будут давать уведомления"
        : "Заглушить группу: сообщения и счётчик останутся, уведомления пропадут";
      groupMuteButton.classList.toggle("is-muted", groupMuted);
    }

    function renderPresence(presence) {
      if (!contactPresence) return;
      const value = presence && typeof presence === "object" ? presence : {};
      if (!page.selectedChatId || page.selectedChatId.endsWith("@g.us")) {
        contactPresence.hidden = true;
        contactPresence.textContent = "";
        contactPresence.className = "contact-presence";
        return;
      }
      contactPresence.hidden = false;
      if (!value.known) {
        contactPresence.className = "contact-presence unknown";
        contactPresence.textContent = "Статус недоступен";
        contactPresence.title = "WhatsApp не передал статус. Он может быть скрыт настройками приватности.";
        return;
      }
      const presenceState = String(value.state || "").toLowerCase();
      contactPresence.className = `contact-presence ${value.online ? "online" : "offline"}${presenceState === "composing" ? " typing" : presenceState === "recording" ? " recording" : ""}`;
      contactPresence.textContent = presenceState === "composing" ? "печатает..." : presenceState === "recording" ? "записывает аудио..." : value.online ? "Онлайн" : "Не в сети";
      contactPresence.title = presenceState === "composing" ? "Пользователь набирает сообщение" : presenceState === "recording" ? "Пользователь записывает голосовое сообщение" : value.online ? "Пользователь сейчас онлайн" : "Пользователь сейчас не отображается онлайн";
    }

    function renderProfile(profile) {
      if (!profileButton || !profileSummary) return;
      currentProfile = profile && typeof profile === "object" ? profile : {};
      profileButton.disabled = !page.selectedChatId;
      // The button stays compact; the opened profile card shows the WhatsApp name and phone number.
      profileButton.textContent = "Профиль";
      drawProfileSummary();
    }

    if (profileButton && profileSummary) profileButton.addEventListener("click", () => {
      if (!page.selectedChatId) return;
      if (profileOpen && profileOpenChatId === page.selectedChatId) {
        profileOpen = false;
        profileOpenChatId = "";
        try { sessionStorage.removeItem(PROFILE_OPEN_STORAGE_KEY); } catch (_) {}
      } else {
        profileOpen = true;
        profileOpenChatId = page.selectedChatId;
        try { sessionStorage.setItem(PROFILE_OPEN_STORAGE_KEY, page.selectedChatId); } catch (_) {}
      }
      drawProfileSummary();
    });

    if (groupMuteButton) groupMuteButton.addEventListener("click", async () => {
      if (!page.selectedChatId || !page.selectedChatId.endsWith("@g.us") || groupMuteButton.dataset.busy === "1") return;
      groupMuteButton.dataset.busy = "1";
      groupMuteButton.disabled = true;
      try {
        const body = new URLSearchParams({chat_id: page.selectedChatId, muted: groupMuted ? "0" : "1"});
        const response = await fetch("/group-mute", {
          method: "POST",
          headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
          body,
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || !result.updated) throw new Error("Не удалось изменить режим уведомлений");
        groupMuted = Boolean(result.muted);
        const row = chats.find((chat) => String(chat.id || "") === page.selectedChatId);
        if (row) row.muted = groupMuted;
        renderGroupMute({selected_muted: groupMuted});
        renderChats();
        window.QueueUI?.toast?.(groupMuted ? "Группа заглушена" : "Уведомления группы включены");
      } catch (error) {
        window.QueueUI?.toast?.(error && error.message ? error.message : "Не удалось изменить режим уведомлений");
      } finally {
        groupMuteButton.disabled = false;
        groupMuteButton.dataset.busy = "0";
      }
    });

    async function loadChatState(force = false) {
      if (loading && !force) return;
      loading = true;
      const sequence = ++stateSequence;
      const requestedChat = page.selectedChatId;
      try {
        let manualUnread = false;
        try { manualUnread = sessionStorage.getItem(`queue-manual-unread:${page.selectedChatId}`) === "1"; } catch (_) {}
        const isReading = !manualUnread && !document.hidden && document.hasFocus() && chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 96;
        const suffix = page.selectedChatId ? `?chat_id=${encodeURIComponent(page.selectedChatId)}&read=${isReading ? 1 : 0}` : "";
        const response = await fetch(`${stateEndpoint}${suffix}`, {cache: "no-store"});
        if (!response.ok) throw new Error("Нет связи с программой");
        const state = await response.json();
        if (sequence !== stateSequence || requestedChat !== page.selectedChatId) return;
        // Let access controls consume only the state snapshot accepted by this poll.
        window.dispatchEvent(new CustomEvent("queue-chat-state-accepted", {detail: state}));
        chats = Array.isArray(state.chats) ? state.chats : [];
        forwardTargets = Array.isArray(state.forward_targets) ? state.forward_targets : chats;
        if (!page.selectedChatId && state.selected_chat_id) page.selectedChatId = state.selected_chat_id;
        try {
          const storedProfileChatId = sessionStorage.getItem(PROFILE_OPEN_STORAGE_KEY) || "";
          if (storedProfileChatId && storedProfileChatId === page.selectedChatId) {
            profileOpen = true;
            profileOpenChatId = storedProfileChatId;
          }
        } catch (_) {}
        chatIdInput.value = page.selectedChatId;
        chatTitle.textContent = page.selectedChatId ? selectedChatName() : selectLabel;
        renderPresence(state.presence || (state.profile && state.profile.presence) || {});
        renderGroupMute(state);
        connection.textContent = state.connected ? "WhatsApp подключён" : "QR-коннектор не подключён";
        connection.className = `connection-badge ${state.connected ? "online" : "offline"}`;
        renderChats();
        updateForwardToolbar();
        page.latestMessages = Array.isArray(state.messages) ? state.messages : [];
        const jumpParams = new URLSearchParams(window.location.search);
        const jumpId = jumpParams.get("message_id") || "";
        const jumpBody = jumpParams.get("quoted_body") || "";
        const jumpSender = jumpParams.get("quoted_sender") || "";
        const jumpBeforeTs = jumpParams.get("before_ts") || "";
        const jumpKey = jumpId || (jumpBody ? `body:${jumpBody}:${jumpBeforeTs}` : "");
        let resolvedJumpId = jumpId;
        if (jumpKey && conversationPage.dataset.jumpLoaded !== jumpKey) {
          const targetQuery = new URLSearchParams({chat_id:page.selectedChatId});
          if (jumpId) targetQuery.set("message_id", jumpId);
          if (jumpBody) targetQuery.set("quoted_body", jumpBody);
          if (jumpSender) targetQuery.set("quoted_sender", jumpSender);
          if (jumpBeforeTs) targetQuery.set("before_ts", jumpBeforeTs);
          const target = await fetch("/api/message-window?" + targetQuery);
          conversationPage.dataset.jumpLoaded = jumpKey;
          if (target.ok) {
            const windowData = await target.json();
            if (sequence !== stateSequence || requestedChat !== page.selectedChatId) return;
            page.olderMessages = windowData.messages || [];
            page.historyCursor = windowData.cursor || "";
            page.historyHasMore = Boolean(windowData.has_more);
            resolvedJumpId = String(windowData.target_id || jumpId || "");
            conversationPage.dataset.jumpResolved = resolvedJumpId;
          } else {
            const reply = page.mergedConversationMessages().find((item) => {
              if (!item) return false;
              const sameQuoteId = jumpId && page.messageKeysMatch(item.quoted_message_key || "", jumpId);
              const sameQuoteBody = jumpBody && page.quoteBodiesMatch(item.quoted_body || "", jumpBody);
              return Boolean(sameQuoteId || sameQuoteBody);
            }) || null;
            const fallback = page.findQuotedMessageCandidate(reply || {timestamp:Number(jumpBeforeTs || 0)}, jumpId, jumpBody, jumpSender);
            if (fallback) {
              resolvedJumpId = String(fallback.id || "");
              conversationPage.dataset.jumpResolved = resolvedJumpId;
            } else {
              window.QueueUI?.toast?.("Сообщение недоступно");
            }
          }
        } else if (jumpKey) {
          resolvedJumpId = conversationPage.dataset.jumpResolved || jumpId;
        }
        if (!page.olderMessages.length) {
          page.historyCursor = String(state.history_cursor || "");
          page.historyHasMore = Boolean(state.history_has_more);
        }
        renderParticipants(state.participants || []);
        page.renderMessages(page.mergedConversationMessages());
        const jumpScrollId = resolvedJumpId || jumpId;
        if (jumpScrollId && conversationPage.dataset.jumpScrolled !== jumpScrollId) {
          if (page.focusMessageRow(jumpScrollId, true)) {
            conversationPage.dataset.jumpScrolled = jumpScrollId;
            const cleanUrl = new URL(window.location.href);
            cleanUrl.searchParams.delete("message_id");
            cleanUrl.searchParams.delete("quoted_body");
            cleanUrl.searchParams.delete("quoted_sender");
            cleanUrl.searchParams.delete("before_ts");
            window.history.replaceState({}, "", cleanUrl);
          }
        }
        renderManualMode(state.manual_mode || {}, Boolean(state.manual_contact));
        renderProfile(state.profile || {});
      } catch (error) {
        connection.textContent = "Нет связи с программой";
        connection.className = "connection-badge offline";
      } finally {
        if (sequence === stateSequence) loading = false;
      }
    }

    if (page.manualModeButton) {
      page.manualModeButton.addEventListener("click", async () => {
        if (!page.selectedChatId || page.manualModeButton.dataset.busy === "1") return;
        page.manualModeButton.dataset.busy = "1";
        page.manualModeButton.disabled = true;
        const action = page.manualModeActive ? "enable" : "disable";
        page.manualModeButton.textContent = page.manualModeActive ? "Включаю..." : "Отключаю...";
        try {
          const response = await postForm("/chat-mode", {
            chat_id: page.selectedChatId,
            action,
          });
          const result = await response.json().catch(() => ({}));
          if (!response.ok || !result.updated) throw new Error(result.error || "Не удалось изменить автоответы");
          renderManualMode(result.manual_mode || {}, false);
          window.QueueUI?.toast?.(result.message || (action === "enable" ? "Автоответы включены" : "Автоответы отключены"));
        } catch (error) {
          queueAlert(error.message || "Не удалось изменить автоответы");
          await loadChatState(true);
        } finally {
          page.manualModeButton.dataset.busy = "0";
          page.manualModeButton.disabled = !page.selectedChatId;
          page.parkHeaderBotControlsV17();
          page.syncOverflowBotActionsV17();
        }
      });
    }

    if (page.botResetButton) {
      page.botResetButton.addEventListener("click", async () => {
        if (!page.selectedChatId || page.botResetButton.dataset.busy === "1") return;
        page.botResetButton.dataset.busy = "1";
        page.botResetButton.disabled = true;
        const oldText = page.botResetButton.textContent;
        page.botResetButton.textContent = "Перезапуск...";
        try {
          const response = await postForm("/chat-mode", {
            chat_id: page.selectedChatId,
            action: "reset",
          });
          const result = await response.json().catch(() => ({}));
          if (!response.ok || !result.updated) throw new Error(result.error || "Не удалось перезапустить бота");
          renderManualMode({}, false);
          manualModeState.textContent = result.message || "Бот перезапущен. Автоответы включены.";
          window.QueueUI?.toast?.("Бот для этого пользователя перезапущен");
        } catch (error) {
          queueAlert(error.message || "Не удалось перезапустить бота");
        } finally {
          page.botResetButton.dataset.busy = "0";
          page.botResetButton.disabled = !page.selectedChatId;
          if (page.botResetButton.textContent === "Перезапуск...") page.botResetButton.textContent = oldText || "↻ Перезагрузить бота";
          page.parkHeaderBotControlsV17();
          page.syncOverflowBotActionsV17();
        }
      });
    }

    const searchStorageKey = "queue-chat-search:" + location.pathname;
    try { chatSearch.value = sessionStorage.getItem(searchStorageKey) || ""; } catch (_) {}
    chatSearch.addEventListener("input", () => {
      try { sessionStorage.setItem(searchStorageKey, chatSearch.value); } catch (_) {}
      renderChats();
    });
    refreshButton.addEventListener("click", () => loadChatState(true));
    const composerTextarea = page.composerTextarea = composer.querySelector("textarea[name='message']");
    if (composerTextarea) {
      composerTextarea.addEventListener("input", () => updateMentionPicker(composerTextarea));
      composerTextarea.addEventListener("click", () => updateMentionPicker(composerTextarea));
      composerTextarea.addEventListener("keydown", (event) => {
        if (event.key === "Escape") mentionPicker.hidden = true;
      });
    }
    if (replyCancel) replyCancel.addEventListener("click", clearReply);
    if (forwardCancel) forwardCancel.addEventListener("click", () => { selectedForForward.clear(); updateForwardToolbar(); page.lastMessageSignature = ""; loadChatState(true); });
    if (forwardTarget) forwardTarget.addEventListener("change", () => {
      if (forwardSend) forwardSend.disabled = forwardBusy || !forwardTarget.value || forwardSend.dataset.lockDisabled === "1";
    });
    if (forwardSend) forwardSend.addEventListener("click", sendForwardSelection);

    const emojiValues = [
      "😀","😃","😄","😁","😆","😅","😂","🤣","😊","🙂","🙃","😉","😍","🥰","😘","😎","🤩","🥳","😇","🤗",
      "🤔","🫡","🤨","😐","😑","😶","🙄","😏","😴","🤤","😮","😲","😳","🥺","😢","😭","😤","😡","🤬","😱",
      "👍","👎","👌","✌️","🤞","🤟","🤘","🤙","👈","👉","👆","👇","☝️","👋","👏","🙌","👐","🤝","🙏","💪",
      "❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎","💔","❣️","💕","💯","💥","✨","⭐","🔥","🎉","🎊","🎁",
      "✅","❌","⚠️","❗","❓","‼️","⁉️","🔴","🟢","🟡","🔵","📌","📎","📄","📁","📷","🎥","🎵","💬","📞",
      "🚗","🚕","🚚","🚌","🚦","🛠️","🔧","🔒","🔓","🔑","💡","🔔","⏰","⌛","📅","📍","🏢","🏠","🌍","☀️",
      "🌙","☁️","🌧️","❄️","☕","🍵","🍕","🍔","🍎","🍉","🐱","🐶","🦊","🐼","👀","🧠","🚀","🏆","🎯","💼"
    ];
    if (emojiPicker) {
      emojiValues.forEach((emoji) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = emoji;
        button.addEventListener("click", () => {
          const textarea = composer.querySelector("textarea[name='message']");
          const start = textarea.selectionStart ?? textarea.value.length;
          textarea.value = textarea.value.slice(0, start) + emoji + textarea.value.slice(start);
          textarea.focus();
          textarea.setSelectionRange(start + emoji.length, start + emoji.length);
        });
        emojiPicker.append(button);
      });
    }
    if (emojiToggle && emojiPicker) emojiToggle.addEventListener("click", () => { emojiPicker.hidden = !emojiPicker.hidden; });
    let uploadController = null;
    let uploadId = "";
    const attachmentRemove = page.attachmentRemove = document.createElement("button");
    attachmentRemove.type = "button";
    attachmentRemove.className = "attachment-remove";
    attachmentRemove.textContent = "× Убрать файл";
    attachmentRemove.hidden = true;
    const attachmentBar = page.attachmentBar = document.createElement("div");
    attachmentBar.className = "attachment-bar";
    attachmentBar.hidden = true;
    if (mediaName) { mediaName.before(attachmentBar); attachmentBar.append(mediaName, attachmentRemove); }
    if (composerTextarea) {
      const field = document.createElement("div");
      field.className = "composer-field";
      composerTextarea.before(field); field.append(composerTextarea);
      const tools = composer.querySelector(".composer-tools");
      if (tools) field.append(tools);
    }
    const composeStatus = page.composeStatus = document.createElement("div");
    composeStatus.className = "compose-status";
    composeStatus.setAttribute("role", "status");
    composer.append(composeStatus);
    attachmentRemove.addEventListener("click", () => {
      if (uploadController) uploadController.abort();
      page.clearAttachment();
    });
    mediaInput?.addEventListener("change", page.showAttachment);
    composerTextarea?.addEventListener("paste", page.queueHandleClipboardPaste);
    // QUEUE_DRAG_DROP_3_3_61: drag a file from Explorer/Desktop directly into the open chat.
    let queueDragDepth = 0;
    const queueDropZone = conversationPage;
    queueDropZone?.addEventListener("dragenter", (event) => {
      if (!page.queueHasFiles(event)) return;
      event.preventDefault();
      queueDragDepth += 1;
      queueDropZone.classList.add("queue-file-drag-active");
    });
    queueDropZone?.addEventListener("dragover", (event) => {
      if (!page.queueHasFiles(event)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      queueDropZone.classList.add("queue-file-drag-active");
    });
    queueDropZone?.addEventListener("dragleave", (event) => {
      if (!page.queueHasFiles(event)) return;
      queueDragDepth = Math.max(0, queueDragDepth - 1);
      if (!queueDragDepth) queueDropZone.classList.remove("queue-file-drag-active");
    });
    queueDropZone?.addEventListener("drop", (event) => {
      if (!page.queueHasFiles(event)) return;
      event.preventDefault();
      queueDragDepth = 0;
      queueDropZone.classList.remove("queue-file-drag-active");
      if (composer.dataset.sending === "1") {
        composeStatus.textContent = "Дождитесь завершения текущей отправки";
        return;
      }
      if (!page.selectedChatId) {
        composeStatus.textContent = "Сначала выберите чат или группу";
        return;
      }
      const files = Array.from(event.dataTransfer?.files || []);
      if (!files.length) return;
      if (files.length > 1) composeStatus.textContent = "Будет прикреплён первый файл. Остальные можно отправить следующим сообщением";
      if (page.queueSetComposerFile(files[0]) && files.length === 1) composeStatus.textContent = "Файл прикреплён. Нажмите «Отправить»";
      composerTextarea?.focus();
    });
    page.restoreDraft();
    composerTextarea?.addEventListener("input", page.saveDraft);
    composerTextarea?.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && mentionPicker.hidden) {
        event.preventDefault();
        if (composer.dataset.sending !== "1") composer.requestSubmit();
      }
    });
    composerTextarea?.setAttribute("title", "Enter: отправить. Shift + Enter: новая строка");

    composer.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (composer.dataset.sending === "1") return;
      const textarea = composer.querySelector("textarea[name='message']");
      const button = composer.querySelector("button[type='submit']");
      const file = mediaInput?.files?.[0];
      if (!page.selectedChatId || (!textarea.value.trim() && !file)) return;
      if (file && file.size > 512 * 1024 * 1024) { page.showAttachment(); return; }
      composer.dataset.sending = "1";
      button.disabled = true; textarea.disabled = true;
      if (mediaInput) mediaInput.disabled = true;
      composeStatus.textContent = file ? "Подготовка файла…" : "Отправка…";
      let completed = false;
      const requestId=page.sendIntent(textarea.value,file);
      try {
        let result;
        if (file) {
          uploadController = new AbortController();
          const signal = uploadController.signal;
          const started = await page.uploadRequest("/upload/start", {
            chat_id: page.selectedChatId, message: textarea.value, request_id: requestId,
            reply_to: replyInput?.value || "", mimetype: file.type,
            filename: file.name, size: file.size, mentions: [...selectedMentionIds],
          }, signal);
          uploadId = started.upload_id;
          attachmentRemove.textContent = "× Отменить загрузку";
          for (let offset = 0; offset < file.size; offset += started.chunk_size) {
            const end = Math.min(file.size, offset + started.chunk_size);
            await page.uploadRequest(`/upload/chunk?id=${encodeURIComponent(uploadId)}&offset=${offset}`, file.slice(offset,end), signal, true);
            composeStatus.textContent = `Загрузка файла: ${Math.round(end/file.size*100)}%`;
          }
          // The commit is atomic. Cancellation is no longer offered once queued.
          attachmentRemove.hidden = true;
          result = await page.uploadRequest("/upload/finish", {upload_id:uploadId});
          completed = Boolean(result.queued);
        } else {
          const response = await postForm(sendEndpoint, {
            chat_id: page.selectedChatId, message: textarea.value, request_id: requestId,
            mentions: mentionsInput?.value || "", reply_to: replyInput?.value || "",
          });
          result = await response.json();
          if (!response.ok) throw new Error(result.error || "Не удалось отправить сообщение");
        }
        if (!result.queued) throw new Error(result.error || "Сообщение не поставлено в очередь");
        try { localStorage.removeItem(page.requestKey()); } catch (_) {}
        textarea.value = ""; page.saveDraft(); clearReply();
        selectedMentionIds.clear();
        if (mentionsInput) mentionsInput.value = "";
        page.clearAttachment();
        if (emojiPicker) emojiPicker.hidden = true;
        composeStatus.textContent = "Отправляется…";
        void loadChatState(true);
        window.setTimeout(() => { composeStatus.textContent = ""; }, 1800);
      } catch (error) {
        composeStatus.textContent = error.name === "AbortError" ? "Загрузка отменена" : (error.message || "Не удалось отправить сообщение");
        if (uploadId && !completed) page.uploadRequest("/upload/cancel", {upload_id:uploadId}).catch(() => {});
      } finally {
        uploadController = null; uploadId = "";
        attachmentRemove.textContent = "× Убрать файл";
        page.showAttachment();
        button.disabled = false; textarea.disabled = false;
        if (mediaInput) mediaInput.disabled = false;
        composer.dataset.sending = "0";
        textarea.focus();
      }
    });

    if (page.selectedChatId) chatIdInput.value = page.selectedChatId;
    // Realtime-first, slow polling only as a safety net.
    let chatPollTimer = null;
    const scheduleChatPoll = () => {
      if (chatPollTimer) window.clearTimeout(chatPollTimer);
      chatPollTimer = window.setTimeout(async () => {
        await loadChatState(false);
        scheduleChatPoll();
      }, document.hidden ? 60000 : 15000);
    };
    window.addEventListener("queue-realtime", () => {
      if (!document.hidden) void loadChatState(false);
    });
    window.addEventListener("queue-open-message", () => {
      const wanted=new URL(location.href).searchParams.get("chat_id");
      if(wanted && wanted!==page.selectedChatId){if(page.selectedChatId) void window.QueueConversationLock?.releaseIfOwned?.(page.selectedChatId); page.saveDraft(); page.selectedChatId=wanted; page.olderMessages=[];page.latestMessages=[];page.historyCursor="";page.historyHasMore=false;page.restoreDraft();}
      delete conversationPage.dataset.jumpLoaded;delete conversationPage.dataset.jumpScrolled;
      loadChatState(true);
    });
    loadChatState(true).finally(scheduleChatPoll);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) loadChatState(true);
      scheduleChatPoll();
    });
  }

  return {start: startConversationPage};
})();
