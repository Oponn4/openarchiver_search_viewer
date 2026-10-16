const viewerSubject = document.querySelector("#viewer-subject");
const viewerMeta = document.querySelector("#viewer-meta");
const mailDetail = document.querySelector("#mail-detail");
const translate = window.t || ((key, values = {}) => key.replace(/\{(\w+)\}/g, (_, name) => values[name] ?? `{${name}}`));
const appLocale = window.appLocale || "ko-KR";

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
  if (isActiveAttachment(attachment)) {
    return false;
  }
  return mimeType.startsWith("image/") || mimeType === "application/pdf" || mimeType.startsWith("text/");
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

function attachmentKind(attachment) {
  const mimeType = (attachment.mimeType || "").toLowerCase();
  const filename = (attachment.filename || "").toLowerCase();
  if (mimeType === "application/pdf" || filename.endsWith(".pdf")) return { label: "PDF", className: "file-pdf" };
  if (mimeType.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp|tiff?)$/.test(filename)) return { label: "IMG", className: "file-image" };
  if (mimeType.includes("spreadsheet") || mimeType.includes("excel") || /\.(xlsx?|csv)$/.test(filename)) return { label: "XLS", className: "file-excel" };
  if (mimeType.includes("word") || /\.(docx?|rtf)$/.test(filename)) return { label: "DOC", className: "file-word" };
  if (mimeType.includes("presentation") || /\.(pptx?)$/.test(filename)) return { label: "PPT", className: "file-ppt" };
  if (mimeType.startsWith("text/") || /\.(txt|log|md)$/.test(filename)) return { label: "TXT", className: "file-text" };
  if (mimeType.includes("zip") || /\.(zip|7z|rar|tar|gz)$/.test(filename)) return { label: "ZIP", className: "file-zip" };
  return { label: "FILE", className: "file-generic" };
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

function renderAttachmentPreview(container, attachment) {
  container.innerHTML = "";
  if (canPreviewAttachment(attachment)) {
    const frame = document.createElement("iframe");
    frame.className = "attachment-frame";
    frame.title = translate("attachmentPreview");
    frame.src = attachment.pageUrl ? `${attachment.pageUrl}?embed=1` : attachment.viewUrl;
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

function createAttachmentPanel(attachments) {
  if (!Array.isArray(attachments) || attachments.length === 0) return null;
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
  attachments.forEach((attachment) => {
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
    if (attachment.pageUrl) {
      // Same window, so the PDF page's back button returns here.
      openLink.href = attachment.pageUrl;
      openLink.textContent = translate("openPdf");
    } else {
      openLink.href = attachment.viewUrl;
      openLink.target = "_blank";
      openLink.rel = "noreferrer";
      openLink.textContent = translate("openInNewWindow");
    }
    const row = document.createElement("div");
    row.className = "attachment-row";
    row.append(button, openLink);
    list.appendChild(row);
  });
  panel.append(title, list, preview);
  return panel;
}

function renderDetail(item) {
  viewerSubject.textContent = item.subject || translate("noSubject");
  viewerMeta.textContent = `${item.from || translate("noSender")} · ${formatDate(item.timestamp)}`;
  document.title = item.subject || translate("mailViewTitle");
  mailDetail.innerHTML = "";
  const fields = document.createElement("div");
  fields.className = "detail-fields";
  fields.append(
    createField(translate("from"), item.from),
    createField(translate("to"), item.to),
    createField(translate("cc"), item.cc),
    createField(translate("date"), formatDate(item.timestamp)),
    createField(translate("attachment"), item.attachmentCount > 0 ? translate("attachments", { count: item.attachmentCount }) : translate("none"))
  );
  const attachments = createAttachmentPanel(item.attachments || []);
  const body = document.createElement("div");
  body.className = "detail-body";
  if (item.bodyHtml) {
    const frame = document.createElement("iframe");
    frame.className = "mail-html-frame";
    frame.title = translate("mailHtmlBody");
    frame.setAttribute("sandbox", "allow-same-origin");
    frame.setAttribute("scrolling", "no");

    const imageStyle = "<style>html, body { height: auto !important; overflow: visible !important; } img { max-width: 100% !important; max-height: 700px !important; width: auto !important; height: auto !important; object-fit: contain !important; } mark { background: #fff1a8; color: inherit; padding: 0 2px; border-radius: 3px; }</style>";
    frame.srcdoc = imageStyle + item.bodyHtml;

    const resizeFrame = () => {
      try {
        const doc = frame.contentWindow.document;
        const contentHeight = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0);
        const remainingViewportHeight = Math.max(320, window.innerHeight - frame.getBoundingClientRect().top - 20);
        const height = Math.max(contentHeight, remainingViewportHeight);
        frame.style.height = height + "px";
      } catch (e) {
        console.error("Failed to resize iframe:", e);
      }
    };
    frame.onload = resizeFrame;
    window.addEventListener("resize", resizeFrame);
    body.appendChild(frame);
  } else {
    body.textContent = item.body || item.bodyPreview || translate("noBody");
  }
  if (attachments) {
    mailDetail.append(fields, attachments, body);
  } else {
    mailDetail.append(fields, body);
  }
}

async function loadMail() {
  const id = window.location.pathname.split("/").filter(Boolean).pop();
  try {
    const response = await fetch(`/api/mail/${encodeURIComponent(id)}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || translate("mailDetailRequestFailed"));
    renderDetail(payload.item);
  } catch (error) {
    viewerSubject.textContent = translate("mailLoadFailed");
    mailDetail.innerHTML = "";
    const errorElement = document.createElement("div");
    errorElement.className = "error-state";
    errorElement.textContent = error.message;
    mailDetail.appendChild(errorElement);
  }
}

loadMail();
