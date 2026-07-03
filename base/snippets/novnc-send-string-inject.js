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
 *    It can also postMessage({ type: "noVNC_toggleKeyboard" }) to click
 *    noVNC's own virtual-keyboard button (noVNC_keyboard_button), which
 *    focuses/blurs noVNC's hidden input so the OS soft keyboard shows/hides
 *    (mobile "Mostrar/ocultar teclado" action).
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
    if (!data) return;
    // Toggle noVNC's virtual keyboard (opens/closes the OS soft keyboard).
    if (data.type === "noVNC_toggleKeyboard") {
      var kb = document.getElementById("noVNC_keyboard_button");
      if (kb) {
        kb.click();
      }
      return;
    }
    if (
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
  var coarse =
    !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  var touch =
    ("ontouchstart" in window) || navigator.maxTouchPoints > 0;
  if (!(coarse || touch)) return;

  var tries = 0;
  var stableSelected = 0;
  var iv = setInterval(function () {
    tries += 1;
    var resize = document.getElementById("noVNC_setting_resize");
    var clip = document.getElementById("noVNC_setting_view_clip");
    var drag = document.getElementById("noVNC_view_drag_button");
    
    // a) Local Scaling -> None.
    if (resize && resize.value !== "off") {
      resize.value = "off";
      resize.dispatchEvent(new Event("change", { bubbles: true }));
    }
    // b) Best-effort: also tick the clip checkbox if it's enabled (on mobile
    //    noVNC already force-clips internally WITHOUT ticking it, so we must
    //    NOT gate the drag on this). Nudge a resize so clippingViewport recomputes.
    if (clip && !clip.disabled && !clip.checked) {
      clip.checked = true;
      clip.dispatchEvent(new Event("change", { bubbles: true }));
      window.dispatchEvent(new Event("resize"));
    }
    // c) Enable pan/drag. The button is in the DOM from the start but hidden
    //    for the first seconds (noVNC reveals it once it decides the session
    //    is touch/clipped) — clicking it while hidden does nothing, which is
    //    why auto-activation failed. Only click once it is actually VISIBLE
    //    (offsetParent != null), and keep re-clicking until it stays selected.
    // The drag button's own VISIBILITY is the connection signal — noVNC only
    // reveals it once the session is connected+clipped. (There is NO element
    // with id noVNC_canvas in the DOM — the canvas is created without an id —
    // so any "connected" check on it never passes; that bug kept the click
    // from ever firing.)
    if (drag && drag.offsetParent !== null) {
      if (drag.classList.contains("noVNC_selected")) {
        stableSelected += 1;
      } else {
        stableSelected = 0;
        drag.click();
      }
    }

    if (tries > 90 || stableSelected >= 3) clearInterval(iv);
  }, 400);
})();
