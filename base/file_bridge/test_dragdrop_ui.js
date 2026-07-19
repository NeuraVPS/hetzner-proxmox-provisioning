// UI tests for static/index.html (drag-and-drop upload), run in jsdom.
// Stubs the bridge API; asserts veil behaviour, folder skipping, the size
// pre-check, upload sequencing and the navigate-away guard.
//
//   npm install jsdom && node test_dragdrop_ui.js       # exits non-zero on failure
//
// Deliberately standalone: this repo carries no node tooling, so there is no
// package.json to keep in sync -- jsdom is the single dependency.
const path = require("path");
const { JSDOM } = require("jsdom");

const HTML = path.join(__dirname, "static", "index.html");
const uploads = [];      // {path, name, bytes} in completion order
let listCalls = 0;

let pass = 0, fail = 0;
const ok = (name, cond, extra="") => {
  if (cond) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; console.log(`  ✗ ${name}${extra ? "  → " + extra : ""}`); }
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

function makeFetch(win) {
  return async (url, opts={}) => {
    const u = String(url);
    const json = body => ({ ok:true, status:200, json:async()=>body });
    if (u.startsWith("/api/session")) return json({ ok:true });
    if (u.startsWith("/api/servers")) return json({ servers:[{ vmid:201, name:"VPS-TEST", type:"sqx" }] });
    if (u.startsWith("/api/list"))    { listCalls++; return json({ entries:[], truncated:false }); }
    if (u.startsWith("/api/upload")) {
      const q = new URLSearchParams(u.split("?")[1]);
      const f = opts.body.get("file");
      await sleep(15);                       // make sequencing observable
      uploads.push({ path:q.get("path"), name:f.name, bytes:f.size });
      return json({ ok:true, bytes:f.size });
    }
    return json({ ok:true });
  };
}

// A DataTransfer good enough for the handler: it only reads .types, .items
// (kind/webkitGetAsEntry/getAsFile), .files and writes .dropEffect.
function dt(entries) {
  const items = entries.map(e => ({
    kind: "file",
    webkitGetAsEntry: () => ({ isFile: !e.dir, isDirectory: !!e.dir }),
    getAsFile: () => e.file || null,
  }));
  return { types:["Files"], items, files: entries.filter(e=>!e.dir).map(e=>e.file), dropEffect:"" };
}
function fire(win, type, dataTransfer) {
  const ev = new win.Event(type, { bubbles:true, cancelable:true });
  ev.dataTransfer = dataTransfer;
  win.dispatchEvent(ev);
  return ev;
}
function mkFile(win, name, size=8) {
  const f = new win.File(["x".repeat(Math.min(size, 1024))], name, { type:"application/octet-stream" });
  Object.defineProperty(f, "size", { value:size });   // fake huge files cheaply
  return f;
}
// `let STATE` / `let UPLOADING` are script-lexical bindings: they live in the
// global LEXICAL scope, never on `window`. Global eval sees them; win.X does not.
const ev_ = (win, expr) => win.eval(expr);
const waitIdle = async win => { for (let i=0;i<200 && ev_(win,"UPLOADING");i++) await sleep(10); await sleep(20); };

(async () => {
  const dom = await JSDOM.fromFile(HTML, {
    url: "https://files-fsn.neuravps.com/?lang=en#t=faketoken",
    runScripts: "dangerously",
    beforeParse(win) {
      win.matchMedia = () => ({ matches:false, addEventListener(){}, removeEventListener(){} });
      win.fetch = makeFetch(win);
      win.scrollTo = () => {};
    },
  });
  const win = dom.window, $ = s => win.document.querySelector(s);
  await sleep(120);                                   // let boot() settle

  console.log("\nboot");
  ok("session started + server list loaded", ev_(win,"STATE.vmid") === 201, `vmid=${ev_(win,"STATE.vmid")}`);
  ev_(win, "STATE.path = " + JSON.stringify("Users\\Administrator\\Desktop"));

  console.log("\noverlay");
  fire(win, "dragenter", dt([]));
  ok("veil shows on dragenter", $("#drop").classList.contains("show"));
  ok("veil names the target folder",
     $("#dropmsg").textContent.includes("C:\\Users\\Administrator\\Desktop"),
     JSON.stringify($("#dropmsg").textContent));
  fire(win, "dragenter", dt([]));                      // entering a child element
  fire(win, "dragleave", dt([]));
  ok("veil survives a child dragleave (depth counter)", $("#drop").classList.contains("show"));
  fire(win, "dragleave", dt([]));
  ok("veil hides when the drag really leaves", !$("#drop").classList.contains("show"));

  console.log("\nnavigate-away guard");
  const ev = fire(win, "drop", dt([{ file: mkFile(win, "guard.txt") }]));
  ok("drop is preventDefault()ed", ev.defaultPrevented);
  await waitIdle(win);
  uploads.length = 0;

  console.log("\nplain files");
  fire(win, "drop", dt([{ file: mkFile(win,"a.csv",10) }, { file: mkFile(win,"b.set",20) }]));
  await waitIdle(win);
  ok("both files uploaded", uploads.length === 2, JSON.stringify(uploads));
  ok("uploaded sequentially, in order", uploads[0]?.name === "a.csv" && uploads[1]?.name === "b.set");
  ok("path joined under the current folder",
     uploads[0]?.path === "Users\\Administrator\\Desktop\\a.csv", uploads[0]?.path);
  ok("veil hidden after drop", !$("#drop").classList.contains("show"));

  console.log("\nfolders (deferred to step 2)");
  uploads.length = 0;
  fire(win, "drop", dt([{ dir:true }, { file: mkFile(win,"loose.txt") }]));
  await waitIdle(win);
  ok("folder skipped, loose file still uploaded",
     uploads.length === 1 && uploads[0].name === "loose.txt", JSON.stringify(uploads));
  ok("skip warning SURVIVES the success toast (folded into it)",
     $("#toast").textContent.includes("Folders were skipped")
     && $("#toast").textContent.includes("Uploaded"), $("#toast").textContent);

  uploads.length = 0;
  fire(win, "drop", dt([{ dir:true }]));
  await waitIdle(win);
  ok("folder-only drop uploads nothing", uploads.length === 0, JSON.stringify(uploads));
  ok("folder-only drop explains why", $("#toast").textContent.includes("can't be dragged in yet"),
     $("#toast").textContent);

  console.log("\nsize pre-check (advisory; server still enforces)");
  uploads.length = 0;
  fire(win, "drop", dt([{ file: mkFile(win,"huge.bin", 21*1024**3) }, { file: mkFile(win,"small.txt",5) }]));
  await waitIdle(win);
  ok("over-limit file never hits the wire", !uploads.some(u=>u.name==="huge.bin"), JSON.stringify(uploads));
  ok("the rest of the batch still uploads", uploads.some(u=>u.name==="small.txt"));

  console.log("\nconcurrent drops");
  uploads.length = 0;
  fire(win, "drop", dt([{ file: mkFile(win,"first.txt") }]));
  fire(win, "drop", dt([{ file: mkFile(win,"second.txt") }]));   // while the first runs
  ok("tells the user an upload is running, at the moment of refusal",
     $("#toast").textContent.includes("already running"), $("#toast").textContent);
  await waitIdle(win);
  ok("second drop refused, not interleaved", uploads.length === 1 && uploads[0].name === "first.txt",
     JSON.stringify(uploads));

  console.log("\ntoolbar button (same code path)");
  uploads.length = 0;
  $("#fileinput").onchange({ target:{ files:[mkFile(win,"viabutton.txt",7)], value:"c:\\fakepath" } });
  await waitIdle(win);
  ok("picking a file with the button still uploads",
     uploads.length === 1 && uploads[0].name === "viabutton.txt", JSON.stringify(uploads));

  console.log("\nno server selected");
  uploads.length = 0;
  ev_(win, "STATE.vmid = null");
  fire(win, "dragenter", dt([]));
  ok("veil asks for a server first", $("#dropmsg").textContent.includes("Pick a server"),
     $("#dropmsg").textContent);
  fire(win, "drop", dt([{ file: mkFile(win,"nope.txt") }]));
  await waitIdle(win);
  ok("drop with no server uploads nothing", uploads.length === 0);

  console.log(`\n${pass}/${pass+fail} passed${fail ? `  — ${fail} FAILED` : ""}`);
  win.close();
  process.exit(fail ? 1 : 0);
})();
