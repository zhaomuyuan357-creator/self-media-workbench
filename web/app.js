import * as account from "./views/account.js";
import * as content from "./views/content.js";
import * as monitor from "./views/monitor.js";
import * as records from "./views/records.js";
import * as settings from "./views/settings.js";
import { api } from "./core/api.js";
import { initialize, mountPage, viewController } from "./core/controller.js";
import { poller } from "./core/poller.js";
import { store } from "./core/state.js";

const views = { account, content, monitor, records, settings };

initialize({
  mount(page) {
    const container = document.querySelector(`#page-${page}`);
    const view = views[page];
    if (!container || !view) return () => {};
    const cleanup = view.mount(container, { store, api, poller, controller: viewController });
    return () => {
      cleanup?.();
      view.unmount();
    };
  },
});

export { mountPage };
