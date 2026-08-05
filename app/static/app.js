"use strict";

// Front end for the draft builder. Talks to the JSON endpoints in app/web.py.

const state = {
  csvId: null,
  filename: null,
  contactCount: 0,
  connected: false,
  lastFocused: null,
};

const el = (id) => document.getElementById(id);

function toast(message, isError) {
  const node = el("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  node.style.background = isError ? "#a4262c" : "#1c1f23";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 4500);
}

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error(`Server returned ${response.status}`);
  }
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `Request failed with ${response.status}`);
  }
  return payload;
}

function postJson(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function templatePayload() {
  return {
    csv_id: state.csvId,
    sender_name: el("sender-name").value,
    subject_template: el("subject").value,
    body_template: el("body").value,
    signoff_template: el("signoff").value,
    cc: el("cc").value,
    bcc: el("bcc").value,
    send_as_html: el("send-as-html").checked,
    skip_incomplete: el("skip-incomplete").checked,
    skip_previously_emailed: el("skip-previously-emailed").checked,
  };
}

function humanSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Connection

async function refreshStatus() {
  const pill = el("account");
  try {
    const status = await api("/api/status");
    state.connected = status.connected;
    if (status.connected) {
      pill.textContent = status.email || "Connected";
      pill.className = "pill pill-ok";
      el("connect").classList.add("hidden");
      el("disconnect").classList.remove("hidden");
    } else {
      pill.textContent = status.client_secret_present
        ? "Not connected"
        : "Missing credentials/client_secret.json";
      pill.className = "pill pill-off";
      el("connect").classList.remove("hidden");
      el("disconnect").classList.add("hidden");
    }
  } catch (error) {
    pill.textContent = "Status unavailable";
    pill.className = "pill pill-off";
  }
  updateCreateButton();
}

function updateCreateButton() {
  el("create").disabled = !(state.connected && state.csvId && state.contactCount > 0);
}

// Field chips

// The server owns the default field list, passed in on the chips container.
function defaultFields() {
  const raw = el("field-chips").dataset.defaultFields || "";
  return raw.split(",").filter(Boolean);
}

function renderChips(fields) {
  const container = el("field-chips");
  container.textContent = "";
  fields.forEach((field) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = `{{${field}}}`;
    chip.title = "Insert into the last field you clicked";
    chip.addEventListener("click", () => insertPlaceholder(field));
    container.appendChild(chip);
  });
}

function insertPlaceholder(field) {
  const target = state.lastFocused || el("body");
  const token = `{{${field}}}`;
  const start = target.selectionStart ?? target.value.length;
  const end = target.selectionEnd ?? target.value.length;
  target.value = target.value.slice(0, start) + token + target.value.slice(end);
  target.focus();
  target.setSelectionRange(start + token.length, start + token.length);
}

// CSV

async function uploadCsv(file) {
  const form = new FormData();
  form.append("file", file);
  el("csv-summary").textContent = "Reading file";
  try {
    const result = await api("/api/upload-csv", { method: "POST", body: form });
    state.csvId = result.csv_id;
    state.filename = result.filename;
    state.contactCount = result.contact_count;
    el("csv-summary").textContent = `${result.filename}: ${result.contact_count} contacts`;
    renderCsvDetail(result);
    // Once a CSV is loaded, offer its real columns instead of the generic list.
    renderChips(
      result.available_fields?.length
        ? result.available_fields.concat("sender_name")
        : defaultFields()
    );
    toast(`Loaded ${result.contact_count} contacts`);
  } catch (error) {
    state.csvId = null;
    state.contactCount = 0;
    el("csv-summary").textContent = "No file loaded";
    el("csv-detail").classList.add("hidden");
    toast(error.message, true);
  }
  updateCreateButton();
}

function renderCsvDetail(result) {
  const detail = el("csv-detail");
  detail.textContent = "";

  const mapped = document.createElement("div");
  mapped.textContent = "Columns matched:";
  const table = document.createElement("table");
  Object.entries(result.detected).forEach(([field, header]) => {
    const row = table.insertRow();
    row.insertCell().textContent = field;
    const cell = row.insertCell();
    const code = document.createElement("code");
    code.textContent = header;
    cell.appendChild(code);
  });
  mapped.appendChild(table);
  detail.appendChild(mapped);

  if (result.skipped?.length) {
    const skipped = document.createElement("div");
    skipped.className = "warn";
    skipped.style.marginTop = "8px";
    const shown = result.skipped.slice(0, 8)
      .map((row) => `row ${row.row}: ${row.reason}`)
      .join("; ");
    const extra = result.skipped.length > 8 ? ` and ${result.skipped.length - 8} more` : "";
    skipped.textContent = `Skipped ${result.skipped.length} row(s). ${shown}${extra}`;
    detail.appendChild(skipped);
  }

  detail.classList.remove("hidden");
}

// Attachments

function renderAttachments(attachments) {
  const list = el("attachment-list");
  list.textContent = "";
  el("attachment-summary").textContent = attachments.length
    ? `${attachments.length} file(s) attached to every draft`
    : "No attachments";

  attachments.forEach((item) => {
    const row = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `${item.filename} (${humanSize(item.size)})`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-small btn-danger";
    remove.textContent = "Remove";
    remove.addEventListener("click", async () => {
      try {
        const result = await postJson("/api/remove-attachment", { filename: item.filename });
        renderAttachments(result.attachments);
      } catch (error) {
        toast(error.message, true);
      }
    });
    row.appendChild(label);
    row.appendChild(remove);
    list.appendChild(row);
  });
}

async function uploadAttachment(file) {
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api("/api/upload-attachment", { method: "POST", body: form });
    renderAttachments(result.attachments);
    toast(`Attached ${file.name}`);
  } catch (error) {
    toast(error.message, true);
  }
}

// Preview

async function runPreview() {
  if (!state.csvId) {
    toast("Upload a CSV first", true);
    return;
  }
  const area = el("preview-area");
  area.textContent = "Rendering";
  area.classList.remove("hidden");
  try {
    const result = await postJson("/api/preview", templatePayload());
    area.textContent = "";

    const summary = document.createElement("div");
    summary.className = "hint";
    summary.textContent =
      `Showing ${result.drafts.length} of ${result.total} contacts.` +
      (result.incomplete
        ? ` ${result.incomplete} contact(s) are missing a field used by the template.`
        : " Every contact has all the fields the template uses.");
    area.appendChild(summary);

    result.drafts.forEach((draft) => {
      const card = document.createElement("div");
      card.className = "draft";

      const head = document.createElement("div");
      head.className = "draft-head";
      head.textContent = `To: ${draft.to}`;
      card.appendChild(head);

      const subject = document.createElement("div");
      subject.className = "draft-subject";
      subject.textContent = draft.subject;
      card.appendChild(subject);

      const body = document.createElement("pre");
      body.className = "draft-body";
      body.textContent = draft.body;
      card.appendChild(body);

      if (draft.missing?.length) {
        const missing = document.createElement("div");
        missing.className = "warn";
        missing.textContent = `Missing: ${draft.missing.join(", ")}`;
        card.appendChild(missing);
      }
      area.appendChild(card);
    });
  } catch (error) {
    area.textContent = "";
    const message = document.createElement("div");
    message.className = "error";
    message.textContent = error.message;
    area.appendChild(message);
  }
}

// Prior contact

// Renders the blocked list shared by the pre-check and the post-create report.
function renderBlockedList(container, blocked) {
  const table = document.createElement("table");
  table.style.width = "100%";
  table.style.borderCollapse = "collapse";
  table.style.marginTop = "8px";
  table.style.fontSize = "13px";

  const head = table.createTHead().insertRow();
  ["Address", "Why", "First emailed", "Messages"].forEach((title) => {
    const cell = document.createElement("th");
    cell.textContent = title;
    cell.style.textAlign = "left";
    cell.style.padding = "4px 8px 4px 0";
    head.appendChild(cell);
  });

  const body = table.createTBody();
  blocked.forEach((entry) => {
    const row = body.insertRow();
    [
      entry.email,
      entry.detail || entry.label,
      entry.first_contact || "not recorded",
      entry.message_count ? String(entry.message_count) : "",
    ].forEach((value) => {
      const cell = row.insertCell();
      cell.textContent = value;
      cell.style.padding = "4px 8px 4px 0";
      cell.style.borderTop = "1px solid var(--border)";
    });
  });
  container.appendChild(table);
}

function renderHistoryReport(container, report, headline) {
  container.textContent = "";

  const summary = document.createElement("div");
  summary.className = report.blocked_count ? "warn" : "ok";
  summary.textContent = headline;
  container.appendChild(summary);

  if (!report.checked_sent_mail) {
    const note = document.createElement("div");
    note.className = "hint";
    note.textContent =
      "Your sent mail was not searched, so someone you emailed outside this app could still be included.";
    container.appendChild(note);
  }

  if (report.sent_check_errors?.length) {
    const note = document.createElement("div");
    note.className = "error";
    note.textContent =
      `${report.sent_check_errors.length} sent mail lookup(s) failed, so those contacts were not verified: ` +
      report.sent_check_errors[0].error;
    container.appendChild(note);
  }

  if (report.blocked?.length) {
    renderBlockedList(container, report.blocked);
  }
  container.classList.remove("hidden");
}

async function checkHistory() {
  if (!state.csvId) {
    toast("Upload a CSV first", true);
    return;
  }
  const area = el("history-area");
  area.textContent = "Checking sent mail, this can take a moment for long lists";
  area.classList.remove("hidden");
  try {
    const result = await postJson("/api/check-history", templatePayload());
    const headline = result.blocked_count
      ? `${result.blocked_count} of ${result.total} contacts were emailed before and will be skipped. ` +
        `${result.would_draft} would be drafted.`
      : `None of the ${result.total} contacts have been emailed before.`;
    renderHistoryReport(area, result, headline);
    if (result.slow_warning) {
      toast("Long list: the sent mail check adds one lookup per contact", false);
    }
  } catch (error) {
    area.textContent = "";
    const message = document.createElement("div");
    message.className = "error";
    message.textContent = error.message;
    area.appendChild(message);
  }
}

// Do not contact list

async function loadSuppression() {
  try {
    const result = await api("/api/suppression");
    const list = el("suppression-list");
    list.textContent = "";
    el("suppression-summary").textContent = result.count
      ? `${result.count} address(es) blocked`
      : "No addresses listed";

    result.entries.forEach((entry) => {
      const row = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = entry.note ? `${entry.email} (${entry.note})` : entry.email;
      row.appendChild(label);
      list.appendChild(row);
    });
  } catch (error) {
    el("suppression-summary").textContent = error.message;
  }
}

async function addSuppression() {
  const value = el("suppression-input").value.trim();
  if (!value) {
    toast("Enter an address first", true);
    return;
  }
  try {
    const result = await postJson("/api/suppression", {
      emails: value.split(/[\s,;]+/).filter(Boolean),
      note: el("suppression-note").value.trim(),
    });
    el("suppression-input").value = "";
    el("suppression-note").value = "";
    toast(result.added ? `${result.added} address(es) added` : "Already on the list");
    await loadSuppression();
  } catch (error) {
    toast(error.message, true);
  }
}

// Create

async function createDrafts() {
  const guarded = el("skip-previously-emailed").checked;
  const caveat = guarded
    ? " Anyone already emailed from this account is skipped, so the final count may be lower."
    : "";
  if (!confirm(`Create up to ${state.contactCount} Gmail drafts? Nothing is sent.${caveat}`)) {
    return;
  }
  const button = el("create");
  const status = el("create-status");
  button.disabled = true;
  status.textContent = "Creating drafts, this can take a while for large lists";
  try {
    const result = await postJson("/api/create-drafts", templatePayload());
    const parts = [`${result.created} draft(s) created`];
    if (result.blocked) parts.push(`${result.blocked} skipped as already emailed`);
    if (result.skipped) parts.push(`${result.skipped} skipped for missing fields`);
    if (result.failed) parts.push(`${result.failed} failed`);
    status.textContent = parts.join(", ") + ".";
    status.className = result.failed ? "warn" : "ok";

    if (result.history_report) {
      const headline = result.blocked
        ? `${result.blocked} contact(s) were held back because they had been emailed before.`
        : "No contacts had been emailed before.";
      renderHistoryReport(el("history-area"), result.history_report, headline);
    }
    if (result.stopped_early) {
      toast("Stopped early after repeated failures. Check the connection and try again.", true);
    } else {
      toast(`${result.created} draft(s) created in Gmail`);
    }
    await loadBatches();
  } catch (error) {
    status.textContent = error.message;
    status.className = "error";
    toast(error.message, true);
  } finally {
    button.disabled = false;
    updateCreateButton();
  }
}

// Batches

async function loadBatches() {
  const area = el("batch-area");
  try {
    const result = await api("/api/batches");
    area.textContent = "";
    if (!result.batches.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "No batches yet.";
      area.appendChild(empty);
      return;
    }
    result.batches.forEach((batch) => area.appendChild(renderBatch(batch)));
  } catch (error) {
    area.textContent = "";
    const message = document.createElement("div");
    message.className = "error";
    message.textContent = error.message;
    area.appendChild(message);
  }
}

function renderBatch(batch) {
  const card = document.createElement("div");
  card.className = "batch";

  const head = document.createElement("div");
  head.className = "batch-head";

  const title = document.createElement("div");
  title.className = "batch-title";
  title.textContent = batch.source_name || batch.batch_id;
  head.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "batch-meta";
  meta.textContent = `${batch.created_at}  |  ${batch.live_count} live  |  ${batch.deleted_count} deleted`;
  head.appendChild(meta);
  card.appendChild(head);

  const subject = document.createElement("div");
  subject.className = "batch-meta";
  subject.textContent = `Subject template: ${batch.subject_template}`;
  card.appendChild(subject);

  if (batch.attachment_names?.length) {
    const attached = document.createElement("div");
    attached.className = "batch-meta";
    attached.textContent = `Attachments: ${batch.attachment_names.join(", ")}`;
    card.appendChild(attached);
  }

  const live = batch.drafts.filter((draft) => !draft.deleted_at);
  const listWrap = document.createElement("div");
  listWrap.className = "batch-drafts";
  const checkboxes = [];

  batch.drafts.forEach((draft) => {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = draft.draft_id;
    box.disabled = Boolean(draft.deleted_at);
    const text = document.createElement("span");
    text.textContent = `${draft.to}: ${draft.subject}`;
    if (draft.deleted_at) {
      text.className = "struck";
      text.textContent += " (deleted)";
    }
    label.appendChild(box);
    label.appendChild(text);
    listWrap.appendChild(label);
    if (!draft.deleted_at) checkboxes.push(box);
  });
  card.appendChild(listWrap);

  if (batch.failures?.length) {
    const failures = document.createElement("div");
    failures.className = "error";
    failures.textContent = `${batch.failures.length} draft(s) failed: ${batch.failures[0].error}`;
    card.appendChild(failures);
  }

  const actions = document.createElement("div");
  actions.className = "row";

  const selectAll = document.createElement("button");
  selectAll.type = "button";
  selectAll.className = "btn btn-quiet btn-small";
  selectAll.textContent = "Select all live";
  selectAll.addEventListener("click", () => {
    const target = !checkboxes.every((box) => box.checked);
    checkboxes.forEach((box) => (box.checked = target));
  });
  actions.appendChild(selectAll);

  const deleteSelected = document.createElement("button");
  deleteSelected.type = "button";
  deleteSelected.className = "btn btn-small btn-danger";
  deleteSelected.textContent = "Delete selected";
  deleteSelected.disabled = live.length === 0;
  deleteSelected.addEventListener("click", async () => {
    const ids = checkboxes.filter((box) => box.checked).map((box) => box.value);
    if (!ids.length) {
      toast("No drafts selected", true);
      return;
    }
    await deleteFromBatch(batch.batch_id, ids, `Delete ${ids.length} selected draft(s)?`);
  });
  actions.appendChild(deleteSelected);

  const deleteAll = document.createElement("button");
  deleteAll.type = "button";
  deleteAll.className = "btn btn-small btn-danger";
  deleteAll.textContent = `Delete all ${live.length} live`;
  deleteAll.disabled = live.length === 0;
  deleteAll.addEventListener("click", async () => {
    await deleteFromBatch(batch.batch_id, null, `Delete all ${live.length} draft(s) from this batch?`);
  });
  actions.appendChild(deleteAll);

  const forget = document.createElement("button");
  forget.type = "button";
  forget.className = "btn btn-quiet btn-small";
  forget.textContent = "Remove record";
  forget.title = "Forget this batch here. Drafts still in Gmail are left alone.";
  forget.addEventListener("click", async () => {
    if (live.length && !confirm(`This batch still has ${live.length} live draft(s) in Gmail. Remove the record anyway?`)) {
      return;
    }
    try {
      await postJson(`/api/batches/${batch.batch_id}/forget`);
      toast("Batch record removed");
      await loadBatches();
    } catch (error) {
      toast(error.message, true);
    }
  });
  actions.appendChild(forget);

  card.appendChild(actions);
  return card;
}

async function deleteFromBatch(batchId, draftIds, confirmMessage) {
  if (!confirm(confirmMessage)) return;
  try {
    const body = draftIds ? { draft_ids: draftIds } : {};
    const result = await postJson(`/api/batches/${batchId}/delete-drafts`, body);
    toast(`${result.deleted} draft(s) deleted`);
    if (result.failures?.length) {
      toast(`${result.failures.length} could not be deleted: ${result.failures[0].error}`, true);
    }
    await loadBatches();
  } catch (error) {
    toast(error.message, true);
  }
}

// Wiring

function wire() {
  el("csv-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) uploadCsv(file);
    event.target.value = "";
  });

  el("attachment-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) uploadAttachment(file);
    event.target.value = "";
  });

  ["subject", "body", "signoff"].forEach((id) => {
    el(id).addEventListener("focus", (event) => {
      state.lastFocused = event.target;
    });
  });

  el("connect").addEventListener("click", async () => {
    toast("Opening the Google sign in window");
    try {
      const result = await postJson("/api/connect");
      toast(`Connected as ${result.email}`);
    } catch (error) {
      toast(error.message, true);
    }
    await refreshStatus();
  });

  el("disconnect").addEventListener("click", async () => {
    if (!confirm("Disconnect Gmail? Drafts already created stay in Gmail.")) return;
    await postJson("/api/disconnect");
    await refreshStatus();
    toast("Disconnected");
  });

  el("save-settings").addEventListener("click", async () => {
    try {
      await postJson("/api/settings", templatePayload());
      el("save-status").textContent = "Saved.";
      el("save-status").className = "ok";
    } catch (error) {
      el("save-status").textContent = error.message;
      el("save-status").className = "error";
    }
  });

  el("preview").addEventListener("click", runPreview);
  el("check-history").addEventListener("click", checkHistory);
  el("create").addEventListener("click", createDrafts);
  el("refresh-batches").addEventListener("click", loadBatches);
  el("suppression-add").addEventListener("click", addSuppression);
  el("suppression-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addSuppression();
  });
}

wire();
renderChips(defaultFields());
refreshStatus();
loadBatches();
loadSuppression();
