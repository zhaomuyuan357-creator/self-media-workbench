const timers = new Map();

function cancelTimer(key) {
  const timer = timers.get(key);
  if (!timer) return;
  clearTimeout(timer.id);
  clearInterval(timer.id);
  timer.resolve?.(false);
  timers.delete(key);
}

export const poller = {
  timeout(key, task, delay) {
    cancelTimer(key);
    const id = setTimeout(() => {
      timers.delete(key);
      Promise.resolve().then(task);
    }, delay);
    timers.set(key, { id, type: "timeout" });
    return id;
  },
  interval(key, task, delay) {
    cancelTimer(key);
    const id = setInterval(task, delay);
    timers.set(key, { id, type: "interval" });
    return id;
  },
  delay(key, duration) {
    cancelTimer(key);
    return new Promise((resolve) => {
      const id = setTimeout(() => {
        timers.delete(key);
        resolve(true);
      }, duration);
      timers.set(key, { id, type: "timeout", resolve });
    });
  },
  stop(key) {
    cancelTimer(key);
  },
  stopAll() {
    for (const key of [...timers.keys()]) cancelTimer(key);
  },
};
