const values = {
  account: null,
  jobs: [],
  settings: {},
  monitor: [],
  generator: {},
  toapis: {},
  trendItems: [],
  generatedImages: [],
  agentReach: {},
};

const listeners = new Map();
let batching = false;
let changedKeys = new Set();
export const state = new Proxy(values, {
  set(target, key, value) {
    target[key] = value;
    if (batching) {
      changedKeys.add(String(key));
      return true;
    }
    for (const listener of listeners.get(String(key)) || []) listener(value, key);
    for (const listener of listeners.get("state") || []) listener(state, key);
    return true;
  },
});

export const runtime = {
  selectedImageIds: [],
  selectedSources: [],
  verifyJobId: null,
  currentPage: "account",
  profileDirty: false,
  connectorMessage: "",
  activeToapisGeneration: "",
  activeToapisGenerationSource: "media",
  activeToapisPolling: false,
  selectedGeneratorProfileId: "",
};

export const store = {
  getState() {
    return state;
  },
  setState(patch) {
    batching = true;
    Object.assign(state, patch);
    batching = false;
    for (const key of changedKeys) {
      for (const listener of listeners.get(key) || []) listener(state[key], key);
    }
    changedKeys.clear();
    for (const listener of listeners.get("state") || []) listener(state);
  },
  subscribe(key, listener) {
    if (typeof key === "function") {
      listener = key;
      key = "state";
    }
    const bucket = listeners.get(key) || new Set();
    bucket.add(listener);
    listeners.set(key, bucket);
    return () => bucket.delete(listener);
  },
};
