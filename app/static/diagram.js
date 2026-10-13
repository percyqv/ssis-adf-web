"use strict";
/* Pipeline diagrams: ADF-style activity cards laid out with ELK, rendered by Cytoscape. */

const Diagram = (() => {
  const CATEGORY_COLOR = {
    move: "#0b6bcb", dataflow: "#7a4fd1", sql: "#0e7c86", loop: "#b26b00", branch: "#b26b00",
    pipeline: "#2f6fd6", web: "#4f6278", function: "#4f6278", variable: "#4f6278",
    wait: "#7d838c", fail: "#c4312b", other: "#7d838c",
  };
  const CONDITION = {
    Succeeded: { color: "var(--ok)", label: "" },
    Failed: { color: "var(--err)", label: "Fallo" },
    Completed: { color: "var(--done)", label: "Al completar" },
    Skipped: { color: "var(--skip)", label: "Omitida" },
  };
  const ICON = {
    move: "M8 8h11v11H8z M5 16V5h11",
    dataflow: "M4 7h5l5 5h6 M4 17h5l5-5 M18 9l3 3-3 3",
    sql: "M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3z M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6 M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3",
    loop: "M4 12a8 8 0 0 1 14-5.3 M18 3v4h-4 M20 12a8 8 0 0 1-14 5.3 M6 21v-4h4",
    branch: "M6 3v12 M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M18 9a9 9 0 0 1-9 9",
    pipeline: "M8 5l11 7-11 7z",
    web: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M3 12h18 M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z",
    function: "M13 2L4 14h7l-1 8 9-12h-7z",
    variable: "M8 4c-2 0-3 1-3 3v2c0 1.5-1 3-2 3 1 0 2 1.5 2 3v2c0 2 1 3 3 3 M16 4c2 0 3 1 3 3v2c0 1.5 1 3 2 3-1 0-2 1.5-2 3v2c0 2-1 3-3 3",
    wait: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7v5l3 2",
    fail: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M9 9l6 6M15 9l-6 6",
    other: "M5 5h14v14H5z",
  };
  const NODE_W = 236, NODE_H = 62;

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function resolve(color) {
    const m = /^var\((--[\w-]+)\)$/.exec(color);
    return m ? css(m[1]) : color;
  }
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function svgUri(svg) {
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  }

  // The whole activity card (frame, category stripe, icon, type, warning badge) is an SVG
  // background; the activity name is the Cytoscape label so it stays crisp when zooming.
  function cardSvg(n) {
    const c = CATEGORY_COLOR[n.category] || CATEGORY_COLOR.other;
    const card = css("--card"), line = n.warning ? css("--warn") : css("--card-line");
    const muted = css("--muted");
    const badge = n.warning
      ? `<circle cx="${NODE_W - 16}" cy="16" r="8" fill="${css("--warn")}"/>
         <path d="M${NODE_W - 16} 11.5v5.5" stroke="#fff" stroke-width="2" stroke-linecap="round"/>
         <circle cx="${NODE_W - 16}" cy="20" r="1.2" fill="#fff"/>` : "";
    return svgUri(`<svg xmlns="http://www.w3.org/2000/svg" width="${NODE_W}" height="${NODE_H}">
      <defs><clipPath id="r"><rect x="1" y="1" width="${NODE_W - 2}" height="${NODE_H - 2}" rx="7"/></clipPath></defs>
      <rect x="1" y="1" width="${NODE_W - 2}" height="${NODE_H - 2}" rx="7" fill="${card}" stroke="${line}" stroke-width="${n.warning ? 2 : 1}"/>
      <rect x="0" y="0" width="5" height="${NODE_H}" fill="${c}" clip-path="url(#r)"/>
      <rect x="15" y="15" width="32" height="32" rx="8" fill="${c}" fill-opacity="0.12"/>
      <g transform="translate(19 19) scale(1)" fill="none" stroke="${c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON[n.category] || ICON.other}"/></g>
      <text x="58" y="24" font-family="Segoe UI, system-ui, sans-serif" font-size="11" fill="${muted}">${esc(n.typeLabel)}</text>
      ${badge}
    </svg>`);
  }

  function iconSvg(category) {
    const c = CATEGORY_COLOR[category] || CATEGORY_COLOR.other;
    return svgUri(`<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="${c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON[category] || ICON.other}"/></svg>`);
  }

  function elements(graph) {
    const nodes = graph.nodes.map((n) => ({
      group: "nodes",
      data: {
        id: n.id, parent: n.parent || undefined, label: n.label,
        clabel: n.branch ? n.label : `${n.typeLabel}: ${n.label}`,
        card: n.container ? undefined : cardSvg(n),
        icon: n.container && !n.branch ? iconSvg(n.category) : undefined,
        raw: n,
      },
      classes: [n.container ? "container" : "activity", n.branch ? "branch" : "", n.warning ? "warning" : ""].join(" "),
    }));
    const edges = graph.edges.map((e) => {
      const cond = CONDITION[e.condition] || { color: "var(--muted)", label: e.condition };
      return {
        group: "edges",
        data: { id: e.id, source: e.source, target: e.target, color: resolve(cond.color), label: cond.label },
      };
    });
    return nodes.concat(edges);
  }

  function style() {
    const ink = css("--ink"), muted = css("--muted"), canvas = css("--canvas"), accent = css("--accent");
    return [
      { selector: "node.activity", style: {
        shape: "round-rectangle", width: NODE_W, height: NODE_H,
        "background-opacity": 0, "background-image": "data(card)", "background-fit": "none",
        "background-width": NODE_W, "background-height": NODE_H, "background-clip": "none",
        label: "data(label)", color: ink, "font-family": "Segoe UI, system-ui, sans-serif",
        "font-size": 13, "font-weight": 600, "text-valign": "center", "text-halign": "center",
        "text-margin-x": 24, "text-margin-y": 9, "text-wrap": "ellipsis", "text-max-width": NODE_W - 92,
      } },
      { selector: "node.container", style: {
        shape: "round-rectangle", "background-color": css("--container"), "background-opacity": 1,
        "border-width": 1.5, "border-style": "dashed", "border-color": css("--container-line"),
        padding: 24, label: "data(clabel)", color: muted, "font-size": 12, "font-weight": 600,
        "font-family": "Segoe UI, system-ui, sans-serif",
        "text-valign": "top", "text-halign": "center", "text-margin-y": -6,
        "background-image": "data(icon)", "background-width": 16, "background-height": 16,
        "background-position-x": 8, "background-position-y": 8, "background-fit": "none",
        "background-clip": "none", "background-image-containment": "over",
      } },
      { selector: "node.branch", style: {
        "background-color": canvas, "border-style": "dotted", "font-weight": 500,
      } },
      { selector: "node.container.warning", style: { "border-color": css("--warn"), "border-style": "solid" } },
      { selector: "node:selected", style: {
        "underlay-color": accent, "underlay-opacity": 0.22, "underlay-padding": 6, "underlay-shape": "round-rectangle",
      } },
      { selector: "edge", style: {
        width: 2, "line-color": "data(color)", "target-arrow-color": "data(color)",
        "target-arrow-shape": "triangle", "arrow-scale": 1.1,
        "curve-style": "taxi", "taxi-direction": "rightward", "taxi-turn": "50%", "taxi-turn-min-distance": 14,
        label: "data(label)", "font-size": 10, "font-weight": 600, color: "data(color)",
        "text-background-color": canvas, "text-background-opacity": 1, "text-background-padding": 2,
        "text-background-shape": "round-rectangle", "font-family": "Segoe UI, system-ui, sans-serif",
      } },
    ];
  }

  // ELK does not inherit spacing into compound nodes, so the same values are
  // applied to the root graph and to every container.
  const SPACING = {
    "elk.layered.spacing.nodeNodeBetweenLayers": 80,
    "elk.layered.spacing.edgeNodeBetweenLayers": 28,
    "elk.spacing.nodeNode": 52,
    "elk.spacing.edgeNode": 24,
    "elk.padding": "[top=24,left=24,bottom=24,right=24]",
  };

  function layout() {
    return {
      name: "elk", fit: true, padding: 28, animate: false,
      nodeLayoutOptions: (n) => (n.isParent() ? SPACING : {}),
      elk: {
        algorithm: "layered",
        "elk.direction": "RIGHT",
        "elk.hierarchyHandling": "INCLUDE_CHILDREN",
        "elk.edgeRouting": "ORTHOGONAL",
        ...SPACING,
        "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      },
    };
  }

  function fitCapped(cy, max = 1.1) {
    cy.fit(undefined, 28);
    if (cy.zoom() > max) {
      cy.zoom(max);
      cy.center();
    }
  }

  function render(container, graph, { interactive = true, onSelect } = {}) {
    const cy = cytoscape({
      container,
      elements: elements(graph),
      style: style(),
      layout: layout(),
      minZoom: 0.2,
      maxZoom: 2.5,
      wheelSensitivity: 0.25,
      userZoomingEnabled: interactive,
      userPanningEnabled: interactive,
      boxSelectionEnabled: false,
      autoungrabify: !interactive,
      autounselectify: !interactive,
    });
    // Small pipelines would be blown up by "fit"; keep cards at their natural size.
    cy.one("layoutstop", () => fitCapped(cy, interactive ? 1.1 : 0.9));
    if (interactive && onSelect) {
      cy.on("tap", "node", (e) => onSelect(e.target.data("raw")));
      cy.on("tap", (e) => { if (e.target === cy) onSelect(null); });
    }
    return cy;
  }

  // ------------------------------------------------------------------ viewer
  let viewerCy = null, viewerGraph = null;

  function detailsHtml(n) {
    if (!n) {
      return `<p class="hint">Selecciona una actividad para ver su configuración.</p>`;
    }
    const warn = n.warning
      ? `<div class="callout">${esc(n.description.replace(/\[(MANUAL REVIEW|UNSUPPORTED)[^\]]*\]\s*/gi, ""))}</div>` : "";
    const rows = (n.details || []).map((d) => {
      const long = d.value.length > 60 || /\n|select |insert |update |truncate |exec /i.test(d.value);
      return `<dt>${esc(d.label)}</dt><dd>${long ? `<pre>${esc(d.value)}</pre>` : esc(d.value)}</dd>`;
    }).join("");
    const json = n.json && Object.keys(n.json).length
      ? `<details class="json"><summary>JSON de la actividad</summary><pre>${esc(JSON.stringify(n.json, null, 2))}</pre></details>` : "";
    return `<p class="kind" style="--cat:${CATEGORY_COLOR[n.category] || CATEGORY_COLOR.other}">${esc(n.typeLabel)}</p>
      <h3>${esc(n.label)}</h3>${warn}<dl>${rows || "<dt>Sin detalles</dt><dd></dd>"}</dl>${json}`;
  }

  function open(graph) {
    viewerGraph = graph;
    const dlg = document.getElementById("viewer");
    document.getElementById("viewer-title").textContent = graph.name;
    const s = graph.stats;
    document.getElementById("viewer-meta").textContent =
      `${s.activities} actividades` + (s.warnings ? `, ${s.warnings} para revisar` : "");
    const panel = document.getElementById("viewer-details");
    panel.innerHTML = detailsHtml(null);
    if (!dlg.open) dlg.showModal();
    if (viewerCy) viewerCy.destroy();
    viewerCy = render(document.getElementById("viewer-canvas"), graph, {
      onSelect: (n) => { panel.innerHTML = detailsHtml(n); },
    });
  }

  function zoom(factor) {
    if (!viewerCy) return;
    const z = viewerCy.zoom() * factor;
    viewerCy.zoom({ level: z, renderedPosition: { x: viewerCy.width() / 2, y: viewerCy.height() / 2 } });
  }

  function exportPng() {
    if (!viewerCy) return;
    const blob = viewerCy.png({ output: "blob", full: true, scale: 2, bg: css("--canvas") });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${viewerGraph?.name || "pipeline"}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  function initViewer() {
    const dlg = document.getElementById("viewer");
    document.getElementById("viewer-fit").onclick = () => viewerCy && fitCapped(viewerCy);
    document.getElementById("viewer-zoom-in").onclick = () => zoom(1.25);
    document.getElementById("viewer-zoom-out").onclick = () => zoom(0.8);
    document.getElementById("viewer-png").onclick = exportPng;
    document.getElementById("viewer-close").onclick = () => dlg.close();
    dlg.addEventListener("close", () => { if (viewerCy) { viewerCy.destroy(); viewerCy = null; } });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (viewerCy && viewerGraph) open(viewerGraph);
    });
  }

  // Compact, non-interactive preview used inside chat and result cards.
  function preview(container, graph) {
    const cy = render(container, graph, { interactive: false });
    cy.on("tap", () => open(graph));
    return cy;
  }

  return { open, preview, initViewer };
})();
