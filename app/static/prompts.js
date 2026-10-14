"use strict";

// Editor for the agent's prompts: base (with factory restore) and additional
// instructions, each with its version history. Uses helpers from app.js.
const Prompts = (() => {
  const TEXT = {
    base: {
      help: "Usa {{ENTRADA}} y {{SALIDA}}: se reemplazan por las rutas de la sesión de cada usuario. " +
        "Las reglas de seguridad (solo los contenedores permitidos, nunca desplegar en ADF) están en el código y no dependen de este texto.",
      factory: "Versión de fábrica",
      factoryNote: "Incluida en la aplicación. Siempre se puede restaurar.",
    },
    extra: {
      help: "Se añaden al final del prompt base. Úsalas para el tono, el alcance (por ejemplo, responder solo sobre SSIS y ADF) o reglas de tu equipo.",
      factory: "Versión de fábrica (vacía)",
      factoryNote: "Sin instrucciones adicionales.",
    },
  };

  let data = null;       // GET /api/prompts
  let tab = "base";
  const drafts = {};      // unsaved editor text per prompt

  const fmt = (iso) => iso
    ? new Date(iso).toLocaleString("es", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : "";
  const who = (u) => u || "alguien sin sesión";

  function versionName(p, id) {
    if (id === data.factory_id) return TEXT[p.kind].factory;
    const v = p.versions.find((x) => x.id === id);
    return v ? `Versión ${v.number}` : id;
  }

  function stateLine(p) {
    const name = versionName(p, p.active);
    if (!p.activated_at) return `Activa: ${name.toLowerCase()}.`;
    return `Activa: ${name.toLowerCase()}, puesta por ${escapeHtml(who(p.activated_by))} el ${fmt(p.activated_at)}.`;
  }

  async function load() {
    const s = currentSettings();
    data = await api(`/api/prompts?input=${encodeURIComponent(s.input)}&output=${encodeURIComponent(s.output)}`);
  }

  function render(status = "") {
    const body = $("prompts-body");
    document.querySelectorAll("#prompts [role=tab]").forEach((t) =>
      t.setAttribute("aria-selected", String(t.dataset.tab === tab)));
    if (tab === "final") {
      body.innerHTML = `<div class="final">
        <p class="hint">Así lo recibe el modelo con tus rutas actuales: prompt base activo más instrucciones adicionales activas.</p>
        <pre>${escapeHtml(data.effective)}</pre></div>`;
      return;
    }
    const p = data.prompts[tab];
    const canEdit = data.can_edit;
    const text = drafts[tab] ?? p.active_text;
    const items = p.versions.map((v) => `
      <li class="${v.id === p.active ? "is-active" : ""}">
        <div class="v-head"><strong>Versión ${v.number}</strong>${v.id === p.active ? ' <span class="status ok">Activa</span>' : ""}</div>
        <small>${escapeHtml(who(v.author))}, ${fmt(v.created)}</small>
        ${v.note ? `<p>${escapeHtml(v.note)}</p>` : ""}
        <div class="v-acts">
          <button type="button" class="link" data-load="${v.id}">Ver en el editor</button>
          ${canEdit && v.id !== p.active ? `<button type="button" class="link" data-activate="${v.id}">Activar</button>` : ""}
        </div>
      </li>`).join("");
    const factoryActive = p.active === data.factory_id;
    body.innerHTML = `<div class="prompt-edit">
      <section class="editor">
        <div class="editor-head">
          <p class="state">${stateLine(p)}</p>
          ${canEdit ? `<button type="button" class="ghost" id="prompt-factory" ${factoryActive ? "disabled" : ""}>Restaurar de fábrica</button>` : ""}
        </div>
        <textarea id="prompt-text" spellcheck="false" ${canEdit ? "" : "readonly"} aria-label="Texto del prompt">${escapeHtml(text)}</textarea>
        <p class="hint">${escapeHtml(TEXT[tab].help)}</p>
        ${canEdit ? `<div class="save-row">
          <input id="prompt-note" maxlength="200" placeholder="Qué cambiaste (opcional)">
          <button type="button" id="prompt-save">Guardar como nueva versión</button>
        </div>` : '<p class="hint">Solo los administradores pueden cambiar los prompts.</p>'}
        <p class="hint" id="prompt-status" role="status">${escapeHtml(status)}</p>
      </section>
      <aside class="history" aria-label="Historial de versiones">
        <h3>Historial</h3>
        <ol>${items}
          <li class="factory ${factoryActive ? "is-active" : ""}">
            <div class="v-head"><strong>${TEXT[tab].factory}</strong>${factoryActive ? ' <span class="status ok">Activa</span>' : ""}</div>
            <small>${TEXT[tab].factoryNote}</small>
            <div class="v-acts"><button type="button" class="link" data-load="${data.factory_id}">Ver en el editor</button></div>
          </li>
        </ol>
      </aside>
    </div>`;
    wire(p);
  }

  function setStatus(msg) { const s = $("prompt-status"); if (s) s.textContent = msg; }

  function wire(p) {
    const ta = $("prompt-text");
    const save = $("prompt-save");
    const sync = () => {
      drafts[tab] = ta.value;
      if (save) save.disabled = ta.value === p.active_text;
    };
    ta.oninput = sync;
    sync();
    if (save) save.onclick = () => act(() => postJson(`/api/prompts/${tab}`, { text: ta.value, note: $("prompt-note").value }), "Guardado como nueva versión y activado.");
    const factory = $("prompt-factory");
    if (factory) factory.onclick = () => act(() => postJson(`/api/prompts/${tab}/activate`, { version: data.factory_id }), "Restaurado el prompt de fábrica. Las versiones anteriores siguen en el historial.");
    $("prompts-body").querySelectorAll("[data-activate]").forEach((b) => (b.onclick = () =>
      act(() => postJson(`/api/prompts/${tab}/activate`, { version: b.dataset.activate }), `${versionName(p, b.dataset.activate)} activada.`)));
    $("prompts-body").querySelectorAll("[data-load]").forEach((b) => (b.onclick = () => {
      const id = b.dataset.load;
      ta.value = id === data.factory_id ? p.factory_text : p.versions.find((v) => v.id === id).text;
      sync();
      setStatus(ta.value === p.active_text
        ? `${versionName(p, id)} es la versión activa.`
        : `Viendo ${versionName(p, id).toLowerCase()}. Pulsa Guardar para usar este texto como una versión nueva, o Activar en el historial.`);
    }));
  }

  async function act(call, okMessage) {
    document.querySelectorAll("#prompts-body button").forEach((b) => (b.disabled = true));
    try {
      await call();
      delete drafts[tab];
      await load();
      render(okMessage);
    } catch (e) {
      render();
      setStatus(e.message);
    }
  }

  async function open() {
    const dlg = $("prompts");
    $("prompts-body").innerHTML = '<p class="thinking">Cargando…</p>';
    if (!dlg.open) dlg.showModal();
    try {
      await load();
      render();
    } catch (e) {
      $("prompts-body").innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`;
    }
  }

  function init() {
    $("open-prompts").onclick = open;
    $("prompts-close").onclick = () => $("prompts").close();
    document.querySelectorAll("#prompts [role=tab]").forEach((t) => (t.onclick = () => {
      if (!data) return;
      tab = t.dataset.tab;
      render();
    }));
  }

  return { init, open };
})();
