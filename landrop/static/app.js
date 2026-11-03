// LanDrop スマホ側の画面の動作（ビルド工程なしの素のJavaScript。先に common.js を読み込む）
"use strict";

const STATUS_LABELS = {
  waiting: "送信待ち",
  uploading: "送信中",
  done: "完了",
  failed: "失敗",
};

// デザインシステムのバッジの色（ui.css の data-tone）
const STATUS_TONES = {
  uploading: "primary",
  done: "success",
  failed: "danger",
};

// 一覧の自動更新の間隔。自動更新は「操作」に数えないので、開いたままでも自動停止は効く
const AUTO_REFRESH_MS = 4000;

const state = {
  maxSize: Infinity,
  queue: [],
  running: false,
  nextId: 1,
  loadingFiles: false,
  filesSignature: "",
};

// 表示用の整形と要素の作成は common.js を使う

class SessionExpired extends Error {}

// ---------- サーバーとの通信 ----------

async function getJSON(path, auto = false) {
  let response;
  try {
    const headers = auto ? { "X-LanDrop-Auto": "1" } : {};
    response = await fetch(path, { cache: "no-store", credentials: "same-origin", headers });
  } catch {
    setOnline(false);
    throw new Error("PCに接続できません。LanDropが起動しているか、同じWi-Fiにつながっているか確認してください。");
  }
  setOnline(true);
  if (response.status === 401) throw new SessionExpired();
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `エラーが発生しました（${response.status}）`);
  return body;
}

function setOnline(online) {
  $("connection").classList.toggle("is-offline", !online);
}

function showExpired() {
  setOnline(false);
  $("expired").hidden = false;
}

async function loadInfo() {
  try {
    const info = await getJSON("/api/info");
    $("pc-name").textContent = info.pc_name;
    $("address").textContent = info.address;
    state.maxSize = info.max_size;
    $("max-size").textContent = ` ${formatSize(info.max_size)} `;
  } catch (error) {
    if (error instanceof SessionExpired) showExpired();
    else $("pc-name").textContent = "接続できません";
  }
}

// ---------- PC → スマホ ----------

async function loadFiles({ auto = false } = {}) {
  if (state.loadingFiles) return;
  state.loadingFiles = true;
  const status = $("files-status");
  const button = $("refresh");
  if (!auto) {
    button.disabled = true;
    status.classList.remove("is-error");
    status.textContent = "読み込んでいます…";
  }
  try {
    const { files } = await getJSON("/api/files", auto);
    const signature = JSON.stringify(files);
    // 自動更新では、変化がなければ描き直さない（スクロール位置や押しかけた操作を保つ）
    if (!auto || signature !== state.filesSignature || status.classList.contains("is-error")) {
      status.classList.remove("is-error");
      state.filesSignature = signature;
      renderFiles(files);
    }
  } catch (error) {
    if (error instanceof SessionExpired) {
      showExpired();
      status.textContent = "";
    } else {
      status.classList.add("is-error");
      status.textContent = error.message;
    }
  } finally {
    state.loadingFiles = false;
    button.disabled = false;
  }
}

function autoRefresh() {
  if (document.visibilityState !== "visible") return;
  if ($("panel-receive").hidden || !$("expired").hidden) return;
  loadFiles({ auto: true });
}

function renderFiles(files) {
  const list = $("file-list");
  const status = $("files-status");
  list.replaceChildren();
  if (files.length === 0) {
    status.textContent = "";
    const empty = el("li", "empty");
    empty.append(el("strong", "", "共有するファイルがありません"));
    empty.append(
      // --share で場所を変えられるため、フォルダ名ではなくPCの画面での操作を案内する
      document.createTextNode(
        "PCのLanDropの画面にファイルをドロップすると、ここに表示されます。"
      )
    );
    list.append(empty);
    return;
  }
  status.textContent = `${files.length}件のファイル`;
  for (const file of files) {
    const item = el("li");
    const link = el("a", "file");
    link.href = `/api/files/${encodeURIComponent(file.name)}`;
    link.setAttribute("download", "");
    link.setAttribute("aria-label", `${file.name} を保存`);
    link.append(el("span", "tile", extensionLabel(file.name)));
    const main = el("span", "file-main");
    main.append(el("span", "file-name", file.name));
    main.append(el("span", "file-meta", `${formatSize(file.size)} ・ ${formatDate(file.modified)}`));
    link.append(main);
    const save = el("span", "file-save");
    save.append(icon("M12 4v12m-5.5-5.5L12 16l5.5-5.5M5 20.5h14"), document.createTextNode("保存"));
    link.append(save);
    item.append(link);
    list.append(item);
  }
}

// ---------- スマホ → PC ----------

function addFiles(fileList) {
  for (const file of fileList) {
    const item = { id: state.nextId++, file, status: "waiting", loaded: 0, message: "", retryable: true };
    if (file.size > state.maxSize) {
      item.status = "failed";
      item.retryable = false;
      item.message = `上限（${formatSize(state.maxSize)}）を超えているため送れません。`;
    }
    item.node = createItemNode(item);
    state.queue.push(item);
    $("queue").append(item.node.root);
    renderItem(item);
  }
  renderSummary();
  pump();
}

function createItemNode(item) {
  const root = el("li", "item");
  const main = el("div");
  const head = el("div", "item-head");
  const status = el("span", "badge");
  head.append(status);
  const name = el("p", "item-name", item.file.name);
  const meta = el("p", "item-meta");
  main.append(head, name, meta);
  const retry = el("button", "btn btn-sm btn-danger", "再送");
  retry.type = "button";
  retry.setAttribute("aria-label", `${item.file.name} を再送`);
  retry.addEventListener("click", () => {
    item.status = "waiting";
    item.loaded = 0;
    item.message = "";
    renderItem(item);
    renderSummary();
    pump();
  });
  const progress = el("div", "progress");
  const bar = el("div", "progress-bar");
  progress.append(bar);
  root.append(main, retry, progress);
  return { root, status, meta, retry, bar };
}

function renderItem(item) {
  const { root, status, meta, retry, bar } = item.node;
  const size = item.file.size;
  root.dataset.status = item.status;
  status.textContent = STATUS_LABELS[item.status];
  if (STATUS_TONES[item.status]) status.dataset.tone = STATUS_TONES[item.status];
  else delete status.dataset.tone;
  retry.hidden = !(item.status === "failed" && item.retryable);
  let ratio = 0;
  if (item.status === "uploading") {
    ratio = size > 0 ? item.loaded / size : 0;
    meta.textContent = `${formatSize(item.loaded)} / ${formatSize(size)}（${Math.floor(ratio * 100)}%）`;
  } else if (item.status === "done") {
    ratio = 1;
    meta.textContent = item.message;
  } else if (item.status === "failed") {
    ratio = size > 0 ? item.loaded / size : 0;
    meta.textContent = item.message;
  } else {
    meta.textContent = formatSize(size);
  }
  // CSPで style 属性を禁止しているため、CSSOM で幅を変える
  bar.style.transform = `scaleX(${Math.min(ratio, 1)})`;
}

function renderSummary() {
  const counts = { waiting: 0, uploading: 0, done: 0, failed: 0 };
  for (const item of state.queue) counts[item.status] += 1;
  const total = state.queue.length;
  const pending = counts.waiting + counts.uploading;
  const parts = [];
  if (total > 0) parts.push(`${total}件中 ${counts.done}件 完了`);
  if (pending > 0) parts.push(`残り ${pending}件`);
  if (counts.failed > 0) parts.push(`失敗 ${counts.failed}件`);
  $("send-summary").textContent = parts.join(" ・ ");
  const badge = $("send-badge");
  badge.hidden = pending === 0;
  badge.textContent = String(pending);
  $("clear-done").hidden = counts.done === 0;
}

// 1ファイルずつ順番に送る
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
    renderSummary();
  }
}

function upload(item) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    item.status = "uploading";
    item.loaded = 0;
    item.message = "";
    renderItem(item);
    renderSummary();

    const finish = (status, message, retryable = true) => {
      item.status = status;
      item.message = message;
      item.retryable = retryable;
      renderItem(item);
      renderSummary();
      resolve();
    };

    xhr.open("POST", `/api/upload?name=${encodeURIComponent(item.file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.addEventListener("progress", (event) => {
      item.loaded = event.loaded;
      renderItem(item);
    });
    xhr.addEventListener("load", () => {
      let body = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // 応答が JSON でなければ、ステータスコードだけで判断する
      }
      if (xhr.status === 201) {
        item.loaded = item.file.size;
        const saved = body.name && body.name !== item.file.name ? `「${body.name}」として` : "";
        finish("done", `PCに${saved}保存しました`);
      } else if (xhr.status === 401) {
        finish("failed", "接続が切れました。QRコードを読み取り直してください。", false);
        showExpired();
      } else {
        const message = body.detail || `送信に失敗しました（${xhr.status}）`;
        finish("failed", message, xhr.status !== 413);
      }
    });
    xhr.addEventListener("error", () => {
      finish("failed", "通信が途切れました。PCとの接続や、PCの空き容量を確認して再送してください。");
    });
    xhr.addEventListener("abort", () => {
      finish("failed", "送信が中断されました。");
    });
    xhr.send(item.file);
  });
}

function clearDone() {
  state.queue = state.queue.filter((item) => {
    if (item.status !== "done") return true;
    item.node.root.remove();
    return false;
  });
  renderSummary();
}

// ---------- タブの切り替え ----------

function selectTab(name) {
  for (const tab of ["receive", "send"]) {
    const selected = tab === name;
    $(`tab-${tab}`).setAttribute("aria-selected", String(selected));
    $(`panel-${tab}`).hidden = !selected;
  }
  if (name === "receive") loadFiles();
  window.scrollTo({ top: 0 });
}

// ---------- 初期化 ----------

document.addEventListener("DOMContentLoaded", () => {
  $("tab-receive").addEventListener("click", () => selectTab("receive"));
  $("tab-send").addEventListener("click", () => selectTab("send"));
  $("refresh").addEventListener("click", () => loadFiles());
  $("clear-done").addEventListener("click", clearDone);
  $("file-input").addEventListener("change", (event) => {
    addFiles(Array.from(event.target.files));
    // 同じファイルをもう一度選べるようにする
    event.target.value = "";
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.queue.some((item) => item.status === "waiting" || item.status === "uploading")) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  document.addEventListener("visibilitychange", autoRefresh);
  setInterval(autoRefresh, AUTO_REFRESH_MS);
  loadInfo();
  loadFiles();
});
