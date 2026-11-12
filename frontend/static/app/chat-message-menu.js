// Меню сообщения в чате и выбор реакции.
window.QueueChatMessageMenu = (page) => {
  function myReaction(message) {
    const rows = Array.isArray(message && message.reactions) ? message.reactions : [];
    const mine = rows.find((item) => item && item.me);
    return mine ? String(mine.emoji || "") : "";
  }

  let activeMessageContextMenu = null;

  function closeMessageContextMenu() {
    if (activeMessageContextMenu && activeMessageContextMenu.isConnected) activeMessageContextMenu.remove();
    activeMessageContextMenu = null;
  }

  function closeReactionPickers() {
    document.querySelectorAll(".message-reaction-picker").forEach((picker) => {
      const triggerId = picker.dataset.triggerId || "";
      if (triggerId) document.getElementById(triggerId)?.setAttribute("aria-expanded", "false");
      picker.remove();
    });
  }

  function clampFloatingPanel(panel, left, top, margin = 10) {
    if (!panel) return;
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    panel.style.left = `${Math.max(margin, Number(left) || margin)}px`;
    panel.style.top = `${Math.max(margin, Number(top) || margin)}px`;
    const adjust = () => {
      if (!panel.isConnected) return;
      const rect = panel.getBoundingClientRect();
      let dx = 0, dy = 0;
      if (rect.left < margin) dx = margin - rect.left;
      else if (rect.right > window.innerWidth - margin) dx = (window.innerWidth - margin) - rect.right;
      if (rect.top < margin) dy = margin - rect.top;
      else if (rect.bottom > window.innerHeight - margin) dy = (window.innerHeight - margin) - rect.bottom;
      if (!dx && !dy) return;
      const scaleX = rect.width > 0 && panel.offsetWidth > 0 ? rect.width / panel.offsetWidth : 1;
      const scaleY = rect.height > 0 && panel.offsetHeight > 0 ? rect.height / panel.offsetHeight : scaleX;
      const currentLeft = Number.parseFloat(panel.style.left) || 0;
      const currentTop = Number.parseFloat(panel.style.top) || 0;
      panel.style.left = `${currentLeft + dx / (scaleX || 1)}px`;
      panel.style.top = `${currentTop + dy / (scaleY || 1)}px`;
    };
    adjust();
    requestAnimationFrame(adjust);
  }

  function openReactionPicker(message, bubble, button, pointer = null) {
    const anchorRect = button?.getBoundingClientRect ? button.getBoundingClientRect() : bubble?.getBoundingClientRect();
    closeReactionPickers();
    closeMessageContextMenu();
    const picker = document.createElement("div");
    picker.className = "message-reaction-picker";
    picker.setAttribute("role", "menu");
    const triggerId = `reaction-trigger-${String(message?.id || "message").replace(/[^a-zA-Z0-9_-]/g, "-")}-${Date.now()}`;
    if (button) {
      button.id = button.id || triggerId;
      picker.dataset.triggerId = button.id;
      button.setAttribute("aria-expanded", "true");
    }
    const current = myReaction(message);
    page.reactionValues.forEach((emoji) => {
      const option = document.createElement("button");
      option.type = "button";
      option.textContent = emoji;
      option.title = emoji === current ? "Убрать реакцию" : `Реакция ${emoji}`;
      option.setAttribute("role", "menuitem");
      if (emoji === current) option.classList.add("selected");
      option.addEventListener("click", async (event) => {
        event.stopPropagation();
        picker.remove();
        if (button) button.setAttribute("aria-expanded", "false");
        await page.queueMessageAction(message, "react", emoji === current ? "" : emoji);
      });
      picker.append(option);
    });
    document.body.append(picker);

    // Position in the viewport instead of inside the message bubble. This keeps
    // the picker visible for the first/last message and prevents clipping by
    // the scrollable chat container.
    const margin = 10;
    const pickerRect = picker.getBoundingClientRect();
    const chatRect = page.chatMessages?.getBoundingClientRect ? page.chatMessages.getBoundingClientRect() : null;
    const safeTop = chatRect ? Math.max(margin, chatRect.top + margin) : margin;
    const safeBottom = chatRect ? Math.min(window.innerHeight - margin, chatRect.bottom - margin) : window.innerHeight - margin;
    let left;
    let top;
    if (pointer && Number.isFinite(pointer.clientX) && Number.isFinite(pointer.clientY)) {
      left = pointer.clientX;
      top = pointer.clientY + 8;
    } else if (anchorRect) {
      left = message?.from_me ? anchorRect.right - pickerRect.width : anchorRect.left;
      const roomAbove = anchorRect.top - safeTop;
      const roomBelow = safeBottom - anchorRect.bottom;
      const messageRows = page.chatMessages ? [...page.chatMessages.querySelectorAll("[data-message-id]")] : [];
      const isFirstMessage = Boolean(bubble?.parentElement && messageRows[0] === bubble.parentElement);
      // The first message should open downward like WhatsApp instead of
      // covering the chat header or being visually clipped at the top.
      if (isFirstMessage && roomBelow > 56) top = anchorRect.bottom + 8;
      else if (roomAbove >= pickerRect.height || roomAbove > roomBelow) top = anchorRect.top - pickerRect.height - 8;
      else top = anchorRect.bottom + 8;
    } else {
      left = margin;
      top = safeTop;
    }
    clampFloatingPanel(picker, left, top, margin);
    if (chatRect && chatRect.height >= pickerRect.height + margin * 2) {
      const minTop = Math.max(margin, chatRect.top + margin);
      const maxTop = Math.min(window.innerHeight - pickerRect.height - margin, chatRect.bottom - pickerRect.height - margin);
      const currentTop = Number.parseFloat(picker.style.top || String(top));
      picker.style.top = `${Math.min(Math.max(minTop, currentTop), Math.max(minTop, maxTop))}px`;
    }

    window.setTimeout(() => {
      const close = (event) => {
        if (picker.isConnected && !picker.contains(event.target) && event.target !== button) {
          picker.remove();
          if (button) button.setAttribute("aria-expanded", "false");
        }
        document.removeEventListener("click", close, true);
      };
      document.addEventListener("click", close, true);
    }, 0);
  }

  function openMessageContextMenu(event, message, bubble) {
    if (!message || !message.id || message.deleted) return;
    event.preventDefault();
    event.stopPropagation();
    closeReactionPickers();
    closeMessageContextMenu();

    const menu = document.createElement("div");
    menu.className = "message-context-menu";
    menu.setAttribute("role", "menu");
    activeMessageContextMenu = menu;

    const addItem = (label, icon, action, danger = false, disabled = false) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = `message-context-item${danger ? " danger" : ""}`;
      item.setAttribute("role", "menuitem");
      item.disabled = Boolean(disabled);
      const iconEl = document.createElement("span");
      iconEl.className = "message-context-icon";
      iconEl.textContent = icon;
      const labelEl = document.createElement("span");
      labelEl.textContent = label;
      item.append(iconEl, labelEl);
      item.addEventListener("click", (clickEvent) => {
        clickEvent.stopPropagation();
        action(item, clickEvent);
      });
      menu.append(item);
      return item;
    };

    addItem("Реакция", "☺", (item) => openReactionPicker(message, bubble, item));
    addItem("Ответить", "↩", () => { closeMessageContextMenu(); page.startReply(message); });
    addItem(page.messageIsBookmarked(page.selectedChatId, message.id) ? "Убрать из избранного" : "В избранное", "★",
      () => { closeMessageContextMenu(); page.toggleMessageBookmark(message).catch((err) => page.queueAlert(err.message)); });
    const forwardBlocked = page.isMentionOnlyMessage(message) && !page.selectedForForward.has(message.id);
    const forwardItem = addItem(
      page.selectedForForward.has(message.id) ? "Убрать из пересылки" : "Переслать",
      "↪",
      () => { closeMessageContextMenu(); page.toggleForward(message); },
      false,
      forwardBlocked,
    );
    if (forwardBlocked) forwardItem.title = "Нельзя пересылать сообщение, состоящее только из тега пользователя";
    if (message.from_me) {
      const separator = document.createElement("div");
      separator.className = "message-context-separator";
      menu.append(separator);
      addItem("Изменить", "✎", () => { closeMessageContextMenu(); page.queueMessageAction(message, "edit"); });
      addItem("Удалить", "⌫", () => { closeMessageContextMenu(); page.queueMessageAction(message, "delete"); }, true);
    }

    document.body.append(menu);
    clampFloatingPanel(menu, event.clientX + 2, event.clientY + 2, 8);
    menu.querySelector("button")?.focus({preventScroll: true});

    window.setTimeout(() => {
      const close = (clickEvent) => {
        if (menu.isConnected && !menu.contains(clickEvent.target)) closeMessageContextMenu();
        document.removeEventListener("click", close, true);
      };
      document.addEventListener("click", close, true);
    }, 0);
  }

  return {
    clampFloatingPanel, openMessageContextMenu, openReactionPicker,
  };
};
