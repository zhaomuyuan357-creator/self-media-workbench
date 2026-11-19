import { bindActions } from "../core/dom.js";

const bindings = [
  ...["#postTitle", "#postBody", "#postTags"].map((selector) => ["input", selector, "updatePreview"]),
  ["click", "#trendScanButton", "scanHotspots"], ["keydown", "#trendQuery", "trendQueryKeydown"],
  ["click", "#generateButton", "generateCopy"], ["click", "#generateImageButton", "generateNoteImage"],
  ["focus", "#imagePrompt", "imagePromptFocus"], ["click", "#publishButton", "openPublishDialog"],
];

export function mount(container, { controller }) {
  return bindActions(container, bindings, controller);
}

export function unmount() {}
