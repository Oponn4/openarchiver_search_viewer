const form = document.querySelector("#search-form");
const queryInput = document.querySelector("#query-input");
const searchHistoryList = document.querySelector("#search-history");
const limitInput = document.querySelector("#limit-input");
const limitValue = document.querySelector("#limit-value");
const limitMaxValue = document.querySelector("#limit-max-value");
const exactInput = document.querySelector("#exact-input");
const advancedToggle = document.querySelector("#advanced-toggle");
const advancedPanel = document.querySelector("#advanced-panel");
const advancedReset = document.querySelector("#advanced-reset");
const fromInput = document.querySelector("#from-input");
const dateFromInput = document.querySelector("#date-from-input");
const dateToInput = document.querySelector("#date-to-input");
const attachmentInput = document.querySelector("#attachment-input");
const sortButtons = Array.from(document.querySelectorAll(".sort-button"));
const searchButton = document.querySelector("#search-button");
const resultList = document.querySelector("#result-list");
const resultSummary = document.querySelector("#result-summary");
const statusText = document.querySelector("#status-text");
const openLink = document.querySelector("#open-link");
const downloadEmlLink = document.querySelector("#download-eml-link");
const viewerSubject = document.querySelector("#viewer-subject");
const viewerMeta = document.querySelector("#viewer-meta");
const mailDetail = document.querySelector("#mail-detail");
const navUpButton = document.querySelector("#nav-up-button");
const navDownButton = document.querySelector("#nav-down-button");
const translate = window.t || ((key, values = {}) => key.replace(/\{(\w+)\}/g, (_, name) => values[name] ?? `{${name}}`));
const appLocale = window.appLocale || "ko-KR";

const DEFAULT_LIMIT = 30;
const MIN_LIMIT = 10;
const SEARCH_HISTORY_KEY = "mailRag.searchHistory";
const MAX_SEARCH_HISTORY = 20;
let currentItems = [];
let selectedId = "";
let detailRequestToken = 0;
const fetchedMailIds = new Set();


const AVATAR_COLORS = [
  '#e17055', '#0984e3', '#00b894', '#6c5ce7', '#e84393',
  '#fdcb6e', '#00cec9', '#d63031', '#0abde3', '#a29bfe',
];

function senderDisplayName(from) {
  if (!from) return '';
  const m = from.match(/^\s*"?([^"<]+?)"?\s*<[^>]+>\s*$/);
  return (m ? m[1] : from).trim();
}

function avatarLetterAndColor(from) {
  const name = senderDisplayName(from) || (from || '').split('@')[0] || '?';
  const letter = name.trim().charAt(0).toUpperCase() || '?';
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  const color = AVATAR_COLORS[hash % AVATAR_COLORS.length];
  return { letter, color };
}

function isExactMode() {
  return exactInput?.getAttribute("aria-pressed") === "true";
}

function searchHighlightTerms(query) {
  return query
    .split(/\s+/)
    .map((term) => term.trim().replace(/^["']+|["']+$/g, ""))
    .filter(Boolean);
}

function loadSearchHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(SEARCH_HISTORY_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string" && item.trim()) : [];
  } catch {
    return [];
  }
}

function renderSearchHistory() {
  if (!searchHistoryList) return;
  searchHistoryList.innerHTML = "";
  loadSearchHistory().forEach((query) => {
    const option = document.createElement("option");
    option.value = query;
    searchHistoryList.appendChild(option);
  });
}

function saveSearchHistory(query) {
  const normalized = query.trim();
  if (!normalized) return;
  const history = loadSearchHistory().filter((item) => item !== normalized);
  history.unshift(normalized);
  localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(history.slice(0, MAX_SEARCH_HISTORY)));
  renderSearchHistory();
}

function maxSearchLimit() {
  const max = Number(limitInput.max);
  return Number.isFinite(max) && max >= MIN_LIMIT ? max : 50;
}

function updateLimitDisplay() {
  if (limitValue) {
    limitValue.textContent = limitInput.value;
  }
  if (limitMaxValue) {
    limitMaxValue.textContent = String(maxSearchLimit());
  }
}

function clampLimitInput({ allowEmpty = false } = {}) {
  if (allowEmpty && limitInput.value === "") {
    return "";
  }
  const rawLimit = Number(limitInput.value);
  const fallbackLimit = Math.min(DEFAULT_LIMIT, maxSearchLimit());
  const limit = Math.min(maxSearchLimit(), Math.max(MIN_LIMIT, Number.isFinite(rawLimit) ? rawLimit : fallbackLimit));
  limitInput.value = String(limit);
  updateLimitDisplay();
  return limit;
}

function formatDate(timestamp) {
  const numeric = Number(timestamp);
  if (!Number.isFinite(numeric)) {
    return "";
  }
  return new Intl.DateTimeFormat(appLocale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(numeric));
}

function formatSize(sizeBytes) {
  const size = Number(sizeBytes);
  if (!Number.isFinite(size) || size <= 0) {
    return "";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function canPreviewAttachment(attachment) {
  const mimeType = (attachment.mimeType || "").toLowerCase();
  const filename = (attachment.filename || "").toLowerCase();
  if (isActiveAttachment(attachment)) {
    return false;
  }
  return (
    mimeType.startsWith("image/") ||
    isPdfAttachment(attachment) ||
    mimeType.startsWith("text/")
  );
}

function isActiveAttachment(attachment) {
  const mimeType = (attachment.mimeType || "").split(";", 1)[0].trim().toLowerCase();
  const filename = (attachment.filename || "").toLowerCase();
  return (
    ["application/xhtml+xml", "application/xml", "image/svg+xml", "text/html", "text/xml"].includes(mimeType) ||
    mimeType.endsWith("+xml") ||
    /\.(html?|mhtml?|svg|xhtml?|xml)$/.test(filename)
  );
}

function isPdfAttachment(attachment) {
  const mimeType = (attachment.mimeType || "").toLowerCase();
  const filename = (attachment.filename || "").toLowerCase();
  return mimeType === "application/pdf" || filename.endsWith(".pdf");
}

function pdfPreviewUrl(url) {
  return `${url.split("#", 1)[0]}#zoom=75`;
}

function attachmentKind(attachment) {
  const mimeType = (attachment.mimeType || "").toLowerCase();
  const filename = (attachment.filename || "").toLowerCase();
  if (mimeType === "application/pdf" || filename.endsWith(".pdf")) {
    return { label: "PDF", className: "file-pdf" };
  }
  if (mimeType.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp|tiff?)$/.test(filename)) {
    return { label: "IMG", className: "file-image" };
  }
  if (mimeType.includes("spreadsheet") || mimeType.includes("excel") || /\.(xlsx?|csv)$/.test(filename)) {
    return { label: "XLS", className: "file-excel" };
  }
  if (mimeType.includes("word") || /\.(docx?|rtf)$/.test(filename)) {
    return { label: "DOC", className: "file-word" };
  }
  if (mimeType.includes("presentation") || /\.(pptx?)$/.test(filename)) {
    return { label: "PPT", className: "file-ppt" };
  }
  if (mimeType.startsWith("text/") || /\.(txt|log|md)$/.test(filename)) {
    return { label: "TXT", className: "file-text" };
  }
  if (mimeType.includes("zip") || /\.(zip|7z|rar|tar|gz)$/.test(filename)) {
    return { label: "ZIP", className: "file-zip" };
  }
  return { label: "FILE", className: "file-generic" };
}

function setLoading(isLoading) {
  searchButton.disabled = isLoading;
  statusText.textContent = isLoading ? translate("searching") : "";
}

function currentSort() {
  return document.querySelector(".sort-button.active")?.dataset.sort || "relevance";
}

function setSort(sort) {
  sortButtons.forEach((button) => {
    const isActive = button.dataset.sort === sort;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
  });
  runSearch();
}

function updateNavButtons() {
  if (!navUpButton || !navDownButton) return;
  if (!selectedId) {
    navUpButton.disabled = true;
    navDownButton.disabled = true;
    return;
  }
  const currentIndex = currentItems.findIndex(i => i.id === selectedId);
  navUpButton.disabled = currentIndex <= 0;
  navDownButton.disabled = currentIndex === -1 || currentIndex >= currentItems.length - 1;
}

function isMobileLayout() {
  return window.matchMedia("(max-width: 760px)").matches;
}

async function setViewer(item) {
  selectedId = item.id;
  if (isMobileLayout()) {
    document.body.classList.add("mobile-detail-open");
  }
  updateNavButtons();
  const url = `/mail/${encodeURIComponent(item.id)}`;
  viewerSubject.textContent = item.subject || translate("noSubject");
  viewerMeta.textContent = `${item.from || translate("noSender")} · ${formatDate(item.timestamp)}`;
  openLink.href = url;
  openLink.setAttribute("aria-disabled", "false");
  if (downloadEmlLink) {
    downloadEmlLink.href = `/api/mail/${encodeURIComponent(item.id)}/eml`;
    downloadEmlLink.setAttribute("aria-disabled", "false");
  }
  renderDetailLoading();
  renderResults();

  const requestToken = ++detailRequestToken;
  try {
    const response = await fetch(`/api/mail/${encodeURIComponent(item.id)}`);
    if (response.status === 401) {
      window.location.href = '/login';
      return;
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || translate("mailDetailRequestFailed"));
    }
    if (requestToken !== detailRequestToken) {
      return;
    }
    const detail = payload.item || item;
    viewerSubject.textContent = detail.subject || translate("noSubject");
    viewerMeta.textContent = `${detail.from || translate("noSender")} · ${formatDate(detail.timestamp)}`;
    openLink.href = payload.mailViewUrl || url;
    renderDetail(detail);
  } catch (error) {
    if (requestToken !== detailRequestToken) {
      return;
    }
    renderDetailError(error.message, item);
  }

  // Prefetch adjacent messages and avoid duplicate requests.
  const currentIndex = currentItems.findIndex(i => i.id === item.id);
  if (currentIndex >= 0) {
    const prefetchIds = [];
    if (currentIndex > 0) prefetchIds.push(currentItems[currentIndex - 1].id);
    if (currentIndex < currentItems.length - 1) prefetchIds.push(currentItems[currentIndex + 1].id);
    
    prefetchIds.forEach(id => {
      if (!fetchedMailIds.has(id)) {
        fetchedMailIds.add(id);
        fetch(`/api/mail/${encodeURIComponent(id)}`).then(res => {
          if (res.status === 401) window.location.href = '/login';
        }).catch(() => {});
      }
    });
  }
}

function createField(label, value) {
  const row = document.createElement("div");
  row.className = "detail-row";

  const labelElement = document.createElement("div");
  labelElement.className = "detail-label";
  labelElement.textContent = label;

  const valueElement = document.createElement("div");
  valueElement.className = "detail-value";
  valueElement.textContent = Array.isArray(value) ? value.join(", ") : value || "-";

  row.append(labelElement, valueElement);
  return row;
}

function renderDetail(item) {
  mailDetail.innerHTML = "";

  const fields = document.createElement("div");
  fields.className = "detail-fields";
  fields.append(
    createField(translate("from"), item.from),
    createField(translate("to"), item.to),
    createField(translate("cc"), item.cc),
    createField(translate("date"), formatDate(item.timestamp))
  );

  const attachments = createAttachmentPanel(item.attachments || []);

  const body = document.createElement("div");
  body.className = "detail-body";

  const q = queryInput.value.trim();
  const terms = searchHighlightTerms(q);

  if (item.bodyHtml) {
    const frame = document.createElement("iframe");
    frame.className = "mail-html-frame";
    frame.title = translate("mailHtmlBody");
    frame.setAttribute("sandbox", "allow-same-origin");
    frame.setAttribute("scrolling", "no");

    // Tote externe Bilder ausblenden (haeufig bei alten Marketing-Mails: Tracking-/CDN-Domains
    // von vor Jahren, DNS loest oft nicht mehr auf), sonst zeigt der Browser sein haessliches
    // kaputt-Icon. Aufgerufen sowohl aus frame.onload (Normalfall: alle Bilder settlen schnell)
    // als auch unabhaengig per Timer (deckt ab, dass EIN Host nicht sauber ablehnt, sondern die
    // Verbindung haengen laesst -- dann wartet frame.onload praktisch endlos, weil es erst
    // feuert, wenn wirklich JEDES Bild fertig ist; beobachtet an einer Mail mit 84 Bildern,
    // mehrere davon auf demselben haengenden Host). sandbox="allow-same-origin" (ohne
    // allow-scripts) erlaubt der Elternseite trotzdem, Listener direkt auf die iframe-Elemente
    // zu haengen -- das Verbot gilt nur fuer <script> INNERHALB des sandboxten Dokuments selbst.
    // Idempotent, darf mehrfach laufen.
    const hideBrokenImages = (giveUpOnPending = false) => {
      let doc;
      try {
        doc = frame.contentWindow.document;
      } catch (e) {
        return;
      }
      const hideIfBroken = (img) => {
        if (img.style.display === "none") return;
        img.style.display = "none";
        const newHeight = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0);
        if (newHeight > 0) frame.style.height = newHeight + "px";
      };
      doc.querySelectorAll("img").forEach((img) => {
        if (img.complete && img.naturalWidth === 0) {
          hideIfBroken(img);
        } else if (!img.complete && giveUpOnPending) {
          // Eine Verbindung, die einfach nie antwortet statt sauber abzulehnen, feuert bei
          // manchen Hosts erst nach Minuten ein error-Event (TCP-Timeout, nicht DNS). Nach 8s
          // ist das fuer die Anzeige kein "gleich fertig" mehr -- lieber ausblenden, als dass
          // die Seite auf unbestimmte Zeit eine grosse leere Box zeigt.
          hideIfBroken(img);
        } else if (!img.complete && !img.dataset.errorListenerAttached) {
          img.dataset.errorListenerAttached = "1";
          img.addEventListener("error", () => hideIfBroken(img), { once: true });
        }
      });
    };

    frame.onload = () => {
      try {
        const doc = frame.contentWindow.document;

        if (terms.length > 0) {
          const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, null, false);
          const nodes = [];
          while(walker.nextNode()) nodes.push(walker.currentNode);

          nodes.forEach(node => {
            if(node.parentNode && node.parentNode.nodeName !== 'SCRIPT' && node.parentNode.nodeName !== 'STYLE') {
              let text = node.nodeValue;
              if(!text.trim()) return;

              let replaced = false;
              let escapedText = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

              terms.forEach(term => {
                const escapedTerm = term.replace(/[.*+?^$()|[\]{}]/g, '\\$&');
                const regex = new RegExp('(' + escapedTerm + ')', 'gi');
                const newText = escapedText.replace(regex, '<mark>$1</mark>');
                if (newText !== escapedText) {
                  escapedText = newText;
                  replaced = true;
                }
              });

              if(replaced) {
                const span = doc.createElement('span');
                span.innerHTML = escapedText;
                node.parentNode.replaceChild(span, node);
              }
            }
          });
        }

        hideBrokenImages();

        const height = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0);
        frame.style.height = height + "px";
      } catch (e) {
        console.error("Failed to resize or highlight iframe:", e);
      }
    };

    // Unabhaengig vom load-Event: nach 2s einmal proaktiv pruefen (holt die schnellen Faelle,
    // z.B. DNS-NXDOMAIN). Nach 8s wird bei allem, was dann noch offen ist, aufgegeben statt
    // weiter auf ein error-Event zu warten -- gemessen an einer Mail mit 84 Bildern: die
    // haengenden Verbindungen zu einem toten Tracking-Host waren nach 35s immer noch nicht
    // gescheitert (kein Timeout, kein Fehler, einfach nichts).
    setTimeout(() => hideBrokenImages(false), 2000);
    setTimeout(() => hideBrokenImages(true), 8000);

    const imageStyle = "<style>img { max-width: 100% !important; max-height: 700px !important; width: auto !important; height: auto !important; object-fit: contain !important; } mark { background: #fff1a8; color: inherit; padding: 0 2px; border-radius: 3px; }</style>";
    frame.srcdoc = imageStyle + item.bodyHtml;

    body.appendChild(frame);
  } else {
    const rawText = item.body || item.bodyPreview || translate("noBodyInSearchResult");
    if (terms.length > 0) {
      let htmlText = rawText.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      terms.forEach(term => {
        const escapedTerm = term.replace(/[.*+?^$()|[\]{}]/g, '\\$&');
        const regex = new RegExp('(' + escapedTerm + ')', 'gi');
        htmlText = htmlText.replace(regex, '<mark>$1</mark>');
      });
      body.innerHTML = htmlText;
    } else {
      body.textContent = rawText;
    }
  }

  if (attachments) {
    mailDetail.append(fields, attachments, body);
  } else {
    mailDetail.append(fields, body);
  }
}

function createAttachmentPanel(attachments) {
  if (!Array.isArray(attachments) || attachments.length === 0) {
    return null;
  }

  const panel = document.createElement("div");
  panel.className = "attachment-panel";

  const title = document.createElement("div");
  title.className = "attachment-title";
  title.textContent = translate("attachments", { count: attachments.length });

  const list = document.createElement("div");
  list.className = "attachment-list";

  const preview = document.createElement("div");
  preview.className = "attachment-preview";
  preview.hidden = true;
  let activeAttachmentId = "";

  attachments.forEach((attachment, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "attachment-button";
    button.setAttribute("aria-expanded", "false");

    const kind = attachmentKind(attachment);
    const icon = document.createElement("span");
    icon.className = `file-badge ${kind.className}`;
    icon.textContent = kind.label;

    const label = document.createElement("span");
    label.className = "attachment-name";
    label.textContent = `${attachment.filename || "attachment"}${formatSize(attachment.sizeBytes) ? ` · ${formatSize(attachment.sizeBytes)}` : ""}`;

    button.append(icon, label);
    button.addEventListener("click", () => {
      if (activeAttachmentId === attachment.id && !preview.hidden) {
        preview.hidden = true;
        activeAttachmentId = "";
        button.classList.remove("active");
        button.setAttribute("aria-expanded", "false");
        return;
      }

      list.querySelectorAll(".attachment-button").forEach((item) => item.classList.remove("active"));
      list.querySelectorAll(".attachment-button").forEach((item) => item.setAttribute("aria-expanded", "false"));
      button.classList.add("active");
      button.setAttribute("aria-expanded", "true");
      activeAttachmentId = attachment.id;
      preview.hidden = false;
      renderAttachmentPreview(preview, attachment);
    });

    const openLink = document.createElement("a");
    openLink.className = "attachment-open";
    openLink.href = attachment.viewUrl;
    openLink.target = "_blank";
    openLink.rel = "noreferrer";
    openLink.textContent = translate("openInNewWindow");

    const row = document.createElement("div");
    row.className = "attachment-row";
    row.append(button, openLink);
    list.appendChild(row);
  });

  panel.append(title, list, preview);
  return panel;
}

function renderAttachmentPreview(container, attachment) {
  container.innerHTML = "";

  const mimeType = attachment.mimeType || "";
  const isImage = !isActiveAttachment(attachment) && (
    mimeType.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp|tiff?)$/i.test(attachment.filename || "")
  );

  if (isImage) {
    const loading = document.createElement("img");
    loading.src = "/static/loading.gif";
    loading.alt = "Loading...";
    loading.style.display = "block";
    loading.style.margin = "0 auto";
    loading.style.maxWidth = "50px";
    loading.style.height = "100%";
    loading.style.objectFit = "contain";

    const img = document.createElement("img");
    img.className = "attachment-image";
    img.alt = translate("attachmentPreview");
    img.style.display = "none";
    
    img.onload = () => {
      loading.remove();
      img.style.display = "";
    };
    img.onerror = () => {
      loading.remove();
      img.style.display = "";
    };

    img.src = attachment.viewUrl;
    container.append(loading, img);
    return;
  }

  if (canPreviewAttachment(attachment)) {
    const frame = document.createElement("iframe");
    frame.className = "attachment-frame";
    frame.title = translate("attachmentPreview");
    frame.src = isPdfAttachment(attachment) ? pdfPreviewUrl(attachment.viewUrl) : attachment.viewUrl;
    container.appendChild(frame);
    return;
  }

  const message = document.createElement("div");
  message.className = "attachment-download";

  const name = document.createElement("div");
  name.className = "attachment-download-name";
  name.textContent = attachment.filename || "attachment";

  const help = document.createElement("div");
  help.className = "attachment-download-help";
  help.textContent = translate("unsupportedPreview");

  const link = document.createElement("a");
  link.className = "attachment-download-link";
  link.href = attachment.viewUrl;
  link.target = "_blank";
  link.rel = "noreferrer";
  link.textContent = translate("openOrDownload");

  message.append(name, help, link);
  container.appendChild(message);
}

function renderDetailLoading() {
  mailDetail.innerHTML = '<div class="empty-state" style="display: flex; justify-content: center; align-items: center; height: 100%;"><img src="/static/loading.gif" alt="Loading..." style="display: block; margin: 0 auto; max-width: 50px;"></div>';
}

function renderDetailError(message, fallbackItem) {
  mailDetail.innerHTML = "";
  const error = document.createElement("div");
  error.className = "error-state";
  error.textContent = message;

  const fallback = document.createElement("div");
  fallback.className = "detail-body";
  fallback.textContent = fallbackItem.body || fallbackItem.bodyPreview || translate("noPreviewBody");

  mailDetail.append(error, fallback);
}

function renderResults() {
  resultList.innerHTML = "";

  if (currentItems.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = translate("noResults");
    resultList.appendChild(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  currentItems.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `result-item${item.id === selectedId ? " active" : ""}`;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", item.id === selectedId ? "true" : "false");
    button.addEventListener("click", () => setViewer(item));

    const subject = document.createElement("div");
    subject.className = "subject";
    if (item.subjectHtml) {
      subject.innerHTML = item.subjectHtml;
    } else {
      subject.textContent = item.subject || translate("noSubject");
    }

    const date = document.createElement("div");
    date.className = "date";
    date.textContent = formatDate(item.timestamp);

    const sender = document.createElement("div");
    sender.className = "sender";
    sender.textContent = item.from || translate("noSender");

    const { letter, color } = avatarLetterAndColor(item.from);
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.style.background = color;
    avatar.textContent = letter;
    avatar.setAttribute("aria-hidden", "true");

    const preview = document.createElement("div");
    preview.className = "preview";
    const attachmentText = item.attachmentCount > 0 ? `${translate("attachmentsShort", { count: item.attachmentCount })} · ` : "";
    if (item.snippetHtml) {
      preview.innerHTML = `${attachmentText}${item.snippetHtml}`;
    } else {
      preview.textContent = `${attachmentText}${item.snippet || item.bodyPreview || ""}`;
    }

    button.append(avatar, subject, date, preview, sender);
    fragment.appendChild(button);
  });

  resultList.appendChild(fragment);

  const activeItem = resultList.querySelector(".result-item.active");
  if (activeItem) {
    activeItem.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function renderError(message) {
  resultList.innerHTML = "";
  const error = document.createElement("div");
  error.className = "error-state";
  error.textContent = message;
  resultList.appendChild(error);
}

async function runSearch() {
  const q = queryInput.value.trim();
  const limit = clampLimitInput();

  if (!q) {
    resultSummary.textContent = translate("enterSearchTerm");
    return;
  }
  saveSearchHistory(q);

  setLoading(true);
  resultSummary.textContent = "";
  resultList.innerHTML = '<div class="empty-state" style="display: flex; justify-content: center; align-items: center; height: 100%; min-height: 200px;"><img src="/static/loading.gif" alt="Loading..." style="display: block; margin: 0 auto; max-width: 50px;"></div>';

  try {
    const params = new URLSearchParams({
      q,
      limit: String(limit),
      sort: currentSort(),
      exact: isExactMode() ? "true" : "false",
    });
    // Erweiterte Filter nur setzen, wenn ausgefuellt -- der leere String wuerde beim
    // Backend gegen das Datumsmuster ("^\d{4}-\d{2}-\d{2}$") scheitern; nicht mitgeschickt
    // bleibt der jeweilige FastAPI-Parameter auf seinem Default None.
    const sender = (fromInput?.value || "").trim();
    if (sender) params.set("sender", sender);
    if (dateFromInput?.value) params.set("date_from", dateFromInput.value);
    if (dateToInput?.value) params.set("date_to", dateToInput.value);
    if (attachmentInput?.value) params.set("has_attachment", attachmentInput.value);

    // Suche in der URL spiegeln: der Browser-Zurueck (Geste oder Knopf) und ein normales
    // Neuladen fanden sonst nichts, weil der App-Zustand ausschliesslich in JS-Variablen lebte.
    const nextUrl = `?${params.toString()}`;
    if (`${location.pathname}${location.search}` !== `/${nextUrl}`) {
      history.pushState({ q, limit, sort: currentSort(), exact: isExactMode() }, "", nextUrl);
    }

    const response = await fetch(`/api/search?${params.toString()}`);
    if (response.status === 401) {
      window.location.href = '/login';
      return;
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || translate("searchRequestFailed"));
    }

    currentItems = payload.items || [];
    if (payload.limit) {
      limitInput.value = String(payload.limit);
    }
    if (payload.maxLimit) {
      limitInput.max = String(payload.maxLimit);
      clampLimitInput();
    }
    updateLimitDisplay();
    selectedId = "";
    updateNavButtons();
    detailRequestToken += 1;
    openLink.href = "#";
    openLink.setAttribute("aria-disabled", "true");
    if (downloadEmlLink) {
      downloadEmlLink.href = "#";
      downloadEmlLink.setAttribute("aria-disabled", "true");
    }
    viewerSubject.textContent = translate("selectMail");
    viewerMeta.textContent = "";
    mailDetail.innerHTML = `<div class="empty-state">${translate("selectMailHelp")}</div>`;

    const sortLabels = {
      relevance: translate("sortRelevanceSummary"),
      desc: translate("sortDescSummary"),
      asc: translate("sortAscSummary"),
    };
    const sortLabel = sortLabels[payload.sort] || translate("sortRelevanceSummary");
    const looseLabel = payload.loose ? translate("looseSummary") : "";
    resultSummary.textContent = translate("shownResults", {
      shown: currentItems.length,
      total: payload.total ?? currentItems.length,
      sortLabel,
      looseLabel,
    });
    if (payload.processingTimeMs !== null && payload.processingTimeMs !== undefined) {
      statusText.textContent = `${payload.processingTimeMs}ms`;
    }
    
    if (currentItems.length > 0) {
      if (isMobileLayout()) {
        selectedId = "";
        document.body.classList.remove("mobile-detail-open");
        renderResults();
      } else {
        setViewer(currentItems[0]);
      }
    } else {
      renderResults();
    }
  } catch (error) {
    currentItems = [];
    resultSummary.textContent = translate("searchFailed");
    renderError(error.message);
  } finally {
    setLoading(false);
  }
}


function restoreStateFromUrl() {
  const params = new URLSearchParams(location.search);
  const q = params.get("q");
  if (q === null) return false;
  queryInput.value = q;
  if (exactInput) exactInput.setAttribute("aria-pressed", params.get("exact") === "true" ? "true" : "false");
  const limitParam = Number(params.get("limit"));
  if (Number.isFinite(limitParam) && limitParam > 0) {
    limitInput.value = String(limitParam);
    updateLimitDisplay();
  }
  const sortParam = params.get("sort");
  if (sortParam) {
    sortButtons.forEach((button) => {
      const isActive = button.dataset.sort === sortParam;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
  }
  const sender = params.get("sender");
  const dateFrom = params.get("date_from");
  const dateTo = params.get("date_to");
  const hasAttachment = params.get("has_attachment");
  if (fromInput) fromInput.value = sender || "";
  if (dateFromInput) dateFromInput.value = dateFrom || "";
  if (dateToInput) dateToInput.value = dateTo || "";
  if (attachmentInput) attachmentInput.value = hasAttachment || "";
  if ((sender || dateFrom || dateTo || hasAttachment) && advancedPanel && advancedToggle) {
    advancedPanel.hidden = false;
    advancedToggle.setAttribute("aria-expanded", "true");
  }
  return true;
}

window.addEventListener("popstate", () => {
  restoreStateFromUrl();
  runSearch();
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});

limitInput.addEventListener("input", () => {
  const rawLimit = Number(limitInput.value);
  if (Number.isFinite(rawLimit) && rawLimit > maxSearchLimit()) {
    limitInput.value = String(maxSearchLimit());
  }
  if (Number.isFinite(rawLimit) && rawLimit < MIN_LIMIT) {
    limitInput.value = String(MIN_LIMIT);
  }
  updateLimitDisplay();
});

limitInput.addEventListener("change", () => {
  clampLimitInput({ allowEmpty: false });
});

updateLimitDisplay();
renderSearchHistory();
restoreStateFromUrl();

sortButtons.forEach((button) => {
  button.addEventListener("click", () => setSort(button.dataset.sort));
});


navUpButton?.addEventListener("click", () => {
  const currentIndex = currentItems.findIndex((i) => i.id === selectedId);
  if (currentIndex > 0) {
    setViewer(currentItems[currentIndex - 1]);
  }
});

navDownButton?.addEventListener("click", () => {
  const currentIndex = currentItems.findIndex((i) => i.id === selectedId);
  if (currentIndex >= 0 && currentIndex < currentItems.length - 1) {
    setViewer(currentItems[currentIndex + 1]);
  }
});

document.querySelector("#logout-button")?.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  window.location.href = "/login";
});

exactInput?.addEventListener("click", () => {
  const pressed = exactInput.getAttribute("aria-pressed") === "true";
  exactInput.setAttribute("aria-pressed", String(!pressed));
});

advancedToggle?.addEventListener("click", () => {
  const expanded = advancedToggle.getAttribute("aria-expanded") === "true";
  advancedToggle.setAttribute("aria-expanded", String(!expanded));
  if (advancedPanel) advancedPanel.hidden = expanded;
});

advancedReset?.addEventListener("click", () => {
  if (fromInput) fromInput.value = "";
  if (dateFromInput) dateFromInput.value = "";
  if (dateToInput) dateToInput.value = "";
  if (attachmentInput) attachmentInput.value = "";
  if (queryInput.value.trim()) runSearch();
});

runSearch();


document.querySelector("#mobile-back-button")?.addEventListener("click", () => {
  document.body.classList.remove("mobile-detail-open");
});
