// Кнопки бота в шапке чата: перенос в меню «ещё» и его копии кнопок ручного режима и сброса бота.
window.QueueChatHeaderBot = (page) => {
  let botOverflowPanelV17 = null;
  let nameAvatarSourceV17 = null;

  const normalizeActionLabelV17 = (value) => String(value || "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase("ru-RU");

  const isVisibleV17 = (element) => {
    if (!element || !element.isConnected) return false;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity || 1) === 0) return false;
    const box = element.getBoundingClientRect();
    return box.width > 4 && box.height > 4;
  };

  function findNameAvatarSourceV17() {
    if (nameAvatarSourceV17 && nameAvatarSourceV17.isConnected) return nameAvatarSourceV17;
    const scope = page.botRestartHeaderActions || document;
    const found = [...scope.querySelectorAll("button, a, [role='button']")].find((item) => {
      if (item === page.manualModeButton || item === page.botResetButton) return false;
      if (item.dataset && item.dataset.eoOverflowProxyV19 === "1") return false;
      const label = normalizeActionLabelV17(item.textContent || item.getAttribute("aria-label") || item.title);
      return label.includes("имя") && label.includes("аватар");
    }) || null;
    if (found) nameAvatarSourceV17 = found;
    return found;
  }

  function parkHeaderBotControlsV17() {
    findNameAvatarSourceV17();
    // "hidden" is CSP-safe; unlike element.style it does not create an
    // inline style declaration.
    for (const button of [page.manualModeButton, page.botResetButton, nameAvatarSourceV17]) {
      if (!button) continue;
      button.classList.remove("toolbar-overflow-source", "bot-reload-visible");
      button.hidden = true;
    }
  }

  function panelLabelScoreV18(element) {
    const text = normalizeActionLabelV17(element && element.textContent);
    if (!text) return 0;
    const labels = ["теги / заметка", "экспорт", "обращения", "заявка"];
    return labels.reduce((score, label) => score + (text.includes(label) ? 1 : 0), 0);
  }

  function findExistingOverflowPanelV17() {
    const selectors = [
      "[role='menu']", "menu", "nav", "aside", "section", "details", "div"
    ].join(",");
    const candidates = [];
    for (const node of document.querySelectorAll(selectors)) {
      if (!isVisibleV17(node)) continue;
      const score = panelLabelScoreV18(node);
      if (score < 3) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width < 120 || rect.width > 560 || rect.height < 80 || rect.height > 900) continue;
      candidates.push({node, score, area: rect.width * rect.height});
    }
    candidates.sort((a,b) => (b.score - a.score) || (a.area - b.area));
    return candidates[0]?.node || null;
  }

  function menuButtonClassV19(panel) {
    const sample = [...panel.querySelectorAll("button, [role='button'], a")].find((item) =>
      item !== page.manualModeButton &&
      item !== page.botResetButton &&
      item !== nameAvatarSourceV17 &&
      item.dataset?.eoOverflowProxyV19 !== "1"
    );
    return sample && sample.className ? String(sample.className) : "button compact";
  }

  function removeDuplicateBotRowsV19(panel) {
    const duplicateLabels = new Set([
      "выключить автоответчик",
      "включить автоответчик",
      "отключить автоответы",
      "включить автоответы",
      "⏸ отключить автоответы",
      "▶ включить автоответы",
      "↻ перезагрузить бота",
      "перезагрузить бота",
      "↻ имя / аватар",
      "имя / аватар",
    ]);
    [...panel.querySelectorAll("button, a, [role='button']")].forEach((item) => {
      if (item.dataset?.eoOverflowProxyV19 === "1") return;
      const label = normalizeActionLabelV17(item.textContent || item.getAttribute("aria-label") || item.title);
      if (duplicateLabels.has(label)) item.remove();
    });
  }

  function createProxyV19(panel, key, label, source) {
    if (!panel || !source) return null;
    let proxy = panel.querySelector(`[data-eo-overflow-proxy-v19="${key}"]`);
    if (!proxy) {
      proxy = document.createElement("button");
      proxy.type = "button";
      proxy.dataset.eoOverflowProxyV19 = key;
      proxy.className = menuButtonClassV19(panel);
      proxy.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();

        const activeSource =
          key === "manual" ? page.manualModeButton :
          key === "reset" ? page.botResetButton :
          findNameAvatarSourceV17();

        if (!activeSource) {
          window.QueueUI?.toast?.("Действие пока недоступно");
          return;
        }
        if (activeSource.disabled) {
          window.QueueUI?.toast?.("Сначала выберите пользователя");
          return;
        }

        // HTMLElement.click() invokes the already-tested v15 handler even
        // when the source control itself is hidden.
        activeSource.click();

        // Close the current overflow popover after the action.
        const moreButton = [...document.querySelectorAll("button, [role='button'], summary")].find((item) => {
          const text = normalizeActionLabelV17(item.textContent || item.getAttribute("aria-label") || item.title);
          return text === "..." || text === "…";
        });
        if (moreButton) window.setTimeout(() => moreButton.click(), 0);
      });
      panel.append(proxy);
    }
    proxy.textContent = label;
    proxy.disabled = !page.selectedChatId || Boolean(source.disabled);
    return proxy;
  }

  function syncOverflowBotActionsV17() {
    // Remove old broken v17 proxies and any stale v19 proxies which ended up
    // outside the currently visible panel.
    document.querySelectorAll("[data-eo-overflow-proxy-v17]").forEach((item) => item.remove());

    const panel = findExistingOverflowPanelV17();
    if (!panel) {
      parkHeaderBotControlsV17();
      return false;
    }
    botOverflowPanelV17 = panel;

    const allowed = Boolean(page.selectedChatId) &&
      !String(page.selectedChatId).endsWith("@g.us") &&
      !page.botManualContactV17;

    if (!allowed) {
      panel.querySelectorAll("[data-eo-overflow-proxy-v19]").forEach((item) => item.remove());
      parkHeaderBotControlsV17();
      return true;
    }

    parkHeaderBotControlsV17();
    removeDuplicateBotRowsV19(panel);

    createProxyV19(
      panel,
      "manual",
      page.manualModeActive ? "▶ Включить автоответы" : "⏸ Отключить автоответы",
      page.manualModeButton
    );
    createProxyV19(panel, "reset", "↻ Перезагрузить бота", page.botResetButton);

    const avatarAction = findNameAvatarSourceV17();
    if (avatarAction) createProxyV19(panel, "avatar", "↻ Имя / аватар", avatarAction);

    return true;
  }

  function scheduleOverflowSyncV17() {
    [0, 25, 70, 150, 300, 600].forEach((delay) => window.setTimeout(syncOverflowBotActionsV17, delay));
  }

  return {
    findExistingOverflowPanelV17, normalizeActionLabelV17, parkHeaderBotControlsV17,
    scheduleOverflowSyncV17, syncOverflowBotActionsV17,
  };
};
