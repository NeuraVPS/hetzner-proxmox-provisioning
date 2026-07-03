/**
 * noVNC injectable script (two features).
 * Injected into PVE/noVNC HTML by nginx sub_filter (same origin as noVNC, so no
 * cross-origin limits). This file is the readable SOURCE; the deployed form is
 * the minified one-liner in snippets/neuravps-redirects.conf (BASE nginx) /
 * base_setup.sh PVE proxy section — keep them in sync.
 *
 * 1) "dispatch event": the console page (www.neuravps.com) can
 *    postMessage({ type: "noVNC_dispatchEvent", event }) to dispatch one
 *    keyboard event on noVNC_keyboardinput (paste / send-key from the parent).
 * 2) Mobile ergonomics: pve-set-ticket builds the console URL with
 *    resize=scale ("Local Scaling"), which looks great on desktop but on a
 *    phone shrinks the whole desktop to fit — tiny and unreadable. On mobile
 *    we switch to resize=off (native 1:1, Proxmox default) AND enable the
 *    view-drag/pan button so the user can drag around the full-size screen.
 *    The two go together: none without drag = cropped and immovable.
 */
(function () {
  "use strict";

  // --- 1) keyboard dispatch bridge ---
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

  // --- 2) mobile: resize=off + clip + view-drag on ---
  // Order matters (noVNC internals): resize=off makes the desktop native 1:1,
  // which ENABLES the view_clip checkbox; view_clip=true clips the viewport,
  // which is REQUIRED for drag to stick (noVNC's updateViewDrag auto-cancels
  // dragViewport whenever clipViewport is false). So we drive them in sequence
  // via polling and only click the drag button once clip is on.
  var isMobile =
    (("ontouchstart" in window) || navigator.maxTouchPoints > 0) &&
    Math.min(screen.width, screen.height) <= 820;
  if (!isMobile) return;

  var tries = 0;
  var iv = setInterval(function () {
    tries += 1;
    var resize = document.getElementById("noVNC_setting_resize");
    var clip = document.getElementById("noVNC_setting_view_clip");
    var drag = document.getElementById("noVNC_view_drag_button");
    var connected = !!document.getElementById("noVNC_canvas");

    // a) Local Scaling -> None.
    if (resize && resize.value !== "off") {
      resize.value = "off";
      resize.dispatchEvent(new Event("change", { bubbles: true }));
    }
    // b) Clip the viewport (enabled only once resize is off).
    if (clip && !clip.disabled && !clip.checked) {
      clip.checked = true;
      clip.dispatchEvent(new Event("change", { bubbles: true }));
    }
    // c) Enable pan/drag — only after clip is on, else it auto-cancels.
    if (
      connected &&
      drag &&
      clip &&
      clip.checked &&
      !drag.classList.contains("noVNC_selected")
    ) {
      drag.click();
    }

    var done =
      connected &&
      resize &&
      resize.value === "off" &&
      drag &&
      drag.classList.contains("noVNC_selected");
    if (tries > 60 || done) clearInterval(iv);
  }, 400);
})();
