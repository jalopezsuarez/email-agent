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
const TONE_OPTIONS = [
  ["neutral", "Neutro"],
  ["friendly", "Cercano"],
  ["warm", "Calido"],
  ["formal", "Formal"],
  ["direct", "Directo"],
  ["brief", "Breve"],
  ["supportive", "Empatico"],
  ["assertive", "Asertivo"],
];
const toneLabel = (value) =>
  TONE_OPTIONS.find(([tone]) => tone === value)?.[1] || value || "Neutro";
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
    if (s.graph_connected) {
      pill.textContent = `Graph ✓ · ${s.llm_provider} · :${s.port}`;
      pill.className = "ok";
    } else if (s.graph_pending) {
      pill.textContent = `Graph esperando login · ${s.llm_provider}`;
      pill.className = "warn";
    } else {
      pill.textContent = `Graph desconectado · ${s.llm_provider}`;
      pill.className = "warn";
    }
    renderGraphStatus(s);
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

function renderGraphStatus(status) {
  const btn = $("#btn-graph-connect");
  const result = $("#graph-connect-result");
  const help = $("#graph-help");
  if (status.graph_connected) {
    btn.style.display = "none";
    btn.disabled = false;
    btn.textContent = "Conectar Microsoft";
    result.textContent = "";
    help.textContent = status.graph_message || "Graph conectado.";
    return;
  }
  btn.style.display = "";
  btn.disabled = Boolean(status.graph_pending);
  btn.textContent = status.graph_pending ? "Esperando login..." : "Conectar Microsoft";
  result.textContent = status.graph_pending
    ? "Abre Microsoft y completa el login."
    : "";
  help.textContent = status.graph_error || status.graph_message || "";
}

const reviewState = {
  items: [],
  folders: [],
  details: new Map(),
  forms: new Map(),
  selectedId: null,
};

async function loadReviews() {
  const [reviews, folders] = await Promise.all([api("/pending-reviews"), api("/folders")]);
  reviewState.items = reviews;
  reviewState.folders = folders;
  const list = $("#reviews-list");
  const detail = $("#review-detail");
  list.innerHTML = "";
  detail.innerHTML = "";
  if (!reviews.length) {
    list.append(el("p", { class: "hint" }, "Nada pendiente. ¡Bien hecho!"));
    detail.append(el("p", { class: "hint review-detail-empty" }, "No hay mensajes para revisar."));
    reviewState.selectedId = null;
    return;
  }
  reviews.forEach((review) => ensureReviewForm(review));
  if (!reviews.some((review) => review.id === reviewState.selectedId)) {
    reviewState.selectedId = reviews[0].id;
  }
  renderReviewList();
  renderReviewDetail(true);
  await ensureReviewDetail(reviewState.selectedId);
  renderReviewDetail(false);
}

function ensureReviewForm(review) {
  if (!reviewState.forms.has(review.id)) {
    reviewState.forms.set(review.id, {
      folderId: review.folder_id || "",
      category: review.category || "",
      note: "",
    });
  }
  return reviewState.forms.get(review.id);
}

function getReviewById(reviewId) {
  return reviewState.items.find((review) => review.id === reviewId) || null;
}

async function ensureReviewDetail(reviewId) {
  if (!reviewId || reviewState.details.has(reviewId)) return reviewState.details.get(reviewId);
  const fallback = getReviewById(reviewId);
  try {
    const detail = await api(`/pending-reviews/${reviewId}`);
    reviewState.details.set(reviewId, detail);
  } catch (e) {
    reviewState.details.set(reviewId, {
      ...(fallback || {}),
      body_text: fallback?.body_snippet || "",
      body_error: e.message,
    });
  }
  return reviewState.details.get(reviewId);
}

function renderReviewList() {
  const list = $("#reviews-list");
  list.innerHTML = "";
  reviewState.items.forEach((review) => {
    const selected = review.id === reviewState.selectedId;
    const item = el(
      "article",
      {
        class: `review-item${selected ? " selected" : ""}`,
        onclick: async (ev) => {
          if (ev.target.closest("input, select, button, textarea")) return;
          reviewState.selectedId = review.id;
          renderReviewList();
          renderReviewDetail(true);
          await ensureReviewDetail(review.id);
          renderReviewDetail(false);
        },
      },
      el(
        "div",
        { class: "review-line review-line-from" },
        el("span", { class: "review-from" }, formatReviewSender(review)),
        el("span", { class: "review-date" }, formatReviewDate(review.received_at)),
      ),
      el("div", { class: "review-line review-line-subject clamp-1" }, review.subject || "(sin asunto)"),
      el("div", { class: "review-line review-line-snippet clamp-2" }, review.body_snippet || ""),
      buildReviewRoutingRow(review, "list"),
      buildReviewActionRow(review, "list"),
    );
    list.append(item);
  });
}

function renderReviewDetail(loading = false) {
  const detailRoot = $("#review-detail");
  detailRoot.innerHTML = "";
  const review = getReviewById(reviewState.selectedId);
  if (!review) {
    detailRoot.append(el("p", { class: "hint review-detail-empty" }, "Selecciona un mensaje."));
    return;
  }
  const detail = reviewState.details.get(review.id) || review;
  detailRoot.append(
    el(
      "div",
      { class: "review-detail-card" },
      el("div", { class: "review-detail-line review-detail-from" }, formatReviewSender(review)),
      el("div", { class: "review-detail-line review-detail-date" }, formatReviewDate(review.received_at)),
      el("div", { class: "review-detail-line review-detail-subject" }, review.subject || "(sin asunto)"),
      el("div", { class: "review-detail-line review-detail-snippet" }, review.body_snippet || ""),
      buildReviewRoutingRow(review, "detail"),
      buildReviewActionRow(review, "detail"),
      el("div", { class: "review-detail-body-title" }, "Cuerpo completo"),
      loading
        ? el("div", { class: "review-detail-body review-detail-loading" }, "Cargando mensaje completo…")
        : el("div", { class: "review-detail-body" }, detail.body_text || review.body_snippet || ""),
      !loading && detail.body_error
        ? el("p", { class: "hint review-detail-error" }, `No se pudo recuperar el cuerpo completo: ${detail.body_error}`)
        : null,
    ),
  );
}

function buildReviewRoutingRow(review, variant) {
  return el(
    "div",
    { class: `review-line review-routing-row ${variant}`.trim() },
    el("span", { class: "review-chip review-chip-accent" }, `Sugerida: ${review.folder_name || "—"}`),
    el("span", { class: "review-chip" }, `Confianza ${formatReviewConfidence(review.confidence)}`),
    buildFolderSelect(review.id),
    buildCategorySelect(review.id),
  );
}

function buildReviewActionRow(review, variant) {
  return el(
    "div",
    { class: `review-line review-actions-row ${variant}`.trim() },
    buildReviewNoteInput(review.id),
    buildReviewSaveButton(review.id),
  );
}

function buildFolderSelect(reviewId) {
  const form = reviewState.forms.get(reviewId);
  const select = el(
    "select",
    {
      class: "review-select review-folder-select",
      "data-review-id": String(reviewId),
      "data-field": "folderId",
      onclick: (ev) => ev.stopPropagation(),
      onchange: (ev) => {
        setReviewFormField(reviewId, "folderId", ev.target.value);
      },
    },
    ...reviewState.folders.map((folder) =>
      el("option", { value: folder.id }, folder.full_name),
    ),
  );
  if (form?.folderId) select.value = form.folderId;
  return select;
}

function buildCategorySelect(reviewId) {
  const form = reviewState.forms.get(reviewId);
  const select = el(
    "select",
    {
      class: "review-select review-category-select",
      "data-review-id": String(reviewId),
      "data-field": "category",
      onclick: (ev) => ev.stopPropagation(),
      onchange: (ev) => {
        setReviewFormField(reviewId, "category", ev.target.value);
      },
    },
    ...["", "personal", "work", "transactional", "marketing", "notification"].map((category) =>
      el("option", { value: category }, category || "(sin categoría)"),
    ),
  );
  select.value = form?.category || "";
  return select;
}

function buildReviewNoteInput(reviewId) {
  const form = reviewState.forms.get(reviewId);
  const input = el("input", {
    type: "text",
    class: "review-note-input",
    placeholder: "Nota en lenguaje natural (opcional)",
    "data-review-id": String(reviewId),
    "data-field": "note",
    value: form?.note || "",
    onclick: (ev) => ev.stopPropagation(),
    oninput: (ev) => {
      setReviewFormField(reviewId, "note", ev.target.value);
    },
  });
  input.value = form?.note || "";
  return input;
}

function buildReviewSaveButton(reviewId) {
  return el(
    "button",
    {
      class: "review-save-button",
      onclick: async (ev) => {
        ev.stopPropagation();
        await saveReviewResolution(reviewId);
      },
    },
    "Guardar y enseñar",
  );
}

function setReviewFormField(reviewId, field, value) {
  const form = reviewState.forms.get(reviewId);
  if (!form) return;
  form[field] = value;
  syncReviewFormField(reviewId, field);
}

function syncReviewFormField(reviewId, field) {
  const form = reviewState.forms.get(reviewId);
  if (!form) return;
  $$(`[data-review-id="${reviewId}"][data-field="${field}"]`).forEach((node) => {
    if (document.activeElement === node) return;
    node.value = form[field] || "";
  });
}

async function saveReviewResolution(reviewId) {
  const review = getReviewById(reviewId);
  const form = reviewState.forms.get(reviewId);
  if (!review || !form?.folderId) return;
  const folder = reviewState.folders.find((entry) => entry.id === form.folderId);
  await api(`/pending-reviews/${reviewId}/resolve`, {
    method: "POST",
    body: {
      folder_id: form.folderId,
      folder_name: folder?.full_name || review.folder_name || "",
      category: form.category || null,
      user_note: form.note || null,
    },
  });
  reviewState.details.delete(reviewId);
  reviewState.forms.delete(reviewId);
  await loadReviews();
  await loadStatus();
}

function formatReviewSender(review) {
  const name = review.from_name || "";
  const addr = review.from_addr || "";
  return name ? `${name} <${addr}>` : addr || "(sin remitente)";
}

function formatReviewDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("es-ES", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatReviewConfidence(value) {
  if (value == null || value === "") return "—";
  return Number(value).toFixed(2);
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
    const selectedTone = s.tone_tag || s.suggested_tone_tag || "neutral";
    const toneSelect = el(
      "select",
      { class: "tone-select" },
      ...TONE_OPTIONS.map(([value, label]) => el("option", { value }, label)),
    );
    toneSelect.value = selectedTone;
    list.append(
      el(
        "div",
        { class: "style-sample" },
        el("h4", {}, s.subject || "(sin asunto)"),
        el("div", { class: "meta" }, `A ${s.recipient || ""} · ${s.sent_at || ""} · ${s.word_count || 0} palabras`),
        el("div", { class: "snippet" }, (s.body_text || "").slice(0, 600)),
        el(
          "div",
          { class: "tone-hint" },
          s.tone_tag
            ? `Tono guardado: ${toneLabel(s.tone_tag)}`
            : `Sugerencia: ${toneLabel(s.suggested_tone_tag || "neutral")}`,
        ),
        el(
          "div",
          { class: "review-actions" },
          toneSelect,
          el(
            "button",
            {
              onclick: async () => {
                await api(`/style-samples/${s.id}/tag`, {
                  method: "POST",
                  body: { tone_tag: toneSelect.value },
                });
                await loadTraining();
              },
            },
            "Guardar tono",
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

$("#btn-graph-connect").addEventListener("click", async () => {
  $("#graph-connect-result").textContent = "Iniciando login…";
  try {
    await api("/graph/connect", { method: "POST" });
    await loadStatus();
  } catch (e) {
    $("#graph-connect-result").textContent = e.message;
  }
});

$("#btn-scan").addEventListener("click", async () => {
  $("#scan-result").textContent = "Escaneando…";
  try {
    const r = await api("/scan-now", { method: "POST" });
    $("#scan-result").textContent =
      `Procesados ${r.processed} · clasificados ${r.classified} · pendientes ${r.pending_review} · borradores ${r.drafted}`;
    loadStatus();
  } catch (e) {
    $("#scan-result").textContent = e.message;
  }
});

$("#btn-train-style").addEventListener("click", async () => {
  $("#train-result").textContent = "Analizando…";
  try {
    const r = await api("/train/style", { method: "POST" });
    $("#train-result").textContent = `Nuevas muestras: ${r.new_samples}`;
    loadTraining();
  } catch (e) {
    $("#train-result").textContent = e.message;
  }
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
setInterval(loadStatus, 5000);
