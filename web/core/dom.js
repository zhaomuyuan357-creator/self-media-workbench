export const $ = (selector, root = document) => root.querySelector(selector);

export function bindActions(container, bindings, actions) {
  const cleanups = [];
  for (const [event, selector, action] of bindings) {
    const node = $(selector, container);
    const handler = typeof action === "function" ? action : actions[action];
    if (!node || !handler) continue;
    node.addEventListener(event, handler);
    cleanups.push(() => node.removeEventListener(event, handler));
  }
  return () => cleanups.forEach((cleanup) => cleanup());
}

let toastTimer = null;

export function toast(message, error = false) {
  const node = $("#toast");
  if (!node) return;
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 3400);
}

export function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes > 1024 * 1024 * 100 ? 0 : 1)} MB`;
}

export function formatDate(value) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(new Date(value));
  } catch (_) { return value; }
}
