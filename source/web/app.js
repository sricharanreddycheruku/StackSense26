"use strict";

const SECTION_ORDER = ["general", "project", "scale", "team", "budget", "timeline",
  "infrastructure", "requirements", "compliance", "priorities"];
const SECTION_TITLES = { general: "Scenario", project: "Project", scale: "Scale", team: "Team",
  budget: "Budget", timeline: "Timeline", infrastructure: "Infrastructure",
  requirements: "Requirements", compliance: "Compliance", priorities: "Priorities (1 to 5)" };
// Optional inputs beyond the base specification; used when the schema does not flag them itself.
const EXTRA_PATHS = new Set(["scale.peak_rps", "scale.user_geography", "team.size_in_12_months",
  "infrastructure.vendor_lock_in_tolerance", "infrastructure.existing_licences",
  "requirements.data_sensitivity", "requirements.consistency", "requirements.background_jobs",
  "requirements.notifications", "requirements.connectivity", "requirements.min_device_ram_gb",
  "requirements.rpo_minutes"]);
const DESC_PATH = "project.description";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { fields: {}, rawOverride: null, lastOutput: null, suggestions: [] };

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { /* non-JSON body */ }
  if (!r.ok) throw new Error((body && body.error) || `${url}: HTTP ${r.status}`);
  return body;
}

/* ---------- form generation ---------- */

function normaliseSchema(s) {
  // The API sends {sections: [{id, fields}]}; a flat array or {fields} is accepted too.
  const list = Array.isArray(s) ? s : (s.fields || (s.sections || []).flatMap((sec) => sec.fields || []));
  return list.map((f) => ({
    ...f,
    extra: !!(f.extra || f.is_extra || EXTRA_PATHS.has(f.path)),
    section: f.path.includes(".") ? f.path.split(".")[0] : (f.path === "compliance" ? "compliance" : "general"),
  }));
}

function labelFor(f) {
  const leaf = f.path.split(".").pop().replace(/_/g, " ");
  return leaf.charAt(0).toUpperCase() + leaf.slice(1);
}

function buildControl(f) {
  const type = String(f.type || "string").toLowerCase();
  const isList = /list|array/.test(type);
  const isBool = /bool/.test(type);
  const isNum = /^(int|integer|num|number|float)$/.test(type);
  const hasEnum = Array.isArray(f.enum) && f.enum.length > 0;
  const id = "f-" + f.path.replace(/\./g, "-");
  const wrap = document.createElement("div");
  wrap.className = "control";
  let get, set;

  if (hasEnum && isList) {
    wrap.innerHTML = `<div class="checks" id="${id}">` + f.enum.map((v, i) =>
      `<label><input type="checkbox" value="${esc(v)}" id="${id}-${i}"> ${esc(v)}</label>`).join("") + "</div>";
    const boxes = () => [...wrap.querySelectorAll("input")];
    get = () => { const v = boxes().filter((b) => b.checked).map((b) => b.value); return v.length ? v : undefined; };
    set = (val) => { const a = Array.isArray(val) ? val : [val]; boxes().forEach((b) => { b.checked = a.includes(b.value); }); };
  } else if (hasEnum) {
    wrap.innerHTML = `<select id="${id}"><option value="">(not set)</option>` +
      f.enum.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("") + "</select>";
    const el = wrap.firstChild;
    get = () => (el.value === "" ? undefined : (isNum ? Number(el.value) : el.value));
    set = (val) => { el.value = val == null ? "" : String(val); };
  } else if (isBool && f.extra) {
    wrap.innerHTML = `<select id="${id}"><option value="">(not set, use default)</option><option value="true">yes</option><option value="false">no</option></select>`;
    const el = wrap.firstChild;
    get = () => (el.value === "" ? undefined : el.value === "true");
    set = (val) => { el.value = val == null ? "" : String(!!val); };
  } else if (isBool) {
    wrap.innerHTML = `<label><input type="checkbox" id="${id}"> yes</label>`;
    const el = wrap.querySelector("input");
    get = () => el.checked;
    set = (val) => { el.checked = !!val; };
  } else if (isList) {
    const ph = f.default && Array.isArray(f.default) && f.default.length ? f.default.join(", ") : "comma separated";
    wrap.innerHTML = `<input type="text" id="${id}" placeholder="${esc(ph)}">`;
    const el = wrap.firstChild;
    get = () => {
      const v = el.value.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
      return v.length || f.required ? v : undefined;
    };
    set = (val) => { el.value = Array.isArray(val) ? val.join(", ") : String(val ?? ""); };
  } else {
    const range = (f.min != null || f.max != null) ? ` (${f.min ?? ""}..${f.max ?? ""})` : "";
    wrap.innerHTML = `<input type="text" id="${id}"${isNum ? ' inputmode="decimal"' : ""} placeholder="${esc(isNum ? "number" + range : "")}">`;
    const el = wrap.firstChild;
    get = () => {
      const t = el.value.trim();
      if (t === "") return undefined;
      if (isNum && /^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(t)) return Number(t);
      return t;
    };
    set = (val) => { el.value = val == null ? "" : String(val); };
  }
  return { wrap, get, set, id };
}

function buildField(f) {
  const c = buildControl(f);
  const box = document.createElement("div");
  box.className = "field" + (f.extra ? " is-extra" : "");
  box.dataset.path = f.path;
  const badges = (f.required ? '<span class="badge req">required</span> ' : "") +
    (f.extra ? '<span class="badge extra">extra input</span> ' : "");
  const affects = (f.affects && f.affects.length)
    ? `<div class="affects">Affects: ${f.affects.map(esc).join(", ")}</div>` : "";
  const dflt = (!f.required && f.default != null && f.default !== "" && !(Array.isArray(f.default) && !f.default.length))
    ? ` <span class="affects">Default: ${esc(JSON.stringify(f.default))}</span>` : "";
  box.innerHTML = `<div><label class="name" for="${c.id}">${esc(labelFor(f))}</label>${badges}<span class="path">${esc(f.path)}</span></div>`;
  box.appendChild(c.wrap);
  const why = document.createElement("div");
  why.innerHTML = `<p class="why"><b>Why we ask:</b> ${esc(f.help || "Used by the recommendation rules.")}${dflt}</p>${affects}`;
  box.appendChild(why);
  const err = document.createElement("div");
  err.className = "err"; err.hidden = true;
  box.appendChild(err);
  const clear = () => { state.rawOverride = null; $("raw-note").hidden = true; clearFieldError(f.path); };
  box.addEventListener("input", clear);
  box.addEventListener("change", clear);
  state.fields[f.path] = { spec: f, get: c.get, set: c.set, box, err };
  return box;
}

function buildForm(fields) {
  const form = $("form");
  form.innerHTML = "";
  if (!fields.some((f) => f.path === "scenario_id")) {
    fields = [{ path: "scenario_id", type: "string", required: false,
      help: "A short label echoed back in the output file name.", section: "general" }, ...fields];
  }
  const groups = {};
  fields.forEach((f) => { (groups[f.section] = groups[f.section] || []).push(f); });
  const names = [...SECTION_ORDER.filter((s) => groups[s]), ...Object.keys(groups).filter((s) => !SECTION_ORDER.includes(s))];
  names.forEach((name) => {
    const fs = document.createElement("fieldset");
    fs.className = "sec"; fs.id = "sec-" + name;
    fs.innerHTML = `<legend>${esc(SECTION_TITLES[name] || name)}</legend>`;
    groups[name].forEach((f) => {
      if (f.path === DESC_PATH) { bindDescription(f); return; }
      fs.appendChild(buildField(f));
    });
    if (fs.children.length > 1) form.appendChild(fs);
  });
}

function bindDescription(f) {
  const el = $("desc-text");
  state.fields[DESC_PATH] = {
    spec: f, get: () => (el.value.trim() === "" ? undefined : el.value),
    set: (v) => { el.value = v == null ? "" : String(v); },
    box: $("description"), err: null,
  };
  el.addEventListener("input", () => { state.rawOverride = null; $("raw-note").hidden = true; });
}

/* ---------- scenario <-> form ---------- */

function setPath(obj, path, val) {
  const parts = path.split(".");
  let cur = obj;
  parts.slice(0, -1).forEach((p) => { cur = cur[p] = cur[p] || {}; });
  cur[parts[parts.length - 1]] = val;
}

function getPath(obj, path) {
  return path.split(".").reduce((o, p) => (o && typeof o === "object" ? o[p] : undefined), obj);
}

function readScenario() {
  if (state.rawOverride !== null) return state.rawOverride;
  const out = {};
  Object.entries(state.fields).forEach(([path, f]) => {
    const v = f.get();
    if (v !== undefined) setPath(out, path, v);
  });
  return out;
}

function loadScenario(sc) {
  clearAllErrors();
  Object.values(state.fields).forEach((f) => f.set(undefined));
  if (sc === null || typeof sc !== "object" || Array.isArray(sc)) {
    state.rawOverride = sc;
    const note = $("raw-note");
    note.hidden = false;
    note.textContent = "This sample is not a JSON object, so it is sent to the engine exactly as it is. Edit any field to switch back to the form.";
    return;
  }
  state.rawOverride = null;
  $("raw-note").hidden = true;
  Object.entries(state.fields).forEach(([path, f]) => {
    const v = getPath(sc, path);
    if (v !== undefined) f.set(v);
  });
}

/* ---------- inline errors ---------- */

function clearFieldError(path) {
  const f = state.fields[path];
  if (!f || !f.err) return;
  f.err.hidden = true; f.err.textContent = "";
  f.box.classList.remove("has-error");
}

function clearAllErrors() {
  Object.keys(state.fields).forEach(clearFieldError);
  $("top-errors").hidden = true;
}

function showErrors(errors) {
  clearAllErrors();
  const loose = [];
  (errors || []).forEach((e) => {
    const path = String(e.field || "").replace(/\[\d*\]$/, "");
    const f = state.fields[path];
    if (f && f.err) {
      f.err.hidden = false;
      f.err.textContent = (f.err.textContent ? f.err.textContent + " " : "") + e.message;
      f.box.classList.add("has-error");
    } else {
      loose.push(e);
    }
  });
  const top = $("top-errors");
  if (loose.length) {
    top.hidden = false;
    top.innerHTML = "<b>Input problems</b><ul class='plain'>" + loose.map((e) =>
      `<li>${e.field ? `<code>${esc(e.field)}</code> ` : ""}${esc(e.message)}</li>`).join("") + "</ul>";
  }
  const first = document.querySelector(".field.has-error");
  if (first) first.scrollIntoView({ block: "center" });
}

/* ---------- description suggestions ---------- */

async function suggest() {
  const text = $("desc-text").value;
  const status = $("suggest-status");
  status.textContent = "Working...";
  try {
    const res = await getJSON("/api/describe", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) });
    state.suggestions = (res && res.suggestions) || [];
  } catch (e) {
    status.textContent = e.message; return;
  }
  const list = $("suggest-list");
  list.innerHTML = "";
  state.suggestions.forEach((s, i) => {
    const path = s.field || s.path;
    const known = !!state.fields[path];
    const li = document.createElement("li");
    const why = s.reason || (s.keyword ? `matched "${s.keyword}"` : "");
    li.innerHTML = `<label><input type="checkbox" data-i="${i}" ${known ? "checked" : "disabled"}> <code>${esc(path)}</code> = <b>${esc(JSON.stringify(s.value))}</b></label>` +
      (why ? `<div class="muted">${esc(why)}${known ? "" : " (informational, not a form field)"}</div>` : "");
    list.appendChild(li);
  });
  status.textContent = state.suggestions.length ? "" : "No suggestions for this text.";
  $("suggestions").hidden = !state.suggestions.length;
}

function applySuggestions() {
  let n = 0;
  $("suggest-list").querySelectorAll("input:checked").forEach((cb) => {
    const s = state.suggestions[Number(cb.dataset.i)];
    const f = state.fields[s.field || s.path];
    if (!f) return;
    let v = s.value;
    if (/list|array/.test(String(f.spec.type).toLowerCase())) {
      const cur = f.get() || [];
      v = [...new Set([...cur, ...(Array.isArray(v) ? v : [v])])];
    }
    f.set(v); n++;
  });
  state.rawOverride = null;
  $("suggest-status").textContent = `${n} field${n === 1 ? "" : "s"} applied. Review them in the form.`;
  $("suggestions").hidden = true;
}

/* ---------- results ---------- */

function chips(list) {
  return list && list.length ? `<div class="chips">${list.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div>` : "";
}
function capacityBlock(c) {
  if (!c) return "";
  const n = (x) => x == null ? "n/a" : Number(x).toLocaleString("en-US");
  return `<h4>Capacity at peak</h4><div class="tablewrap"><table><thead><tr><th>Users</th><th>Concurrent</th><th>Requests/s</th><th>API instances</th><th>Data (GB, 12 mo)</th></tr></thead><tbody>` +
    `<tr><td>${n(c.peak_users)}</td><td>${n(c.peak_concurrent_users)}</td><td>${n(c.peak_rps)}</td><td>${c.api_instances_at_peak == null ? "none to size" : n(c.api_instances_at_peak)}</td><td>${n(c.data_volume_gb_12_months)}</td></tr></tbody></table></div>` +
    bullets(c.basis);
}
function bullets(list) {
  return list && list.length ? `<ul>${list.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
}
function money(n) { return n == null ? "n/a" : "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 }); }

function alternatives(alts) {
  if (!alts || !alts.length) return "";
  return `<h4>Alternatives considered</h4><div class="tablewrap"><table class="alts"><tbody>` +
    alts.map((a) => `<tr><td>${esc(a.choice)}</td><td>${esc(a.why_not)}</td></tr>`).join("") + "</tbody></table></div>";
}

function componentCard(c, layer) {
  const lay = c.layer || layer;
  const meta = [c.sub_layer, c.region].filter(Boolean).map(esc).join(" / ");
  const src = (c.sources || []).map((u) => /^https?:/.test(u) ? `<a href="${esc(u)}" rel="noopener noreferrer" target="_blank">${esc(u)}</a>` : esc(u));
  return `<article class="comp"><div class="comp-head"><span class="layer">${esc(lay)}</span><span class="choice">${esc(c.choice)}</span>` +
    (meta ? `<span class="muted">${meta}</span>` : "") + (c.score != null ? `<span class="muted">score ${esc(c.score)}</span>` : "") +
    (c.confidence ? `<span class="muted" title="points ahead of the nearest rival">${esc(c.confidence)} confidence, +${esc(c.margin)}</span>` : "") + "</div>" +
    `<p>${esc(c.justification)}</p>` +
    (c.driven_by && c.driven_by.length ? `<h4>Driven by</h4>${chips(c.driven_by)}` : "") +
    alternatives(c.alternatives) +
    (c.tradeoffs && c.tradeoffs.length ? `<h4>Tradeoffs</h4>${bullets(c.tradeoffs)}` : "") +
    (src.length ? `<h4>Sources</h4><ul>${src.map((s) => `<li>${s}</li>`).join("")}</ul>` : "") + "</article>";
}

function signalLine(s) {
  const field = s.field || s.path || "";
  const val = s.value !== undefined ? ` = ${JSON.stringify(s.value)}` : "";
  const kw = s.keyword ? `keyword "${s.keyword}"` : "";
  const tail = [kw, s.applied === true ? "applied as a default" : (s.applied === false ? "suggestion only" : ""), s.reason].filter(Boolean).join("; ");
  return `${field}${val}${tail ? " (" + tail + ")" : ""}`;
}

function diagram(o) {
  const st = o.stack || {};
  const sup = st.supporting || [];
  const pick = (layers) => sup.filter((c) => layers.includes(c.layer)).map((c) => [c.layer, c.choice]);
  const real = (c) => c && c.choice && c.choice !== "None";
  const box = ([l, n]) => `<div class="node"><span class="lyr">${esc(l === "ci_cd" ? "ci/cd" : l.replace(/_/g, " "))}</span>${esc(n)}</div>`;
  const tier = (name, items) => items.length ? `<div class="tier"><div class="tname">${esc(name)}</div><div class="nodes">${items.map(box).join("")}</div></div>` : "";
  const clients = real(st.frontend) ? [["frontend", st.frontend.choice]] : [];
  const app = [];
  if (real(st.backend)) app.push(["backend", st.backend.choice]);
  if (real(st.hosting)) app.push(["hosting", st.hosting.choice]);
  const data = real(st.database) ? [["database", st.database.choice]] : [];
  const tiers = [
    tier("Clients", clients),
    tier("Edge", pick(["cdn", "auth"])),
    tier("Application", app.concat(pick(["realtime", "queue", "ml"]))),
    tier("Data", data.concat(pick(["cache", "storage", "search"]))),
    tier("Services and operations", pick(["payments", "other", "ci_cd", "observability", "cms", "bi"])),
  ].filter(Boolean);
  return `<section class="card" id="diagram"><h2>Architecture diagram</h2><div class="diagram">${tiers.join('<div class="arrow">&#8595;</div>')}</div>` +
    `<p class="muted">Requests flow top to bottom; the last row lists external services and the delivery and monitoring tools.</p></section>`;
}

function renderResults(o) {
  const parts = [];
  const ok = o.status === "ok" || o.status === "ok_with_warnings";
  parts.push(`<section class="card" id="summary"><h2>Recommendation <span class="status ${esc(o.status)}">${esc(String(o.status).replace(/_/g, " "))}</span></h2>` +
    `<p class="summary">${esc(o.summary)}</p><p class="muted">Scenario ${esc(o.scenario_id)} | engine ${esc(o.engine_version)} | knowledge base as of ${esc(o.kb_as_of)}` +
    (o.region && o.region.primary ? ` | region ${esc(o.region.primary)}` + (o.region.dr ? ` (DR ${esc(o.region.dr)})` : "") : "") + "</p></section>");

  if (o.validation_errors && o.validation_errors.length) {
    parts.push(`<section class="card" id="errors"><h2>Validation errors</h2><ul class="plain">` + o.validation_errors.map((e) =>
      `<li>${e.field ? `<code>${esc(e.field)}</code> ` : ""}${esc(e.message)}</li>`).join("") + "</ul></section>");
  }

  if (ok && o.architecture_style) {
    const a = o.architecture || {};
    parts.push(`<section class="card arch" id="architecture"><h2>Architecture</h2><div class="style">${esc(String(o.architecture_style).replace(/_/g, " "))}</div>` +
      (a.justification ? `<p>${esc(a.justification)}</p>` : "") + chips(a.driven_by) + alternatives(a.alternatives) + "</section>");
  }

  if (ok && o.stack) parts.push(diagram(o));

  if (ok && o.stack) {
    const rows = [["frontend", o.stack.frontend], ["backend", o.stack.backend], ["database", o.stack.database], ["hosting", o.stack.hosting]]
      .concat((o.stack.supporting || []).map((c) => [c.layer, c]));
    parts.push(`<section class="card" id="stack"><h2>Recommended stack</h2><div class="tablewrap"><table><thead><tr><th>Layer</th><th>Choice</th><th>Detail</th></tr></thead><tbody>` +
      rows.map(([l, c]) => c ? `<tr><td>${esc(l)}</td><td><b>${esc(c.choice)}</b></td><td class="muted">${[c.sub_layer, c.region].filter(Boolean).map(esc).join(" / ")}</td></tr>` : "").join("") +
      "</tbody></table></div></section>");
  }

  const cost = o.estimated_monthly_cost_usd;
  if (cost) {
    const band = cost.min == null ? "Not estimated" : `${money(cost.min)} to ${money(cost.max)} <small>per month</small>`;
    const bd = (cost.breakdown || []);
    parts.push(`<section class="card" id="cost"><h2>Estimated monthly cost</h2><div class="cost">${band}</div>` + bullets(cost.assumptions) +
      (bd.length ? `<h4>Breakdown</h4><div class="tablewrap"><table><thead><tr><th>Component</th><th>Min</th><th>Max</th><th>Basis</th></tr></thead><tbody>` +
        bd.map((b) => `<tr><td>${esc(b.component)}</td><td>${money(b.min)}</td><td>${money(b.max)}</td><td class="muted">${esc(b.basis)}</td></tr>`).join("") + "</tbody></table></div>" : "") +
      capacityBlock(o.capacity) + "</section>");
  }

  const w = o.warnings || [];
  const rank = { critical: 0, warning: 1, info: 2 };
  parts.push(`<section class="card" id="warnings"><h2>Warnings (${w.length})</h2>` + (w.length ? [...w].sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3)).map((x) =>
    `<div class="warn-item ${esc(x.severity)}"><span class="sev">${esc(x.severity)}</span>${esc(x.message)}<span class="code">${esc(x.code)}</span>${chips(x.fields)}</div>`).join("") : '<p class="muted">No warnings.</p>') + "</section>");

  if (o.suggested_relaxations && o.suggested_relaxations.length) {
    parts.push(`<section class="card" id="relaxations"><h2>Suggested relaxations</h2>${bullets(o.suggested_relaxations.map((r) => typeof r === "string" ? r : JSON.stringify(r)))}</section>`);
  }

  if (ok && o.stack) {
    const cards = [["frontend", o.stack.frontend], ["backend", o.stack.backend], ["database", o.stack.database], ["hosting", o.stack.hosting]]
      .concat((o.stack.supporting || []).map((c) => [c.layer, c])).filter(([, c]) => c).map(([l, c]) => componentCard(c, l));
    parts.push(`<section id="components"><h2 style="margin:.2rem 0 .7rem">Why each component</h2>${cards.join("")}</section>`);
  }

  const sig = o.description_signals || [];
  parts.push(`<section class="card" id="assumptions"><h2>Assumptions (${(o.assumptions || []).length})</h2>` +
    ((o.assumptions || []).length ? bullets(o.assumptions) : '<p class="muted">None.</p>') +
    (sig.length ? `<h4>Signals read from the description</h4>${bullets(sig.map(signalLine))}` : "") +
    `<p class="muted">${esc(o.llm && o.llm.note ? o.llm.note : "LLM features: off (none used).")}</p></section>`);

  $("results").innerHTML = parts.join("");
  $("results").hidden = false;
  $("empty").hidden = true;
}

/* ---------- actions ---------- */

async function run() {
  const body = readScenario();
  const btns = [$("run"), $("run2")];
  btns.forEach((b) => { b.disabled = true; b.textContent = "Running..."; });
  try {
    const out = await getJSON("/api/recommend", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    state.lastOutput = out;
    $("dl-output").disabled = false;
    showErrors(out.validation_errors);
    renderResults(out);
  } catch (e) {
    $("top-errors").hidden = false;
    $("top-errors").textContent = e.message;
  } finally {
    btns.forEach((b) => { b.disabled = false; b.textContent = "Run recommendation"; });
  }
}

function download(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function downloadInput() {
  const sc = readScenario();
  const id = sc && typeof sc === "object" && sc.scenario_id ? sc.scenario_id : "scenario";
  download(`${id}.json`, typeof sc === "string" ? sc : JSON.stringify(sc, null, 2));
}

function downloadOutput() {
  const o = state.lastOutput;
  if (!o) return;
  download(`${o.scenario_id || "scenario"}_output.json`, JSON.stringify(o, null, 2));
}

function importFile(file) {
  const rd = new FileReader();
  rd.onload = () => {
    let sc;
    try { sc = JSON.parse(rd.result); } catch (e) { sc = String(rd.result); }
    loadScenario(sc);
  };
  rd.readAsText(file);
}

async function loadSample(id) {
  const res = await getJSON("/api/samples/" + id);
  loadScenario(res.scenario);
  $("sample").value = id;
}

// Show only the requested section at the top of the page (used for screenshots).
function focusSection(el) {
  const inResults = $("results").contains(el);
  document.body.classList.add("focus");
  $(inResults ? "input-col" : "output-col").hidden = true;
  const host = inResults ? $("results") : $("form");
  [...host.children].forEach((c) => { if (c !== el && !c.contains(el)) c.hidden = true; });
  if (!inResults) [...$("input-col").children].forEach((c) => { if (c !== el && !c.contains(el) && c !== host) c.hidden = true; });
  window.scrollTo(0, 0);
}

async function init() {
  $("run").onclick = run; $("run2").onclick = run;
  $("suggest").onclick = suggest;
  $("apply-suggest").onclick = applySuggestions;
  $("dl-input").onclick = downloadInput;
  $("dl-output").onclick = downloadOutput;
  $("import-file").onchange = (e) => { if (e.target.files[0]) importFile(e.target.files[0]); e.target.value = ""; };
  $("sample").onchange = (e) => { if (e.target.value) loadSample(e.target.value).catch((x) => alert(x.message)); };

  try {
    buildForm(normaliseSchema(await getJSON("/api/schema")));
  } catch (e) {
    $("form").innerHTML = `<div class="errors-banner">Could not load the form schema: ${esc(e.message)}</div>`;
  }
  try {
    const items = await getJSON("/api/samples");
    ["public", "edge"].forEach((g) => {
      const og = document.createElement("optgroup");
      og.label = g === "public" ? "Public scenarios" : "Edge cases";
      items.filter((i) => i.group === g).forEach((i) => {
        const o = document.createElement("option");
        o.value = i.id; o.textContent = i.name + (i.label ? " - " + i.label : "");
        og.appendChild(o);
      });
      $("sample").appendChild(og);
    });
  } catch (e) { /* the dropdown stays empty */ }

  const q = new URLSearchParams(location.search);
  if (q.get("sample")) { try { await loadSample(q.get("sample")); } catch (e) { /* ignore bad id */ } }
  if (q.get("autorun") === "1") await run();
  const focus = q.get("focus");
  if (focus && $(focus)) focusSection($(focus));
  document.body.dataset.ready = "1";
}

init();
