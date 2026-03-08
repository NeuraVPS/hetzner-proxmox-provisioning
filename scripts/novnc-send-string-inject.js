/**
 * noVNC "dispatch event" injectable script.
 * Injected into PVE/noVNC HTML by nginx sub_filter so the console page (www.neuravps.com)
 * can postMessage({ type: "noVNC_dispatchEvent", event }) to dispatch one keyboard event
 * on noVNC_keyboardinput. The parent builds the event list and implements delays.
 *
 * Deploy: use the minified one-liner in nginx sub_filter (see docs/pve-proxy-base-server-setup.md
 * section "noVNC send-text injection"). This file is the readable source.
 */
(function () {
  "use strict";

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (
      !data ||
      data.type !== "noVNC_dispatchEvent" ||
      !data.event ||
      typeof data.event !== "object"
    )
      return;
    var e = data.event;
    if (e.type !== "keydown" && e.type !== "keyup") return;
    var el = document.getElementById("noVNC_keyboardinput");
    if (!el) return;
    var opts = { key: e.key, keyCode: e.keyCode };
    if (e.code != null) opts.code = e.code;
    if (e.ctrlKey != null) opts.ctrlKey = !!e.ctrlKey;
    if (e.altKey != null) opts.altKey = !!e.altKey;
    if (e.shiftKey != null) opts.shiftKey = !!e.shiftKey;
    if (e.metaKey != null) opts.metaKey = !!e.metaKey;
    el.dispatchEvent(new KeyboardEvent(e.type, opts));
  });
})();
