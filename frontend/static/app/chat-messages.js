// Переписка в чате: текст и вложения сообщений, цитаты, старые сообщения и закладки.
window.QueueChatMessages = (page) => {
  // QUEUE_LINKIFY_3_3_79: render http(s), www and plain-domain links as safe clickable anchors.
  function appendMessageText(container, text, message) {
    const value = String(text || "");
    const exactMentions = [];
    (Array.isArray(message && message.mentions) ? message.mentions : []).forEach((mention) => {
      if (!mention || typeof mention !== "object") return;
      const name = String(mention.name || "").trim();
      if (name) exactMentions.push(`@${name}`);
    });
    exactMentions.sort((a, b) => b.length - a.length);
    const escapedMentions = exactMentions.map((item) => item.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const mentionSource = escapedMentions.length
      ? `(?:${escapedMentions.join("|")}|@[\\p{L}\\p{N}_.+:-]+)`
      : "(?:@[\\p{L}\\p{N}_.+:-]+)";
    const urlSource = "(?:(?:https?:\\/\\/|www\\.)[^\\s<>\\\"']+|(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?\\.)+[A-Za-z]{2,24}(?:\\/[^\\s<>\\\"']*)?)";
    let matcher;
    try { matcher = new RegExp(`${mentionSource}|${urlSource}`, "giu"); }
    catch (_) { matcher = /@[A-Za-zА-Яа-яЁё0-9_.+:-]+|https?:\/\/[^\s<>"']+|www\.[^\s<>"']+/gi; }
    let cursor = 0;
    for (const match of value.matchAll(matcher)) {
      const index = Number(match.index || 0);
      if (index > cursor) container.append(document.createTextNode(value.slice(cursor, index)));
      let token = String(match[0] || "");
      const isMention = token.startsWith("@");
      const previousChar = index > 0 ? value[index - 1] : "";
      const looksLikeBareDomain = !isMention && !/^https?:\/\//i.test(token) && !/^www\./i.test(token);
      if (!isMention && looksLikeBareDomain && previousChar === "@") {
        container.append(document.createTextNode(token));
        cursor = index + token.length;
        continue;
      }
      if (isMention) {
        const mention = document.createElement("span");
        mention.className = `chat-inline-mention${message && message.highlight_mention ? " mention-highlighted" : ""}`;
        mention.textContent = token;
        container.append(mention);
        cursor = index + token.length;
        continue;
      }
      let trailing = "";
      while (token && /[.,!?;:]$/.test(token)) {
        trailing = token.slice(-1) + trailing;
        token = token.slice(0, -1);
      }
      if (!token) {
        container.append(document.createTextNode(match[0]));
        cursor = index + String(match[0]).length;
        continue;
      }
      const link = document.createElement("a");
      link.className = "chat-inline-link";
      link.textContent = token;
      link.href = /^https?:\/\//i.test(token) ? token : `https://${token}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = "Открыть ссылку";
      link.addEventListener("click", (event) => event.stopPropagation());
      container.append(link);
      if (trailing) container.append(document.createTextNode(trailing));
      cursor = index + String(match[0]).length;
    }
    if (cursor < value.length) container.append(document.createTextNode(value.slice(cursor)));
  }

  function isMentionOnlyMessage(message) {
    if (!message || message.deleted) return false;
    if (message.media_url || message.media_name || message.quoted_message_key) return false;
    const body = String(message.body || "").trim();
    if (!body || !body.includes("@")) return false;
    let rest = body;
    const tokens = [];
    (Array.isArray(message.mentions) ? message.mentions : []).forEach((mention) => {
      if (!mention || typeof mention !== "object") return;
      [mention.name, mention.id, mention.resolved_id].forEach((raw) => {
        let value = String(raw || "").trim();
        if (!value) return;
        if (value.includes("@") && !value.startsWith("@")) value = value.split("@", 1)[0];
        value = value.replace(/^@+/, "").trim();
        if (value) tokens.push(`@${value}`);
      });
    });
    [...new Set(tokens)].sort((a, b) => b.length - a.length).forEach((token) => {
      const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      rest = rest.replace(new RegExp(escaped, "giu"), "");
    });
    try { rest = rest.replace(/@[\p{L}\p{N}_.+:-]+/gu, ""); }
    catch (_) { rest = rest.replace(/@[A-Za-zА-Яа-яЁё0-9_.+:-]+/g, ""); }
    return rest.trim() === "";
  }

  // Saved messages are per logged-in employee and persisted in app_settings.
  let messageBookmarks = [];
  const bookmarkKey = (chatId, messageId) => `${String(chatId || '')}\u0001${String(messageId || '')}`;
  function messageIsBookmarked(chatId, messageId) {
    return messageBookmarks.some((item) => bookmarkKey(item.chat_id, item.message_id) === bookmarkKey(chatId, messageId));
  }
  async function refreshMessageBookmarks(refreshMessages = false) {
    const response = await fetch('/api/message-bookmarks', {cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось загрузить избранные сообщения');
    const data = await response.json();
    messageBookmarks = Array.isArray(data.bookmarks) ? data.bookmarks : [];
    if (refreshMessages && page.selectedChatId) {
      page.lastMessageSignature = '';
      renderMessages(mergedConversationMessages());
    }
    return messageBookmarks;
  }
  async function toggleMessageBookmark(message) {
    if (!message?.id || !page.selectedChatId) return;
    const newValue = !messageIsBookmarked(page.selectedChatId, message.id);
    const body = new URLSearchParams({
      chat_id: page.selectedChatId, message_id: String(message.id), favorite: newValue ? '1' : '0',
      preview: String(message.body || message.media_name || '[Вложение]').slice(0, 280),
      sender: String(message.sender || (message.from_me ? 'Вы' : '')).slice(0, 100),
      chat_name: page.selectedChatName(), timestamp: String(message.timestamp || 0),
    });
    const response = await fetch('/api/message-bookmarks', {
      method: 'POST', headers: {'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'}, body,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.updated) throw new Error(data.error || 'Не удалось изменить закладку');
    messageBookmarks = Array.isArray(data.bookmarks) ? data.bookmarks : [];
    page.lastMessageSignature = '';
    renderMessages(mergedConversationMessages());
    window.QueueUI?.toast?.(newValue ? 'Сообщение сохранено в избранное' : 'Сообщение удалено из избранного');
  }
  async function showMessageBookmarks() {
    try { await refreshMessageBookmarks(); }
    catch (error) { window.QueueUI?.toast?.(error.message); return; }
    const overlay = document.createElement('div');
    overlay.className = 'queue-bookmarks-overlay';
    const panel = document.createElement('section');
    panel.className = 'queue-bookmarks-panel';
    panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-label', 'Избранные сообщения');
    const header = document.createElement('header');
    const heading = document.createElement('h2'); heading.textContent = `★ Избранные сообщения (${messageBookmarks.length})`;
    const close = document.createElement('button'); close.type = 'button'; close.className = 'button compact ghost'; close.textContent = 'Закрыть';
    const onKey = (event) => { if (event.key === 'Escape') dismiss(); };
    const dismiss = () => { overlay.remove(); document.removeEventListener('keydown', onKey); };
    close.addEventListener('click', dismiss); header.append(heading, close); panel.append(header);
    const list = document.createElement('div'); list.className = 'queue-bookmarks-list';
    if (!messageBookmarks.length) {
      const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = 'Пока нет сохранённых сообщений.'; list.append(empty);
    }
    for (const item of messageBookmarks) {
      const link = document.createElement('a'); link.className = 'queue-bookmark-entry';
      const isGroup = String(item.chat_id || '').endsWith('@g.us');
      const query = new URLSearchParams({chat_id: String(item.chat_id), message_id: String(item.message_id)});
      if (item.timestamp) query.set('before_ts', String(item.timestamp));
      link.href = (isGroup ? '/groups?' : '/whatsapp?') + query;
      const name = document.createElement('strong'); name.textContent = String(item.chat_name || item.chat_id || 'Чат');
      const preview = document.createElement('span'); preview.textContent = `${item.sender ? item.sender + ': ' : ''}${item.preview || '[Сообщение]'}`;
      link.append(name, preview); list.append(link);
    }
    panel.append(list); overlay.append(panel);
    overlay.addEventListener('click', (event) => { if (event.target === overlay) dismiss(); });
    document.addEventListener('keydown', onKey); document.body.append(overlay); close.focus();
  }

  function appendMedia(bubble, message) {
    if (!message.media_url || message.deleted) return;
    const type = String(message.type || "");
    const mime = String(message.media_mime || "");
    if (type === "image" || mime.startsWith("image/")) {
      const zoom = document.createElement("button");
      zoom.type = "button";
      zoom.className = "chat-media-zoom";
      zoom.dataset.mediaZoom = message.media_url;
      zoom.dataset.mediaName = message.media_name || "Фото";
      zoom.title = "Нажмите, чтобы увеличить фото";
      const image = document.createElement("img");
      image.className = "chat-media-image";
      image.loading = "lazy";
      image.decoding = "async";
      if (mime === "image/gif" || String(message.media_name || "").toLowerCase().endsWith(".gif")) zoom.classList.add("is-gif");
      image.src = message.media_url;
      image.addEventListener("error", () => {
        image.hidden = true;
        zoom.textContent = "Фото недоступно";
      });
      image.alt = message.media_name || "Фото";
      zoom.append(image);
      bubble.append(zoom);
    } else if (["audio", "ptt"].includes(type) || mime.startsWith("audio/")) {
      const audio = document.createElement("button");
      audio.type = "button"; audio.className = "voice-source-button";
      audio.textContent = "▶ Голосовое сообщение";
      const sourceChat = page.selectedChatId;
      audio.addEventListener("click", () => window.QueueVoice.play(message, sourceChat));
      bubble.append(audio);
    } else if (type === "video" || mime.startsWith("video/")) {
      const video = document.createElement("video");
      video.className = "chat-media-video";
      video.controls = true;
      video.preload = "metadata";
      video.src = message.media_url;
      video.addEventListener("error", () => { video.title = "Видео недоступно или формат не поддерживается браузером"; });
      video.playsInline = true;
      video.addEventListener("play", () => {
        document.querySelectorAll("audio,video").forEach(other => { if (other !== video) other.pause(); });
      });
      bubble.append(video);
    } else {
      const box = document.createElement("span");
      box.className = "chat-media-file-box";
      const link = document.createElement("a");
      link.className = "chat-media-file";
      link.href = message.media_url;
      link.target = "_blank";
      link.textContent = message.media_name || "Открыть вложение";
      const download = document.createElement("a");
      download.className = "chat-media-download";
      download.href = `${message.media_url}&download=1`;
      download.textContent = "Скачать";
      box.append(link, download);
      bubble.append(box);
    }
  }

  function mergedConversationMessages() {
    const byId = new Map();
    [...page.olderMessages, ...page.latestMessages].forEach((message) => {
      const key = String(message && message.id || `${message.timestamp}:${message.from_me ? 1 : 0}:${message.body || ""}`);
      const previous = byId.get(key) || {};
      byId.set(key, {...previous, ...message, deleted: Boolean(previous.deleted || message.deleted)});
    });
    return [...byId.values()].sort((a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0));
  }

  let historyLoading = false;
  async function loadOlderMessages() {
    if (historyLoading || !page.selectedChatId || !page.historyHasMore || !page.historyCursor) return;
    historyLoading = true;
    const historyChat = page.selectedChatId;
    const oldHeight = page.chatMessages.scrollHeight;
    const oldTop = page.chatMessages.scrollTop;
    try {
      const response = await fetch(`/api/chat-history?chat_id=${encodeURIComponent(page.selectedChatId)}&cursor=${encodeURIComponent(page.historyCursor)}`, {cache: "no-store"});
      if (!response.ok) throw new Error("Не удалось загрузить историю");
      const result = await response.json();
      if (historyChat !== page.selectedChatId) return;
      const pageMessages = Array.isArray(result.messages) ? result.messages : [];
      page.olderMessages = [...pageMessages, ...page.olderMessages];
      page.historyCursor = String(result.cursor || "");
      page.historyHasMore = Boolean(result.has_more);
      page.lastMessageSignature = "";
      renderMessages(mergedConversationMessages(), true, oldHeight, oldTop);
    } catch (error) {
      page.queueAlert(error.message || "Не удалось загрузить предыдущие сообщения");
    } finally { historyLoading = false; }
  }

  function focusMessageRow(messageId, smooth = true) {
    const wanted = String(messageId || "");
    if (!wanted || !page.chatMessages) return false;
    const targetRow = [...page.chatMessages.querySelectorAll("[data-message-id]")].find((row) => row.dataset.messageId === wanted);
    if (!targetRow) return false;
    targetRow.classList.remove("search-message-target");
    void targetRow.offsetWidth;
    targetRow.classList.add("search-message-target");
    window.setTimeout(() => targetRow.classList.remove("search-message-target"), 6000);
    targetRow.scrollIntoView({
      block: "center",
      behavior: smooth && !window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto",
    });
    return true;
  }


  function normalizeQuoteText(value) {
    return String(value || "")
      .replace(/[.…]+$/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .toLocaleLowerCase("ru-RU");
  }

  function normalizedMessageKey(value) {
    return String(value || "").trim().replace(/\s+/g, "");
  }

  function messageKeysMatch(left, right) {
    const a = String(left || "").trim(), b = String(right || "").trim();
    if (!a || !b) return false;
    if (a === b) return true;
    const parse = value => { const m = /^(true|false)_([^_]+)_(.+)$/.exec(value); return m ? [m[1], m[3]] : ["", value]; };
    const [ad, aid] = parse(a), [bd, bid] = parse(b);
    return aid === bid && aid.length >= 8 && (!ad || !bd || ad === bd);
  }
  function quoteBodiesMatch() { return false; }
  function findQuotedMessageCandidate(reply, targetId) {
    const matches = mergedConversationMessages().filter(m => !m.deleted && messageKeysMatch(m.id, targetId));
    return matches.length === 1 ? matches[0] : null;
  }

  function renderMessages(messages, preserveScroll = false, oldHeight = 0, oldTop = 0) {
    // Do not destroy an audio/video player during background polling.

    // Auto-refresh must not throw the employee back to the newest message while
    // they are reading older history. If the user is already near the bottom,
    // keep following new messages like WhatsApp does.
    const anchor = [...page.chatMessages.querySelectorAll("[data-message-id]")].find(row => row.getBoundingClientRect().bottom > page.chatMessages.getBoundingClientRect().top);
    const anchorOffset = anchor ? anchor.getBoundingClientRect().top - page.chatMessages.getBoundingClientRect().top : 0;
    const currentTop = page.chatMessages.scrollTop;
    const currentHeight = page.chatMessages.scrollHeight;
    const currentClientHeight = page.chatMessages.clientHeight;
    const wasNearBottom = !new URL(location.href).searchParams.has("message_id") && currentHeight - (currentTop + currentClientHeight) <= 96;
    const signature = `${page.historyHasMore ? 1 : 0}:${page.historyCursor}|` + messages
      .map((message) => `${message.id}:${message.timestamp}:${message.ack || 0}:${message.edited ? 1 : 0}:${message.deleted ? 1 : 0}:${message.forwarded ? 1 : 0}:${message.sender || ""}:${message.sender_phone || ""}:${message.sender_avatar_url || ""}:${message.body || ""}:${message.media_url || ""}:${message.quoted_message_key || ""}:${message.quoted_sender || ""}:${message.quoted_body || ""}:${message.quote_preview_url || ""}:${message.quote_unavailable ? 1 : 0}:${message.transcript || ""}:${message.highlight_mention ? 1 : 0}:${message.media_name || ""}:${(Array.isArray(message.reactions) ? message.reactions : []).map((reaction) => `${reaction.emoji || ""},${reaction.count || 0},${reaction.me ? 1 : 0}`).join(";")}:${page.selectedForForward.has(message.id) ? 1 : 0}:${messageIsBookmarked(page.selectedChatId, message.id) ? 1 : 0}`)
      .join("|");
    if (signature === page.lastMessageSignature) return;
    page.lastMessageSignature = signature;
    const oldRows = new Map([...page.chatMessages.querySelectorAll("[data-message-id]")].map(row => [row.dataset.messageId, row]));
    const nodes = [];
    if (!page.selectedChatId) {
      page.placeholder(page.chatMessages, "Выберите чат слева, чтобы увидеть последние сообщения");
      return;
    }
    if (!messages.length) {
      page.placeholder(page.chatMessages, "В этом чате нет загруженных сообщений");
      return;
    }
    if (page.historyHasMore) {
      const historyBar = document.createElement("div");
      historyBar.className = "chat-history-more";
      const historyButton = document.createElement("button");
      historyButton.type = "button";
      historyButton.className = "button compact ghost";
      historyButton.textContent = "Загрузить ещё 50 сообщений";
      historyButton.addEventListener("click", loadOlderMessages);
      historyBar.append(historyButton);
      nodes.push(historyBar);
    }
    messages.forEach((message) => {
      const key = JSON.stringify(message) + page.selectedForForward.has(message.id) + ':' + messageIsBookmarked(page.selectedChatId, message.id);
      const existing = oldRows.get(String(message.id));
      if (existing && (existing.dataset.renderKey === key || (!message.deleted && [...existing.querySelectorAll("audio,video")].some(media => !media.paused && !media.ended)))) {
        nodes.push(existing);
        if (existing.dataset.renderKey !== key) page.lastMessageSignature = "";
        return;
      }
      const row = document.createElement("div");
      row.className = `chat-message-row ${message.from_me ? "outgoing" : "incoming"}${page.selectedForForward.has(message.id) ? " selected-forward" : ""}`;
      row.dataset.messageId = message.id;
      row.dataset.renderKey = key;
      if (message.highlight_mention) row.classList.add("mentioned-message");
      const bubble = document.createElement("div");
      bubble.className = "chat-bubble";
      let messageParticipant = null;
      if (!message.from_me && page.selectedChatId.endsWith("@g.us")) {
        const senderId = String(message.sender_id || "").trim();
        messageParticipant = page.participants.find((item) =>
          (senderId && [item.mention_id, item.resolved_id].map((value) => String(value || "").trim()).includes(senderId))
        ) || null;
        const senderAvatar = document.createElement("span");
        senderAvatar.className = "chat-message-author-avatar";
        senderAvatar.textContent = page.avatarLetters(message.sender, senderId);
        const avatarUrl = String(message.sender_avatar_url || (messageParticipant && messageParticipant.avatar_url) || "").trim();
        if (avatarUrl) {
          const img = document.createElement("img");
          img.src = avatarUrl;
          img.alt = "";
          img.loading = "lazy";
          img.addEventListener("error", () => img.remove());
          senderAvatar.append(img);
        }
        row.append(senderAvatar);
      }
      if (!message.deleted && message.id) {
        row.addEventListener("contextmenu", (event) => page.openMessageContextMenu(event, message, bubble));
      }
      if (!message.from_me && message.sender) {
        const sender = document.createElement("strong");
        sender.className = "chat-message-sender";
        sender.textContent = message.sender;
        bubble.append(sender);
        if (page.selectedChatId.endsWith("@g.us")) {
          const participant = messageParticipant || page.participants.find((item) => {
            const senderId = String(message.sender_id || "").trim();
            return (senderId && [item.mention_id, item.resolved_id].map((value) => String(value || "").trim()).includes(senderId));
          });
          const phoneValue = String(message.sender_phone || (participant && participant.phone) || "").trim();
          if (phoneValue) {
            const phoneLine = document.createElement("small");
            phoneLine.className = "chat-message-sender-phone";
            phoneLine.textContent = phoneValue;
            bubble.append(phoneLine);
          }
        }
      }
      if (message.forwarded && !message.deleted) {
        const forwarded = document.createElement("small");
        forwarded.className = "chat-forwarded-label";
        forwarded.textContent = "Переслано";
        bubble.append(forwarded);
      }
      if (message.quoted_message_key && !message.deleted) {
        const quote = document.createElement("div");
        quote.className = "chat-quote";
        const qSender = document.createElement("strong");
        qSender.textContent = message.quoted_sender || "Сообщение";
        const qBody = document.createElement("span");
        qBody.textContent = message.quoted_body || "Вложение";
        if (message.quote_preview_url) {
          const thumb = document.createElement("img");
          thumb.className = "quote-thumbnail"; thumb.alt = "Фото"; thumb.src = message.quote_preview_url;
          thumb.loading = "lazy"; thumb.addEventListener("error", () => thumb.remove()); quote.append(thumb);
        }
        quote.setAttribute("role", "button"); quote.tabIndex = 0;
        quote.title = "Перейти к сообщению";
        const jump = () => {
          if (message.quote_unavailable) { qBody.textContent = "Сообщение недоступно"; return; }
          const targetId = String(message.quoted_message_key || "").trim();
          const quotedBody = String(message.quoted_body || "").trim();
          const quotedSender = String(message.quoted_sender || "").trim();
          if (!targetId && !quotedBody) return;

          const candidate = findQuotedMessageCandidate(message, targetId, quotedBody, quotedSender);
          if (candidate && focusMessageRow(String(candidate.id || ""), true)) {
            const realId = String(candidate.id || "");
            page.conversationPage.dataset.jumpLoaded = realId;
            page.conversationPage.dataset.jumpResolved = realId;
            page.conversationPage.dataset.jumpScrolled = realId;
            const cleanUrl = new URL(window.location.href);
            ["message_id", "quoted_body", "quoted_sender", "before_ts"].forEach((key) => cleanUrl.searchParams.delete(key));
            window.history.replaceState({}, "", cleanUrl);
            return;
          }

          const url = new URL(window.location.href);
          if (targetId) url.searchParams.set("message_id", targetId); else url.searchParams.delete("message_id");
          if (quotedBody) url.searchParams.set("quoted_body", quotedBody); else url.searchParams.delete("quoted_body");
          if (quotedSender) url.searchParams.set("quoted_sender", quotedSender); else url.searchParams.delete("quoted_sender");
          if (message.timestamp) url.searchParams.set("before_ts", String(message.timestamp)); else url.searchParams.delete("before_ts");
          window.history.replaceState({}, "", url);
          delete page.conversationPage.dataset.jumpLoaded;
          delete page.conversationPage.dataset.jumpResolved;
          delete page.conversationPage.dataset.jumpScrolled;
          page.loadChatState(true);
        };
        quote.addEventListener("click", jump);
        quote.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") {event.preventDefault(); jump();} });
        quote.append(qSender, qBody);
        bubble.append(quote);
      }
      if (message.deleted) {
        const deleted = document.createElement("span");
        deleted.className = "chat-message-deleted";
        deleted.textContent = "Сообщение удалено";
        bubble.append(deleted);
      } else {
        appendMedia(bubble, message);
        const newMedia = bubble.querySelector("audio,video");
        const previousMedia = existing?.querySelector("audio,video");
        if (newMedia && previousMedia && newMedia.src === previousMedia.src) {
          newMedia.replaceWith(previousMedia.closest(".queue-player") || previousMedia);
        }
        const bodyText = String(message.body || "").trim();
        const genericMediaLabel = /^\[(?:Фото|Видео|Аудио|Голосовое сообщение|Документ|Вложение)\]$/.test(bodyText);
        if (bodyText && !(message.media_url && genericMediaLabel)) {
          const body = document.createElement("span");
          body.className = "chat-message-body";
          appendMessageText(body, bodyText, message);
          // QUEUE_LONG_MESSAGE_3_3_61: keep the full text in DOM, but collapse very large blocks by default.
          const longMessage = bodyText.length > 1800 || bodyText.split(/\r?\n/).length > 24;
          const messageKey = String(message.id || `${message.timestamp}:${bodyText.slice(0,80)}`);
          if (longMessage && !page.expandedLongMessages.has(messageKey)) body.classList.add("queue-long-message-collapsed");
          bubble.append(body);
          if (longMessage) {
            const toggleLong = document.createElement("button");
            toggleLong.type = "button";
            toggleLong.className = "queue-long-message-toggle";
            const syncLongState = () => {
              const expanded = page.expandedLongMessages.has(messageKey);
              body.classList.toggle("queue-long-message-collapsed", !expanded);
              toggleLong.textContent = expanded ? "Свернуть" : "Показать полностью";
            };
            toggleLong.addEventListener("click", () => {
              if (page.expandedLongMessages.has(messageKey)) page.expandedLongMessages.delete(messageKey);
              else page.expandedLongMessages.add(messageKey);
              syncLongState();
            });
            syncLongState();
            bubble.append(toggleLong);
          }
        } else if (!message.media_url && !bodyText) {
          const body = document.createElement("span");
          body.textContent = "[Сообщение]";
          bubble.append(body);
        }
      }
      // Voice transcription is not shown in the chat UI.
      if (!message.deleted && Array.isArray(message.reactions) && message.reactions.length) {
        const reactionBar = document.createElement("div");
        reactionBar.className = "chat-reactions";
        message.reactions.forEach((reaction) => {
          if (!reaction || !reaction.emoji || Number(reaction.count || 0) <= 0) return;
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = `chat-reaction-chip${reaction.me ? " mine" : ""}`;
          chip.textContent = `${reaction.emoji}${Number(reaction.count || 0) > 1 ? ` ${reaction.count}` : ""}`;
          chip.title = reaction.me ? "Нажмите, чтобы убрать свою реакцию" : `Поставить ${reaction.emoji}`;
          chip.addEventListener("click", () => page.queueMessageAction(message, "react", reaction.me ? "" : reaction.emoji));
          reactionBar.append(chip);
        });
        if (reactionBar.childElementCount) bubble.append(reactionBar);
      }
      const meta = document.createElement("span");
      meta.className = "chat-message-meta";
      if (message.edited && !message.deleted) {
        const edited = document.createElement("span");
        edited.textContent = "изменено";
        meta.append(edited);
      }
      if (message.from_me) {
        const delivery = document.createElement("span");
        delivery.className = "chat-delivery";
        const ack = Number(message.ack || 0);
        delivery.textContent = ack >= 3 ? "Прочитано" : ack >= 2 ? "Доставлено" : ack >= 1 ? "Отправлено" : "Отправляется";
        meta.append(delivery);
      }
      const time = document.createElement("time");
      time.textContent = page.formatChatTime(message.timestamp);
      meta.append(time);
      bubble.append(meta);

      if (!message.deleted && message.id) {
        const actions = document.createElement("span");
        actions.className = "chat-message-actions";
        const reaction = document.createElement("button");
        reaction.type = "button";
        reaction.className = "chat-action-reaction";
        reaction.textContent = "Реакция";
        reaction.setAttribute("aria-expanded", "false");
        reaction.addEventListener("click", (event) => {
          event.stopPropagation();
          page.openReactionPicker(message, bubble, reaction);
        });
        const reply = document.createElement("button");
        reply.type = "button";
        reply.textContent = "Ответить";
        reply.addEventListener("click", () => page.startReply(message));
        const forward = document.createElement("button");
        forward.type = "button";
        forward.textContent = page.selectedForForward.has(message.id) ? "Выбрано ✓" : "Переслать";
        const forwardBlocked = isMentionOnlyMessage(message) && !page.selectedForForward.has(message.id);
        forward.disabled = forwardBlocked;
        if (forwardBlocked) forward.title = "Нельзя пересылать сообщение, состоящее только из тега пользователя";
        forward.addEventListener("click", () => page.toggleForward(message));
        const bookmark = document.createElement('button');
        bookmark.type = 'button'; bookmark.className = 'message-bookmark';
        bookmark.textContent = messageIsBookmarked(page.selectedChatId, message.id) ? '★ В избранном' : '☆ Избранное';
        bookmark.title = bookmark.textContent;
        bookmark.addEventListener('click', () => toggleMessageBookmark(message).catch((err) => page.queueAlert(err.message)));
        actions.append(reaction, reply, bookmark, forward);
        if (message.from_me) {
          const edit = document.createElement("button");
          edit.type = "button";
          edit.textContent = "Изменить";
          edit.addEventListener("click", () => page.queueMessageAction(message, "edit"));
          const remove = document.createElement("button");
          remove.type = "button";
          remove.textContent = "Удалить";
          remove.addEventListener("click", () => page.queueMessageAction(message, "delete"));
          actions.append(edit, remove);
        }
        bubble.append(actions);
      }
      row.append(bubble);
      nodes.push(row);
    });
    const keep = new Set(nodes);
    [...page.chatMessages.children].forEach(node => { if (!keep.has(node)) node.remove(); });
    nodes.forEach((node, index) => {
      if (page.chatMessages.children[index] !== node) page.chatMessages.insertBefore(node, page.chatMessages.children[index] || null);
    });
    window.QueueUI?.enhance(page.chatMessages);
    if (preserveScroll) {
      // Older history was inserted above the current viewport. Keep the same
      // visible messages in place instead of moving the reader.
      page.chatMessages.scrollTop = Math.max(0, page.chatMessages.scrollHeight - oldHeight + oldTop);
    } else if (wasNearBottom) {
      // The employee was following the latest messages, so continue doing so.
      page.chatMessages.scrollTop = page.chatMessages.scrollHeight;
    } else {
      // Background polling may update ack/status, quotes, names or add new
      // messages. Preserve the user's reading position in both personal chats
      // and groups instead of jumping to the bottom.
      const maxTop = Math.max(0, page.chatMessages.scrollHeight - page.chatMessages.clientHeight);
      page.chatMessages.scrollTop = Math.min(currentTop, maxTop);
      const restoredAnchor = anchor && [...page.chatMessages.querySelectorAll("[data-message-id]")].find(row => row.dataset.messageId === anchor.dataset.messageId);
      if (restoredAnchor) page.chatMessages.scrollTop += restoredAnchor.getBoundingClientRect().top - page.chatMessages.getBoundingClientRect().top - anchorOffset;
    }
  }

  return {
    findQuotedMessageCandidate, focusMessageRow, isMentionOnlyMessage, mergedConversationMessages,
    messageIsBookmarked, messageKeysMatch, quoteBodiesMatch, refreshMessageBookmarks, renderMessages,
    showMessageBookmarks, toggleMessageBookmark,
  };
};
