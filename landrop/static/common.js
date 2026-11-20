"use strict";

const $ = (id) => document.getElementById(id);

const NOT_USER_ACTIVITY_HEADERS = { "X-LanDrop-Auto": "1" };

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

function el(tag, className, plainText) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (plainText !== undefined) node.textContent = plainText;
  return node;
}

function parseJsonOrEmpty(text) {
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

function setProgressBarRatio(bar, ratio) {
  bar.style.transform = `scaleX(${Math.min(ratio, 1)})`;
}

function clearFileInput(input) {
  input.value = "";
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
