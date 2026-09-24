// The code map's page. Lays the reading order out as columns (one per chapter), the tracked class's fields as lines across the
// top, and every function, class and constant as a box; clicking anything lights up what it touches and opens it on the right.
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const COL_W = 420, COL_GAP = 170, LEFT = 90, PAD = 12, ROW = 16, HEAD = 40, GAP_Y = 16, TRACK_H = 22;
const KIND_COLORS = {
  function: "--kind-function", method: "--kind-method", record: "--kind-record", enum: "--kind-enum", class: "--kind-class",
  error: "--kind-error", constant: "--kind-constant", group: "--kind-constant", stale: "--kind-error",
};
const KIND_LABEL = {
  function: "function", method: "method", record: "record", enum: "enum", class: "class", error: "error",
  constant: "constant", group: "constants", stale: "missing",
};
const CLASS_KINDS = ["record", "enum", "class", "error"];

const S = {
  map: null, ref: "", live: true, compact: false, allCalls: false,
  notes: { items: {} },
  boxes: [], byId: new Map(), symbolBox: new Map(), order: [], cols: [], tracks: [], trackByField: new Map(),
  fieldReaders: new Map(), enumUsers: new Map(), constUsers: new Map(), implementedBy: new Map(),
  sel: null, chapter: null, field: null, recordField: null,
  view: { x: 0, y: 0, k: 0.6 }, charW: 7, sansW: 6.2, busBottom: 0, width: 0, height: 0,
  firstLoad: true, polling: null,
};

const $ = (id) => document.getElementById(id);
const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const shortName = (id) => (id || "").split(".").slice(-2).join(".");
const lastName = (id) => (id || "").split(".").pop();
const trackName = () => S.map?.track?.name || "state";
const shortFile = (file) => (S.map?.source.common && file.startsWith(S.map.source.common) ? file.slice(S.map.source.common.length) : file);
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const el = (tag, attrs = {}, parent = null) => {
  const node = document.createElementNS(SVGNS, tag);
  for (const [key, value] of Object.entries(attrs)) if (value !== undefined && value !== null) node.setAttribute(key, value);
  if (parent) parent.appendChild(node);
  return node;
};
// Inline code in notes: `x` becomes <code>x</code>.
const rich = (text) => esc(text).replace(/`([^`]+)`/g, "<code>$1</code>");

// --- loading ------------------------------------------------------------------------------------------------------------

async function loadRefs() {
  try {
    const res = await fetch("/api/refs");
    const data = await res.json();
    const select = $("ref");
    select.innerHTML = "";
    const add = (value, label) => { const option = document.createElement("option"); option.value = value; option.textContent = label; select.appendChild(option); };
    add("", `Working tree (${data.current})`);
    for (const ref of data.refs) add(ref, ref);
    select.value = S.ref;
  } catch (err) { /* the map still works without the list */ }
}

async function loadNotes() {
  try { S.notes = await (await fetch("/api/notes")).json(); } catch (err) { S.notes = { items: {} }; }
  if (!S.notes.items) S.notes.items = {};
}

async function loadMap(keep = false) {
  setStatus("Reading the code…");
  let data;
  try {
    const res = await fetch("/api/map" + (S.ref ? "?ref=" + encodeURIComponent(S.ref) : ""));
    data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  } catch (err) {
    showError("Couldn't build the map:\n" + err.message);
    setStatus("");
    return;
  }
  hideError();
  const before = keep ? { sel: S.sel && S.byId.get(S.sel)?.key, chapter: S.chapter, field: S.field, recordField: S.recordField, view: { ...S.view } } : null;
  S.map = data;
  document.title = `${data.source.name} · codemap`;
  $("brand").innerHTML = `${esc(data.source.name)} <span>code map</span>`;
  $("legend-read").textContent = `reads a ${trackName()} field`;
  $("legend-write").textContent = `writes a ${trackName()} field`;
  document.querySelectorAll(".track-only").forEach((node) => { node.hidden = !data.track; });
  prepare();
  layout();
  render();
  renderSidebar();
  renderWarnings();
  const source = data.source;
  setStatus(`${esc(source.ref === "working tree" ? "Working tree" : source.ref)}` + (source.branch ? ` · ${esc(source.branch)} @ ${esc(source.commit)}` : "")
    + (source.dirty ? ' · <span class="dirty">uncommitted changes</span>' : "")
    + ` · built ${esc(data.generated.slice(11))}`);
  if (before) {
    S.view = before.view;
    const box = S.boxes.find((b) => b.key === before.sel);
    applyView();
    if (box) select({ box: box.id }, { pan: false });
    else if (before.recordField) select({ recordField: before.recordField }, { pan: false });
    else if (before.field) select({ field: before.field }, { pan: false });
    else if (before.chapter) select({ chapter: before.chapter }, { pan: false });
    else select({}, { pan: false });
  } else if (S.firstLoad) {
    S.firstLoad = false;
    focusChapter(S.map.chapters[0]?.id);
    select({}, { pan: false });
    renderWelcome();
  }
}

function setStatus(html) { $("status").innerHTML = html; }
function showError(text) { const box = $("error"); box.textContent = text; box.hidden = false; }
function hideError() { $("error").hidden = true; }

// --- preparing the data -------------------------------------------------------------------------------------------------

function prepare() {
  const { symbols, chapters } = S.map;
  S.boxes = []; S.byId = new Map(); S.symbolBox = new Map(); S.order = [];
  S.fieldReaders = new Map(); S.enumUsers = new Map(); S.constUsers = new Map(); S.implementedBy = new Map();
  const pushTo = (map, key, value) => { if (!map.has(key)) map.set(key, []); if (!map.get(key).includes(value)) map.get(key).push(value); };
  for (const symbol of Object.values(symbols)) {
    for (const event of symbol.events || []) {
      if (event.kind === "field") pushTo(S.fieldReaders, `${event.owner}.${event.field}`, symbol.id);
      if (event.kind === "enum") pushTo(S.enumUsers, `${event.owner}.${event.field}`, symbol.id);
      if (event.kind === "const") pushTo(S.constUsers, event.target, symbol.id);
    }
    if (symbol.overrides) pushTo(S.implementedBy, symbol.overrides, symbol.id);
  }
  chapters.forEach((chapter, ci) => {
    chapter.items.forEach((item, ii) => {
      const box = { id: `${chapter.id}:${ii}`, chapter, ci, ii, step: ii + 1, item, ref: item.ref, part: item.part || null };
      box.key = item.ref + (item.part ? "#" + item.part : "");
      if (item.stale) box.kind = "stale";
      else if (item.ref.startsWith("group:")) { box.kind = "group"; box.members = (item.members || []).map((id) => symbols[id]).filter(Boolean); }
      else { box.symbol = symbols[item.ref]; box.kind = box.symbol.kind; }
      box.range = null;
      if (box.symbol && item.part) box.range = partRange(box.symbol, item.from, item.to);
      box.events = box.symbol ? (box.symbol.events || []).filter((e) => !box.range || (e.line >= box.range[0] && e.line <= box.range[1])) : [];
      S.boxes.push(box); S.byId.set(box.id, box); S.order.push(box.id);
      if (box.symbol && !S.symbolBox.has(box.symbol.id)) S.symbolBox.set(box.symbol.id, box.id);
      for (const member of box.members || []) if (!S.symbolBox.has(member.id)) S.symbolBox.set(member.id, box.id);
    });
  });
}

// The first and last line of a function part, found by text so the part survives edits elsewhere in the function.
function partRange(symbol, from, to) {
  const lines = symbol.code;
  let start = lines.findIndex((line) => from && line.text.includes(from));
  if (start < 0) start = 0;
  while (start > 0 && lines[start - 1].text.trim().startsWith("#")) start--;   // keep the step comment right above it
  let end = lines.findIndex((line, i) => i >= start && to && line.text.includes(to));
  if (end < 0) end = lines.length - 1;
  return [lines[start].n, lines[end].n];
}

// --- text measuring and wrapping ------------------------------------------------------------------------------------------

function measureFonts() {
  const canvas = document.createElement("canvas").getContext("2d");
  canvas.font = `11.5px ${cssVar("--mono") || "monospace"}`;
  S.charW = canvas.measureText("abcdefghijklmnopqrstuvwxyz0123456789_(): ").width / 41;
  canvas.font = `11.5px ${cssVar("--sans") || "sans-serif"}`;
  const sample = "The model proposes the duties and code checks every script against the description.";
  S.sansW = canvas.measureText(sample).width / sample.length;
}

function wrap(text, width) {
  const words = String(text ?? "").split(/\s+/).filter(Boolean);
  const lines = [];
  let line = "";
  for (let word of words) {
    while (word.length > width) {
      if (line) { lines.push(line); line = ""; }
      lines.push(word.slice(0, width)); word = word.slice(width);
    }
    if (!line) line = word;
    else if (line.length + 1 + word.length <= width) line += " " + word;
    else { lines.push(line); line = word; }
  }
  if (line) lines.push(line);
  return lines;
}

const clip = (text, n) => (text.length > n ? text.slice(0, Math.max(0, n - 1)) + "…" : text);

// --- the contents of each box ---------------------------------------------------------------------------------------------

function boxRows(box) {
  const rows = [];
  const inner = COL_W - 2 * PAD;
  const mono = Math.floor(inner / S.charW);
  const sans = Math.floor(inner / S.sansW);
  const add = (spans, opts = {}) => rows.push({ spans, font: opts.font || "mono", h: opts.h || ROW, bg: opts.bg || null, detail: !!opts.detail, gapBefore: opts.gapBefore || 0 });
  const addWrapped = (text, cls, opts = {}, maxLines = 99) => {
    const width = opts.font === "sans" ? sans : mono;
    const lines = wrap(text, width);
    lines.slice(0, maxLines).forEach((line, i) => {
      const last = i === maxLines - 1 && lines.length > maxLines;
      add([{ t: last ? clip(line + " …", width) : line, cls }], { ...opts, gapBefore: i === 0 ? opts.gapBefore : 0 });
    });
  };
  const chipRow = (label, items, cls, data, opts = {}) => {
    if (!items.length) return;
    let spans = [{ t: label.padEnd(8), cls: "lbl" }];
    let used = 8;
    const flush = () => { add(spans, { detail: opts.detail }); spans = [{ t: " ".repeat(8), cls: "lbl" }]; used = 8; };
    items.forEach((item, i) => {
      const text = item.text + (i < items.length - 1 ? "," : "");
      if (used + text.length + 1 > mono && used > 8) flush();
      spans.push({ t: (used > 8 ? " " : "") + text, cls: `chip ${cls}` + (item.data ? " clickable" : ""), data: item.data });
      used += text.length + 1;
    });
    if (spans.length > 1) add(spans, { detail: opts.detail });
  };
  const note = (box.item.note || "").replace(/`/g, "");   // backticks mark code in the panel; boxes show plain text
  const addNote = () => { if (note) addWrapped(note, "note", { font: "sans", bg: "note", gapBefore: 6 }, S.compact ? 3 : 8); };
  const symbol = box.symbol;

  if (box.kind === "stale") {
    addWrapped(`${box.ref} isn't in the code any more: it was renamed or removed. Its note is kept below until the order file is updated.`, "com", { font: "sans" });
    addNote();
    return rows;
  }

  if (box.kind === "function" || box.kind === "method") {
    const params = (symbol.params || []).filter((p, i) => !(i === 0 && ["self", "cls"].includes(p.name) && box.kind === "method"));
    let sig = "(";
    let starred = false;
    sig += params.map((p) => {
      let text = "";
      if (p.kwonly && !starred && !p.name.startsWith("*")) { starred = true; text += "*, "; }
      return text + p.name + (p.type ? `: ${p.type}` : "");
    }).join(", ");
    sig += ")" + (symbol.returns ? ` -> ${symbol.returns}` : "");
    addWrapped(sig, "sig");
    if (symbol.comment?.length) addWrapped(symbol.comment.join(" "), "com", { font: "sans", gapBefore: 4 }, S.compact ? 2 : 5);
    addNote();
    const events = box.events;
    const uniq = (list) => [...new Set(list)];
    const reads = uniq(events.filter((e) => e.kind === "read").map((e) => e.field));
    const writes = uniq(events.filter((e) => e.kind === "write").map((e) => e.field));
    const calls = uniq(events.filter((e) => e.kind === "call").map((e) => e.target));
    const builds = uniq(events.filter((e) => e.kind === "build").map((e) => e.target));
    const raises = uniq(events.filter((e) => e.kind === "raise").map((e) => e.name));
    const attrs = uniq(events.filter((e) => e.kind === "attr").map((e) => "self." + e.field));
    const consts = uniq(events.filter((e) => e.kind === "const").map((e) => e.target));
    const fields = new Map();
    for (const e of events.filter((e) => e.kind === "field")) {
      if (!fields.has(e.owner)) fields.set(e.owner, []);
      if (!fields.get(e.owner).includes(e.field)) fields.get(e.owner).push(e.field);
    }
    if (reads.length || writes.length || calls.length || builds.length) rows.push({ spans: [], h: 6 });
    chipRow("in", reads.map((f) => ({ text: f, data: { field: f } })), "chip-read");
    chipRow("out", writes.map((f) => ({ text: f, data: { field: f } })), "chip-write");
    chipRow("calls", calls.map((t) => ({ text: calledName(t, box), data: { sym: t } })), "chip-link");
    chipRow("builds", builds.map((t) => ({ text: lastName(t), data: { sym: t } })), "chip-build");
    if (!S.compact) {
      for (const [owner, names] of fields) chipRow(lastName(owner), names.map((f) => ({ text: f, data: { rfield: `${owner}.${f}` } })), "chip-read", null, { detail: true });
      chipRow("uses", [...attrs.map((a) => ({ text: a })), ...consts.map((c) => ({ text: lastName(c), data: { sym: c } }))], "chip-link", null, { detail: true });
      chipRow("raises", raises.map((r) => ({ text: r })), "chip-flag", null, { detail: true });
    }
    const implementors = S.implementedBy.get(symbol.id) || [];
    chipRow("done by", implementors.map((t) => ({ text: shortName(t), data: { sym: t } })), "chip-link");
    return rows;
  }

  if (box.kind === "record") {
    if (symbol.bases?.length) add([{ t: `(${symbol.bases.map(lastName).join(", ")})`, cls: "dimtext" }]);
    for (const field of symbol.fields) {
      const spans = [{ t: field.name, cls: "field-name clickable", data: { rfield: `${symbol.id}.${field.name}` } }, { t: `: ${field.type}`, cls: "field-type" }];
      let text = field.name.length + field.type.length + 2;
      if (field.default !== null && field.default !== undefined) {
        const def = ` = ${field.default}`;
        spans.push({ t: clip(def, Math.max(4, mono - text)), cls: "dimtext" }); text += def.length;
      }
      add(spans);
      if (field.comment && !S.compact) add([{ t: clip("  " + field.comment, sans), cls: "com" }], { font: "sans", detail: true });
    }
    const methods = (symbol.methods || []).map((m) => ({ text: lastName(m), data: { sym: m } }));
    if (methods.length) { rows.push({ spans: [], h: 4 }); chipRow("methods", methods, "chip-link"); }
    addNote();
    return rows;
  }

  if (box.kind === "enum") {
    for (const member of symbol.members) {
      add([{ t: member.name, cls: "field-name" }, { t: ` = ${JSON.stringify(member.value)}`, cls: "dimtext" }]);
      if (member.comment && !S.compact) add([{ t: clip("  " + member.comment, sans), cls: "com" }], { font: "sans", detail: true });
    }
    const methods = (symbol.methods || []).map((m) => ({ text: lastName(m), data: { sym: m } }));
    if (methods.length) chipRow("methods", methods, "chip-link");
    addNote();
    return rows;
  }

  if (box.kind === "class" || box.kind === "error") {
    if (symbol.bases?.length) add([{ t: `(${symbol.bases.map(lastName).join(", ")})`, cls: "dimtext" }]);
    for (const attr of symbol.class_attrs || []) add([{ t: attr.name, cls: "field-name" }, { t: clip(` = ${attr.value}`, Math.max(4, mono - attr.name.length)), cls: "dimtext" }]);
    for (const attr of symbol.inst_attrs || []) add([{ t: `self.${attr.name}`, cls: "field-name" }, { t: attr.type && attr.type !== "?" ? `: ${attr.type}` : "", cls: "field-type" }], { detail: true });
    const methods = (symbol.methods || []).map((m) => ({ text: lastName(m), data: { sym: m } }));
    if (methods.length) { rows.push({ spans: [], h: 4 }); chipRow("methods", methods, "chip-link"); }
    if (symbol.comment?.length) addWrapped(symbol.comment.join(" "), "com", { font: "sans", gapBefore: 4 }, 3);
    addNote();
    return rows;
  }

  if (box.kind === "constant" || box.kind === "group") {
    const members = box.kind === "group" ? box.members : [symbol];
    for (const member of members) {
      const value = member.value || "";
      const firstLine = value.split("\n")[0];
      const more = value.includes("\n") ? ` … (${value.split("\n").length} lines)` : "";
      add([{ t: member.name, cls: "field-name clickable", data: { sym: member.id } }, { t: clip(` = ${firstLine}${more}`, Math.max(4, mono - member.name.length)), cls: "dimtext" }]);
      const comment = (member.comment || []).join(" ");
      if (comment && !S.compact) addWrapped(comment, "com", { font: "sans", detail: true }, 2);
    }
    addNote();
    return rows;
  }

  return rows;
}

// How a call target is named inside a box: a method on the same class by its own name, anything else as Class.method.
function calledName(target, box) {
  const owner = box.symbol?.owner;
  if (owner && target.startsWith(owner + ".")) return lastName(target);
  const symbol = S.map.symbols[target];
  return symbol?.owner ? shortName(target) : lastName(target);
}

// --- layout ---------------------------------------------------------------------------------------------------------------

function layout() {
  const fields = S.map.track?.fields || [];
  const trackTop = 70;
  S.busBottom = fields.length ? trackTop + fields.length * TRACK_H + 40 : 0;
  const colTop = S.busBottom + 40;
  S.cols = S.map.chapters.map((chapter, ci) => {
    const x = LEFT + ci * (COL_W + COL_GAP);
    const goal = wrap(chapter.goal || "", Math.floor((COL_W - 2 * PAD) / (S.sansW * 1.04)));
    const headH = 64 + goal.length * 16 + 10;
    return { chapter, ci, x, y: colTop, headH, goal, bottom: colTop + headH };
  });
  for (const box of S.boxes) {
    box.rows = boxRows(box).filter((row) => !(S.compact && row.detail));
    box.w = COL_W;
    box.h = HEAD + box.rows.reduce((sum, row) => sum + row.h + (row.gapBefore || 0) + (row.bg ? 0 : 0), 0) + PAD;
    const col = S.cols[box.ci];
    box.x = col.x;
    box.y = col.bottom + GAP_Y;
    col.bottom = box.y + box.h;
  }
  S.width = LEFT + S.cols.length * (COL_W + COL_GAP);
  S.height = Math.max(...S.cols.map((c) => c.bottom)) + 120;
  const colOf = (id) => S.byId.get(S.symbolBox.get(id))?.ci;
  S.tracks = fields.map((field, i) => {
    const writerCols = field.writers.map(colOf).filter((c) => c !== undefined);
    const readerCols = field.readers.map(colOf).filter((c) => c !== undefined);
    const start = writerCols.length ? Math.min(...writerCols) : 0;
    const end = Math.max(start, ...readerCols, ...writerCols);
    return { field, i, y: trackTop + i * TRACK_H, x0: S.cols[start].x - 60, x1: S.cols[end].x + COL_W + 20, writerCols, readerCols };
  });
  S.trackByField = new Map(S.tracks.map((t) => [t.field.name, t]));
}

// --- drawing ---------------------------------------------------------------------------------------------------------------

function render() {
  const bus = $("bus"), columns = $("columns");
  bus.innerHTML = ""; columns.innerHTML = ""; $("edges").innerHTML = "";
  // the tracked class's fields as lines
  if (S.tracks.length) {
    el("text", { x: LEFT - 60, y: 30, class: "bus-title" }, bus).textContent = `The ${trackName()}, field by field`;
    el("text", { x: LEFT - 60, y: 48, class: "bus-sub" }, bus).textContent = `The object the code passes along. Each line is one ${trackName()} field: it starts where the field is written (●) and runs past every function that reads it (○). Click a line to see who.`;
  }
  let group = null;
  for (const track of S.tracks) {
    const g = el("g", { class: "track", "data-field": track.field.name }, bus);
    el("line", { x1: track.x0, x2: track.x1, y1: track.y, y2: track.y, class: "rail" }, g);
    el("rect", { x: track.x0, y: track.y - TRACK_H / 2, width: track.x1 - track.x0, height: TRACK_H, fill: "transparent" }, g);
    const label = el("text", { x: track.x0 - 8, y: track.y + 4, "text-anchor": "end" }, g);
    const name = el("tspan", { class: "t-label" }, label); name.textContent = track.field.name;
    const type = el("tspan", { class: "t-type" }, label); type.textContent = `: ${track.field.type}`;
    if (track.field.above && track.field.above !== group) {
      group = track.field.above;
      const heading = group.split(":")[0];
      el("text", { x: track.x0 - 8, y: track.y - 12, "text-anchor": "end", class: "t-group" }, bus).textContent = heading;
    }
    for (const ci of new Set(track.writerCols)) el("circle", { cx: S.cols[ci].x - 30, cy: track.y, r: 5, class: "w" }, g);
    for (const ci of new Set(track.readerCols)) el("circle", { cx: S.cols[ci].x - 18, cy: track.y, r: 4, class: "r" }, g);
    g.addEventListener("click", (ev) => { ev.stopPropagation(); select({ field: track.field.name }); });
  }
  // columns
  for (const col of S.cols) {
    const head = el("g", { class: "col-head", "data-chapter": col.chapter.id }, columns);
    el("rect", { x: col.x, y: col.y, width: COL_W, height: col.headH, rx: 10, class: "card" }, head);
    el("text", { x: col.x + PAD, y: col.y + 20, class: "ch-num" }, head).textContent = `CHAPTER ${col.chapter.number}`;
    el("text", { x: col.x + PAD, y: col.y + 40, class: "ch-title" }, head).textContent = clip(col.chapter.title, 44);
    el("text", { x: col.x + PAD, y: col.y + 56, class: "ch-files" }, head).textContent = col.chapter.files;
    col.goal.forEach((line, i) => { el("text", { x: col.x + PAD, y: col.y + 76 + i * 16, class: "ch-goal" }, head).textContent = line; });
    head.addEventListener("click", (ev) => { ev.stopPropagation(); select({ chapter: col.chapter.id }); focusChapter(col.chapter.id); });
  }
  for (const box of S.boxes) drawBox(box, columns);
  applyView();
  $("calls-toggle").classList.toggle("on", S.allCalls);
}

function drawBox(box, parent) {
  const read = S.notes.items[box.key]?.read;
  const g = el("g", { class: `box kind-${box.kind}` + (box.kind === "stale" ? " stale" : "") + (read ? " read-done" : ""), "data-id": box.id }, parent);
  box.el = g;
  el("rect", { x: box.x, y: box.y, width: box.w, height: box.h, rx: 9, class: "body" }, g);
  el("rect", { x: box.x, y: box.y + 8, width: 4, height: box.h - 16, rx: 2, class: "accent", fill: cssVar(KIND_COLORS[box.kind] || "--kind-method") }, g);
  const badge = el("text", { x: box.x + PAD, y: box.y + 16, class: "badge" }, g);
  const sub = box.symbol?.subkind ? ` · ${box.symbol.subkind}` : "";
  badge.textContent = `${box.chapter.number}.${box.step}  ${KIND_LABEL[box.kind] || box.kind}${sub}` + (read ? "  ✓ read" : "");
  if (box.symbol) {
    const where = el("text", { x: box.x + box.w - PAD, y: box.y + 16, "text-anchor": "end", class: "lineno" }, g);
    const range = box.range ? `${box.range[0]}-${box.range[1]}` : `${box.symbol.line}`;
    where.textContent = `${shortFile(box.symbol.file)}:${range}`;
  }
  const title = el("text", { x: box.x + PAD, y: box.y + 33, class: "title" }, g);
  title.textContent = clip(boxTitle(box), Math.floor((COL_W - 2 * PAD) / (S.charW * 1.12)));
  let y = box.y + HEAD;
  for (const row of box.rows) {
    y += row.gapBefore || 0;
    if (row.bg === "note") el("rect", { x: box.x + PAD - 4, y: y - 1, width: box.w - 2 * PAD + 8, height: row.h + 1, class: "note-bg" }, g);
    if (row.spans.length) {
      const text = el("text", { x: box.x + PAD, y: y + 12 }, g);
      for (const span of row.spans) {
        const t = el("tspan", { class: span.cls || "" }, text);
        t.textContent = span.t;
        if (span.data) for (const [k, v] of Object.entries(span.data)) t.dataset[k] = v;
      }
    }
    y += row.h;
  }
  g.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (S.dragMoved) return;
    const data = ev.target.dataset || {};
    if (data.field) { select({ field: data.field }); return; }
    if (data.rfield) { select({ recordField: data.rfield }); return; }
    if (data.sym) { const target = S.symbolBox.get(data.sym); if (target) { select({ box: target }); return; } }
    select({ box: box.id });
  });
}

function boxTitle(box) {
  if (box.kind === "group") return box.item.title || box.ref.slice(6);
  if (box.kind === "stale") return box.ref;
  const s = box.symbol;
  const name = s.owner ? `${lastName(s.owner)}.${s.name}` : s.name;
  return box.part ? `${name}  (${box.part})` : name;
}

// --- selection ---------------------------------------------------------------------------------------------------------------

function select(what = {}, opts = {}) {
  if (what.box || what.field || what.recordField) { $("app").classList.remove("no-detail"); $("detail-toggle").textContent = "Hide panel"; }
  S.sel = what.box || null;
  S.field = what.field || null;
  S.recordField = what.recordField || null;
  if (what.chapter) S.chapter = what.chapter;
  else if (what.box) S.chapter = S.byId.get(what.box)?.chapter.id || S.chapter;
  else if (what.recordField) { const box = S.byId.get(S.symbolBox.get(what.recordField.split(".").slice(0, -1).join("."))); if (box) S.chapter = box.chapter.id; }
  highlight();
  drawEdges();
  renderSidebarState();
  if (S.sel) { renderDetail(S.byId.get(S.sel)); if (opts.pan !== false) reveal(S.byId.get(S.sel)); }
  else if (S.field) renderFieldDetail(S.field);
  else if (S.recordField) { renderRecordFieldDetail(S.recordField); const box = S.byId.get(S.symbolBox.get(S.recordField.split(".").slice(0, -1).join("."))); if (box && opts.pan !== false) reveal(box); }
  else if (what.chapter) renderChapterDetail(what.chapter);
}

function neighbours(box) {
  const out = { calls: [], builds: [], callers: [], consts: [], records: [], reads: [], writes: [] };
  if (!box.symbol && box.kind !== "group") return out;
  if (box.kind === "group") {
    for (const member of box.members) for (const user of S.constUsers.get(member.id) || []) out.callers.push(user);
    return out;
  }
  const s = box.symbol;
  for (const e of box.events) {
    if (e.kind === "call") out.calls.push(e.target);
    else if (e.kind === "build") out.builds.push(e.target);
    else if (e.kind === "const") out.consts.push(e.target);
    else if (e.kind === "field") out.records.push(e.owner);
    else if (e.kind === "read") out.reads.push(e.field);
    else if (e.kind === "write") out.writes.push(e.field);
  }
  out.callers = [...(s.called_by || [])];
  if (CLASS_KINDS.includes(s.kind)) {
    for (const [key, users] of S.fieldReaders) if (key.startsWith(s.id + ".")) out.callers.push(...users);
    for (const [key, users] of S.enumUsers) if (key.startsWith(s.id + ".")) out.callers.push(...users);
  }
  if (s.kind === "constant") out.callers.push(...(S.constUsers.get(s.id) || []));
  for (const impl of S.implementedBy.get(s.id) || []) out.calls.push(impl);
  for (const key of Object.keys(out)) out[key] = [...new Set(out[key])];
  return out;
}

function highlight() {
  const viewport = $("viewport");
  for (const box of S.boxes) box.el.classList.remove("lit", "selected", "callee", "caller", "built", "field-hit", "field-write");
  document.querySelectorAll(".col-head, .track").forEach((node) => node.classList.remove("lit"));
  const lit = (id, cls) => { const box = S.byId.get(S.symbolBox.get(id) || id); if (box) { box.el.classList.add("lit"); if (cls) box.el.classList.add(cls); } return box; };
  const litTrack = (name) => document.querySelector(`.track[data-field="${CSS.escape(name)}"]`)?.classList.add("lit");
  let dim = true;
  if (S.sel) {
    const box = S.byId.get(S.sel);
    box.el.classList.add("lit", "selected");
    const n = neighbours(box);
    n.calls.forEach((t) => lit(t, "callee")); n.consts.forEach((t) => lit(t, "callee"));
    n.builds.forEach((t) => lit(t, "built")); n.callers.forEach((t) => lit(t, "caller"));
    n.records.forEach((t) => lit(t, "field-hit"));
    n.reads.forEach(litTrack); n.writes.forEach(litTrack);
    document.querySelector(`.col-head[data-chapter="${box.chapter.id}"]`)?.classList.add("lit");
  } else if (S.field) {
    const track = S.trackByField.get(S.field);
    litTrack(S.field);
    track.field.writers.forEach((t) => lit(t, "field-write"));
    track.field.readers.forEach((t) => lit(t, "field-hit"));
  } else if (S.recordField) {
    const owner = S.recordField.split(".").slice(0, -1).join(".");
    lit(owner, "selected");
    (S.fieldReaders.get(S.recordField) || []).forEach((t) => lit(t, "field-hit"));
  } else if (S.chapter) {
    for (const box of S.boxes.filter((b) => b.chapter.id === S.chapter)) {
      box.el.classList.add("lit");
      const n = neighbours(box);
      [...n.calls, ...n.builds, ...n.callers, ...n.consts].forEach((t) => lit(t));
      n.reads.forEach(litTrack); n.writes.forEach(litTrack);
    }
    document.querySelector(`.col-head[data-chapter="${S.chapter}"]`)?.classList.add("lit");
  } else dim = false;
  viewport.classList.toggle("dimmed", dim);
}

// --- edges -------------------------------------------------------------------------------------------------------------------

function anchor(box, side, dy = 24) {
  return { x: side === "right" ? box.x + box.w : box.x, y: box.y + Math.min(dy, box.h / 2) };
}

function edge(from, to, cls, marker, parent, order = null, lane = 0) {
  if (!from || !to || from === to) return;
  let d;
  if (from.ci === to.ci) {
    const a = anchor(from, "right"), b = anchor(to, "right");
    const out = a.x + 18 + (lane % 7) * 8;
    const r = 8;
    const down = b.y > a.y ? 1 : -1;
    d = `M${a.x},${a.y} H${out - r} Q${out},${a.y} ${out},${a.y + r * down} V${b.y - r * down} Q${out},${b.y} ${out - r},${b.y} H${b.x + 2}`;
  } else {
    const rightward = to.ci > from.ci;
    const a = anchor(from, rightward ? "right" : "left"), b = anchor(to, rightward ? "left" : "right");
    const dx = Math.max(60, Math.abs(b.x - a.x) * 0.45) * (rightward ? 1 : -1);
    d = `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x + (rightward ? -2 : 2)},${b.y}`;
  }
  el("path", { d, class: cls, "marker-end": marker ? `url(#arrow-${marker})` : null }, parent);
  if (order !== null) {
    const a = from.ci === to.ci ? anchor(from, "right") : anchor(from, to.ci > from.ci ? "right" : "left");
    const dxLabel = from.ci === to.ci || to.ci > from.ci ? 10 : -16;
    el("text", { x: a.x + dxLabel, y: a.y - 4 - (lane % 3) * 0, class: "e-num" }, parent).textContent = order;
  }
}

function busConnector(box, fieldName, kind, parent, lane) {
  const track = S.trackByField.get(fieldName);
  if (!track) return;
  const gx = box.x - 30 + (kind === "read" ? 12 : 0) - (lane % 4) * 3;
  const y = box.y + 16;
  if (kind === "read") {
    el("path", { d: `M${gx},${track.y} V${y} H${box.x - 2}`, class: "e-read", "marker-end": "url(#arrow-read)" }, parent);
    el("circle", { cx: gx, cy: track.y, r: 3.5, class: "tap-r" }, parent);
  } else {
    el("path", { d: `M${box.x},${y} H${gx} V${track.y + 5}`, class: "e-write", "marker-end": "url(#arrow-write)" }, parent);
    el("circle", { cx: gx, cy: track.y, r: 4, class: "tap-w" }, parent);
  }
}

function drawEdges() {
  const layer = $("edges");
  layer.innerHTML = "";
  const boxOf = (id) => S.byId.get(S.symbolBox.get(id) || id);
  if (S.allCalls) {
    let lane = 0;
    for (const box of S.boxes) for (const t of new Set(box.events.filter((e) => e.kind === "call").map((e) => e.target))) edge(box, boxOf(t), "e-all", null, layer, null, lane++);
  }
  const drawFor = (box, withIncoming, numbered) => {
    const n = neighbours(box);
    let lane = 0, order = 1;
    for (const t of n.calls) edge(box, boxOf(t), "e-call", "call", layer, numbered ? order++ : null, lane++);
    for (const t of n.consts) edge(box, boxOf(t), "e-callin", "callin", layer, null, lane++);
    for (const t of n.builds) edge(box, boxOf(t), "e-build", "build", layer, null, lane++);
    if (withIncoming) for (const t of n.callers) edge(boxOf(t), box, "e-callin", "callin", layer, null, lane++);
    n.reads.forEach((f, i) => busConnector(box, f, "read", layer, i));
    n.writes.forEach((f, i) => busConnector(box, f, "write", layer, i));
  };
  if (S.sel) drawFor(S.byId.get(S.sel), true, true);
  else if (S.field) {
    const track = S.trackByField.get(S.field);
    track.field.writers.forEach((t, i) => { const b = boxOf(t); if (b) busConnector(b, S.field, "write", layer, i); });
    track.field.readers.forEach((t, i) => { const b = boxOf(t); if (b) busConnector(b, S.field, "read", layer, i); });
  } else if (S.recordField) {
    const owner = boxOf(S.recordField.split(".").slice(0, -1).join("."));
    (S.fieldReaders.get(S.recordField) || []).forEach((t, i) => edge(owner, boxOf(t), "e-read", "read", layer, null, i));
  } else if (S.chapter) {
    for (const box of S.boxes.filter((b) => b.chapter.id === S.chapter)) drawFor(box, false, false);
  }
}

// --- panning and zooming ------------------------------------------------------------------------------------------------------

function applyView() {
  $("viewport").setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
  $("zoom-level").textContent = `${Math.round(S.view.k * 100)}%`;
}

function stageSize() { const r = $("canvas").getBoundingClientRect(); return { w: r.width, h: r.height }; }

function fitAll() {
  const { w, h } = stageSize();
  const k = Math.min(w / S.width, h / S.height) * 0.96;
  S.view = { k, x: (w - S.width * k) / 2, y: 10 };
  applyView();
}

function focusChapter(id) {
  const col = S.cols.find((c) => c.chapter.id === id);
  if (!col) return;
  const { w } = stageSize();
  const k = Math.max(0.35, Math.min(1, w / (COL_W + COL_GAP * 1.6)));
  S.view = { k, x: -(col.x - COL_GAP * 0.55) * k, y: -(col.y - 30) * k };
  applyView();
}

function reveal(box) {
  if (!box) return;
  const { w, h } = stageSize();
  let k = Math.max(S.view.k, 0.55);
  const left = box.x * k + S.view.x, top = box.y * k + S.view.y;
  const inside = left > 20 && left + box.w * k < w - 20 && top > 20 && top + Math.min(box.h, 300) * k < h - 20 && k === S.view.k;
  if (inside) return;
  S.view = { k, x: w / 2 - (box.x + box.w / 2) * k, y: Math.min(60, h / 3 - box.y * k) };
  applyView();
}

function zoomAt(factor, cx, cy) {
  const k = Math.max(0.06, Math.min(2.5, S.view.k * factor));
  S.view.x = cx - (cx - S.view.x) * (k / S.view.k);
  S.view.y = cy - (cy - S.view.y) * (k / S.view.k);
  S.view.k = k;
  applyView();
}

function setupPanZoom() {
  const svg = $("canvas");
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = svg.getBoundingClientRect();
    if (ev.ctrlKey || ev.metaKey) zoomAt(Math.exp(-ev.deltaY * 0.01), ev.clientX - r.left, ev.clientY - r.top);
    else { S.view.x -= ev.deltaX; S.view.y -= ev.deltaY; applyView(); }
  }, { passive: false });
  let drag = null;
  svg.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    drag = { x: ev.clientX, y: ev.clientY, vx: S.view.x, vy: S.view.y };
    S.dragMoved = false;
  });
  window.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (!S.dragMoved && Math.hypot(dx, dy) < 4) return;
    S.dragMoved = true;
    svg.classList.add("panning");
    S.view.x = drag.vx + dx; S.view.y = drag.vy + dy; applyView();
  });
  window.addEventListener("pointerup", () => {
    svg.classList.remove("panning");
    drag = null;
    setTimeout(() => { S.dragMoved = false; }, 0);
  });
  svg.addEventListener("click", () => { if (!S.dragMoved) { S.chapter = null; select({}); renderWelcome(); } });
}

// --- sidebar -------------------------------------------------------------------------------------------------------------

function renderSidebar() {
  const nav = $("chapters");
  nav.innerHTML = "";
  for (const chapter of S.map.chapters) {
    const wrapNode = document.createElement("div");
    wrapNode.className = "chapter";
    wrapNode.dataset.chapter = chapter.id;
    const boxes = S.boxes.filter((b) => b.chapter.id === chapter.id);
    const done = boxes.filter((b) => S.notes.items[b.key]?.read).length;
    wrapNode.innerHTML = `<div class="chapter-head"><span class="chapter-num">${chapter.number}</span><span class="chapter-title">${esc(chapter.title)}</span>`
      + `<span class="chapter-progress ${done === boxes.length && boxes.length ? "done" : ""}">${done}/${boxes.length}</span></div><div class="chapter-items"></div>`;
    const list = wrapNode.querySelector(".chapter-items");
    for (const box of boxes) {
      const step = document.createElement("div");
      step.className = "step" + (box.kind === "stale" ? " stale" : "");
      step.dataset.id = box.id;
      const read = S.notes.items[box.key]?.read;
      step.innerHTML = `<span class="n">${box.step}</span><span class="dot" style="background:${cssVar(KIND_COLORS[box.kind] || "--kind-method")}"></span>`
        + `<span class="name" title="${esc(box.ref)}">${esc(boxTitle(box))}</span><span class="tick">${read ? "✓" : ""}</span>`;
      step.addEventListener("click", () => select({ box: box.id }));
      list.appendChild(step);
    }
    wrapNode.querySelector(".chapter-head").addEventListener("click", () => {
      const open = wrapNode.classList.contains("open") && S.chapter === chapter.id && !S.sel;
      select({ chapter: chapter.id });
      focusChapter(chapter.id);
      wrapNode.classList.toggle("open", !open);
    });
    nav.appendChild(wrapNode);
  }
  renderSidebarState();
}

function renderSidebarState() {
  document.querySelectorAll(".chapter").forEach((node) => {
    const active = node.dataset.chapter === S.chapter;
    node.classList.toggle("active", active);
    if (active) node.classList.add("open");
  });
  document.querySelectorAll(".step").forEach((node) => node.classList.toggle("active", node.dataset.id === S.sel));
  const active = document.querySelector(".step.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function renderWarnings() {
  const box = $("warnings");
  box.innerHTML = (S.map.warnings || []).map((w) => `<div>${esc(w)}</div>`).join("");
}

// --- the detail panel ----------------------------------------------------------------------------------------------------------

const chipList = (items, cls, attr) => `<div class="chips">${items.map((item) => `<span class="chip ${cls}" ${attr ? `data-${attr}="${esc(item.value)}"` : ""}>${esc(item.label)}</span>`).join("")}</div>`;
const section = (title) => `<div class="d-section">${esc(title)}</div>`;
const row = (label, html) => `<div class="d-row"><span>${esc(label)}</span><div>${html}</div></div>`;

function crumbs(box) {
  const index = S.order.indexOf(box.id);
  const total = S.boxes.filter((b) => b.chapter.id === box.chapter.id).length;
  return `<div class="d-crumbs"><span>Chapter ${box.chapter.number} · ${esc(box.chapter.title)} · step ${box.step} of ${total}</span>`
    + `<span><button data-nav="${index - 1}" ${index <= 0 ? "disabled" : ""}>◀ prev</button> <button data-nav="${index + 1}" ${index >= S.order.length - 1 ? "disabled" : ""}>next ▶</button></span></div>`;
}

function kindBadge(kind) { return `<span class="d-kind" style="background:${cssVar(KIND_COLORS[kind] || "--kind-method")}">${esc(KIND_LABEL[kind] || kind)}</span>`; }

function codeBlock(symbol, range) {
  const lines = symbol.code.filter((line) => !range || (line.n >= range[0] && line.n <= range[1]));
  return `<div class="code">${lines.map((line) => `<div class="ln"><span class="num">${line.n}</span><span class="src">${line.html || " "}</span></div>`).join("")}</div>`;
}

function whereLine(symbol, range) {
  const repo = S.map.source.repo;
  const line = range ? range[0] : symbol.line;
  const end = range ? range[1] : symbol.end_line;
  const path = `${repo}/${symbol.file}`;
  return `<div class="d-where">${esc(symbol.file)}:${line}–${end}`
    + `<a href="vscode://file/${encodeURI(path)}:${line}" title="Open in VS Code">VS Code</a>`
    + `<a href="#" data-copy="${esc(path)}:${line}" title="Copy path:line">copy path</a></div>`;
}

function myNoteBlock(box) {
  const entry = S.notes.items[box.key] || {};
  return `${section("Your notes")}<label class="readbox"><input type="checkbox" id="read-mark" ${entry.read ? "checked" : ""}> I've read and understood this</label>`
    + `<textarea class="mynote" id="my-note" placeholder="Anything to remember, rename or fix here…">${esc(entry.note || "")}</textarea><div class="saved" id="saved"></div>`;
}

function renderDetail(box) {
  const body = $("detail-body");
  const s = box.symbol;
  let html = crumbs(box);
  html += `<div class="d-title">${kindBadge(box.kind)}<h2>${esc(boxTitle(box))}</h2></div>`;
  if (s) html += whereLine(s, box.range);
  if (box.item.note) html += `<div class="d-note"><b>Reading note</b>${rich(box.item.note)}</div>`;
  if (box.kind === "stale") html += `<p>This reference isn't in the code any more. Rename or remove it in the order file${S.map.source.order_file ? ` (<code>${esc(S.map.source.order_file)}</code>)` : ""}.</p>`;
  if (s?.comment?.length) html += `<div class="d-comment">${rich(s.comment.join(" "))}</div>`;

  if (box.kind === "function" || box.kind === "method") {
    const params = (s.params || []).map((p) => `${p.name}${p.type ? ": " + p.type : ""}`).join(",\n    ");
    html += `<div class="d-sig">${esc(s.name)}(\n    ${esc(params)}\n)${s.returns ? " -> " + esc(s.returns) : ""}</div>`;
    const events = box.events;
    const uniq = (list) => [...new Set(list)];
    const reads = uniq(events.filter((e) => e.kind === "read").map((e) => e.field));
    const writes = uniq(events.filter((e) => e.kind === "write").map((e) => e.field));
    html += section("Data");
    if (reads.length) html += row(`reads ${trackName()}`, chipList(reads.map((f) => ({ label: f, value: f })), "read", "field"));
    if (writes.length) html += row(`writes ${trackName()}`, chipList(writes.map((f) => ({ label: f, value: f })), "write", "field"));
    const fields = new Map();
    for (const e of events.filter((e) => e.kind === "field")) { if (!fields.has(e.owner)) fields.set(e.owner, new Set()); fields.get(e.owner).add(e.field); }
    for (const [owner, names] of fields) html += row(`reads ${lastName(owner)}`, chipList([...names].map((f) => ({ label: f, value: `${owner}.${f}` })), "read", "rfield"));
    const attrs = uniq(events.filter((e) => e.kind === "attr").map((e) => e.field));
    if (attrs.length) html += row("uses self", chipList(attrs.map((a) => ({ label: "self." + a })), "plain"));
    const consts = uniq(events.filter((e) => e.kind === "const").map((e) => e.target));
    if (consts.length) html += row("constants", chipList(consts.map((c) => ({ label: lastName(c), value: c })), "", "sym"));
    const builds = uniq(events.filter((e) => e.kind === "build").map((e) => e.target));
    if (builds.length) html += row("builds", chipList(builds.map((b) => ({ label: lastName(b), value: b })), "build", "sym"));
    const raises = uniq(events.filter((e) => e.kind === "raise").map((e) => e.name));
    if (raises.length) html += row("raises", chipList(raises.map((r) => ({ label: r })), "flag"));
    const calls = uniq(events.filter((e) => e.kind === "call").map((e) => e.target));
    html += section("Calls, in the order they run");
    html += calls.length ? chipList(calls.map((c, i) => ({ label: `${i + 1}. ${calledName(c, box)}`, value: c })), "", "sym") : `<div class="d-row"><span>none</span></div>`;
    const external = uniq(events.filter((e) => e.kind === "external").map((e) => e.name)).filter((n) => !/^(len|str|int|float|bool|sorted|sum|any|all|max|min|round|isinstance|range|enumerate|zip|list|set|dict|next|print|super|getattr|type|repr)$/.test(n));
    if (external.length) html += row("also calls", chipList(external.slice(0, 30).map((n) => ({ label: n })), "plain"));
    html += section("Called by");
    const through = s.overrides ? (S.map.symbols[s.overrides]?.called_by || []) : [];
    if (s.called_by?.length) html += chipList(s.called_by.map((c) => ({ label: shortName(c), value: c })), "", "sym");
    else if (through.length) html += row(`through ${shortName(s.overrides)}`, chipList(through.map((c) => ({ label: shortName(c), value: c })), "", "sym"));
    else html += `<div class="d-row"><span>nothing in the repo calls it directly (an entry point, a callback, or called by name)</span></div>`;
    const impl = S.implementedBy.get(s.id) || [];
    if (impl.length) html += row("done by", chipList(impl.map((c) => ({ label: shortName(c), value: c })), "", "sym"));
    if (s.overrides) html += row("overrides", chipList([{ label: shortName(s.overrides), value: s.overrides }], "", "sym"));
    html += section("Source") + codeBlock(s, box.range);
  } else if (CLASS_KINDS.includes(box.kind)) {
    if (s.bases?.length) html += row("based on", esc(s.bases.join(", ")));
    if (s.fields?.length) {
      html += section("Fields") + `<table class="fields">${s.fields.map((f) => {
        const users = S.fieldReaders.get(`${s.id}.${f.name}`) || [];
        return `<tr><td class="fname">${esc(f.name)}</td><td class="ftype">${esc(f.type)}${f.default != null ? ` = ${esc(f.default)}` : ""}</td>`
          + `<td class="fcom">${rich(f.comment || f.above || "")}${users.length ? `<div class="users">${chipList(users.map((u) => ({ label: shortName(u), value: u })), "read", "sym")}</div>` : ""}</td></tr>`;
      }).join("")}</table>`;
    }
    if (s.members?.length) {
      html += section("Values") + `<table class="fields">${s.members.map((m) => {
        const users = S.enumUsers.get(`${s.id}.${m.name}`) || [];
        return `<tr><td class="fname">${esc(m.name)}</td><td class="ftype">${esc(JSON.stringify(m.value))}</td><td class="fcom">${rich(m.comment)}${users.length ? `<div class="users">${chipList(users.map((u) => ({ label: shortName(u), value: u })), "", "sym")}</div>` : ""}</td></tr>`;
      }).join("")}</table>`;
    }
    if (s.class_attrs?.length) html += section("Class attributes") + `<table class="fields">${s.class_attrs.map((a) => `<tr><td class="fname">${esc(a.name)}</td><td class="ftype">${esc(a.value)}</td><td class="fcom">${rich(a.comment)}</td></tr>`).join("")}</table>`;
    if (s.inst_attrs?.length) html += section("Set in __init__") + `<table class="fields">${s.inst_attrs.map((a) => `<tr><td class="fname">self.${esc(a.name)}</td><td class="ftype">${esc(a.type)}</td><td></td></tr>`).join("")}</table>`;
    if (s.methods?.length) html += section("Methods") + chipList(s.methods.map((m) => ({ label: lastName(m), value: m })), "", "sym");
    if (s.called_by?.length) html += section("Built or used by") + chipList(s.called_by.map((c) => ({ label: shortName(c), value: c })), "build", "sym");
    html += section("Source") + codeBlock(s, null);
  } else if (box.kind === "constant" || box.kind === "group") {
    const members = box.kind === "group" ? box.members : [s];
    for (const m of members) {
      const users = S.constUsers.get(m.id) || [];
      html += section(m.name);
      if (m.comment?.length) html += `<div class="d-comment">${rich(m.comment.join(" "))}</div>`;
      html += codeBlock(m, null);
      if (users.length) html += row("used by", chipList(users.map((u) => ({ label: shortName(u), value: u })), "", "sym"));
    }
  }
  html += myNoteBlock(box);
  body.innerHTML = html;
  wireDetail(box);
}

function renderFieldDetail(name) {
  const track = S.trackByField.get(name);
  const field = track.field;
  $("detail-body").innerHTML = `<div class="d-crumbs"><span>The ${esc(trackName())}</span></div><div class="d-title">${kindBadge("record")}<h2>${esc(trackName())}.${esc(name)}</h2></div>`
    + `<div class="d-sig">${esc(name)}: ${esc(field.type)}${field.default != null ? " = " + esc(field.default) : ""}</div>`
    + (field.above ? `<div class="d-comment">${rich(field.above)}</div>` : "") + (field.comment ? `<div class="d-comment">${rich(field.comment)}</div>` : "")
    + section("Written by") + chipList(field.writers.map((w) => ({ label: shortName(w), value: w })), "write", "sym")
    + section("Read by") + (field.readers.length ? chipList(field.readers.map((r) => ({ label: shortName(r), value: r })), "read", "sym") : "<div class='d-row'><span>nothing reads it yet</span></div>");
  wireDetail(null);
}

function renderRecordFieldDetail(key) {
  const owner = key.split(".").slice(0, -1).join(".");
  const name = key.split(".").pop();
  const symbol = S.map.symbols[owner];
  const field = symbol?.fields?.find((f) => f.name === name);
  const users = S.fieldReaders.get(key) || [];
  $("detail-body").innerHTML = `<div class="d-crumbs"><span>A record field</span></div><div class="d-title">${kindBadge("record")}<h2>${esc(lastName(owner))}.${esc(name)}</h2></div>`
    + (field ? `<div class="d-sig">${esc(name)}: ${esc(field.type)}${field.default != null ? " = " + esc(field.default) : ""}</div>` + (field.comment ? `<div class="d-comment">${rich(field.comment)}</div>` : "") : "")
    + section("Built in") + chipList((symbol?.called_by || []).map((c) => ({ label: shortName(c), value: c })), "build", "sym")
    + section("Read by") + (users.length ? chipList(users.map((u) => ({ label: shortName(u), value: u })), "read", "sym") : "<div class='d-row'><span>no method reads this field directly</span></div>")
    + `<p class="d-row"><span></span><span>Blue lines on the map run from the record to every method that reads this field.</span></p>`;
  wireDetail(null);
}

function renderChapterDetail(id) {
  const chapter = S.map.chapters.find((c) => c.id === id);
  if (!chapter) return;
  const boxes = S.boxes.filter((b) => b.chapter.id === id);
  $("detail-body").innerHTML = `<div class="d-crumbs"><span>Chapter ${chapter.number} of ${S.map.chapters.length}</span></div>`
    + `<div class="d-title"><h2 style="font-family:var(--sans)">${esc(chapter.title)}</h2></div><div class="d-where">${esc(chapter.files)}</div>`
    + (chapter.goal ? `<div class="d-note"><b>What happens here</b>${rich(chapter.goal)}</div>` : "")
    + (chapter.discuss?.length ? section("To discuss") + `<ul class="d-discuss">${chapter.discuss.map((d) => `<li>${rich(d)}</li>`).join("")}</ul>` : "")
    + section("Read in this order") + `<ol class="d-discuss">${boxes.map((b) => `<li><a href="#" data-box="${b.id}">${esc(boxTitle(b))}</a>${S.notes.items[b.key]?.read ? " ✓" : ""}</li>`).join("")}</ol>`;
  wireDetail(null);
}

function renderWelcome() {
  const chapters = S.map?.chapters || [];
  const notes = S.map?.entry_notes || [];
  const hand = chapters.some((c) => c.section === "hand");
  $("detail-body").innerHTML = `<div class="empty"><h2>How to read ${esc(S.map?.source.name || "the code")} with this map</h2>`
    + `<p>The columns are a reading order${hand ? " (the first ones from your order file)" : ""}: the map starts where a run starts and follows each call the first time it happens, so a helper sits right after the code that first uses it, and a class right before the code that first builds or reads it.</p>`
    + (notes.length ? `<p>It starts at ${notes.map((n) => `<code>${esc(n)}</code>`).join(", ")}.</p>` : "")
    + `<ol>${chapters.map((c) => `<li><a href="#" data-chapter="${c.id}">${esc(c.title)}</a></li>`).join("")}</ol>`
    + `<p>Click a chapter to light up everything it touches, a box to see its code and connections${S.map?.track ? `, a line at the top to see who writes and reads that ${esc(trackName())} field` : ""}, and a record's field to see every function that reads it. `
    + `Scroll to pan, pinch or ⌘-scroll to zoom. The map rebuilds itself whenever the code changes, and your read marks follow code that moves.</p></div>`;
  wireDetail(null);
}

function wireDetail(box) {
  const body = $("detail-body");
  body.querySelectorAll("[data-nav]").forEach((b) => b.addEventListener("click", () => {
    const id = S.order[Number(b.dataset.nav)];
    if (id) select({ box: id });
  }));
  body.querySelectorAll("[data-sym]").forEach((node) => node.addEventListener("click", () => {
    const target = S.symbolBox.get(node.dataset.sym);
    if (target) select({ box: target });
  }));
  body.querySelectorAll("[data-field]").forEach((node) => node.addEventListener("click", () => select({ field: node.dataset.field })));
  body.querySelectorAll("[data-rfield]").forEach((node) => node.addEventListener("click", () => select({ recordField: node.dataset.rfield })));
  body.querySelectorAll("[data-box]").forEach((node) => node.addEventListener("click", (ev) => { ev.preventDefault(); select({ box: node.dataset.box }); }));
  body.querySelectorAll("[data-chapter]").forEach((node) => node.addEventListener("click", (ev) => { ev.preventDefault(); select({ chapter: node.dataset.chapter }); focusChapter(node.dataset.chapter); }));
  body.querySelectorAll("a.ref").forEach((node) => node.addEventListener("click", () => {
    const target = S.symbolBox.get(node.dataset.ref);
    if (target) select({ box: target });
  }));
  body.querySelectorAll("[data-copy]").forEach((node) => node.addEventListener("click", (ev) => {
    ev.preventDefault();
    navigator.clipboard?.writeText(node.dataset.copy);
    node.textContent = "copied";
  }));
  if (!box) return;
  const note = $("my-note"), read = $("read-mark"), saved = $("saved");
  let timer = null;
  note?.addEventListener("input", () => {
    clearTimeout(timer);
    saved.textContent = "…";
    timer = setTimeout(() => saveNote(box, { note: note.value }).then(() => { saved.textContent = "saved"; }), 500);
  });
  read?.addEventListener("change", () => saveNote(box, { read: read.checked }).then(() => refreshRead(box)));
}

async function saveNote(box, update) {
  const entry = (S.notes.items[box.key] = { ...(S.notes.items[box.key] || {}), ...update });
  try {
    await fetch("/api/notes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: box.key, ...update }) });
  } catch (err) { showError("Couldn't save the note: " + err.message); }
  return entry;
}

function refreshRead(box) {
  const read = S.notes.items[box.key]?.read;
  box.el.classList.toggle("read-done", !!read);
  const badge = box.el.querySelector(".badge");
  if (badge) badge.textContent = badge.textContent.replace(/\s+✓ read$/, "") + (read ? "  ✓ read" : "");
  renderSidebar();
}

// --- search -------------------------------------------------------------------------------------------------------------------

function setupSearch() {
  const input = $("search"), results = $("search-results");
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    results.innerHTML = "";
    if (!q || !S.map) return;
    const hits = [];
    for (const box of S.boxes) if (boxTitle(box).toLowerCase().includes(q) || box.ref.toLowerCase().includes(q)) hits.push({ label: boxTitle(box), hint: `ch ${box.chapter.number}.${box.step}`, go: () => select({ box: box.id }) });
    for (const track of S.tracks) if (track.field.name.toLowerCase().includes(q)) hits.push({ label: `${trackName()}.${track.field.name}`, hint: `${trackName()} field`, go: () => select({ field: track.field.name }) });
    for (const symbol of Object.values(S.map.symbols)) for (const field of symbol.fields || []) {
      const key = `${symbol.id}.${field.name}`;
      if (`${symbol.name}.${field.name}`.toLowerCase().includes(q) && symbol.id !== S.map.track?.id) hits.push({ label: `${symbol.name}.${field.name}`, hint: "field", go: () => select({ recordField: key }) });
    }
    for (const hit of hits.slice(0, 40)) {
      const node = document.createElement("div");
      node.innerHTML = `<span>${esc(hit.label)}</span><small>${esc(hit.hint)}</small>`;
      node.addEventListener("click", () => { hit.go(); results.innerHTML = ""; input.value = ""; });
      results.appendChild(node);
    }
  });
  input.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { input.value = ""; results.innerHTML = ""; input.blur(); } if (ev.key === "Enter") results.firstChild?.click(); });
}

// --- keys, toolbar, live reload ---------------------------------------------------------------------------------------------

function setupKeys() {
  window.addEventListener("keydown", (ev) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    const index = S.sel ? S.order.indexOf(S.sel) : -1;
    if (ev.key === "j" || ev.key === "ArrowDown" || ev.key === "ArrowRight") { ev.preventDefault(); select({ box: S.order[Math.min(S.order.length - 1, index + 1)] }); }
    else if (ev.key === "k" || ev.key === "ArrowUp" || ev.key === "ArrowLeft") { ev.preventDefault(); if (index > 0) select({ box: S.order[index - 1] }); }
    else if (ev.key === "f") fitAll();
    else if (ev.key === "r" && S.sel) { const box = S.byId.get(S.sel); saveNote(box, { read: !S.notes.items[box.key]?.read }).then(() => { refreshRead(box); renderDetail(box); }); }
    else if (ev.key === "Escape") { S.chapter = null; select({}); renderWelcome(); }
    else if (ev.key === "+" || ev.key === "=") { const { w, h } = stageSize(); zoomAt(1.2, w / 2, h / 2); }
    else if (ev.key === "-") { const { w, h } = stageSize(); zoomAt(1 / 1.2, w / 2, h / 2); }
  });
}

function setupToolbar() {
  $("fit").addEventListener("click", fitAll);
  $("detail-toggle").addEventListener("click", () => {
    const hidden = $("app").classList.toggle("no-detail");
    $("detail-toggle").textContent = hidden ? "Show panel" : "Hide panel";
  });
  $("calls-toggle").addEventListener("click", () => { S.allCalls = !S.allCalls; $("calls-toggle").classList.toggle("on", S.allCalls); drawEdges(); });
  $("reload").addEventListener("click", () => loadMap(true));
  $("ref").addEventListener("change", (ev) => { S.ref = ev.target.value; loadMap(true); });
  $("live").addEventListener("change", (ev) => { S.live = ev.target.checked; });
  const toolbar = document.querySelector(".toolbar");
  const compact = document.createElement("button");
  compact.textContent = "Compact";
  compact.title = "Hide the per-field comments and the fields/uses/raises rows";
  compact.addEventListener("click", () => {
    S.compact = !S.compact; compact.classList.toggle("on", S.compact);
    const keep = { sel: S.sel, chapter: S.chapter, field: S.field, recordField: S.recordField };
    layout(); render();
    select(keep.sel ? { box: keep.sel } : keep.field ? { field: keep.field } : keep.recordField ? { recordField: keep.recordField } : keep.chapter ? { chapter: keep.chapter } : {}, { pan: false });
  });
  toolbar.insertBefore(compact, $("zoom-level"));
  for (const [label, factor] of [["−", 1 / 1.25], ["+", 1.25]]) {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { const { w, h } = stageSize(); zoomAt(factor, w / 2, h / 2); });
    toolbar.insertBefore(b, $("zoom-level"));
  }
}

function startPolling() {
  clearInterval(S.polling);
  S.polling = setInterval(async () => {
    if (!S.live || document.hidden || !S.map) return;
    try {
      const res = await fetch("/api/fingerprint" + (S.ref ? "?ref=" + encodeURIComponent(S.ref) : ""));
      const data = await res.json();
      if (data.fingerprint && data.fingerprint !== S.map.fingerprint) loadMap(true);
    } catch (err) { /* the server is restarting; try again next tick */ }
  }, 2000);
}

async function main() {
  measureFonts();
  setupPanZoom();
  setupSearch();
  setupKeys();
  setupToolbar();
  await Promise.all([loadRefs(), loadNotes()]);
  await loadMap(false);
  startPolling();
  window.addEventListener("resize", () => applyView());
}

main();
