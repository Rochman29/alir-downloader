const $ = (id) => document.getElementById(id);
let video = null;
let inspecting = false;
let submitting = false;
let pollTimer;
let previousJobs = "";
let storageMode = "local";
let serviceReady = false;

function historyCaption() {
  return storageMode === "server" ? "Riwayat untuk sesi browser ini" : "Riwayat tersimpan di perangkat ini";
}

function applyHealth(health) {
  storageMode = health.mode || "local";
  serviceReady = Boolean(health.ready);
  $("location-badge").textContent = storageMode === "server" ? "Terhubung ke server" : "Berjalan di komputermu";
  $("storage-note").textContent = storageMode === "server" ? "Simpan ke perangkatmu" : "Tersimpan secara lokal";
  $("collection-label").textContent = storageMode === "server" ? "KOLEKSI SESIMU" : "KOLEKSI LOKAL";
  $("edition").textContent = `${storageMode === "server" ? "ONLINE" : "LOCAL"} EDITION · V1.1`;
  $("inspect-button").disabled = !serviceReady || inspecting;
  if (!serviceReady) showError(storageMode === "server" ? "Layanan unduh belum siap. Coba lagi sebentar." : "Node.js atau Deno belum ditemukan. Pasang Node.js 22+ lalu jalankan ulang aplikasi.");
}

function bytes(value) {
  if (!value || !Number.isFinite(value)) return "Ukuran belum tersedia";
  const unit = value >= 1024 ** 3 ? "GB" : "MB";
  return `${(value / (unit === "GB" ? 1024 ** 3 : 1024 ** 2)).toFixed(1)} ${unit}`;
}

function duration(seconds) {
  if (seconds == null) return "";
  const value = Math.floor(seconds);
  return value >= 3600 ? `${Math.floor(value / 3600)}:${String(Math.floor(value / 60) % 60).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}` : `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

async function api(path, body) {
  const response = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : { cache: "no-store" });
  let data;
  try { data = await response.json(); }
  catch { throw new Error("Layanan unduh memberikan respons yang tidak valid. Coba lagi sebentar."); }
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Data tidak valid. Periksa inputmu.");
  return data;
}

function showError(message) {
  $("form-error").textContent = message;
  $("form-error").hidden = !message;
}

function updateNote() {
  const quality = video?.qualities.find((q) => q.id === $("quality").value);
  const storage = storageMode === "server" ? "Saat selesai, klik Simpan berkas untuk mengunduh ke perangkatmu." : "Disimpan di folder downloads proyek.";
  $("quality-note").textContent = quality ? `${quality.extension.toUpperCase()} · ${quality.estimated_bytes ? `Perkiraan ${bytes(quality.estimated_bytes)}` : "Ukuran diketahui saat mengunduh"} · Sudah termasuk audio. ${storage}` : "";
}

$("video-url").addEventListener("input", () => {
  if (!inspecting) { video = null; $("video-result").hidden = true; showError(""); }
});
$("quality").addEventListener("change", updateNote);
$("inspect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (inspecting || !serviceReady) return;
  inspecting = true;
  video = null;
  showError("");
  $("video-result").hidden = true;
  $("download-message").hidden = true;
  $("inspect-button").disabled = true;
  $("video-url").disabled = true;
  $("inspect-button").textContent = "Memeriksa…";
  $("inspect-status").textContent = "Mengambil informasi dan kualitas dari YouTube. Ini bisa memerlukan beberapa saat…";
  try {
    video = await api("/api/inspect", { url: $("video-url").value.trim() });
    $("video-title").textContent = video.title;
    $("uploader").textContent = video.uploader;
    $("duration").textContent = duration(video.duration);
    $("thumbnail").src = video.thumbnail || "/static/icon.svg";
    $("quality").replaceChildren(...video.qualities.map((quality, index) => {
      const option = document.createElement("option");
      option.value = quality.id;
      option.textContent = `${quality.label} · ${quality.extension.toUpperCase()}${index === 0 ? " · Terbaik" : ""}`;
      return option;
    }));
    updateNote();
    $("video-result").hidden = false;
    $("inspect-status").textContent = `${video.qualities.length} pilihan kualitas tersedia. Pilih yang sesuai kebutuhanmu.`;
    $("quality").focus();
  } catch (error) {
    showError(error.message === "Failed to fetch" ? "Aplikasi tidak terhubung. Pastikan server masih berjalan." : error.message);
    $("inspect-status").textContent = "Coba link video YouTube yang lain atau periksa kembali koneksimu.";
  } finally {
    inspecting = false;
    $("inspect-button").disabled = !serviceReady;
    $("video-url").disabled = false;
    $("inspect-button").textContent = "Cek video →";
  }
});

$("download-button").addEventListener("click", async () => {
  if (!video || submitting) return;
  submitting = true;
  showError("");
  $("download-button").disabled = true;
  $("download-message").hidden = true;
  try {
    await api("/api/downloads", { inspection_id: video.inspection_id, quality_id: $("quality").value });
    $("download-message").textContent = storageMode === "server"
      ? "Masuk antrean! Proses tetap berjalan walau halaman ditutup. Saat selesai, klik Simpan berkas untuk mengunduh ke perangkatmu."
      : "Masuk antrean! Hasil tersimpan otomatis di folder downloads. Kamu juga bisa menyimpannya lewat tombol Simpan berkas.";
    $("download-message").hidden = false;
    clearTimeout(pollTimer);
    await refreshJobs();
  } catch (error) {
    showError(error.message === "Failed to fetch" ? "Tidak terhubung ke aplikasi. Periksa server lalu coba lagi." : error.message);
  } finally {
    submitting = false;
    $("download-button").disabled = false;
  }
});

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function renderJob(job) {
  const labels = { queued: "Dalam antrean", downloading: "Mengunduh", processing: "Menggabungkan video + audio", completed: "Selesai", failed: "Gagal" };
  const row = element("article", `job ${job.status}`);
  row.append(element("div", "job-icon", job.status === "completed" ? "✓" : "↓"));
  const body = element("div", "job-body");
  const title = element("h3", "job-title", job.title);
  title.title = job.title;
  body.append(title);
  const meta = element("div", "job-meta");
  meta.append(element("span", "", job.quality), element("span", "", "·"), element("span", "job-status", labels[job.status] || job.status));
  if (job.downloaded_bytes) meta.append(element("span", "", bytes(job.downloaded_bytes)));
  if (job.status === "downloading" && job.speed) meta.append(element("span", "", `${bytes(job.speed)}/s`));
  if (job.status === "downloading" && job.eta != null) meta.append(element("span", "", `sisa tahap ini ${duration(job.eta)}`));
  body.append(meta);
  if (["queued", "downloading", "processing"].includes(job.status)) {
    const progress = element("progress", "progress-track");
    progress.max = 100;
    progress.value = job.progress;
    progress.setAttribute("aria-label", `Progres unduhan ${job.title}`);
    if (job.status === "downloading" && job.progress === 0) progress.removeAttribute("value");
    body.append(progress);
  }
  if (job.error) body.append(element("p", "job-error", job.error));
  row.append(body);
  if (job.file_url) {
    const link = element("a", "file-link", "Simpan berkas ↓");
    link.href = job.file_url;
    // The online gateway redirects to a signed URL on the worker; cross-origin downloads
    // use Content-Disposition from the worker instead of the download attribute.
    if (storageMode === "local") link.setAttribute("download", "");
    row.append(link);
  } else if (["downloading", "processing"].includes(job.status) && job.progress > 0) {
    row.append(element("span", "job-percent", `~${Math.floor(job.progress)}%`));
  }
  return row;
}

async function refreshJobs() {
  let delay = 2500;
  try {
    const jobs = await api("/api/downloads");
    const serialized = JSON.stringify(jobs);
    if (serialized !== previousJobs) {
      $("jobs").replaceChildren(...jobs.map(renderJob));
      previousJobs = serialized;
    }
    $("empty-state").hidden = jobs.length > 0;
    $("job-count").textContent = jobs.length;
    $("connection-status").textContent = historyCaption();
    if (!jobs.some((job) => ["queued", "downloading", "processing"].includes(job.status))) delay = 6000;
  } catch {
    $("connection-status").textContent = "Koneksi terputus. Mencoba kembali…";
    delay = 6000;
  }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refreshJobs, delay);
}

async function boot() {
  try {
    // Establish one signed browser session before sending concurrent API requests.
    applyHealth(await api("/api/health"));
    await refreshJobs();
  } catch (error) {
    $("location-badge").textContent = "Belum terhubung";
    $("connection-status").textContent = "Layanan unduh belum tersedia";
    showError(error.message === "Failed to fetch" ? "Server belum terhubung. Coba lagi sebentar." : error.message);
    setTimeout(boot, 10000);
  }
}
boot();
