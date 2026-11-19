import { bindActions } from "../core/dom.js";

export function mount(container, { controller }) {
  return bindActions(container, [["click", "#refreshMonitorButton", "refreshMonitor"]], controller);
}

export function unmount() {}
