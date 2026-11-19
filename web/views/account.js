import { bindActions } from "../core/dom.js";

const bindings = [
  ["click", "#saveProfileButton", "saveProfile"], ["click", "#goContentButton", "goContent"],
  ...["#accountName", "#profileLane", "#audience", "#positioning", "#tone", "#structure", "#boundaries", "#factRules", "#topics"]
    .map((selector) => ["input", selector, "profileInput"]),
];

export function mount(container, { controller }) {
  return bindActions(container, bindings, controller);
}

export function unmount() {}
