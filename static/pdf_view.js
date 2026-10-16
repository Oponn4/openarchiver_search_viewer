import * as pdfjsLib from "/static/vendor/pdfjs/pdf.min.mjs";

const PDFJS_BASE = "/static/vendor/pdfjs/";
pdfjsLib.GlobalWorkerOptions.workerSrc = `${PDFJS_BASE}pdf.worker.min.mjs`;

// Render pages shortly before they scroll into view and drop the bitmap again once they are far
// away. iOS Safari caps total canvas memory per tab; keeping every page of a long PDF rendered
// at retina resolution runs into that limit and pages turn blank.
const RENDER_MARGIN = "1500px 0px";
const MAX_DEVICE_PIXEL_RATIO = 2;
const MAX_CANVAS_PIXELS = 8_000_000;

const translate = window.t || ((key) => key);
const params = new URLSearchParams(location.search);
// embed: inline preview inside the mail view (no toolbar).
// overlay: full-screen layer over the search page, which keeps its state; back closes the layer.
const mode = params.get("embed") === "1" ? "embed" : params.get("overlay") === "1" ? "overlay" : "page";

const [, , messageId, , attachmentId] = location.pathname.split("/").map(decodeURIComponent);
const fileUrl = `/api/mail/${encodeURIComponent(messageId)}/attachments/${encodeURIComponent(attachmentId)}`;

const pagesContainer = document.querySelector("#pdf-pages");
const statusElement = document.querySelector("#pdf-status");
const titleElement = document.querySelector("#pdf-title");
const backButton = document.querySelector("#pdf-back");
const shareButton = document.querySelector("#pdf-share");
const downloadLink = document.querySelector("#pdf-download");

if (mode === "embed") {
  document.body.classList.add("embed");
}

downloadLink.href = `${fileUrl}?download=1`;

function goToLogin() {
  (mode === "page" ? window : window.top).location.href = "/login";
}

function goBack() {
  if (mode === "overlay") {
    window.parent.postMessage({ type: "pdf-viewer-close" }, location.origin);
    return;
  }
  let sameOriginReferrer = false;
  try {
    sameOriginReferrer = Boolean(document.referrer) && new URL(document.referrer).origin === location.origin;
  } catch {
    sameOriginReferrer = false;
  }
  if (sameOriginReferrer && history.length > 1) {
    history.back();
  } else {
    location.href = `/mail/${encodeURIComponent(messageId)}`;
  }
}

backButton.addEventListener("click", goBack);

function filenameFromDisposition(header) {
  if (!header) return "";
  const extended = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      // fall through to the plain filename
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : "";
}

function showError(message) {
  pagesContainer.innerHTML = "";
  const element = document.createElement("div");
  element.className = "pdf-status error";
  element.textContent = message;
  pagesContainer.appendChild(element);
}

function setupShare(blob, filename) {
  if (!navigator.share || !navigator.canShare) return;
  const file = new File([blob], filename, { type: "application/pdf" });
  if (!navigator.canShare({ files: [file] })) return;
  shareButton.hidden = false;
  shareButton.addEventListener("click", async () => {
    try {
      await navigator.share({ files: [file], title: filename });
    } catch (error) {
      if (error.name !== "AbortError") {
        console.error("Share failed:", error);
      }
    }
  });
}

function createPageView(page) {
  const baseViewport = page.getViewport({ scale: 1 });
  const box = document.createElement("div");
  box.className = "pdf-page-box";
  box.style.aspectRatio = `${baseViewport.width} / ${baseViewport.height}`;
  box.setAttribute("aria-label", `${page.pageNumber}`);

  const state = { page, baseViewport, box, canvas: null, task: null, renderedWidth: 0 };

  state.render = () => {
    const cssWidth = box.clientWidth;
    if (!cssWidth || state.renderedWidth === cssWidth) return;
    state.release();
    let scale = (cssWidth / baseViewport.width) * Math.min(window.devicePixelRatio || 1, MAX_DEVICE_PIXEL_RATIO);
    const pixels = baseViewport.width * baseViewport.height * scale * scale;
    if (pixels > MAX_CANVAS_PIXELS) {
      scale *= Math.sqrt(MAX_CANVAS_PIXELS / pixels);
    }
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);
    box.appendChild(canvas);
    state.canvas = canvas;
    state.renderedWidth = cssWidth;
    state.task = page.render({ canvas, viewport });
    state.task.promise.catch((error) => {
      if (error?.name !== "RenderingCancelledException") {
        console.error(`Rendering page ${page.pageNumber} failed:`, error);
      }
    });
  };

  state.release = () => {
    state.task?.cancel();
    state.task = null;
    if (state.canvas) {
      // Zeroing the size frees the backing store right away on iOS instead of at GC time.
      state.canvas.width = 0;
      state.canvas.height = 0;
      state.canvas.remove();
      state.canvas = null;
    }
    state.renderedWidth = 0;
  };

  return state;
}

async function load() {
  let response;
  try {
    response = await fetch(fileUrl);
  } catch {
    showError(translate("pdfLoadFailed"));
    return;
  }
  if (response.status === 401) {
    goToLogin();
    return;
  }
  if (!response.ok) {
    showError(translate("pdfLoadFailed"));
    return;
  }

  const filename = filenameFromDisposition(response.headers.get("content-disposition")) || "document.pdf";
  titleElement.textContent = filename;
  document.title = filename;
  downloadLink.setAttribute("download", filename);

  const buffer = await response.arrayBuffer();
  setupShare(new Blob([buffer], { type: "application/pdf" }), filename);

  let pdf;
  try {
    pdf = await pdfjsLib.getDocument({
      // pdf.js transfers the buffer to its worker, so hand it a copy and keep ours for sharing.
      data: new Uint8Array(buffer.slice(0)),
      cMapUrl: `${PDFJS_BASE}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${PDFJS_BASE}standard_fonts/`,
      wasmUrl: `${PDFJS_BASE}wasm/`,
      iccUrl: `${PDFJS_BASE}iccs/`,
    }).promise;
  } catch (error) {
    console.error("Opening PDF failed:", error);
    showError(translate("pdfLoadFailed"));
    return;
  }

  const views = [];
  for (let number = 1; number <= pdf.numPages; number += 1) {
    views.push(createPageView(await pdf.getPage(number)));
  }
  statusElement.remove();
  views.forEach((view) => pagesContainer.appendChild(view.box));

  const visible = new Set();
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        const view = views.find((item) => item.box === entry.target);
        if (!view) return;
        if (entry.isIntersecting) {
          visible.add(view);
          view.render();
        } else {
          visible.delete(view);
          view.release();
        }
      });
    },
    { rootMargin: RENDER_MARGIN }
  );
  views.forEach((view) => observer.observe(view.box));

  // Rotation or a resized window changes the page width; re-render what is near the viewport.
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => visible.forEach((view) => view.render()), 200);
  });
}

load();
