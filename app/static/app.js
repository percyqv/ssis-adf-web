"use strict";

const $ = (id) => document.getElementById(id);
const state = { messages: [], busy: false };
const STORE_KEY = "ssis-adf-web.settings";

// ---------------------------------------------------------------- utils
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Minimal, safe Markdown: escape first, then **bold**, `code`, lists, paragraphs.
function renderMarkdown(text) {
  const lines = escapeHtml(text || "").split("\n");
  let html = "", inList = false;
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  for (const raw of lines) {
    const line = raw.trimEnd();
    const item = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (item) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(item[1])}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.trim()) html += `<p>${inline(line.replace(/^#+\s*/, ""))}</p>`;
  }
  if (inList) html += "</ul>";
  return html;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) throw new Error((isJson && body.error) || `Error ${res.status}`);
  return body;
}

const postJson = (path, data) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
});

function loadSettings() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { return null; }
}
function saveSettings(s) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(s)); } catch { /* storage unavailable */ }
}
function currentSettings() {
  return { input: $("input-location").value.trim(), output: $("output-location").value.trim() };
}

const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const button = (text, onclick, cls = "") => {
  const b = el("button", cls);
  b.type = "button";
  b.textContent = text;
  b.onclick = onclick;
  return b;
};

// ---------------------------------------------------------------- chat
function clearWelcome() {
  const welcome = document.querySelector(".welcome");
  if (welcome) welcome.remove();
}
function scrollDown() { $("messages").scrollTop = $("messages").scrollHeight; }

function addMessage(role, html, extraClass = "") {
  clearWelcome();
  const div = el("div", `msg ${role} ${extraClass}`.trim(), html);
  $("messages").appendChild(div);
  scrollDown();
  return div;
}

function stepsHtml(steps) {
  if (!steps || !steps.length) return "";
  const items = steps.map((s) =>
    `<li class="${s.ok ? "ok" : "fail"}"><code>${escapeHtml(s.tool)}</code>: ${escapeHtml(s.summary)}</li>`
  ).join("");
  const n = steps.length;
  return `<details class="steps"><summary>${n} ${n === 1 ? "herramienta usada" : "herramientas usadas"}</summary><ul>${items}</ul></details>`;
}

function diagramCard(graph) {
  const card = el("div", "diagram-card");
  const canvas = el("div", "canvas");
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", `Diagrama del pipeline ${graph.name}`);
  const bar = el("div", "bar");
  const s = graph.stats;
  bar.appendChild(el("span", "", escapeHtml(`${graph.name}: ${s.activities} actividades` +
    (s.warnings ? `, ${s.warnings} para revisar` : ""))));
  bar.appendChild(button("Abrir diagrama", () => Diagram.open(graph)));
  card.append(canvas, bar);
  // Cytoscape needs the container in the DOM with a size before rendering.
  requestAnimationFrame(() => Diagram.preview(canvas, graph));
  return card;
}

function renderAttachments(target, attachments) {
  for (const a of attachments || []) {
    if (a.type === "pipeline_graph" && a.graph) target.appendChild(diagramCard(a.graph));
  }
}

async function send(text) {
  text = (text || "").trim();
  if (!text || state.busy) return;
  const s = currentSettings();
  setBusy(true);
  state.messages.push({ role: "user", content: text });
  addMessage("user", renderMarkdown(text));
  const pending = addMessage("bot", '<span class="thinking">Trabajando…</span>');
  try {
    const res = await postJson("/api/chat", {
      messages: state.messages, input_location: s.input, output_location: s.output,
    });
    state.messages.push({ role: "assistant", content: res.reply });
    pending.innerHTML = renderMarkdown(res.reply) + stepsHtml(res.steps);
    renderAttachments(pending, res.attachments);
    if ((res.steps || []).some((st) => st.tool === "convert_ssis_package")) refreshArtifacts();
  } catch (e) {
    state.messages.pop();
    pending.classList.add("err");
    pending.textContent = e.message;
  } finally {
    setBusy(false);
    scrollDown();
  }
}

function setBusy(busy) {
  state.busy = busy;
  $("send").disabled = busy;
  document.querySelectorAll(".files .act").forEach((b) => (b.disabled = busy));
}

// Results of the direct actions go into the history as short notes, so a later
// "explain it" in the chat has the context without repeating the work.
function remember(text) { state.messages.push({ role: "assistant", content: text }); }

// ---------------------------------------------------------------- direct actions (no AI)
const EFFORT = { "Low": "Baja", "Medium": "Media", "High": "Alta", "Very High": "Muy alta" };

function countsHtml(pairs) {
  return `<dl class="counts">${pairs.map(([label, n]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd class="${n ? "" : "zero"}">${n}</dd></div>`).join("")}</dl>`;
}

function resultCard(title, sub) {
  clearWelcome();
  const card = el("article", "result");
  card.innerHTML = `<h3>${title}</h3><p class="sub">${escapeHtml(sub)}</p><div class="body"><span class="thinking">Trabajando…</span></div>`;
  $("messages").appendChild(card);
  scrollDown();
  return card;
}

function failCard(card, e) {
  card.querySelector(".body").innerHTML = `<ul class="notes"><li class="err">${escapeHtml(e.message)}</li></ul>`;
}

async function runAnalyze(item) {
  const pkg = item.name.replace(/\.dtsx$/i, "");
  const card = resultCard(`Análisis de ${escapeHtml(pkg)}`, item.path);
  setBusy(true);
  try {
    const { analysis: a } = await postJson("/api/actions/analyze", { package_path: item.path });
    const c = a.complexity || {};
    const gaps = a.gaps_by_severity || {};
    const manual = gaps.manual_required || [], warn = gaps.warning || [];
    const effort = EFFORT[c.effort_estimate] || c.effort_estimate || "—";
    const tone = c.score > 50 ? "err" : c.score > 20 ? "warn" : "ok";
    const notes = [...manual.map((g) => ["err", g]), ...warn.map((g) => ["", g])].slice(0, 8);
    card.querySelector("h3").innerHTML += ` <span class="status ${tone}">Esfuerzo ${escapeHtml(effort.toLowerCase())}</span>`;
    card.querySelector(".body").innerHTML =
      `<p class="sub">Complejidad ${c.score ?? "?"} de 100<span class="meter"><i style="width:${Math.min(100, c.score || 0)}%"></i></span></p>` +
      countsHtml([
        ["Tareas", c.total_tasks || 0], ["Data flows", c.data_flow_task_count || 0],
        ["Scripts", c.script_task_count || 0], ["Bucles", c.loop_container_count || 0],
        ["Conexiones", (a.connection_managers || []).length],
        ["Revisión manual", manual.length],
      ]) +
      (a.execution_order?.length
        ? `<details><summary>Orden de ejecución (${a.execution_order.length} tareas)</summary><ol class="order">${a.execution_order.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ol></details>`
        : "") +
      (notes.length ? `<ul class="notes">${notes.map(([cls, g]) =>
        `<li class="${cls}"><strong>${escapeHtml(g.task_name)}</strong>: ${escapeHtml(g.message)}</li>`).join("")}</ul>` : "");
    const acts = el("div", "actions");
    acts.append(
      button("Convertir", () => runConvert(item)),
      button("Explicar con IA", () => send(`Explícame el análisis del paquete ${pkg}: qué hace, qué será difícil de migrar y qué recomiendas.`), "ghost"),
    );
    card.appendChild(acts);
    remember(`Analicé ${pkg}: complejidad ${c.score}/100 (esfuerzo ${effort}), ${c.total_tasks} tareas, ${manual.length} puntos de revisión manual.`);
  } catch (e) {
    failCard(card, e);
  } finally {
    setBusy(false);
    scrollDown();
  }
}

const VALIDATION = {
  valid: ["ok", "Válido"],
  valid_with_warnings: ["warn", "Válido con avisos"],
  issues_found: ["err", "Con errores"],
};

async function runConvert(item) {
  const pkg = item.name.replace(/\.dtsx$/i, "");
  const card = resultCard(`Conversión de ${escapeHtml(pkg)}`, item.path);
  setBusy(true);
  try {
    const res = await postJson("/api/actions/convert", {
      package_path: item.path, output_location: currentSettings().output,
    });
    const g = res.convert.artifacts_generated || {};
    const v = res.validate || {};
    const [tone, label] = VALIDATION[v.status] || ["warn", v.status || "Sin validar"];
    const warnings = res.convert.warnings || [];
    const issues = (v.issues || []).filter((i) => (i.severity || "error") === "error");
    card.querySelector("h3").innerHTML += ` <span class="status ${tone}">${escapeHtml(label)}</span>`;
    card.querySelector(".sub").textContent = `Artefactos en ${res.output_dir}`;
    card.querySelector(".body").innerHTML =
      countsHtml([
        ["Pipelines", g.pipelines || 0], ["Linked services", g.linked_services || 0],
        ["Datasets", g.datasets || 0], ["Data flows", g.data_flows || 0],
        ["Triggers", g.triggers || 0], ["Revisión manual", res.convert.manual_review_required || 0],
      ]) +
      ((issues.length || warnings.length) ? `<ul class="notes">${[
        ...issues.slice(0, 5).map((i) => `<li class="err">${escapeHtml(i.message || JSON.stringify(i))}</li>`),
        ...warnings.slice(0, 6).map((w) => `<li>${escapeHtml(w)}</li>`),
      ].join("")}</ul>` : "");
    const acts = el("div", "actions");
    if (res.pipeline_path) {
      const preview = el("div");
      card.querySelector(".body").appendChild(preview);
      try {
        const graph = await api(`/api/pipeline-graph?path=${encodeURIComponent(res.pipeline_path)}`);
        preview.appendChild(diagramCard(graph));
      } catch { /* the buttons below still work */ }
      acts.appendChild(button("Ver flujo", () => openPipeline(res.pipeline_path)));
    }
    acts.appendChild(button("Explicar con IA", () => send(`Muéstrame el flujo de ${pkg} y explícalo: qué hace cada actividad y qué hay que revisar a mano.`), "ghost"));
    card.appendChild(acts);
    remember(`Convertí ${pkg} en ${res.output_dir}: ${g.linked_services || 0} linked services, ${g.datasets || 0} datasets, ${g.data_flows || 0} data flows; ${res.convert.manual_review_required || 0} puntos de revisión manual; validación: ${label}.`);
    refreshArtifacts();
  } catch (e) {
    failCard(card, e);
  } finally {
    setBusy(false);
    scrollDown();
  }
}

async function openPipeline(path) {
  try {
    Diagram.open(await api(`/api/pipeline-graph?path=${encodeURIComponent(path)}`));
  } catch (e) {
    addMessage("bot", escapeHtml(e.message), "err");
  }
}

// ---------------------------------------------------------------- files
function fileItem(name, title, actions) {
  const li = el("li");
  li.appendChild(el("span", "name", name)).title = title;
  const box = el("span", "acts");
  actions.forEach((a) => box.appendChild(a));
  li.appendChild(box);
  return li;
}

function setList(ul, nodes, emptyText) {
  ul.innerHTML = "";
  if (!nodes.length) {
    ul.innerHTML = `<li class="empty">${escapeHtml(emptyText)}</li>`;
    return;
  }
  nodes.forEach((n) => ul.appendChild(n));
}

const act = (text, onclick) => {
  const b = button(text, onclick, "act");
  b.disabled = state.busy;
  return b;
};

async function refreshPackages() {
  const ul = $("packages");
  try {
    const { items } = await api(`/api/files?suffix=.dtsx&location=${encodeURIComponent(currentSettings().input)}`);
    setList(ul, items.map((it) => fileItem(escapeHtml(it.name), it.path, [
      act("Analizar", () => runAnalyze(it)),
      act("Convertir", () => runConvert(it)),
    ])), "No hay paquetes en la carpeta de entrada. Sube un .dtsx para empezar.");
  } catch (e) {
    setList(ul, [], e.message);
  }
}

const KIND = { pipeline: "pipeline", linkedService: "linked service", dataset: "dataset", dataflow: "data flow", trigger: "trigger" };

async function refreshArtifacts() {
  const ul = $("artifacts");
  try {
    const { items } = await api(`/api/files?suffix=.json&location=${encodeURIComponent(currentSettings().output)}`);
    // Group by package folder: <package>/<kind>/<file>.json
    const nodes = [];
    let group = null;
    for (const it of items) {
      const parts = it.name.split("/");
      const pkg = parts.length > 2 ? parts[0] : "";
      if (pkg !== group) {
        group = pkg;
        if (pkg) nodes.push(el("li", "group", escapeHtml(pkg)));
      }
      const kind = parts.length > 1 ? parts[parts.length - 2] : "";
      const file = parts[parts.length - 1].replace(/\.json$/i, "");
      const dl = el("a", "", "Descargar");
      dl.href = `/api/download?path=${encodeURIComponent(it.path)}`;
      const actions = kind === "pipeline" ? [act("Ver flujo", () => openPipeline(it.path)), dl] : [dl];
      nodes.push(fileItem(`${escapeHtml(file)} <small>${escapeHtml(KIND[kind] || kind)}</small>`, it.name, actions));
    }
    setList(ul, nodes, "Todavía no hay artefactos. Convierte un paquete para generarlos.");
  } catch (e) {
    setList(ul, [], e.message);
  }
}

async function uploadFiles(files) {
  const status = $("settings-status");
  for (const f of files) {
    const form = new FormData();
    form.append("location", currentSettings().input);
    form.append("file", f);
    try {
      status.textContent = `Subiendo ${f.name}…`;
      await api("/api/upload", { method: "POST", body: form });
      status.textContent = `${f.name} subido.`;
    } catch (e) {
      status.textContent = `${f.name}: ${e.message}`;
    }
  }
  $("upload-file").value = "";
  refreshPackages();
}

// ---------------------------------------------------------------- init
async function init() {
  Diagram.initViewer();
  let cfg = { default_input: "", default_output: "" };
  try {
    cfg = await api("/api/config");
    if (cfg.user) $("user").textContent = cfg.user;
    if (cfg.missing_settings && cfg.missing_settings.length) {
      const b = $("config-warning");
      b.hidden = false;
      b.textContent = `Configuración incompleta en el servidor: ${cfg.missing_settings.join(", ")}`;
    }
  } catch { /* shown by later calls */ }

  const saved = loadSettings();
  $("input-location").value = (saved && saved.input) || cfg.default_input;
  $("output-location").value = (saved && saved.output) || cfg.default_output;

  $("save-settings").onclick = () => {
    saveSettings(currentSettings());
    $("settings-status").textContent = "Rutas guardadas.";
    refreshPackages();
    refreshArtifacts();
  };
  $("refresh-packages").onclick = refreshPackages;
  $("refresh-artifacts").onclick = refreshArtifacts;
  $("upload-file").onchange = (e) => uploadFiles(e.target.files);

  document.querySelectorAll(".chip").forEach((c) => (c.onclick = () => send(c.textContent)));
  $("composer").onsubmit = (e) => { e.preventDefault(); const t = $("prompt").value; $("prompt").value = ""; send(t); };
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
  });

  refreshPackages();
  refreshArtifacts();
}

init();
