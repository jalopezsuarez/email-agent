// Vanilla JS panel for Email-Agent.

const api = (path, opts = {}) =>
  fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  }).then(async (r) => {
    const text = await r.text();
    const data = text ? JSON.parse(text) : {};
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  });

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const el = (tag, attrs = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.append(c.nodeType ? c : document.createTextNode(c));
  }
  return e;
};

// Tabs --------------------------------------------------------------------
$$("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("nav button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === tab));
    loadTab(tab);
  });
});

async function loadStatus() {
  const pill = $("#status-pill");
  try {
    const s = await api("/status");
    $("#kpi-total").textContent = s.stats.total_emails ?? 0;
    $("#kpi-classified").textContent = s.stats.classified ?? 0;
    $("#kpi-pending").textContent = s.stats.pending_review ?? 0;
    $("#kpi-drafts").textContent = s.stats.drafts ?? 0;
    $("#kpi-style").textContent = s.style_samples ?? 0;
    $("#kpi-conf").textContent =
      s.stats.avg_classifier_confidence != null
        ? s.stats.avg_classifier_confidence
        : "—";
    pill.textContent = s.graph_connected
      ? `Graph ✓ · ${s.llm_provider} · :${s.port}`
      : `Graph desconectado · ${s.llm_provider}`;
    pill.className = s.graph_connected ? "ok" : "warn";
    // Populate form with current runtime values
    $$("#config-form [name]").forEach((f) => {
      const key = f.name;
      if (key in (s.classifier_threshold != null ? { "classifier.threshold": s.classifier_threshold } : {})) {
        f.value = s.classifier_threshold;
      }
    });
    $('#config-form [name="classifier.threshold"]').value = s.classifier_threshold;
    $('#config-form [name="responder.personal_confidence_threshold"]').value =
      s.personal_threshold;
  } catch (e) {
    pill.textContent = "Error: " + e.message;
    pill.className = "err";
  }
}

async function loadReviews() {
  const [reviews, folders] = await Promise.all([
    api("/pending-reviews"),
    api("/folders"),
  ]);
  const list = $("#reviews-list");
  list.innerHTML = "";
  if (!reviews.length) {
    list.append(el("p", { class: "hint" }, "Nada pendiente. ¡Bien hecho!"));
    return;
  }
  reviews.forEach((r) => {
    const folderSelect = el(
      "select",
      {},
      ...folders.map((f) =>
        el("option", { value: f.id, "data-name": f.full_name }, f.full_name),
      ),
    );
    if (r.folder_id) folderSelect.value = r.folder_id;

    const categorySelect = el(
      "select",
      {},
      ...["", "personal", "work", "transactional", "marketing", "notification"].map((c) =>
        el("option", { value: c }, c || "(sin categoría)"),
      ),
    );
    if (r.category) categorySelect.value = r.category;

    const note = el("input", {
      type: "text",
      placeholder: "Nota en lenguaje natural (opcional)",
      style: "flex:1",
    });

    const card = el(
      "div",
      { class: "review" },
      el("h4", {}, r.subject || "(sin asunto)"),
      el("div", { class: "meta" }, `De ${r.from_name || ""} <${r.from_addr || ""}> · ${r.received_at}`),
      el("div", { class: "snippet" }, r.body_snippet || ""),
      el(
        "div",
        { class: "suggestion" },
        "Sugerencia: ",
        el("span", { class: "folder" }, r.folder_name || "—"),
        " · confianza ",
        el("span", { class: "conf" }, String(r.confidence ?? "—")),
      ),
      el(
        "div",
        { class: "review-actions" },
        folderSelect,
        categorySelect,
        note,
        el(
          "button",
          {
            onclick: async () => {
              await api(`/pending-reviews/${r.id}/resolve`, {
                method: "POST",
                body: {
                  folder_id: folderSelect.value,
                  folder_name:
                    folderSelect.options[folderSelect.selectedIndex].dataset.name,
                  category: categorySelect.value || null,
                  user_note: note.value || null,
                },
              });
              loadReviews();
              loadStatus();
            },
          },
          "Guardar y enseñar",
        ),
      ),
    );
    list.append(card);
  });
}

async function loadDrafts() {
  const drafts = await api("/drafts");
  const list = $("#drafts-list");
  list.innerHTML = "";
  if (!drafts.length) {
    list.append(el("p", { class: "hint" }, "Aún no hay borradores generados."));
    return;
  }
  drafts.forEach((d) => {
    const ta = el("textarea", {}, d.body_html || "");
    const card = el(
      "div",
      { class: "draft" },
      el("h4", {}, "Re: " + (d.subject || "")),
      el("div", { class: "meta" }, `De ${d.from_name || ""} <${d.from_addr || ""}>`),
      el("div", { class: "snippet" }, d.body_snippet || ""),
      ta,
      el(
        "div",
        { class: "review-actions" },
        el(
          "button",
          {
            onclick: async () => {
              await api(`/drafts/${d.id}`, {
                method: "PUT",
                body: { body_html: ta.value },
              });
              alert("Borrador actualizado en Outlook.");
            },
          },
          "Guardar cambios",
        ),
        el(
          "button",
          {
            class: "secondary",
            onclick: async () => {
              await api(`/drafts/${d.id}/approve`, {
                method: "POST",
                body: { approved: true },
              });
              alert("Marcado como aprobado. (Envío manual desde Outlook.)");
            },
          },
          "Marcar aprobado",
        ),
      ),
    );
    list.append(card);
  });
}

async function loadTraining() {
  const samples = await api("/style-samples");
  const list = $("#style-samples");
  list.innerHTML = "";
  if (!samples.length) {
    list.append(
      el("p", { class: "hint" }, 'Sin muestras. Pulsa «Analizar Sent Items».'),
    );
    return;
  }
  samples.forEach((s) => {
    const tagInput = el("input", { type: "text", value: s.tone_tag || "", placeholder: "ej. cercano" });
    list.append(
      el(
        "div",
        { class: "style-sample" },
        el("h4", {}, s.subject || "(sin asunto)"),
        el("div", { class: "meta" }, `A ${s.recipient || ""} · ${s.sent_at || ""} · ${s.word_count || 0} palabras`),
        el("div", { class: "snippet" }, (s.body_text || "").slice(0, 600)),
        el(
          "div",
          { class: "review-actions" },
          tagInput,
          el(
            "button",
            {
              onclick: async () => {
                await api(`/style-samples/${s.id}/tag`, {
                  method: "POST",
                  body: { tone_tag: tagInput.value },
                });
              },
            },
            "Etiquetar tono",
          ),
        ),
      ),
    );
  });
}

async function loadConfig() {
  const data = await api("/config");
  $("#config-view").innerHTML =
    "<pre>" + escapeHtml(JSON.stringify(data, null, 2)) + "</pre>";
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

$("#config-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const values = {};
  $$("#config-form [name]").forEach((f) => {
    if (f.value !== "") values[f.name] = String(f.value);
  });
  await api("/config", { method: "PUT", body: { values } });
  alert("Configuración guardada.");
  loadStatus();
});

$("#btn-scan").addEventListener("click", async () => {
  $("#scan-result").textContent = "Escaneando…";
  const r = await api("/scan-now", { method: "POST" });
  $("#scan-result").textContent =
    `Procesados ${r.processed} · clasificados ${r.classified} · pendientes ${r.pending_review} · borradores ${r.drafted}`;
  loadStatus();
});

$("#btn-train-style").addEventListener("click", async () => {
  $("#train-result").textContent = "Analizando…";
  const r = await api("/train/style", { method: "POST" });
  $("#train-result").textContent = `Nuevas muestras: ${r.new_samples}`;
  loadTraining();
});

function loadTab(tab) {
  if (tab === "dashboard") loadStatus();
  if (tab === "reviews") loadReviews();
  if (tab === "drafts") loadDrafts();
  if (tab === "training") loadTraining();
  if (tab === "config") loadConfig();
}

// Initial load
loadStatus();
setInterval(loadStatus, 20000);
