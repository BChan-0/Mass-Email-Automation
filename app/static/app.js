"use strict";

// Front end for the draft builder. Talks to the JSON endpoints in app/web.py.

const state = {
  csvId: null,
  filename: null,
  contactCount: 0,
  connected: false,
  lastFocused: null,
  // Row index to the field names that differ from the uploaded file.
  editedRows: {},
  statusLoaded: false,
  statusRows: [],
  statusFilter: "all",
  sheetRows: [],
  sheetChoices: { status: [], re_emailed: [], assignee: [] },
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
    use_markdown: el("use-markdown").checked,
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
    el("contacts-panel").classList.remove("hidden");
    await loadContacts();
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

// Views

const VIEWS = ["compose", "status", "sheet"];

function showView(name) {
  VIEWS.forEach((view) => {
    el(`pane-${view}`).classList.toggle("hidden", view !== name);
    el(`view-${view}`).classList.toggle("active", view === name);
  });
  if (name === "status" && !state.statusLoaded) loadStatus();
}

// Saved lists

async function loadLibrary() {
  try {
    const result = await api("/api/library");
    const list = el("library-list");
    list.textContent = "";
    el("library-summary").textContent = result.lists.length
      ? `${result.lists.length} saved`
      : "Nothing saved yet";

    result.lists.forEach((entry) => {
      const row = document.createElement("li");
      const label = document.createElement("span");
      const edited = entry.edited_count
        ? `, ${entry.edited_count} edited row(s)`
        : "";
      const dropped = entry.removed_count ? `, ${entry.removed_count} removed` : "";
      label.textContent = `${entry.name} (${entry.row_count} contacts${edited}${dropped}) last used ${entry.last_used_at.slice(0, 10)}`;
      row.appendChild(label);

      const actions = document.createElement("span");
      const load = document.createElement("button");
      load.type = "button";
      load.className = "btn btn-secondary btn-small";
      load.textContent = "Load";
      load.addEventListener("click", () => loadSavedList(entry.list_id));
      actions.appendChild(load);

      if (entry.edited_count || entry.removed_count) {
        const reset = document.createElement("button");
        reset.type = "button";
        reset.className = "btn btn-quiet btn-small";
        reset.textContent = "Forget edits";
        reset.addEventListener("click", async () => {
          if (!confirm(`Forget the edits saved for ${entry.name}? The original file is kept.`)) return;
          await postJson(`/api/library/${entry.list_id}/forget-edits`, {});
          toast("Edits forgotten");
          await loadLibrary();
        });
        actions.appendChild(reset);
      }

      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "btn btn-quiet btn-small btn-danger";
      drop.textContent = "Delete";
      drop.addEventListener("click", async () => {
        if (!confirm(`Forget ${entry.name} entirely?`)) return;
        await postJson(`/api/library/${entry.list_id}/delete`, {});
        toast("Saved list removed");
        await loadLibrary();
      });
      actions.appendChild(drop);

      row.appendChild(actions);
      list.appendChild(row);
    });
  } catch (error) {
    el("library-summary").textContent = error.message;
  }
}

async function loadSavedList(listId) {
  try {
    const result = await postJson(`/api/library/${listId}/load`, {});
    state.csvId = result.csv_id;
    state.filename = result.filename;
    state.contactCount = result.contact_count;
    state.editedRows = result.edited_rows || {};
    el("csv-summary").textContent = `${result.filename}: ${result.contact_count} contacts`;
    renderCsvDetail({ detected: result.detected, skipped: [] });
    renderChips(
      result.available_fields?.length
        ? result.available_fields.concat("sender_name")
        : defaultFields()
    );
    el("contacts-panel").classList.remove("hidden");
    await loadContacts();
    const edits = Object.keys(state.editedRows).length;
    toast(
      edits
        ? `Loaded ${result.contact_count} contacts, ${edits} edited row(s) highlighted`
        : `Loaded ${result.contact_count} contacts`
    );
    updateCreateButton();
  } catch (error) {
    toast(error.message, true);
  }
}

// Contact table

const EMAIL_SHAPE = /^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$/;

const contactState = { rows: [], fields: [], dropped: new Set() };

async function loadContacts() {
  if (!state.csvId) return;
  try {
    const result = await api(`/api/contacts?csv_id=${encodeURIComponent(state.csvId)}`);
    contactState.rows = result.contacts;
    contactState.fields = result.editable_fields;
    contactState.dropped = new Set();
    renderContactTable();
    el("contacts-status").textContent = `${result.contacts.length} row(s)`;
  } catch (error) {
    el("contacts-status").textContent = error.message;
  }
}

function renderContactTable() {
  const table = el("contacts-table");
  table.textContent = "";

  const head = table.createTHead().insertRow();
  ["CSV row", ...contactState.fields.map(fieldLabel), ""].forEach((title) => {
    const cell = document.createElement("th");
    cell.textContent = title;
    head.appendChild(cell);
  });

  const body = table.createTBody();
  contactState.rows.forEach((contact) => {
    const row = body.insertRow();
    row.dataset.index = String(contact.index);

    const number = row.insertCell();
    number.className = "row-number";
    number.textContent = String(contact.row_number);

    contactState.fields.forEach((field) => {
      const cell = row.insertCell();
      const input = document.createElement("input");
      input.type = "text";
      input.value = contact[field] || "";
      input.dataset.field = field;
      input.dataset.index = String(contact.index);
      // Mark cells that differ from the uploaded file, so a reloaded list shows
      // at a glance what was changed by hand.
      if ((state.editedRows[String(contact.index)] || []).includes(field)) {
        input.classList.add("edited");
        input.title = "Edited, differs from the uploaded file";
      }
      input.addEventListener("input", () => {
        if (field === "email") {
          input.classList.toggle("invalid", !EMAIL_SHAPE.test(input.value.trim()));
        }
      });
      cell.appendChild(input);
    });

    const actions = row.insertCell();
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-quiet btn-small";
    remove.textContent = contactState.dropped.has(contact.index) ? "Keep" : "Remove";
    remove.addEventListener("click", () => {
      if (contactState.dropped.has(contact.index)) {
        contactState.dropped.delete(contact.index);
      } else {
        contactState.dropped.add(contact.index);
      }
      row.classList.toggle("dropped", contactState.dropped.has(contact.index));
      remove.textContent = contactState.dropped.has(contact.index) ? "Keep" : "Remove";
    });
    actions.appendChild(remove);
  });
}

function fieldLabel(field) {
  return field.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}

async function saveContacts() {
  const inputs = Array.from(el("contacts-table").querySelectorAll("input[data-field]"));
  const byIndex = new Map();
  inputs.forEach((input) => {
    const index = Number(input.dataset.index);
    if (contactState.dropped.has(index)) return;
    if (!byIndex.has(index)) byIndex.set(index, { index });
    byIndex.get(index)[input.dataset.field] = input.value;
  });

  const invalid = inputs.filter((input) => input.classList.contains("invalid"));
  if (invalid.length) {
    toast(`${invalid.length} address(es) do not look valid. Fix them or remove the row.`, true);
    return;
  }

  try {
    const result = await postJson("/api/contacts", {
      csv_id: state.csvId,
      edits: Array.from(byIndex.values()),
      remove: Array.from(contactState.dropped),
    });
    state.contactCount = result.contact_count;
    el("csv-summary").textContent = `${state.filename}: ${result.contact_count} contacts`;
    const parts = [];
    if (result.applied) parts.push(`${result.applied} row(s) updated`);
    if (result.removed) parts.push(`${result.removed} removed`);
    el("contacts-status").textContent = parts.join(", ") || "No changes";
    if (result.rejected?.length) {
      toast(`${result.rejected.length} address(es) rejected: ${result.rejected[0].reason}`, true);
    } else {
      toast(parts.join(", ") || "No changes to apply");
    }
    await loadContacts();
    updateCreateButton();
  } catch (error) {
    toast(error.message, true);
  }
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

// Shows the formatted result and the Markdown source behind two tabs. The HTML is
// sanitized server side, in app/markup.py, before it reaches this point.
function buildBodyTabs(draft) {
  const wrap = document.createElement("div");

  const tabs = document.createElement("div");
  tabs.className = "tabs";
  const formatted = document.createElement("button");
  formatted.type = "button";
  formatted.className = "tab active";
  formatted.textContent = "Formatted";
  const source = document.createElement("button");
  source.type = "button";
  source.className = "tab";
  source.textContent = "Markdown source";
  tabs.appendChild(formatted);
  tabs.appendChild(source);
  wrap.appendChild(tabs);

  const htmlView = document.createElement("div");
  htmlView.className = "draft-html";
  htmlView.innerHTML = draft.html;
  wrap.appendChild(htmlView);

  const textView = document.createElement("pre");
  textView.className = "draft-body hidden";
  textView.textContent = draft.body;
  wrap.appendChild(textView);

  formatted.addEventListener("click", () => {
    formatted.classList.add("active");
    source.classList.remove("active");
    htmlView.classList.remove("hidden");
    textView.classList.add("hidden");
  });
  source.addEventListener("click", () => {
    source.classList.add("active");
    formatted.classList.remove("active");
    textView.classList.remove("hidden");
    htmlView.classList.add("hidden");
  });

  return wrap;
}

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

    if (result.markdown_unused) {
      const nudge = document.createElement("div");
      nudge.className = "warn";
      nudge.textContent =
        "This message looks like it uses Markdown, but Markdown is off, so the syntax will be sent literally.";
      area.appendChild(nudge);
    }

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

      if (draft.html) {
        card.appendChild(buildBodyTabs(draft));
      } else {
        const body = document.createElement("pre");
        body.className = "draft-body";
        body.textContent = draft.body;
        card.appendChild(body);
      }

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
    // The record is what remembers which addresses have a draft waiting, so
    // forgetting it while drafts are live can produce a duplicate later.
    const warning = live.length
      ? `This batch still has ${live.length} live draft(s) in Gmail. Removing the record also forgets ` +
        "that those people have a draft waiting, so a later run could draft to them again. " +
        "Delete the drafts instead if you want them gone. Remove the record anyway?"
      : "Remove this batch record?";
    if (!confirm(warning)) {
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

// Message status

// Each filter is a predicate over one status row.
const STATUS_FILTERS = {
  all: () => true,
  replied: (row) => row.replied,
  awaiting: (row) => row.status === "sent" && !row.replied,
  sent: (row) => row.status === "sent" || row.status === "replied",
  scheduled: (row) => row.status === "scheduled",
  draft: (row) => row.status === "draft",
  bounced: (row) => row.status === "bounced",
  deleted: (row) => row.status === "deleted",
  re_emailed: (row) => row.re_emailed === "Yes",
};

function renderStatusRows() {
  const area = el("status-area");
  const existing = area.querySelector(".table-wrap");
  if (existing) existing.remove();

  const search = el("status-search").value.trim().toLowerCase();
  const keep = STATUS_FILTERS[state.statusFilter] || STATUS_FILTERS.all;
  const rows = state.statusRows.filter(
    (row) =>
      keep(row) &&
      (!search ||
        row.email.toLowerCase().includes(search) ||
        (row.subject || "").toLowerCase().includes(search))
  );

  const wrap = document.createElement("div");
  wrap.className = "table-wrap";

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = state.statusRows.length
      ? "No rows match that filter."
      : "Nothing tracked yet.";
    wrap.appendChild(empty);
    area.appendChild(wrap);
    return;
  }

  const table = document.createElement("table");
  table.className = "grid";
  const head = table.createTHead().insertRow();
  ["Status", "Replied", "Address", "Subject", "Date", "Re-emailed"].forEach((title) => {
    const cell = document.createElement("th");
    cell.textContent = title;
    head.appendChild(cell);
  });

  const body = table.createTBody();
  rows.forEach((row) => {
    const line = body.insertRow();
    const pill = document.createElement("span");
    pill.className = `status-pill status-${row.status}`;
    pill.textContent = row.label;
    line.insertCell().appendChild(pill);
    [
      row.replied ? "Yes" : row.bounced ? "bounced" : "",
      row.email,
      row.subject,
      row.when,
      row.re_emailed,
    ].forEach((value) => {
      line.insertCell().textContent = value || "";
    });
  });
  wrap.appendChild(table);

  const shown = document.createElement("div");
  shown.className = "hint";
  shown.textContent = `${rows.length} of ${state.statusRows.length} shown`;
  wrap.appendChild(shown);
  area.appendChild(wrap);
}

async function loadStatus() {
  const area = el("status-area");
  area.textContent = "Reading Gmail, this takes a moment on a large mailbox";
  const checkSent = el("status-check-sent").checked ? "1" : "0";
  try {
    const result = await api(`/api/message-status?check_sent=${checkSent}`);
    state.statusLoaded = true;

    const counts = el("status-counts");
    counts.textContent = "";
    ["replied", "bounced", "sent", "scheduled", "draft", "deleted"].forEach((key) => {
      if (!result.counts[key]) return;
      const chip = document.createElement("span");
      chip.className = "count-chip";
      chip.textContent = `${result.counts[key]} ${key}`;
      counts.appendChild(chip);
    });

    area.textContent = "";
    if (result.errors?.length) {
      const warn = document.createElement("div");
      warn.className = "error";
      warn.textContent = result.errors.join("; ");
      area.appendChild(warn);
    }
    if (!el("status-check-sent").checked) {
      const note = document.createElement("div");
      note.className = "warn";
      note.textContent =
        "Sent mail was not checked, so a message that was sent shows as deleted.";
      area.appendChild(note);
    }

    state.statusRows = result.rows;
    renderStatusRows();
  } catch (error) {
    area.textContent = "";
    const message = document.createElement("div");
    message.className = "error";
    message.textContent = error.message;
    area.appendChild(message);
  }
}

// Tracking sheet

// Columns the user can type into. The rest come from the CSV and the mailbox.
const SHEET_EDITABLE = ["client", "status", "linkedin", "re_emailed", "assignee", "notes"];

async function buildSheet() {
  if (!state.csvId) {
    toast("Load a CSV first, on the Compose tab", true);
    return;
  }
  el("sheet-status").textContent = "Building rows from Gmail";
  try {
    const result = await postJson("/api/tracker", {
      csv_id: state.csvId,
      default_assignee: el("sheet-assignee").value,
    });
    state.sheetRows = result.rows;
    state.sheetColumns = result.columns;
    state.sheetChoices = {
      status: result.status_choices || [],
      re_emailed: result.re_emailed_choices || [],
      assignee: result.assignee_choices || [],
    };
    // The default assignee offers the same names, so a typo cannot produce a value the
    // sheet will not render as a chip.
    const options = el("assignee-options");
    options.textContent = "";
    state.sheetChoices.assignee.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      options.appendChild(option);
    });
    renderSheet(result.columns, result.rows);
    el("sheet-status").textContent = `${result.rows.length} row(s)`;
    el("sheet-status").className = "hint";
    if (result.errors?.length) {
      toast(result.errors[0], true);
    }
  } catch (error) {
    el("sheet-status").textContent = error.message;
    el("sheet-status").className = "error";
  }
}

// Field order has to match the column order the server sends.
const SHEET_ORDER = [
  "client",
  "status",
  "name",
  "title",
  "email",
  "linkedin",
  "re_emailed",
  "assignee",
  "notes",
  "last_contact",
];

// Columns the sheet defines as dropdowns, so a typed value cannot fall outside them.
const SHEET_DROPDOWNS = { status: "status", re_emailed: "re_emailed", assignee: "assignee" };

function columnLetter(index) {
  let letter = "";
  let value = index;
  while (value >= 0) {
    letter = String.fromCharCode(65 + (value % 26)) + letter;
    value = Math.floor(value / 26) - 1;
  }
  return letter;
}

// Draws the rows the way a spreadsheet does, with column letters across the top and
// numbered rows down the side, so this page and the pasted result look the same.
function renderSheet(columns, rows) {
  const table = el("sheet-table");
  table.textContent = "";
  const firstRow = Math.max(1, parseInt(el("sheet-first-row").value, 10) || 2);

  const letters = table.createTHead().insertRow();
  const corner = document.createElement("th");
  corner.className = "colhead corner";
  letters.appendChild(corner);
  columns.forEach((_name, index) => {
    const cell = document.createElement("th");
    cell.className = "colhead";
    cell.textContent = columnLetter(index);
    letters.appendChild(cell);
  });

  const body = table.createTBody();

  // The sheet's own header row, shown so the column names line up with the letters.
  const names = body.insertRow();
  names.className = "namerow";
  const nameGutter = names.insertCell();
  nameGutter.className = "rowhead";
  nameGutter.textContent = "1";
  columns.forEach((name) => {
    names.insertCell().textContent = name;
  });

  rows.forEach((row, index) => {
    const line = body.insertRow();
    const gutter = line.insertCell();
    gutter.className = "rowhead";
    gutter.textContent = String(firstRow + index);

    SHEET_ORDER.forEach((field) => {
      const cell = line.insertCell();
      cell.className = "cell";
      const value = row[field] || "";

      if (!SHEET_EDITABLE.includes(field)) {
        const text = document.createElement("span");
        text.textContent = value;
        if (!value) cell.classList.add("blank");
        cell.appendChild(text);
        return;
      }

      const choices = state.sheetChoices[SHEET_DROPDOWNS[field]];
      const control =
        choices && choices.length
          ? buildSheetSelect(choices, value)
          : Object.assign(document.createElement("input"), { type: "text", value });
      control.dataset.field = field;
      control.dataset.row = String(index);
      control.addEventListener("change", () => {
        state.sheetRows[index][field] = control.value;
        el("sheet-tsv").value = sheetToTsv(columns, SHEET_ORDER);
      });
      control.addEventListener("input", () => {
        state.sheetRows[index][field] = control.value;
        el("sheet-tsv").value = sheetToTsv(columns, SHEET_ORDER);
      });
      cell.appendChild(control);
    });
  });
  el("sheet-tsv").value = sheetToTsv(columns, SHEET_ORDER);
}

function buildSheetSelect(choices, value) {
  const select = document.createElement("select");
  // A blank option matters because an empty cell is a valid state in the sheet.
  const options = choices.includes(value) || !value ? ["", ...choices] : ["", value, ...choices];
  options.forEach((choice) => {
    const option = document.createElement("option");
    option.value = choice;
    option.textContent = choice;
    if (choice === value) option.selected = true;
    select.appendChild(option);
  });
  return select;
}

// Data rows only, with no header. These get appended below rows that already exist,
// so a header line would land in the middle of the sheet.
function sheetToTsv(_columns, order) {
  const clean = (value) => String(value || "").replace(/[\t\r\n]+/g, " ").trim();
  return state.sheetRows.map((row) => order.map((field) => clean(row[field])).join("\t")).join("\n");
}

async function saveSheetFields() {
  if (!state.sheetRows.length) {
    toast("Build the rows first", true);
    return;
  }
  try {
    const result = await postJson("/api/tracker/save", {
      entries: state.sheetRows.map((row) => ({
        email: row.email,
        client: row.client,
        assignee: row.assignee,
        notes: row.notes,
        re_emailed: row.re_emailed,
        linkedin: row.linkedin,
        status_override: row.status,
      })),
    });
    toast(`Remembered values for ${result.saved} contact(s)`);
  } catch (error) {
    toast(error.message, true);
  }
}

// Shows what the app has saved per contact, so a stored value is never a surprise.
async function loadRemembered() {
  const area = el("remembered-area");
  try {
    const result = await api("/api/tracker/saved");
    area.textContent = "";
    el("remembered-summary").textContent = result.count
      ? `${result.count} contact(s) have saved values in ${result.path}`
      : "Nothing saved yet";

    if (!result.count) {
      area.classList.remove("hidden");
      return;
    }

    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    ["Address", "Saved values", ""].forEach((title) => {
      const cell = document.createElement("th");
      cell.textContent = title;
      cell.style.textAlign = "left";
      head.appendChild(cell);
    });
    const body = table.createTBody();
    result.entries.forEach((entry) => {
      const row = body.insertRow();
      row.insertCell().textContent = entry.email;
      const summary = Object.entries(entry)
        .filter(([key, value]) => key !== "email" && value)
        .map(([key, value]) => `${key}: ${value}`)
        .join("; ");
      row.insertCell().textContent = summary || "(empty)";
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "btn btn-quiet btn-small btn-danger";
      clear.textContent = "Forget";
      clear.addEventListener("click", async () => {
        await postJson("/api/tracker/forget", { emails: [entry.email] });
        toast(`Forgot saved values for ${entry.email}`);
        await loadRemembered();
      });
      row.insertCell().appendChild(clear);
    });
    area.appendChild(table);

    const clearAll = document.createElement("button");
    clearAll.type = "button";
    clearAll.className = "btn btn-small btn-danger";
    clearAll.textContent = `Forget all ${result.count}`;
    clearAll.addEventListener("click", async () => {
      if (!confirm(`Forget saved values for all ${result.count} contact(s)?`)) return;
      await postJson("/api/tracker/forget", { all: true });
      toast("All saved values forgotten");
      await loadRemembered();
    });
    area.appendChild(clearAll);
    area.classList.remove("hidden");
  } catch (error) {
    el("remembered-summary").textContent = error.message;
  }
}

async function copySheet() {
  const text = el("sheet-tsv").value;
  if (!text.trim()) {
    toast("Build the rows first", true);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    const first = el("sheet-first-row").value.trim() || "the next empty row";
    toast(`Copied ${state.sheetRows.length} row(s). In Sheets, click cell A${first} and paste.`);
  } catch (error) {
    // Clipboard access needs a secure context, which plain http may not be.
    el("sheet-tsv").select();
    toast("Could not copy automatically. The text is selected, press Cmd C.", true);
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

  el("toggle-contacts").addEventListener("click", () => {
    const wrap = el("contacts-table-wrap");
    const hidden = wrap.classList.toggle("hidden");
    el("toggle-contacts").textContent = hidden ? "Show contact table" : "Hide contact table";
  });
  el("save-contacts").addEventListener("click", saveContacts);
  el("reload-contacts").addEventListener("click", async () => {
    await loadContacts();
    toast("Reloaded from the uploaded file");
  });

  el("preview").addEventListener("click", runPreview);
  el("check-history").addEventListener("click", checkHistory);
  el("create").addEventListener("click", createDrafts);
  el("refresh-batches").addEventListener("click", loadBatches);
  el("suppression-add").addEventListener("click", addSuppression);
  el("suppression-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addSuppression();
  });

  VIEWS.forEach((view) => {
    el(`view-${view}`).addEventListener("click", () => showView(view));
  });
  el("toggle-library").addEventListener("click", () => {
    el("library-list").classList.toggle("hidden");
  });
  el("refresh-status").addEventListener("click", loadStatus);
  document.querySelectorAll(".chip.filter").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".chip.filter").forEach((other) => other.classList.remove("active"));
      button.classList.add("active");
      state.statusFilter = button.dataset.filter;
      renderStatusRows();
    });
  });
  el("status-search").addEventListener("input", renderStatusRows);
  el("sheet-first-row").addEventListener("input", () => {
    if (state.sheetRows.length) renderSheet(state.sheetColumns, state.sheetRows);
  });

  el("build-sheet").addEventListener("click", buildSheet);
  el("save-sheet").addEventListener("click", saveSheetFields);
  el("copy-sheet").addEventListener("click", copySheet);
  el("show-remembered").addEventListener("click", () => {
    const area = el("remembered-area");
    if (area.classList.contains("hidden")) {
      loadRemembered();
    } else {
      area.classList.add("hidden");
    }
  });
}

wire();
renderChips(defaultFields());
refreshStatus();
loadBatches();
loadSuppression();
loadLibrary();
