/**
 * noVNC "send text to host" injectable script.
 * Injected into PVE/noVNC HTML by nginx sub_filter so the console page (www.neuravps.com)
 * can postMessage({ type: "noVNC_sendString", text }) and have it typed into the VNC session.
 *
 * Deploy: use the minified one-liner in nginx sub_filter (see docs/pve-proxy-base-server-setup.md
 * section "noVNC send-text injection"). This file is the readable source.
 */
(function () {
  "use strict";

  var DELAY_MS = 15;
  var ALLOWED_ORIGINS = [
    "https://www.neuravps.com",
    "https://neuravps.com",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
  ];

  function sendString(text) {
    var el = document.getElementById("noVNC_keyboardinput");
    if (!el || typeof text !== "string") return;
    var i = 0;
    text.split("").forEach(function (x) {
      var t = i * DELAY_MS;
      i += 1;
      setTimeout(function () {
        var needsShift = /[A-Z!@#\x24%^&*()_+{}:"<>?~|]/.test(x);
        var evt;
        if (needsShift) {
          evt = new KeyboardEvent("keydown", { keyCode: 16, key: "Shift", code: "ShiftLeft" });
          el.dispatchEvent(evt);
          evt = new KeyboardEvent("keydown", { key: x, shiftKey: true });
          el.dispatchEvent(evt);
          evt = new KeyboardEvent("keyup", { key: x, shiftKey: true });
          el.dispatchEvent(evt);
          evt = new KeyboardEvent("keyup", { keyCode: 16, key: "Shift", code: "ShiftLeft" });
          el.dispatchEvent(evt);
        } else {
          evt = new KeyboardEvent("keydown", { key: x });
          el.dispatchEvent(evt);
          evt = new KeyboardEvent("keyup", { key: x });
          el.dispatchEvent(evt);
        }
      }, t);
    });
  }

  window.addEventListener("message", function (event) {
    if (ALLOWED_ORIGINS.indexOf(event.origin) === -1) return;
    var data = event.data;
    if (data && data.type === "noVNC_sendString" && typeof data.text === "string") {
      sendString(data.text);
    }
  });
})();
