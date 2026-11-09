"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  doc: null,          // document info from the server
  zoom: 1,            // 1 = 100%  (1pt -> 4/3 css px)
  mode: "edit",
  lines: {},          // page index -> lines
  sel: null,          // {page, line} or {page, add:{x,y}}
  fonts: [],
  lastStyle: { family: "Sarabun", size: 16, color: "#000000", bold: false, italic: false, align: "left" },
  mergeFiles: [],
};
const PX = 4 / 3;
const config = { maxUploadMB: 30, maxMergeMB: 80, maxPages: 300, docTtlMinutes: 60, sourceUrl: "" };

// a random id for this browser, so uploaded fonts are private to it
const CLIENT_ID = (() => {
  const make = () => [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, "0")).join("");
  try {
    let id = localStorage.getItem("tps_client");
    if (!/^[a-f0-9]{32}$/.test(id || "")) { id = make(); localStorage.setItem("tps_client", id); }
    return id;
  } catch { return make(); }
})();
const HINTS = {
  edit: "คลิกที่บรรทัดข้อความเพื่อแก้ไข",
  add: "คลิกตรงจุดที่ต้องการวางข้อความใหม่ (มุมซ้ายบนของข้อความ)",
  erase: "ลากกรอบครอบข้อความที่ต้องการลบ",
  view: "โหมดอ่านอย่างเดียว",
};

// ------------------------------------------------------------------ helpers
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = ""), isErr ? 5000 : 2600);
}
function busy(on) { $("#busy").hidden = !on; }

async function api(url, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.method = o.method || "POST";
    o.headers = { "Content-Type": "application/json" };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const r = await fetch(url, o);
  if (!r.ok) {
    let msg = "เกิดข้อผิดพลาด (" + r.status + ")";
    try { msg = (await r.json()).error || msg; } catch {}
    throw new Error(msg);
  }
  return r;
}

async function saveResponse(r, fallbackName) {
  const cd = r.headers.get("Content-Disposition") || "";
  const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
  const name = m ? decodeURIComponent(m[1]) : fallbackName;
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

const loadedFaces = new Set();
function previewFamily(key, bold, italic) {
  const id = "pv_" + key.replace(/[^a-z0-9]/gi, "_") + (bold ? "_b" : "") + (italic ? "_i" : "");
  if (!loadedFaces.has(id)) {
    loadedFaces.add(id);
    const url = `/fontfile/${encodeURIComponent(key)}?bold=${bold}&italic=${italic}`;
    const face = new FontFace(id, `url(${url})`);
    face.load().then(f => document.fonts.add(f)).catch(() => {});
  }
  return id;
}

// ------------------------------------------------------------------ fonts
async function loadFonts() {
  state.fonts = await (await api("/api/fonts?client=" + CLIENT_ID)).json();
  const sel = $("#edFont");
  sel.innerHTML = state.fonts.map(f => `<option value="${f.key}">${f.label}</option>`).join("");
}

// ------------------------------------------------------------------ open / render
async function openFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
    toast("กรุณาเลือกไฟล์ PDF", true);
    return;
  }
  if (file.size > config.maxUploadMB * 1024 * 1024) {
    toast(`ไฟล์ใหญ่เกิน ${config.maxUploadMB} MB`, true);
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  busy(true);
  try {
    const info = await (await api("/api/docs", { method: "POST", body: fd })).json();
    setDoc(info, true);
    toast(`เปิด “${file.name}” แล้ว (${info.pages.length} หน้า)`);
  } catch (e) { toast(e.message, true); }
  finally { busy(false); }
}

function setDoc(info, fresh = false) {
  state.doc = info;
  $("#thumbs").hidden = false;
  $("#toolbar").hidden = false;
  $("#emptyState").hidden = true;
  if (fresh) {
    state.lines = {};
    // fit page width into the viewer
    const avail = $("#viewer").clientWidth - 48;
    const w = info.pages[0].w * PX;
    state.zoom = Math.max(0.3, Math.min(1.5, Math.floor((avail / w) * 10) / 10));
  }
  document.title = info.name + " – Thai PDF Studio";
  $$(".needs-doc").forEach(b => (b.disabled = false));
  $("#undoBtn").disabled = !info.canUndo;
  $("#toolbar").hidden = false;
  $("#thumbs").hidden = false;
  $("#emptyState").hidden = true;
  renderAll();
}

function imgUrl(n, scale) {
  return `/api/docs/${state.doc.id}/pages/${n}/image?scale=${scale.toFixed(2)}&v=${state.doc.version}`;
}

function renderAll() {
  closeEditor();
  const d = state.doc;
  $("#zoomLabel").textContent = Math.round(state.zoom * 100) + "%";
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const scale = Math.min(state.zoom * PX * dpr, 5);

  const pages = $("#pages");
  pages.innerHTML = "";
  d.pages.forEach((p, n) => {
    const el = document.createElement("div");
    el.className = "page";
    el.dataset.n = n;
    el.style.width = p.w * state.zoom * PX + "px";
    el.style.height = p.h * state.zoom * PX + "px";
    el.innerHTML = `<img loading="lazy" alt="หน้า ${n + 1}" src="${imgUrl(n, scale)}">
      <div class="overlay"></div><div class="num">หน้า ${n + 1} / ${d.pages.length}</div>
      ${p.hasText ? "" : '<div class="notext">หน้านี้ไม่มีข้อความ (อาจเป็นภาพสแกน) — ใช้ “เพิ่มข้อความ” ได้</div>'}`;
    pages.appendChild(el);
    bindOverlay(el, n);
  });
  observePages();

  const th = $("#thumbs");
  th.innerHTML = "";
  d.pages.forEach((p, n) => {
    const t = document.createElement("div");
    t.className = "thumb";
    t.innerHTML = `<img loading="lazy" src="${imgUrl(n, 0.25)}" style="aspect-ratio:${p.w}/${p.h}">
      <div>${n + 1}</div>
      <div class="tools">
        <button title="เลื่อนขึ้น" data-a="up">↑</button>
        <button title="เลื่อนลง" data-a="down">↓</button>
        <button title="หมุน 90°" data-a="rot">↻</button>
        <button title="ลบหน้า" data-a="del">🗑</button>
      </div>`;
    t.addEventListener("click", e => {
      const a = e.target.dataset?.a;
      if (a) { e.stopPropagation(); pageAction(a, n); return; }
      $(`.page[data-n="${n}"]`).scrollIntoView({ behavior: "smooth", block: "start" });
    });
    th.appendChild(t);
  });
}

// load text lines only for pages that come into view
let io;
function observePages() {
  if (io) io.disconnect();
  io = new IntersectionObserver(entries => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const n = +e.target.dataset.n;
      loadLines(n);
      $$(".thumb").forEach((t, i) => t.classList.toggle("current", i === n));
    }
  }, { root: $("#viewer"), rootMargin: "200px" });
  $$(".page").forEach(p => io.observe(p));
}

async function loadLines(n, force = false) {
  if (!state.doc || !state.doc.pages[n].hasText) return;
  const key = state.doc.version + ":" + n;
  if (!force && state.lines[n]?._key === key) { drawLines(n); return; }
  try {
    const lines = await (await api(`/api/docs/${state.doc.id}/pages/${n}/lines`)).json();
    lines._key = key;
    state.lines[n] = lines;
    drawLines(n);
  } catch (e) { /* ignore: page may have been removed */ }
}

function drawLines(n) {
  const page = $(`.page[data-n="${n}"]`);
  if (!page) return;
  const ov = $(".overlay", page);
  $$(".ln", ov).forEach(x => x.remove());
  const z = state.zoom * PX;
  (state.lines[n] || []).forEach((ln, i) => {
    const [x0, y0, x1, y1] = ln.view_bbox || ln.bbox;
    const d = document.createElement("div");
    d.className = "ln";
    d.style.left = x0 * z + "px";
    d.style.top = y0 * z + "px";
    d.style.width = (x1 - x0) * z + "px";
    d.style.height = (y1 - y0) * z + "px";
    d.title = ln.text;
    d.addEventListener("click", e => {
      e.stopPropagation();
      if (state.mode !== "edit") return;
      $$(".ln.sel").forEach(x => x.classList.remove("sel"));
      d.classList.add("sel");
      openEditor({ page: n, line: ln }, d.getBoundingClientRect());
    });
    ov.appendChild(d);
  });
}

// ------------------------------------------------------------------ overlay interactions
function bindOverlay(pageEl, n) {
  const ov = $(".overlay", pageEl);
  let start = null, box = null;

  const pt = e => {
    const r = ov.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  ov.addEventListener("pointerdown", e => {
    if (e.target !== ov) return;
    if (state.mode === "add") {
      const p = pt(e);
      const z = state.zoom * PX;
      $$(".addmark").forEach(x => x.remove());
      const m = document.createElement("div");
      m.className = "addmark";
      m.style.left = p.x + "px"; m.style.top = p.y + "px";
      m.style.height = state.lastStyle.size * 1.2 * z + "px";
      ov.appendChild(m);
      openEditor({ page: n, add: { x: p.x / z, y: p.y / z } }, { left: e.clientX, right: e.clientX, top: e.clientY, bottom: e.clientY + 20 });
    } else if (state.mode === "erase") {
      e.preventDefault();  // no text selection while dragging
      start = pt(e);
      box = document.createElement("div");
      box.className = "marquee";
      ov.appendChild(box);
      ov.setPointerCapture(e.pointerId);
    } else if (state.mode === "edit") {
      closeEditor();
    }
  });
  ov.addEventListener("pointermove", e => {
    if (!start) return;
    const p = pt(e);
    Object.assign(box.style, {
      left: Math.min(p.x, start.x) + "px", top: Math.min(p.y, start.y) + "px",
      width: Math.abs(p.x - start.x) + "px", height: Math.abs(p.y - start.y) + "px",
    });
  });
  ov.addEventListener("pointerup", async e => {
    if (!start) return;
    const p = pt(e), z = state.zoom * PX;
    const r = [Math.min(p.x, start.x) / z, Math.min(p.y, start.y) / z, Math.max(p.x, start.x) / z, Math.max(p.y, start.y) / z];
    start = null;
    box.remove();
    if (r[2] - r[0] < 3 || r[3] - r[1] < 3) return;
    try {
      busy(true);
      const info = await (await api(`/api/docs/${state.doc.id}/pages/${n}/erase`, { json: { bbox: r } })).json();
      afterEdit(info, n);
      toast("ลบข้อความในพื้นที่แล้ว");
    } catch (err) { toast(err.message, true); }
    finally { busy(false); }
  });
}

// ------------------------------------------------------------------ editor popover
function openEditor(sel, anchor) {
  state.sel = sel;
  const ed = $("#editor");
  const st = sel.line ? {
    family: sel.line.family, size: sel.line.size, color: sel.line.color,
    bold: sel.line.bold, italic: sel.line.italic, align: "left",
  } : { ...state.lastStyle };

  $("#editorTitle").textContent = sel.line ? `แก้ไขข้อความ (หน้า ${sel.page + 1})` : `เพิ่มข้อความใหม่ (หน้า ${sel.page + 1})`;
  $("#edText").value = sel.line ? sel.line.text : "";
  if (![...$("#edFont").options].some(o => o.value === st.family)) st.family = "Sarabun";
  $("#edFont").value = st.family;
  $("#edSize").value = st.size;
  $("#edColor").value = st.color;
  $("#edBold").checked = st.bold;
  $("#edItalic").checked = st.italic;
  $("#edAlign").value = st.align;
  $("#edFill").checked = false;
  $("#edDelete").hidden = !sel.line;
  $("#edOrig").textContent = sel.line ? `ฟอนต์เดิมในไฟล์: ${sel.line.font}` : "";
  updatePreview();

  ed.hidden = false;
  // place below (or above) the anchor, inside the window
  const W = innerWidth, H = innerHeight, pw = ed.offsetWidth, ph = ed.offsetHeight;
  let left = Math.min(Math.max(16, anchor.left), W - pw - 16);
  let top = anchor.bottom + 8;
  if (top + ph > H - 16) top = Math.max(16, anchor.top - ph - 8);
  ed.style.left = left + "px";
  ed.style.top = top + "px";
  setTimeout(() => $("#edText").focus(), 0);
}

function closeEditor() {
  $("#editor").hidden = true;
  state.sel = null;
  $$(".ln.sel").forEach(x => x.classList.remove("sel"));
  $$(".addmark").forEach(x => x.remove());
}

function currentStyle() {
  return {
    family: $("#edFont").value,
    size: parseFloat($("#edSize").value) || 16,
    color: $("#edColor").value,
    bold: $("#edBold").checked,
    italic: $("#edItalic").checked,
    align: $("#edAlign").value,
  };
}

function updatePreview() {
  const st = currentStyle();
  const ta = $("#edText");
  ta.style.fontFamily = `"${previewFamily(st.family, st.bold, st.italic)}", "UI Sarabun", sans-serif`;
  ta.style.color = st.color;
  ta.style.textAlign = st.align;
  ta.style.background = $("#edFill").checked ? $("#edFillColor").value : "#fff";
}

async function saveEdit(deleteLine = false) {
  const sel = state.sel;
  if (!sel) return;
  const style = currentStyle();
  const text = deleteLine ? "" : $("#edText").value;
  const fill = $("#edFill").checked ? $("#edFillColor").value : null;
  const id = state.doc.id;
  try {
    busy(true);
    let info;
    if (sel.line) {
      info = await (await api(`/api/docs/${id}/pages/${sel.page}/replace`, {
        json: { bbox: sel.line.bbox, origin: sel.line.origin, kind: sel.line.kind, view_bbox: sel.line.view_bbox, text, style, fill },
      })).json();
    } else {
      if (!text.trim()) { closeEditor(); return; }
      info = await (await api(`/api/docs/${id}/pages/${sel.page}/add`, {
        json: { x: sel.add.x, y: sel.add.y, text, style },
      })).json();
    }
    state.lastStyle = { ...style };
    afterEdit(info, sel.page);
    toast(deleteLine ? "ลบบรรทัดแล้ว" : "บันทึกแล้ว");
  } catch (e) { toast(e.message, true); }
  finally { busy(false); }
}

// after a change on one page: refresh only that page
function afterEdit(info, n) {
  state.doc = info;
  $("#undoBtn").disabled = !info.canUndo;
  closeEditor();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const page = $(`.page[data-n="${n}"]`);
  const img = $("img", page);
  const fresh = new Image();
  fresh.onload = () => { img.src = fresh.src; };
  fresh.src = imgUrl(n, Math.min(state.zoom * PX * dpr, 5));
  const th = $$(".thumb img")[n];
  if (th) th.src = imgUrl(n, 0.25);
  info.pages.forEach((p, i) => {
    const note = $(`.page[data-n="${i}"] .notext`);
    if (note && p.hasText) note.remove();
  });
  loadLines(n, true);
}

// ------------------------------------------------------------------ page actions
async function pageAction(a, n) {
  const id = state.doc.id;
  const count = state.doc.pages.length;
  try {
    let info;
    if (a === "rot") {
      info = await (await api(`/api/docs/${id}/pages/${n}/rotate`, { json: { degrees: 90 } })).json();
    } else if (a === "del") {
      if (!confirm(`ลบหน้า ${n + 1}?`)) return;
      info = await (await api(`/api/docs/${id}/pages/${n}`, { method: "DELETE" })).json();
    } else {
      const to = a === "up" ? n - 1 : n + 1;
      if (to < 0 || to >= count) return;
      const order = [...Array(count).keys()];
      [order[n], order[to]] = [order[to], order[n]];
      info = await (await api(`/api/docs/${id}/reorder`, { json: { order } })).json();
    }
    state.lines = {};
    setDoc(info);
  } catch (e) { toast(e.message, true); }
}

async function undo() {
  if (!state.doc || !state.doc.canUndo) return;
  try {
    busy(true);
    const info = await (await api(`/api/docs/${state.doc.id}/undo`, { method: "POST" })).json();
    const scroll = $("#viewer").scrollTop;
    state.lines = {};
    setDoc(info);
    $("#viewer").scrollTop = scroll;
    toast("ย้อนกลับแล้ว");
  } catch (e) { toast(e.message, true); }
  finally { busy(false); }
}

function setMode(m) {
  state.mode = m;
  document.body.className = "mode-" + m;
  $$("#modeSeg button").forEach(b => b.classList.toggle("on", b.dataset.mode === m));
  $("#modeHint").textContent = HINTS[m];
  closeEditor();
}

function setZoom(z) {
  state.zoom = Math.max(0.3, Math.min(3, Math.round(z * 10) / 10));
  const viewer = $("#viewer");
  const ratio = viewer.scrollTop / Math.max(1, viewer.scrollHeight);
  renderAll();
  viewer.scrollTop = ratio * viewer.scrollHeight;
}

// ------------------------------------------------------------------ merge
function renderMergeList() {
  const ol = $("#mergeList");
  ol.innerHTML = "";
  state.mergeFiles.forEach((f, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<div class="r"><span class="name"></span>
      <button type="button" data-a="up">↑</button><button type="button" data-a="down">↓</button>
      <button type="button" data-a="rm">×</button></div>`;
    $(".name", li).textContent = f.name;
    li.addEventListener("click", e => {
      const a = e.target.dataset?.a;
      if (!a) return;
      const L = state.mergeFiles;
      if (a === "rm") L.splice(i, 1);
      if (a === "up" && i > 0) [L[i - 1], L[i]] = [L[i], L[i - 1]];
      if (a === "down" && i < L.length - 1) [L[i + 1], L[i]] = [L[i], L[i + 1]];
      renderMergeList();
    });
    ol.appendChild(li);
  });
}

async function doMerge() {
  const fd = new FormData();
  let count = 0;
  try {
    busy(true);
    if ($("#mergeIncludeCurrent").checked && state.doc) {
      const r = await api(`/api/docs/${state.doc.id}/download`);
      fd.append("files", new File([await r.blob()], state.doc.name, { type: "application/pdf" }));
      count++;
    }
    let total = 0;
    for (const f of state.mergeFiles) { fd.append("files", f); count++; total += f.size; }
    if (total > config.maxMergeMB * 1024 * 1024) { toast(`ไฟล์รวมกันใหญ่เกิน ${config.maxMergeMB} MB`, true); return; }
    if (count < 2) { toast("เลือกอย่างน้อย 2 ไฟล์เพื่อรวม", true); return; }
    const info = await (await api("/api/merge", { method: "POST", body: fd })).json();
    state.mergeFiles = [];
    setDoc(info, true);
    toast(`รวม ${count} ไฟล์แล้ว (${info.pages.length} หน้า) — กด “ดาวน์โหลด PDF” เพื่อบันทึก`);
  } catch (e) { toast(e.message, true); }
  finally { busy(false); }
}

// ------------------------------------------------------------------ split
async function doSplit() {
  const mode = $("input[name=smode]:checked").value;
  const body = { mode, ranges: $("#splitRanges").value, every: parseInt($("#splitEvery").value) || 1 };
  try {
    busy(true);
    const r = await api(`/api/docs/${state.doc.id}/split`, { json: body });
    await saveResponse(r, "split.zip");
    toast("แยกไฟล์แล้ว");
  } catch (e) { toast(e.message, true); }
  finally { busy(false); }
}

// ------------------------------------------------------------------ wiring
async function loadConfig() {
  try { Object.assign(config, await (await api("/api/config")).json()); } catch {}
  $("#limitNote").textContent =
    `ไฟล์ละไม่เกิน ${config.maxUploadMB} MB / ${config.maxPages} หน้า · ไฟล์จะถูกลบจากเซิร์ฟเวอร์อัตโนมัติเมื่อไม่ได้ใช้ ${config.docTtlMinutes} นาที`;
  if (config.sourceUrl) { $("#sourceLink").href = config.sourceUrl; $("#sourceLink").hidden = false; }
}

function init() {
  loadConfig();
  loadFonts().catch(() => toast("โหลดรายการฟอนต์ไม่สำเร็จ", true));
  setMode("edit");

  $("#fileInput").addEventListener("change", e => { openFile(e.target.files[0]); e.target.value = ""; });
  $("#downloadBtn").addEventListener("click", async () => {
    try { busy(true); await saveResponse(await api(`/api/docs/${state.doc.id}/download`), "document.pdf"); }
    catch (e) { toast(e.message, true); } finally { busy(false); }
  });
  $("#textBtn").addEventListener("click", async () => {
    try { await saveResponse(await api(`/api/docs/${state.doc.id}/text`), "text.txt"); }
    catch (e) { toast(e.message, true); }
  });
  $("#undoBtn").addEventListener("click", undo);
  $("#zoomIn").addEventListener("click", () => setZoom(state.zoom + 0.1));
  $("#zoomOut").addEventListener("click", () => setZoom(state.zoom - 0.1));
  $$("#modeSeg button").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));

  // editor
  $("#editorClose").addEventListener("click", closeEditor);
  $("#edSave").addEventListener("click", () => saveEdit(false));
  $("#edDelete").addEventListener("click", () => saveEdit(true));
  ["#edFont", "#edSize", "#edColor", "#edBold", "#edItalic", "#edAlign", "#edFill", "#edFillColor"]
    .forEach(s => $(s).addEventListener("input", updatePreview));
  $("#edText").addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveEdit(false); }
  });
  $("#fontUpload").addEventListener("change", async e => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    const fd = new FormData(); fd.append("file", f); fd.append("client", CLIENT_ID);
    try {
      const keep = currentStyle();
      const before = new Set(state.fonts.map(x => x.key));
      state.fonts = await (await api("/api/fonts", { method: "POST", body: fd })).json();
      $("#edFont").innerHTML = state.fonts.map(x => `<option value="${x.key}">${x.label}</option>`).join("");
      const added = state.fonts.find(x => !before.has(x.key));
      $("#edFont").value = added ? added.key : keep.family;
      if (!$("#edFont").value) $("#edFont").value = keep.family;
      updatePreview();
      toast("เพิ่มฟอนต์ " + f.name + " แล้ว");
    } catch (err) { toast(err.message, true); }
  });
  // drag the editor by its header
  (() => {
    const ed = $("#editor"), head = $(".panel-head", ed);
    let off = null;
    head.addEventListener("pointerdown", e => {
      if (e.target.closest("button")) return;
      off = { x: e.clientX - ed.offsetLeft, y: e.clientY - ed.offsetTop };
      head.setPointerCapture(e.pointerId);
    });
    head.addEventListener("pointermove", e => {
      if (!off) return;
      ed.style.left = e.clientX - off.x + "px";
      ed.style.top = e.clientY - off.y + "px";
    });
    head.addEventListener("pointerup", () => (off = null));
  })();

  // merge
  $("#mergeBtn").addEventListener("click", () => {
    $("#mergeIncludeCurrent").closest("label").hidden = !state.doc;
    $("#mergeIncludeCurrent").checked = !!state.doc;
    renderMergeList();
    $("#mergeDlg").showModal();
  });
  $("#mergeInput").addEventListener("change", e => {
    state.mergeFiles.push(...e.target.files);
    e.target.value = "";
    renderMergeList();
  });
  $("#mergeGo").addEventListener("click", e => { e.preventDefault(); $("#mergeDlg").close(); doMerge(); });

  // split
  $("#splitBtn").addEventListener("click", () => {
    const n = state.doc.pages.length;
    $("#splitInfo").textContent = `เอกสาร “${state.doc.name}” มี ${n} หน้า`;
    $("#splitRanges").placeholder = n > 1 ? `เช่น 1-${Math.ceil(n / 2)}, ${Math.ceil(n / 2) + 1}-${n}` : "1";
    $("#splitDlg").showModal();
  });
  $("#splitRanges").addEventListener("focus", () => ($("input[name=smode][value=ranges]").checked = true));
  $("#splitEvery").addEventListener("focus", () => ($("input[name=smode][value=every]").checked = true));
  $("#splitGo").addEventListener("click", e => { e.preventDefault(); $("#splitDlg").close(); doSplit(); });

  // keyboard
  document.addEventListener("keydown", e => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
    if (e.key === "Escape") closeEditor();
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); undo(); }
  });

  // drag & drop a PDF anywhere
  const dz = $("#dropZone");
  window.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("over"); });
  window.addEventListener("dragleave", e => { if (!e.relatedTarget) dz.classList.remove("over"); });
  window.addEventListener("drop", e => {
    e.preventDefault();
    dz.classList.remove("over");
    const files = [...e.dataTransfer.files].filter(f => /\.pdf$/i.test(f.name));
    if (files.length) openFile(files[0]);
  });
}

init();
