import { bindActions } from "../core/dom.js";

const bindings = [
  ["click", "#saveConnectionButton", "saveConnectionSettings"], ["click", "#checkConnectionButton", "checkConnection"],
  ["change", "#publisherMode", "publisherModeChanged"], ["click", "#saveGeneratorButton", "saveGeneratorSettings"],
  ["click", "#addGeneratorProfileButton", "addGeneratorProfile"], ["click", "#moveGeneratorProfileUpButton", "moveGeneratorProfileUp"],
  ["click", "#moveGeneratorProfileDownButton", "moveGeneratorProfileDown"], ["click", "#deleteGeneratorProfileButton", "deleteGeneratorProfile"],
  ["change", "#generatorProvider", "applyGeneratorProviderPreset"], ["input", "#generatorBaseUrl", "generatorBaseUrlInput"],
  ["input", "#generatorProfileName", "syncGeneratorProfileForm"], ["input", "#generatorModel", "syncGeneratorProfileForm"],
  ["change", "#generatorProtocol", "syncGeneratorProfileForm"], ["change", "#generatorAutoFallback", "generatorAutoFallback"],
  ["click", "#testGeneratorButton", "testGenerator"], ["click", "#saveToapisSettingsButton", "saveToapisSettings"],
  ["click", "#checkToapisButton", "checkToapis"], ["click", "#toapisLoadModelsButton", "loadToapisModels"],
  ["click", "#toapisLoadDocsButton", "loadToapisDocs"], ["click", "#toapisGenerateButton", "submitToapisGeneration"],
  ["click", "#toapisRetryDownloadButton", "retryToapisDownload"], ["change", "#toapisImageModel", "toapisImageModelChanged"],
  ["click", "#loadToapisImageModelsButton", "loadToapisImageModels"], ["click", "#saveToapisImageModelButton", "saveToapisImageModel"],
  ["click", "#retryNoteImageDownloadButton", "retryToapisDownload"],
];

export function mount(container, { controller }) {
  return bindActions(container, bindings, controller);
}

export function unmount() {}
