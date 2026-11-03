// LanDrop スマホ画面とPC画面で共通に使う小さな関数（ビルド工程なしの素のJavaScript）
"use strict";

const $ = (id) => document.getElementById(id);

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "B";
  for (const next of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = next;
  }
  const digits = value < 10 ? 1 : 0;
  return `${value.toFixed(digits).replace(/\.0$/, "")} ${unit}`;
}

function formatDate(seconds) {
  return new Date(seconds * 1000).toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function extensionLabel(name) {
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "FILE";
  return name.slice(dot + 1, dot + 5).toUpperCase();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  // ファイル名は必ず textContent で入れる（HTMLとして解釈させない）
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(path) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", path);
  svg.append(p);
  return svg;
}
