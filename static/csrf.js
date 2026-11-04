// Adds the signed-in user's CSRF token (<meta name="queue-csrf">) to every
// same-origin fetch() that changes data. Loaded before all other scripts.
(() => {
  "use strict";
  const nativeFetch = window.fetch.bind(window);
  const token = () => document.querySelector('meta[name="queue-csrf"]')?.content || "";
  window.fetch = (input, init = {}) => {
    const request = input instanceof Request ? input : null;
    const method = String(init.method || request?.method || "GET").toUpperCase();
    const url = new URL(request ? request.url : String(input), location.href);
    if (method !== "GET" && method !== "HEAD" && url.origin === location.origin) {
      const headers = new Headers(init.headers || request?.headers || undefined);
      if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", token());
      init = {...init, headers};
    }
    return nativeFetch(input, init);
  };
})();
