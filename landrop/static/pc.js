// LanDrop PCの画面の動作（ビルド工程なしの素のJavaScript。先に common.js を読み込む）
"use strict";

const ICONS = {
  folder: "M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5Z",
  eye: "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Zm9.5 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
  play: "M8.5 5.5v13l10-6.5Z",
};
const STATUS_LABELS = { waiting: "待機中", uploading: "追加中", done: "追加済み", failed: "失敗" };
const STATUS_TONES = { uploading: "primary", done: "success", failed: "danger" };
const IMAGE_EXT = /\.(jpe?g|png|gif|webp|bmp|avif)$/i;
// 一覧の自動更新の間隔。自動更新は「操作」に数えないので、開いたままでも自動停止は効く
const POLL_MS = 2000;
const STATUS_POLL_MS = 5000;
const AUTO_HEADERS = { "X-LanDrop-Auto": "1" };

const state = {
  tab: "share",
  view: "grid",
  status: null,
  files: { share: [], received: [] },
  signatures: { share: "", received: "" },
  // 画面を開いたときにあったファイル。これ以外は「新着」として目立たせる
  knownReceived: null,
  newNames: new Set(),
  incoming: [],
  queue: [],
  running: false,
  nextId: 1,
  failures: 0,
  stopped: false,
  viewerFiles: [],
  viewerIndex: -1,
};

class Stopped extends Error {}

// ---------- サーバーとの通信 ----------

async function api(path, { method = "GET", auto = false } = {}) {
  const headers = auto ? AUTO_HEADERS : {};
  let response;
  try {
    response = await fetch(path, { method, headers, cache: "no-store", credentials: "same-origin" });
  } catch {
    state.failures += 1;
    // 一時的な失敗で「停止」と出さないよう、続けて失敗したときだけ停止とみなす
    if (state.failures >= 2) showStopped("LanDrop が停止しています", "ターミナルを閉じたか、自動停止しました。この画面は閉じてかまいません。");
    throw new Stopped();
  }
  state.failures = 0;
  if (response.status === 401) {
    showStopped("接続が切れました", "LanDropを起動し直してください。");
    throw new Stopped();
  }
  if (response.status === 204 || response.status === 202) return {};
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `エラーが発生しました（${response.status}）`);
  return body;
}

function query(params) {
  return new URLSearchParams(params).toString();
}

// 画面が開いている間、サーバーとの接続をつないでおく。サーバーはこれが切れたら、画面が閉じられたとみなす
async function holdConnection() {
  try {
    const response = await fetch("/pc/api/events", { headers: AUTO_HEADERS, cache: "no-store", credentials: "same-origin" });
    if (response.ok) {
      const reader = response.body.getReader();
      while (!(await reader.read()).done) {
        // 中身は使わない。つながっていることだけが大事
      }
    }
  } catch {
    // 下で状態を確かめる
  }
  // 切れたのは、サーバーが止まったか一時的な失敗。動いていれば、つなぎ直す
  for (let attempt = 0; attempt < 2 && !state.stopped; attempt += 1) {
    try {
      await api("/pc/api/status", { auto: true });
      setTimeout(holdConnection, 500);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
}

function previewUrl(folder, file) {
  // 同じ名前で中身が変わったときに古い画像を出さないよう、更新日時を付ける
  return `/pc/preview/${folder}/${encodeURIComponent(file.name)}?v=${file.modified}`;
}

async function reveal(folder, name) {
  try {
    await api(`/pc/api/reveal?${query({ folder, name })}`, { method: "POST" });
  } catch (error) {
    if (!(error instanceof Stopped)) toast(error.message, "danger");
  }
}

async function openFolder(folder) {
  try {
    await api(`/pc/api/open-folder?${query({ folder })}`, { method: "POST" });
  } catch (error) {
    if (!(error instanceof Stopped)) toast(error.message, "danger");
  }
}

// ---------- 状態の表示 ----------

async function loadStatus(auto = false) {
  try {
    const status = await api("/pc/api/status", { auto });
    state.status = status;
    $("pc-name").textContent = status.pc_name;
    $("address").textContent = status.address.replace(/^http:\/\//, "");
    $("share-dir").textContent = status.share_dir;
    $("received-dir").textContent = status.received_dir;
    renderTimer(status.remaining_seconds);
    renderNotices(status.notices || []);
  } catch {
    // 停止の表示は api() が行う
  }
}

function renderTimer(remaining) {
  const timer = $("timer");
  if (remaining === null) {
    timer.textContent = "自動停止しない設定です";
  } else if (remaining < 60) {
    timer.textContent = "まもなく自動停止します（操作すると延長されます）";
  } else {
    timer.textContent = `操作がなければ あと${Math.ceil(remaining / 60)}分で自動停止`;
  }
}

// 起動時の注意（パブリック ネットワーク、ポートの変更など）。起動中は変わらないので一度だけ描く
function renderNotices(notices) {
  const box = $("notices");
  if (box.childElementCount === notices.length) return;
  box.replaceChildren(
    ...notices.map((text) => {
      const notice = el("p", "notice", text);
      notice.dataset.tone = "warning";
      return notice;
    })
  );
}

let toastTimer = 0;

function toast(message, tone) {
  const node = $("toast");
  node.textContent = message;
  if (tone) node.dataset.tone = tone;
  else delete node.dataset.tone;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 3200);
}

function showStopped(title, text) {
  if (state.stopped) return;
  state.stopped = true;
  $("stopped-title").textContent = title;
  $("stopped-text").textContent = text;
  $("stopped").hidden = false;
  $("connection").classList.add("is-offline");
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
}

// ---------- 一覧 ----------

async function loadFolder(folder, auto = false) {
  try {
    const body = await api(`/pc/api/files/${folder}`, { auto });
    if (folder === "received") renderIncoming(body.incoming || []);
    const signature = JSON.stringify(body.files);
    if (signature === state.signatures[folder]) return;
    state.signatures[folder] = signature;
    state.files[folder] = body.files;
    if (folder === "share") renderShare();
    else updateReceived();
  } catch (error) {
    if (!(error instanceof Stopped)) {
      const status = $(`${folder}-status`);
      status.classList.add("is-error");
      status.textContent = error.message;
    }
  }
}

function renderShare() {
  const files = state.files.share;
  const list = $("share-list");
  const status = $("share-status");
  status.classList.remove("is-error");
  $("share-count").textContent = files.length ? String(files.length) : "";
  list.replaceChildren();
  if (files.length === 0) {
    status.textContent = "";
    const empty = el("li", "empty");
    empty.append(el("strong", "", "まだ共有していません"));
    empty.append(document.createTextNode("上の枠にファイルをドロップすると、スマホから受け取れるようになります。"));
    list.append(empty);
    return;
  }
  status.textContent = `${files.length}件 ・ スマホの「受け取る」に表示中`;
  for (const file of files) list.append(fileRow("share", file));
}

function fileRow(folder, file) {
  const row = el("li", "file");
  const tile = el("span", "tile");
  if (folder === "received" && file.preview && IMAGE_EXT.test(file.name)) {
    tile.append(thumbImage(folder, file));
  } else {
    tile.textContent = extensionLabel(file.name);
  }
  const main = el("div", "file-main");
  main.append(el("span", "file-name", file.name));
  main.append(el("span", "file-meta", `${formatSize(file.size)} ・ ${formatDate(file.modified)}`));
  const actions = el("div", "file-actions");
  if (state.newNames.has(file.name) && folder === "received") actions.append(newBadge());
  if (file.preview) actions.append(actionButton(ICONS.eye, "表示", () => openViewer(file)));
  actions.append(actionButton(ICONS.folder, "フォルダで表示", () => reveal(folder, file.name)));
  row.append(tile, main, actions);
  return row;
}

function actionButton(path, label, onClick) {
  const button = el("button", "btn btn-quiet btn-sm");
  button.type = "button";
  button.append(icon(path), document.createTextNode(label));
  button.addEventListener("click", onClick);
  return button;
}

function newBadge() {
  const badge = el("span", "badge", "NEW");
  badge.dataset.tone = "primary";
  return badge;
}

function thumbImage(folder, file) {
  const img = el("img");
  img.src = previewUrl(folder, file);
  img.alt = "";
  img.loading = "lazy";
  img.decoding = "async";
  return img;
}

function updateReceived() {
  const files = state.files.received;
  if (state.knownReceived === null) {
    state.knownReceived = new Set(files.map((f) => f.name));
  } else {
    const arrived = files.filter((f) => !state.knownReceived.has(f.name) && !state.newNames.has(f.name));
    for (const file of arrived) state.newNames.add(file.name);
    if (arrived.length > 0) {
      const first = arrived[0].name;
      toast(arrived.length === 1 ? `スマホから「${first}」が届きました` : `スマホから${arrived.length}件のファイルが届きました`);
      if (state.tab !== "received") $("received-new").hidden = false;
    }
  }
  renderReceived();
}

function renderReceived() {
  const files = state.files.received;
  const list = $("received-list");
  const status = $("received-status");
  status.classList.remove("is-error");
  list.dataset.view = state.view;
  $("received-count").textContent = files.length ? String(files.length) : "";
  list.replaceChildren();
  state.viewerFiles = files.filter((f) => f.preview);
  if (files.length === 0) {
    status.textContent = "";
    const empty = el("li", "empty");
    empty.append(el("strong", "", "まだ届いていません"));
    empty.append(document.createTextNode("スマホの「送る」からファイルを送ると、ここに表示されます。"));
    list.append(empty);
    return;
  }
  status.textContent = `${files.length}件 ・ 新しい順`;
  for (const file of files) {
    list.append(state.view === "grid" ? receivedCard(file) : fileRow("received", file));
  }
}

function receivedCard(file) {
  const card = el("li", "thumb-card");
  const thumb = el("button", "thumb");
  thumb.type = "button";
  const isImage = file.preview && IMAGE_EXT.test(file.name);
  if (isImage) {
    thumb.append(thumbImage("received", file));
  } else {
    thumb.append(el("span", "tile", extensionLabel(file.name)));
    if (file.preview) {
      const play = el("span", "thumb-play");
      play.append(icon(ICONS.play));
      thumb.append(play);
    }
  }
  thumb.setAttribute("aria-label", file.preview ? `${file.name} を表示` : `${file.name} をフォルダで表示`);
  thumb.addEventListener("click", () => (file.preview ? openViewer(file) : reveal("received", file.name)));
  card.append(thumb);
  if (state.newNames.has(file.name)) card.append(newBadge());

  const body = el("div", "thumb-body");
  const text = el("div", "thumb-text");
  const name = el("p", "thumb-name", file.name);
  name.title = file.name;
  text.append(name, el("p", "thumb-meta", `${formatSize(file.size)} ・ ${formatDate(file.modified)}`));
  const folderButton = el("button", "btn btn-quiet btn-sm btn-icon");
  folderButton.type = "button";
  folderButton.setAttribute("aria-label", `${file.name} をフォルダで表示`);
  folderButton.title = "フォルダで表示";
  folderButton.append(icon(ICONS.folder));
  folderButton.addEventListener("click", () => reveal("received", file.name));
  body.append(text, folderButton);
  card.append(body);
  return card;
}

function renderIncoming(incoming) {
  const signature = JSON.stringify(incoming);
  if (signature === JSON.stringify(state.incoming)) return;
  state.incoming = incoming;
  $("received-busy").hidden = incoming.length === 0;
  const list = $("incoming");
  list.replaceChildren();
  for (const item of incoming) {
    const row = el("li", "item");
    row.dataset.status = "uploading";
    const main = el("div");
    const head = el("div", "item-head");
    const badge = el("span", "badge", "受信中");
    badge.dataset.tone = "primary";
    head.append(badge);
    const ratio = item.size > 0 ? item.received / item.size : 0;
    main.append(
      head,
      el("p", "item-name", item.name),
      el("p", "item-meta", `${formatSize(item.received)} / ${formatSize(item.size)}（${Math.floor(ratio * 100)}%）`)
    );
    const progress = el("div", "progress");
    const bar = el("div", "progress-bar");
    // CSPで style 属性を禁止しているため、CSSOM で幅を変える
    bar.style.transform = `scaleX(${Math.min(ratio, 1)})`;
    progress.append(bar);
    row.append(main, progress);
    list.append(row);
  }
}

// ---------- 共有フォルダへの追加 ----------

function addToShare(files) {
  for (const file of files) {
    const item = { id: state.nextId++, file, status: "waiting", loaded: 0, message: "" };
    item.node = createQueueNode(item);
    state.queue.push(item);
    $("share-queue").append(item.node.root);
    renderQueueItem(item);
  }
  pump();
}

function createQueueNode(item) {
  const root = el("li", "item");
  const main = el("div");
  const head = el("div", "item-head");
  const status = el("span", "badge");
  head.append(status);
  const meta = el("p", "item-meta");
  main.append(head, el("p", "item-name", item.file.name), meta);
  const retry = el("button", "btn btn-sm btn-danger", "もう一度");
  retry.type = "button";
  retry.addEventListener("click", () => {
    item.status = "waiting";
    renderQueueItem(item);
    pump();
  });
  const progress = el("div", "progress");
  const bar = el("div", "progress-bar");
  progress.append(bar);
  root.append(main, retry, progress);
  return { root, status, meta, retry, bar };
}

function renderQueueItem(item) {
  const { root, status, meta, retry, bar } = item.node;
  const size = item.file.size;
  root.dataset.status = item.status;
  status.textContent = STATUS_LABELS[item.status];
  if (STATUS_TONES[item.status]) status.dataset.tone = STATUS_TONES[item.status];
  else delete status.dataset.tone;
  retry.hidden = item.status !== "failed";
  let ratio = 0;
  if (item.status === "uploading") {
    ratio = size > 0 ? item.loaded / size : 0;
    meta.textContent = `${formatSize(item.loaded)} / ${formatSize(size)}（${Math.floor(ratio * 100)}%）`;
  } else if (item.status === "done") {
    ratio = 1;
    meta.textContent = item.message;
  } else if (item.status === "failed") {
    meta.textContent = item.message;
  } else {
    meta.textContent = formatSize(size);
  }
  bar.style.transform = `scaleX(${Math.min(ratio, 1)})`;
}

async function pump() {
  if (state.running) return;
  state.running = true;
  try {
    let next;
    while ((next = state.queue.find((item) => item.status === "waiting"))) {
      await upload(next);
    }
  } finally {
    state.running = false;
  }
}

function upload(item) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    item.status = "uploading";
    item.loaded = 0;
    renderQueueItem(item);

    const finish = (status, message) => {
      item.status = status;
      item.message = message;
      renderQueueItem(item);
      if (status === "done") {
        loadFolder("share");
        // 一覧に加わったので、しばらくしたら進み具合の表示は消す
        setTimeout(() => {
          item.node.root.remove();
          state.queue = state.queue.filter((other) => other !== item);
        }, 2500);
      }
      resolve();
    };

    xhr.open("POST", `/pc/api/share?${query({ name: item.file.name })}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.addEventListener("progress", (event) => {
      item.loaded = event.loaded;
      renderQueueItem(item);
    });
    xhr.addEventListener("load", () => {
      let body = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // 応答が JSON でなければ、ステータスコードだけで判断する
      }
      if (xhr.status === 201) {
        const renamed = body.name && body.name !== item.file.name ? `「${body.name}」として` : "";
        finish("done", `${renamed}共有しました`);
      } else {
        finish("failed", body.detail || `追加できませんでした（${xhr.status}）`);
      }
    });
    xhr.addEventListener("error", () => finish("failed", "追加できませんでした。LanDropが動いているか確認してください。"));
    xhr.send(item.file);
  });
}

// ドロップされたもののうち、フォルダは送れないので除く
function filesFromDrop(dataTransfer) {
  const files = [];
  let folders = 0;
  for (const item of dataTransfer.items) {
    if (item.kind !== "file") continue;
    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    if (entry && entry.isDirectory) {
      folders += 1;
      continue;
    }
    const file = item.getAsFile();
    if (file) files.push(file);
  }
  if (folders > 0) toast("フォルダは送れません。中のファイルを選んでドロップしてください。", "danger");
  return files;
}

function setupDragAndDrop() {
  let depth = 0;
  const overlay = $("drop-overlay");
  const hasFiles = (event) => event.dataTransfer && Array.from(event.dataTransfer.types).includes("Files");
  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event) || state.stopped) return;
    event.preventDefault();
    depth += 1;
    overlay.hidden = false;
  });
  window.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = state.stopped ? "none" : "copy";
  });
  window.addEventListener("dragleave", () => {
    // 窓の外へ出たときやドラッグをやめたときは、種類を問わず数え直す
    if (overlay.hidden) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) overlay.hidden = true;
  });
  window.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    overlay.hidden = true;
    if (state.stopped) return;
    const files = filesFromDrop(event.dataTransfer);
    if (files.length === 0) return;
    selectTab("share");
    addToShare(files);
  });
}

// ---------- 画像と動画の表示 ----------

function openViewer(file) {
  const index = state.viewerFiles.findIndex((f) => f.name === file.name);
  if (index < 0) return;
  showInViewer(index);
  const viewer = $("viewer");
  if (!viewer.open) viewer.showModal();
}

function showInViewer(index) {
  state.viewerIndex = index;
  const file = state.viewerFiles[index];
  const media = $("viewer-media");
  media.replaceChildren();
  if (IMAGE_EXT.test(file.name)) {
    const img = el("img");
    img.src = previewUrl("received", file);
    img.alt = file.name;
    media.append(img);
  } else {
    const video = el("video");
    video.src = previewUrl("received", file);
    video.controls = true;
    video.preload = "metadata";
    media.append(video);
  }
  $("viewer-name").textContent = file.name;
  $("viewer-meta").textContent = `${formatSize(file.size)} ・ ${formatDate(file.modified)} ・ ${index + 1} / ${state.viewerFiles.length}`;
  $("viewer-prev").disabled = index === 0;
  $("viewer-next").disabled = index === state.viewerFiles.length - 1;
}

function moveViewer(step) {
  const next = state.viewerIndex + step;
  if (next >= 0 && next < state.viewerFiles.length) showInViewer(next);
}

function setupViewer() {
  const viewer = $("viewer");
  $("viewer-close").addEventListener("click", () => viewer.close());
  $("viewer-prev").addEventListener("click", () => moveViewer(-1));
  $("viewer-next").addEventListener("click", () => moveViewer(1));
  $("viewer-reveal").addEventListener("click", () => {
    const file = state.viewerFiles[state.viewerIndex];
    if (file) reveal("received", file.name);
  });
  viewer.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") moveViewer(-1);
    if (event.key === "ArrowRight") moveViewer(1);
  });
  // 背景のクリックで閉じる
  viewer.addEventListener("click", (event) => {
    if (event.target === viewer) viewer.close();
  });
  // 閉じたら動画の再生を止める
  viewer.addEventListener("close", () => $("viewer-media").replaceChildren());
}

// ---------- 停止 ----------

function setupStop() {
  const dialog = $("confirm-stop");
  $("stop").addEventListener("click", () => {
    $("confirm-text").textContent =
      state.incoming.length > 0
        ? "スマホから受信中のファイルがあります。停止すると、受信は中止されます。"
        : "停止すると、スマホからは接続できなくなります。";
    dialog.showModal();
  });
  $("confirm-cancel").addEventListener("click", () => dialog.close());
  $("confirm-stop-button").addEventListener("click", async () => {
    dialog.close();
    try {
      await api("/pc/api/stop", { method: "POST" });
    } catch {
      // すでに止まっている
    }
    showStopped("停止しました", "この画面は閉じてかまいません。");
    // 起動時に開いた窓なら、そのまま閉じる（閉じられないブラウザでは表示だけ残る）
    setTimeout(() => window.close(), 600);
  });
}

// ---------- 切り替え ----------

function selectTab(name) {
  state.tab = name;
  for (const tab of ["share", "received"]) {
    const selected = tab === name;
    $(`tab-${tab}`).setAttribute("aria-selected", String(selected));
    $(`panel-${tab}`).hidden = !selected;
  }
  if (name === "received") $("received-new").hidden = true;
}

function selectView(view) {
  state.view = view;
  for (const button of document.querySelectorAll(".view-toggle button")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
  try {
    localStorage.setItem("landrop.view", view);
  } catch {
    // 保存できなくても表示は切り替える
  }
  renderReceived();
}

// ---------- 初期化 ----------

document.addEventListener("DOMContentLoaded", () => {
  try {
    state.view = localStorage.getItem("landrop.view") === "list" ? "list" : "grid";
  } catch {
    state.view = "grid";
  }
  for (const button of document.querySelectorAll(".view-toggle button")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
    button.addEventListener("click", () => selectView(button.dataset.view));
  }
  $("tab-share").addEventListener("click", () => selectTab("share"));
  $("tab-received").addEventListener("click", () => selectTab("received"));
  $("share-input").addEventListener("change", (event) => {
    addToShare(Array.from(event.target.files));
    // 同じファイルをもう一度選べるようにする
    event.target.value = "";
  });
  for (const button of document.querySelectorAll("[data-open-folder]")) {
    button.addEventListener("click", () => openFolder(button.dataset.openFolder));
  }
  $("copy-url").addEventListener("click", async () => {
    if (!state.status) return;
    try {
      await navigator.clipboard.writeText(state.status.phone_url);
      toast("スマホ用のURLをコピーしました");
    } catch {
      toast("コピーできませんでした", "danger");
    }
  });
  setupDragAndDrop();
  setupViewer();
  setupStop();

  window.addEventListener("beforeunload", (event) => {
    if (state.queue.some((item) => item.status === "waiting" || item.status === "uploading")) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  loadStatus();
  loadFolder("share");
  loadFolder("received");
  holdConnection();
  setInterval(() => {
    if (state.stopped) return;
    loadFolder("share", true);
    loadFolder("received", true);
  }, POLL_MS);
  setInterval(() => {
    if (!state.stopped) loadStatus(true);
  }, STATUS_POLL_MS);
});
