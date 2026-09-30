const state = {
  emails: new Map(),
  orderedIds: [],
  connected: false,
  firstPage: true,
  nextPageToken: null,
  hasMore: true,
  loadingEmails: false,
  activeFilter: "All",
  search: "",
  batchSize: 5,
  debounceMs: 450,
  classificationQueue: new Set(),
  retryIds: new Set(),
  classificationActive: false,
  classificationTimer: null,
  detailId: null,
};

const elements = {
  list: document.querySelector("#emailList"),
  sentinel: document.querySelector("#loadSentinel"),
  listStatus: document.querySelector("#listStatus"),
  listStatusTitle: document.querySelector("#listStatusTitle"),
  listStatusCopy: document.querySelector("#listStatusCopy"),
  retryLoad: document.querySelector("#retryLoad"),
  autoToggle: document.querySelector("#autoToggle"),
  searchInput: document.querySelector("#searchInput"),
  emptyState: document.querySelector("#emptyState"),
  connectState: document.querySelector("#connectState"),
  dialog: document.querySelector("#emailDialog"),
  toast: document.querySelector("#toast"),
};

let classificationObserver;
let paginationObserver;
let toastTimer;

function escapeHTML(value = "") {
  return String(value).replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[character]);
}

function cssClass(value = "") {
  return String(value).toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

function senderParts(raw = "") {
  const match = raw.match(/^(.*?)\s*<([^>]+)>$/);
  if (!match) return { name: raw || "Unknown sender", address: raw };
  return { name: match[1].replace(/^"|"$/g, "").trim() || match[2], address: match[2] };
}

function initials(name = "") {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]).join("").toUpperCase() || "EM";
}

function toneFor(id = "") {
  const tones = ["", "amber", "blue", "red"];
  const number = [...id].reduce((sum, letter) => sum + letter.charCodeAt(0), 0);
  return tones[number % tones.length];
}

function formatDate(value) {
  if (!value) return "Unknown date";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatNumber(value) {
  return new Intl.NumberFormat().format(Math.round(value || 0));
}

function formatCost(value) {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "$0.00";
  return value < .01 ? `$${value.toFixed(6)}` : `$${value.toFixed(4)}`;
}

function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return "—";
  return milliseconds < 1000
    ? `${Math.round(milliseconds)} ms`
    : `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 2 : 1)} s`;
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let data = null;
  try { data = await response.json(); } catch (_) { /* response had no JSON */ }
  if (!response.ok) {
    const error = new Error(data?.detail?.message || data?.detail || `Request failed (${response.status})`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function confidenceLabel(details) {
  return typeof details?.confidence === "number" ? `${Math.round(details.confidence * 100)}%` : null;
}

function classificationMarkup(email) {
  const status = email.classification_status;
  const result = email.classification;
  if (status === "failed") {
    return `<span class="failed-label">Classification failed</span><button class="retry-classification" data-id="${escapeHTML(email.id)}">Retry</button>`;
  }
  if (status === "classifying") {
    return '<span class="thinking">Analyzing <i></i><i></i><i></i></span>';
  }
  if (status !== "classified" || !result) {
    return '<span class="pending-label"><i></i> Analyzing…</span>';
  }

  const categoryConfidence = confidenceLabel(result.category_details);
  const priorityConfidence = confidenceLabel(result.priority_details);
  return `
    <span class="tag ${cssClass(result.category)}" title="${categoryConfidence ? `Confidence ${categoryConfidence}` : "Confidence unavailable"}">${escapeHTML(result.category)}</span>
    <span class="tag ${cssClass(result.priority)}" title="${priorityConfidence ? `Confidence ${priorityConfidence}` : "Confidence unavailable"}">${escapeHTML(result.priority)}</span>
    <span class="urgency-chip">U ${Number(result.urgency).toFixed(1)}</span>
    <span class="timing-chip" title="JEV batch processing time">◷ ${formatDuration(result.classification_latency_ms)}</span>
    <button class="classification-info" data-id="${escapeHTML(email.id)}" aria-label="Show classification details">Details</button>`;
}

function inspectorMarkup(email) {
  const result = email.classification;
  if (!result) return "";
  const value = details => typeof details?.confidence === "number" ? `${Math.round(details.confidence * 100)}% confidence` : "Confidence unavailable";
  const probability = number => typeof number === "number" ? `${Math.round(number * 100)}% probability` : "Unavailable";
  return `<div class="classification-inspector" id="inspect-${escapeHTML(email.id)}" hidden>
    <div><span>Category</span><strong>${escapeHTML(result.category)}</strong><small>${value(result.category_details)}</small></div>
    <div><span>Priority</span><strong>${escapeHTML(result.priority)}</strong><small>${value(result.priority_details)}</small></div>
    <div><span>Urgency</span><strong>${Number(result.urgency).toFixed(1)}</strong><small>${value(result.urgency_details)}</small></div>
    <div><span>Requires action</span><strong>${probability(result.requires_action)}</strong><small>Noul yes probability</small></div>
    <div><span>Has deadline</span><strong>${probability(result.has_deadline)}</strong><small>Noul yes probability</small></div>
    <div><span>Processing time</span><strong>${formatDuration(result.classification_latency_ms)}</strong><small>Actual JEV batch latency</small></div>
  </div>`;
}

function emailRowMarkup(email) {
  const sender = senderParts(email.sender);
  const labels = (email.labels || []).slice(0, 2).map(label => `<span>${escapeHTML(label)}</span>`).join("");
  return `<article class="email-card" data-id="${escapeHTML(email.id)}" data-state="${email.classification_status}" tabindex="0" role="button" aria-label="Open ${escapeHTML(email.subject || "email")}">
    <span class="sender-avatar ${toneFor(email.id)}">${escapeHTML(initials(sender.name))}</span>
    <div class="sender"><strong>${escapeHTML(sender.name)}</strong><span>${escapeHTML(sender.address)}</span></div>
    <div class="subject"><strong>${escapeHTML(email.subject || "(No subject)")}</strong><p>${escapeHTML(email.snippet || "")}</p><div class="gmail-labels">${labels}</div></div>
    <div class="classification">${classificationMarkup(email)}</div>
    <span class="time">${escapeHTML(formatDate(email.timestamp))}</span>
    ${inspectorMarkup(email)}
  </article>`;
}

function appendEmails(emails) {
  const fragment = document.createDocumentFragment();
  emails.forEach(email => {
    if (state.emails.has(email.id)) {
      state.emails.set(email.id, email);
      updateEmailRow(email);
      return;
    }
    state.emails.set(email.id, email);
    state.orderedIds.push(email.id);
    const template = document.createElement("template");
    template.innerHTML = emailRowMarkup(email);
    const row = template.content.firstElementChild;
    fragment.appendChild(row);
  });
  elements.list.appendChild(fragment);
  elements.list.querySelectorAll('.email-card[data-state="pending"]').forEach(row => classificationObserver.observe(row));
  applyFilters();
  updateCounts();
}

function updateEmailRow(email) {
  const row = elements.list.querySelector(`.email-card[data-id="${CSS.escape(email.id)}"]`);
  if (!row) return;
  row.dataset.state = email.classification_status;
  row.querySelector(".classification").innerHTML = classificationMarkup(email);
  const oldInspector = row.querySelector(".classification-inspector");
  if (oldInspector) oldInspector.remove();
  if (email.classification) row.insertAdjacentHTML("beforeend", inspectorMarkup(email));
  if (email.classification_status === "pending") classificationObserver.observe(row);
  else classificationObserver.unobserve(row);
}

function setListStatus(mode, title, copy = "") {
  elements.listStatus.dataset.mode = mode;
  elements.listStatusTitle.textContent = title;
  elements.listStatusCopy.textContent = copy;
  elements.retryLoad.hidden = mode !== "error";
  elements.listStatus.hidden = mode === "hidden";
}

async function loadNextPage() {
  if (!state.connected || state.loadingEmails || !state.hasMore) return false;
  state.loadingEmails = true;
  setListStatus("loading", state.firstPage ? "Loading emails…" : "Loading more emails…", "Fetching Gmail metadata only");
  const params = new URLSearchParams({ max_results: "20" });
  if (!state.firstPage && state.nextPageToken) params.set("page_token", state.nextPageToken);

  try {
    const data = await apiFetch(`/emails?${params}`);
    appendEmails(data.emails || []);
    state.nextPageToken = data.next_page_token;
    state.hasMore = Boolean(data.has_more && data.next_page_token);
    state.firstPage = false;
    if (state.hasMore) setListStatus("ready", "Scroll for more", "The next Gmail page loads automatically");
    else setListStatus("end", "No more emails", `${state.emails.size} emails loaded in this session`);
    return true;
  } catch (error) {
    if (error.status === 401) showConnectState();
    else setListStatus("error", "Couldn’t load Gmail", "Check your connection and retry.");
    return false;
  } finally {
    state.loadingEmails = false;
    updateCounts();
  }
}

function enqueueClassification(emailId, retry = false) {
  const email = state.emails.get(emailId);
  if (!email || email.classification_status === "classified" || email.classification_status === "classifying") return;
  if (email.classification_status === "failed" && !retry) return;
  state.classificationQueue.add(emailId);
  if (retry) state.retryIds.add(emailId);
  updateCounts();
  scheduleClassification(state.classificationQueue.size >= state.batchSize ? 0 : state.debounceMs);
}

function scheduleClassification(delay) {
  window.clearTimeout(state.classificationTimer);
  state.classificationTimer = window.setTimeout(flushClassificationQueue, delay);
}

async function flushClassificationQueue() {
  if (state.classificationActive || !state.classificationQueue.size) return;
  const ids = [...state.classificationQueue].slice(0, state.batchSize);
  ids.forEach(id => state.classificationQueue.delete(id));
  const retryFailed = ids.some(id => state.retryIds.has(id));
  ids.forEach(id => {
    state.retryIds.delete(id);
    const email = state.emails.get(id);
    if (email) {
      email.classification_status = "classifying";
      email.classification_error = null;
      updateEmailRow(email);
    }
  });
  state.classificationActive = true;
  updateCounts();

  try {
    const data = await apiFetch("/emails/classify", {
      method: "POST",
      body: JSON.stringify({ email_ids: ids, retry_failed: retryFailed }),
    });
    (data.emails || []).forEach(updated => {
      state.emails.set(updated.id, updated);
      updateEmailRow(updated);
    });
    showToast(
      "Batch classified",
      `${ids.length} email${ids.length === 1 ? "" : "s"} processed in ${formatDuration(data.usage?.latency_ms)}.`,
    );
    await refreshUsage();
  } catch (error) {
    ids.forEach(id => {
      const email = state.emails.get(id);
      if (email) {
        email.classification_status = "failed";
        email.classification_error = "Classification failed";
        updateEmailRow(email);
      }
    });
    showToast("Classification failed", "The emails were kept. Use Retry to run this batch again.", true);
    await refreshUsage().catch(() => {});
  } finally {
    state.classificationActive = false;
    updateCounts();
    if (state.classificationQueue.size) {
      scheduleClassification(state.classificationQueue.size >= state.batchSize ? 0 : state.debounceMs);
    }
  }
}

async function refreshUsage() {
  const usage = await apiFetch("/usage");
  state.batchSize = usage.batch_size || 5;
  state.debounceMs = usage.batch_debounce_ms || 450;
  document.querySelector("#classifiedStat").textContent = formatNumber(usage.emails_classified);
  document.querySelector("#completeCount").textContent = formatNumber(usage.emails_classified);
  document.querySelector("#requestStat").textContent = formatNumber(usage.jev_requests);
  document.querySelector("#sideRequests").textContent = formatNumber(usage.jev_requests);
  document.querySelector("#sideTokens").textContent = `${formatNumber(usage.total_tokens)} tokens used this session`;
  document.querySelector("#usageTokensSummary").textContent = formatNumber(usage.total_tokens);
  document.querySelector("#usageRequestsSummary").textContent = formatNumber(usage.jev_requests);
  document.querySelector("#inputTokens").textContent = formatNumber(usage.input_tokens);
  document.querySelector("#outputTokens").textContent = formatNumber(usage.output_tokens);
  document.querySelector("#avgTokens").textContent = formatNumber(usage.average_tokens_per_email);
  document.querySelector("#avgBatch").textContent = Number(usage.average_batch_size || 0).toFixed(1);
  const totalClassificationTime = formatDuration(usage.total_classification_time_ms || 0);
  document.querySelector("#usageTimeSummary").textContent = totalClassificationTime;
  document.querySelector("#totalClassificationTime").textContent = totalClassificationTime;

  const totalCost = formatCost(usage.total_cost);
  const averageCost = formatCost(usage.average_cost_per_email);
  document.querySelector("#costStat").textContent = totalCost;
  document.querySelector("#usageCostSummary").textContent = totalCost;
  document.querySelector("#avgCost").textContent = averageCost;
  document.querySelector("#costMeta").textContent = usage.pricing_configured ? "Configured token pricing" : "Pricing not configured";
  document.querySelector("#batchMeta").textContent = `Up to ${state.batchSize} emails per call`;
  document.querySelector("#usageTrack").style.width = `${Math.min(100, usage.jev_requests * 5)}%`;

  const batch = usage.current_batch;
  if (batch) {
    document.querySelector("#currentBatchTitle").textContent = `${batch.email_count} emails · ${batch.success ? "Success" : "Failed"}`;
    document.querySelector("#currentBatchMeta").textContent = `${formatNumber(batch.input_tokens)} in / ${formatNumber(batch.output_tokens)} out · ${formatCost(batch.total_cost)} · ${Math.round(batch.latency_ms)} ms`;
  }
}

function updateCounts() {
  document.querySelector("#loadedStat").textContent = formatNumber(state.emails.size);
  document.querySelector("#queueCount").textContent = formatNumber(state.emails.size);
  document.querySelector("#sidebarCount").textContent = formatNumber(state.emails.size);
  document.querySelector("#pendingCount").textContent = formatNumber(state.classificationQueue.size);
  document.querySelector("#loadMeta").textContent = state.hasMore ? "More available on scroll" : "Mailbox page end reached";
  updateScreenCount();
}

function visibleRows() {
  return [...elements.list.querySelectorAll(".email-card:not(.hidden)")].filter(row => {
    const rect = row.getBoundingClientRect();
    return rect.top < window.innerHeight && rect.bottom > 0;
  });
}

function updateScreenCount() {
  document.querySelector("#screenCount").textContent = visibleRows().length;
}

function applyFilters() {
  const query = state.search.toLowerCase();
  let count = 0;
  state.orderedIds.forEach(id => {
    const email = state.emails.get(id);
    const row = elements.list.querySelector(`.email-card[data-id="${CSS.escape(id)}"]`);
    if (!email || !row) return;
    const result = email.classification;
    const haystack = `${email.sender} ${email.subject} ${email.snippet}`.toLowerCase();
    const textMatch = !query || haystack.includes(query);
    const filterMatch = state.activeFilter === "All"
      || (state.activeFilter === "Action" && Number(result?.requires_action) >= .5)
      || (state.activeFilter === "Important" && ["Important", "Critical"].includes(result?.priority))
      || (state.activeFilter === "Failed" && email.classification_status === "failed");
    const show = textMatch && filterMatch;
    row.classList.toggle("hidden", !show);
    if (show) count += 1;
  });
  elements.emptyState.hidden = count !== 0 || state.emails.size === 0;
  updateScreenCount();
}

function showConnectState() {
  state.connected = false;
  elements.connectState.hidden = false;
  elements.listStatus.hidden = true;
  document.querySelector("#connectionLabel").textContent = "Gmail not connected";
  document.querySelector("#liveStatus").classList.add("offline");
  document.querySelector("#liveStatus span").textContent = "Gmail not connected";
}

function showToast(title, copy, error = false) {
  window.clearTimeout(toastTimer);
  document.querySelector("#toastTitle").textContent = title;
  document.querySelector("#toastCopy").textContent = copy;
  elements.toast.classList.toggle("error", error);
  elements.toast.classList.add("show");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("show"), 3200);
}

function sanitizeEmailHtml(html, inlineContent = []) {
  const documentNode = new DOMParser().parseFromString(html, "text/html");
  documentNode.querySelectorAll("script, iframe, object, embed, form, meta, base, link").forEach(node => node.remove());
  const inlineMap = new Map(inlineContent.map(item => [item.content_id, item.data_url]));
  documentNode.querySelectorAll("*").forEach(node => {
    [...node.attributes].forEach(attribute => {
      if (attribute.name.toLowerCase().startsWith("on") || attribute.name.toLowerCase() === "srcset") node.removeAttribute(attribute.name);
    });
  });
  documentNode.querySelectorAll("img").forEach(image => {
    const source = image.getAttribute("src") || "";
    if (source.startsWith("cid:") && inlineMap.has(source.slice(4))) image.setAttribute("src", inlineMap.get(source.slice(4)));
    else if (!source.startsWith("data:")) image.removeAttribute("src");
  });
  documentNode.querySelectorAll("a").forEach(link => {
    link.setAttribute("target", "_blank");
    link.setAttribute("rel", "noreferrer noopener");
  });
  return documentNode.body.innerHTML;
}

async function openEmail(emailId) {
  state.detailId = emailId;
  const email = state.emails.get(emailId);
  document.querySelector("#detailHeaderTitle").textContent = email?.subject || "Message";
  document.querySelector("#detailLoading").hidden = false;
  document.querySelector("#detailError").hidden = true;
  document.querySelector("#emailDetail").hidden = true;
  if (!elements.dialog.open) elements.dialog.showModal();

  try {
    const detail = await apiFetch(`/emails/${encodeURIComponent(emailId)}`);
    renderEmailDetail(detail);
  } catch (error) {
    document.querySelector("#detailLoading").hidden = true;
    document.querySelector("#detailError").hidden = false;
    document.querySelector("#detailErrorCopy").textContent = error.message || "Please try again.";
  }
}

function renderEmailDetail(detail) {
  const sender = senderParts(detail.sender);
  document.querySelector("#detailLoading").hidden = true;
  document.querySelector("#detailError").hidden = true;
  document.querySelector("#emailDetail").hidden = false;
  document.querySelector("#detailSubject").textContent = detail.subject || "(No subject)";
  document.querySelector("#detailTimestamp").textContent = detail.timestamp || "";
  document.querySelector("#detailSender").textContent = `${sender.name} <${sender.address}>`;
  document.querySelector("#detailAvatar").textContent = initials(sender.name);
  document.querySelector("#detailLabels").innerHTML = (detail.labels || []).map(label => `<span>${escapeHTML(label)}</span>`).join("");

  const recipients = document.querySelector("#recipientDetails");
  recipients.replaceChildren();
  [["From", detail.sender], ["To", detail.recipient], ["Cc", detail.cc], ["Bcc", detail.bcc], ["Date", detail.timestamp]].forEach(([label, value]) => {
    if (!value) return;
    const row = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = label;
    row.append(strong, document.createTextNode(value));
    recipients.appendChild(row);
  });

  const body = document.querySelector("#messageBody");
  body.replaceChildren();
  if (detail.body_html) {
    const frame = document.createElement("iframe");
    frame.className = "email-body-frame";
    // Same-origin is allowed only so the parent can size the srcdoc frame.
    // Scripts, forms, popups and top navigation remain sandbox-blocked.
    frame.setAttribute("sandbox", "allow-same-origin");
    const safeBody = sanitizeEmailHtml(detail.body_html, detail.inline_content || []);
    frame.srcdoc = `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'"><style>body{font:14px/1.65 Arial,sans-serif;color:#27322c;margin:0;overflow-wrap:anywhere}img{max-width:100%;height:auto}a{color:#246b4a}</style></head><body>${safeBody}</body></html>`;
    frame.addEventListener("load", () => {
      try { frame.style.height = `${Math.max(320, frame.contentDocument.body.scrollHeight + 24)}px`; } catch (_) { /* sandbox may block measurement */ }
    });
    body.appendChild(frame);
  } else {
    const pre = document.createElement("pre");
    pre.textContent = detail.body_text || "This message has no readable text body.";
    body.appendChild(pre);
  }

  const attachments = detail.attachments || [];
  const attachmentSection = document.querySelector("#attachments");
  attachmentSection.hidden = attachments.length === 0;
  document.querySelector("#attachmentList").innerHTML = attachments.map(item => `<div class="attachment-card"><span>▱</span><div><strong>${escapeHTML(item.filename)}</strong><small>${escapeHTML(item.mime_type)} · ${formatNumber(item.size)} bytes</small></div></div>`).join("");
}

function initializeObservers() {
  classificationObserver = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting && entry.intersectionRatio >= .35 && elements.autoToggle.checked) {
        enqueueClassification(entry.target.dataset.id);
      }
    });
    updateScreenCount();
  }, { threshold: [.15, .35, .75] });

  paginationObserver = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) loadNextPage();
  }, { rootMargin: "800px 0px" });
  paginationObserver.observe(elements.sentinel);
}

function setClassificationMode(mode, classifyVisible = true) {
  const scrollMode = mode === "scroll";
  elements.autoToggle.checked = scrollMode;
  document.querySelectorAll(".mode-option").forEach(button => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
  document.querySelector("#bulkClassify").hidden = scrollMode;
  localStorage.setItem("jevClassificationMode", mode);

  window.clearTimeout(state.classificationTimer);
  if (!scrollMode) {
    state.classificationQueue.clear();
    state.retryIds.clear();
    updateCounts();
  } else if (classifyVisible) {
    visibleRows().forEach(row => enqueueClassification(row.dataset.id));
  }
}

elements.list.addEventListener("click", event => {
  const retry = event.target.closest(".retry-classification");
  if (retry) {
    event.stopPropagation();
    const email = state.emails.get(retry.dataset.id);
    if (email) {
      email.classification_status = "pending";
      updateEmailRow(email);
      enqueueClassification(email.id, true);
    }
    return;
  }
  const info = event.target.closest(".classification-info");
  if (info) {
    event.stopPropagation();
    const inspector = document.querySelector(`#inspect-${CSS.escape(info.dataset.id)}`);
    if (inspector) inspector.hidden = !inspector.hidden;
    return;
  }
  const row = event.target.closest(".email-card");
  if (row) openEmail(row.dataset.id);
});

elements.list.addEventListener("keydown", event => {
  if ((event.key === "Enter" || event.key === " ") && event.target.classList.contains("email-card")) {
    event.preventDefault();
    openEmail(event.target.dataset.id);
  }
});

document.querySelectorAll(".filter").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".filter").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  state.activeFilter = button.dataset.filter;
  applyFilters();
}));

elements.searchInput.addEventListener("input", () => { state.search = elements.searchInput.value.trim(); applyFilters(); });
document.querySelectorAll(".mode-option").forEach(button => button.addEventListener("click", () => {
  if (state.classificationActive) {
    showToast("Batch still running", "The mode will be available after the current JEV request finishes.", true);
    return;
  }
  setClassificationMode(button.dataset.mode);
}));
elements.retryLoad.addEventListener("click", loadNextPage);
window.addEventListener("scroll", updateScreenCount, { passive: true });
window.addEventListener("resize", updateScreenCount);
document.querySelector("#mobileMenu").addEventListener("click", () => document.body.classList.toggle("menu-open"));
document.querySelector("#closeEmail").addEventListener("click", () => elements.dialog.close());
elements.dialog.addEventListener("click", event => { if (event.target === elements.dialog) elements.dialog.close(); });
document.querySelector("#toggleRecipients").addEventListener("click", () => {
  const recipients = document.querySelector("#recipientDetails");
  recipients.hidden = !recipients.hidden;
});
document.querySelector("#retryDetail").addEventListener("click", () => state.detailId && openEmail(state.detailId));
document.querySelector("#bulkClassify").addEventListener("click", async event => {
  const button = event.currentTarget;
  if (state.classificationActive || state.loadingEmails) {
    showToast("Please wait", "A Gmail or JEV request is already running.", true);
    return;
  }

  const autoWasEnabled = elements.autoToggle.checked;
  elements.autoToggle.checked = false;
  button.disabled = true;
  button.textContent = "Preparing 100 emails…";
  window.clearTimeout(state.classificationTimer);
  state.classificationQueue.clear();
  state.retryIds.clear();

  try {
    let pending = state.orderedIds.filter(id => state.emails.get(id)?.classification_status === "pending");
    while (pending.length < 100 && state.hasMore) {
      const loaded = await loadNextPage();
      if (!loaded) break;
      pending = state.orderedIds.filter(id => state.emails.get(id)?.classification_status === "pending");
    }

    const batch = pending.slice(0, 100);
    if (!batch.length) {
      showToast("Nothing to classify", "All currently available emails already have saved classifications.");
      return;
    }

    batch.forEach(id => state.classificationQueue.add(id));
    updateCounts();
    button.textContent = `Classifying ${batch.length} in one call…`;
    await flushClassificationQueue();
  } finally {
    elements.autoToggle.checked = autoWasEnabled;
    button.disabled = false;
    button.textContent = "Classify next 100";
  }
});
document.querySelector("#resetHistory").addEventListener("click", async () => {
  if (state.classificationActive) {
    showToast("Batch still running", "Wait for the current JEV request to finish, then reset history.", true);
    return;
  }
  const confirmed = window.confirm(
    "Erase all loaded email metadata, classifications, page history, and JEV usage? Your Gmail connection will remain active."
  );
  if (!confirmed) return;

  try {
    await apiFetch("/history/reset", { method: "POST", body: "{}" });
    state.classificationQueue.clear();
    state.retryIds.clear();
    window.clearTimeout(state.classificationTimer);
    classificationObserver.disconnect();
    state.emails.clear();
    state.orderedIds = [];
    state.firstPage = true;
    state.nextPageToken = null;
    state.hasMore = true;
    elements.list.replaceChildren();
    await refreshUsage();
    updateCounts();
    showToast("History erased", "Gmail remains connected. Emails will load and classify again as you scroll.");
    await loadNextPage();
  } catch (error) {
    showToast("Couldn’t reset history", error.message || "Try again after the active batch finishes.", true);
  }
});

async function init() {
  initializeObservers();
  const savedMode = localStorage.getItem("jevClassificationMode");
  setClassificationMode(savedMode === "bulk" ? "bulk" : "scroll", false);
  try {
    const auth = await apiFetch("/auth/status");
    state.connected = Boolean(auth.connected);
    if (!state.connected) {
      showConnectState();
      return;
    }
    document.querySelector("#connectionLabel").textContent = "Gmail connected";
    document.querySelector("#liveStatus span").textContent = "Gmail connected";
    document.querySelector("#connectButton").textContent = "Reconnect Gmail";
    await refreshUsage();
    await loadNextPage();
  } catch (error) {
    if (error.status === 401) showConnectState();
    else setListStatus("error", "Couldn’t start the inbox", "Refresh the page or reconnect Gmail.");
  }
}

init();
