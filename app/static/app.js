"use strict";

const $ = (id) => document.getElementById(id);
const state = { messages: [], busy: false };
const STORE_KEY = "ssis-adf-web.settings";

// ---------------------------------------------------------------- utils
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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

function loadSettings() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { return null; }
}
function saveSettings(s) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(s)); } catch { /* storage unavailable */ }
}
function currentSettings() {
  return { input: $("input-location").value.trim(), output: $("output-location").value.trim() };
}

// ---------------------------------------------------------------- chat
function addMessage(role, html, extraClass = "") {
  const welcome = document.querySelector(".welcome");
  if (welcome) welcome.remove();
  const div = document.createElement("div");
  div.className = `msg ${role} ${extraClass}`.trim();
  div.innerHTML = html;
  $("messages").appendChild(div);
  $("messages").scrollTop = $("messages").scrollHeight;
  return div;
}

function stepsHtml(steps) {
  if (!steps || !steps.length) return "";
  const items = steps.map((s) =>
    `<li class="${s.ok ? "ok" : "fail"}"><code>${escapeHtml(s.tool)}</code> — ${escapeHtml(s.summary)}</li>`
  ).join("");
  return `<details class="steps"><summary>${steps.length} herramienta(s) usada(s)</summary><ul>${items}</ul></details>`;
}

async function send(text) {
  text = (text || "").trim();
  if (!text || state.busy) return;
  const s = currentSettings();
  state.busy = true;
  $("send").disabled = true;
  state.messages.push({ role: "user", content: text });
  addMessage("user", renderMarkdown(text));
  const pending = addMessage("bot", '<span class="thinking">Trabajando…</span>');
  try {
    const res = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: state.messages, input_location: s.input, output_location: s.output }),
    });
    state.messages.push({ role: "assistant", content: res.reply });
    pending.innerHTML = renderMarkdown(res.reply) + stepsHtml(res.steps);
    if ((res.steps || []).some((st) => st.tool === "convert_ssis_package")) refreshArtifacts();
  } catch (e) {
    state.messages.pop();
    pending.classList.add("err");
    pending.textContent = e.message;
  } finally {
    state.busy = false;
    $("send").disabled = false;
    $("messages").scrollTop = $("messages").scrollHeight;
  }
}

// ---------------------------------------------------------------- files
function fileItem(item, actions) {
  const li = document.createElement("li");
  li.innerHTML = `<span class="name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>`;
  const box = document.createElement("span");
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

async function refreshPackages() {
  const ul = $("packages");
  try {
    const { items } = await api(`/api/files?suffix=.dtsx&location=${encodeURIComponent(currentSettings().input)}`);
    setList(ul, items.map((it) => {
      const pkg = it.name.replace(/\.dtsx$/i, "");
      const btn = document.createElement("button");
      btn.className = "act";
      btn.textContent = "Convertir";
      btn.onclick = () => send(`Convierte el paquete ${pkg} y valida el resultado.`);
      return fileItem(it, [btn]);
    }), "No hay paquetes en la carpeta de entrada");
  } catch (e) {
    setList(ul, [], e.message);
  }
}

async function refreshArtifacts() {
  const ul = $("artifacts");
  try {
    const { items } = await api(`/api/files?suffix=.json&location=${encodeURIComponent(currentSettings().output)}`);
    setList(ul, items.map((it) => {
      const a = document.createElement("a");
      a.href = `/api/download?path=${encodeURIComponent(it.path)}`;
      a.textContent = "Descargar";
      return fileItem(it, [a]);
    }), "Todavía no hay artefactos");
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
