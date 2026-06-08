let api = null;
let eventCursor = 0;
let pollTimer = null;
let monitorTimer = null;
let devnameRetryTimer = null;
let drawRaf = null;
let passwordPromptResolve = null;
let passwordPromptValidator = null;
let passwordPromptBusy = false;
let activeDialogDrag = null;
let firmwareCancelRequestPending = false;
let confirmDialogResolve = null;
const nativeAlert = (typeof window.alert === "function")
  ? window.alert.bind(window)
  : (msg) => console.warn(msg);
const nativeConfirm = (typeof window.confirm === "function")
  ? window.confirm.bind(window)
  : null;
const uiAlertQueue = [];
let uiAlertOpen = false;
let uiAlertActiveOnClose = null;

const bpRowsMax = 10;
const maxLogLines = 700;
const themeStorageKey = "servo_evdr_theme";
const cfgModeLimits = {
  "0-5V": [0, 5],
  "0-10V": [0, 10],
  "Resistance (0-10)": [0, 10],
  "0-20mA": [0, 20],
};
const graphColors = {
  voltage: "#57c4ff",
  current: "#e85656",
  setpoint: "#71d18a",
};
const graphThemeColors = {
  dark: {
    frame: "#2f3d4b",
    grid: "#1a2632",
    tickText: "#8397ab",
    axisText: "#9cb6cf",
    waitText: "#7f93a8",
    hoverText: "#eaf3ff",
    hoverBg: "#0f1a24",
  },
  light: {
    frame: "#93abc3",
    grid: "#d3dfeb",
    tickText: "#5b748d",
    axisText: "#46627d",
    waitText: "#6e849b",
    hoverText: "#1f3e5a",
    hoverBg: "#f8fbff",
  },
};
const graphHoverMaxDistancePx = 14;
const state = {
  connected: false,
  transport: "UART",
  monitorOn: false,
  monitorAwaitingRx: false,
  monitorLastSentAt: 0,
  points: [],
  trend: [],
  deviceId: "-",
  currentProfilePath: "",
  lastPoint: null,
  devUnlocked: false,
  firmwarePath: "",
  firmwareSizeBytes: 0,
  lastRxMonotonic: 0,
  pendingLoadUntil: 0,
  pendingLoadScope: "both",
  protocolWarnAt: {},
  firmwareUploadBusy: false,
  firmwareAuthPassword: "",
  autoLoadOnConnectBusy: false,
  linearityHover: null,
  theme: "dark",
};

const calibrationFieldMap = [
  ["MA_SCALE_FACTOR_4_20MA", "calMaScale"],
  ["RESISTANCE_SCALE_FACTOR", "calResistanceScale"],
  ["VOLTAGE_SCALE_FACTOR", "calVoltageScale"],
  ["FB_TO_CURRENT_FACTOR", "calFbToCurrent"],
  ["FB_OFFSET", "calFbOffset"],
];

const el = {
  screen: document.querySelector(".screen"),
  tabFiles: document.getElementById("tabFiles"),
  tabSettings: document.getElementById("tabSettings"),
  tabActions: document.getElementById("tabActions"),

  menuAdvanced: document.getElementById("menuAdvanced"),
  menuDeveloper: document.getElementById("menuDeveloper"),
  menuFirmware: document.getElementById("menuFirmware"),
  menuEventLog: document.getElementById("menuEventLog"),
  menuNewFile: document.getElementById("menuNewFile"),
  menuOpenFile: document.getElementById("menuOpenFile"),
  menuSaveFile: document.getElementById("menuSaveFile"),
  menuTheme: document.getElementById("menuTheme"),

  transportSelect: document.getElementById("transportSelect"),
  endpointLabel: document.getElementById("endpointLabel"),
  endpointSelect: document.getElementById("endpointSelect"),
  btnRefresh: document.getElementById("btnRefresh"),
  btnConnect: document.getElementById("btnConnect"),
  btnReadToggle: document.getElementById("btnReadToggle"),
  btnDeviceName: document.getElementById("btnDeviceName"),
  connState: document.getElementById("connState"),
  panelState: document.getElementById("panelState"),
  errorText: document.getElementById("errorText"),
  topConnState: document.getElementById("topConnState"),
  topDeviceId: document.getElementById("topDeviceId"),

  teleInput: document.getElementById("teleInput"),
  teleInputTitle: document.getElementById("teleInputTitle"),
  teleInputHint: document.getElementById("teleInputHint"),
  teleOutput: document.getElementById("teleOutput"),
  teleSat: document.getElementById("teleSat"),
  limitStatusCard: document.getElementById("limitStatusCard"),
  configPanel: document.getElementById("configPanel"),

  cfgMode: document.getElementById("cfgMode"),
  cfgInMin: document.getElementById("cfgInMin"),
  cfgInMax: document.getElementById("cfgInMax"),
  cfgOutMin: document.getElementById("cfgOutMin"),
  cfgOutMax: document.getElementById("cfgOutMax"),
  cfgInMinLabel: document.getElementById("cfgInMinLabel"),
  cfgInMaxLabel: document.getElementById("cfgInMaxLabel"),
  cfgOutMinLabel: document.getElementById("cfgOutMinLabel"),
  cfgOutMaxLabel: document.getElementById("cfgOutMaxLabel"),
  cfgUnit: document.getElementById("cfgUnit"),
  bpInputHead: document.getElementById("bpInputHead"),
  bpOutputHead: document.getElementById("bpOutputHead"),
  bpEmpty: document.getElementById("bpEmpty"),
  bpRows: document.getElementById("bpRows"),
  btnAddPoint: document.getElementById("btnAddPoint"),
  btnReadDevice: document.getElementById("btnReadDevice"),
  btnApplyCfg: document.getElementById("btnApplyCfg"),

  btnClearLog: document.getElementById("btnClearLog"),
  logBox: document.getElementById("logBox"),

  linearityCanvas: document.getElementById("linearityCanvas"),
  trendCanvas: document.getElementById("trendCanvas"),
  btnResetPlot: document.getElementById("btnResetPlot"),
  metaVolt: document.getElementById("metaVolt"),
  metaCurrent: document.getElementById("metaCurrent"),

  advancedDialog: document.getElementById("advancedDialog"),
  themeDialog: document.getElementById("themeDialog"),
  themeSelect: document.getElementById("themeSelect"),
  btnThemeApply: document.getElementById("btnThemeApply"),
  advKp: document.getElementById("advKp"),
  advKi: document.getElementById("advKi"),
  advDitherFreq: document.getElementById("advDitherFreq"),
  advDitherAmp: document.getElementById("advDitherAmp"),
  advPwmFreq: document.getElementById("advPwmFreq"),
  advDitherEnable: document.getElementById("advDitherEnable"),
  advDitherToggleWrap: document.getElementById("advDitherToggleWrap"),
  advDitherState: document.getElementById("advDitherState"),
  btnApplyAdv: document.getElementById("btnApplyAdv"),
  eventLogDialog: document.getElementById("eventLogDialog"),
  passwordPromptDialog: document.getElementById("passwordPromptDialog"),
  passwordPromptLabel: document.getElementById("passwordPromptLabel"),
  passwordPromptInput: document.getElementById("passwordPromptInput"),
  passwordPromptError: document.getElementById("passwordPromptError"),
  btnPasswordPromptOk: document.getElementById("btnPasswordPromptOk"),
  btnPasswordPromptCancel: document.getElementById("btnPasswordPromptCancel"),
  warningDialog: document.getElementById("warningDialog"),
  warningDialogTitle: document.getElementById("warningDialogTitle"),
  warningDialogIcon: document.getElementById("warningDialogIcon"),
  warningDialogMessage: document.getElementById("warningDialogMessage"),
  btnWarningDialogOk: document.getElementById("btnWarningDialogOk"),
  confirmDialog: document.getElementById("confirmDialog"),
  confirmDialogTitle: document.getElementById("confirmDialogTitle"),
  confirmDialogMessage: document.getElementById("confirmDialogMessage"),
  btnConfirmDialogYes: document.getElementById("btnConfirmDialogYes"),
  btnConfirmDialogNo: document.getElementById("btnConfirmDialogNo"),

  developerDialog: document.getElementById("developerDialog"),
  calMaScale: document.getElementById("calMaScale"),
  calResistanceScale: document.getElementById("calResistanceScale"),
  calVoltageScale: document.getElementById("calVoltageScale"),
  calFbToCurrent: document.getElementById("calFbToCurrent"),
  calFbOffset: document.getElementById("calFbOffset"),
  devStatusText: document.getElementById("devStatusText"),
  btnCalLoad: document.getElementById("btnCalLoad"),
  btnCalSave: document.getElementById("btnCalSave"),

  firmwareDialog: document.getElementById("firmwareDialog"),
  fwFilePath: document.getElementById("fwFilePath"),
  fwFileSize: document.getElementById("fwFileSize"),
  fwProgressTrack: document.getElementById("fwProgressTrack"),
  fwProgressFill: document.getElementById("fwProgressFill"),
  fwProgressText: document.getElementById("fwProgressText"),
  fwStatusText: document.getElementById("fwStatusText"),
  btnPickFirmware: document.getElementById("btnPickFirmware"),
  btnFirmwareUpload: document.getElementById("btnFirmwareUpload"),
  btnFirmwareCancel: document.getElementById("btnFirmwareCancel"),
  footerPort: document.getElementById("footerPort"),
  footerBaud: document.getElementById("footerBaud"),
  footerErr: document.getElementById("footerErr"),
  footerTime: document.getElementById("footerTime"),
};

function setActiveTopTab(tabName) {
  const name = String(tabName || "").toLowerCase();
  if (el.tabFiles) el.tabFiles.classList.toggle("active", name === "files");
  if (el.tabSettings) el.tabSettings.classList.toggle("active", name === "settings");
}

function showTabActions(groupName, anchorBtn = null) {
  if (!el.tabActions) return;
  setActiveTopTab(groupName);
  el.tabActions.classList.add("open");
  el.tabActions.setAttribute("aria-hidden", "false");
  const group = String(groupName || "").toLowerCase();
  for (const btn of el.tabActions.querySelectorAll("button")) {
    const btnGroup = String(btn.dataset.group || "").toLowerCase();
    btn.style.display = btnGroup === group ? "inline-flex" : "none";
  }
  if (anchorBtn && el.screen) {
    const screenRect = el.screen.getBoundingClientRect();
    const btnRect = anchorBtn.getBoundingClientRect();
    const top = Math.round(btnRect.bottom - screenRect.top + 8);
    let left = Math.round(btnRect.left - screenRect.left);
    const menuRect = el.tabActions.getBoundingClientRect();
    const maxLeft = Math.max(12, Math.floor(screenRect.width - menuRect.width - 12));
    if (left < 12) left = 12;
    if (left > maxLeft) left = maxLeft;
    el.tabActions.style.top = `${top}px`;
    el.tabActions.style.left = `${left}px`;
  }
}

function hideTabActions() {
  if (!el.tabActions) return;
  el.tabActions.classList.remove("open");
  el.tabActions.setAttribute("aria-hidden", "true");
}

function clampValue(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function setDialogPosition(dialog, left, top) {
  if (!dialog) return;
  const rect = dialog.getBoundingClientRect();
  const margin = 6;
  const maxLeft = Math.max(margin, Math.floor(window.innerWidth - rect.width - margin));
  const maxTop = Math.max(margin, Math.floor(window.innerHeight - rect.height - margin));
  const nextLeft = clampValue(Math.round(left), margin, maxLeft);
  const nextTop = clampValue(Math.round(top), margin, maxTop);
  dialog.style.margin = "0";
  dialog.style.left = `${nextLeft}px`;
  dialog.style.top = `${nextTop}px`;
}

function stopDialogDrag() {
  if (!activeDialogDrag) return;
  const dialog = activeDialogDrag.dialog;
  if (dialog) dialog.classList.remove("dragging-dialog");
  activeDialogDrag = null;
  window.removeEventListener("mousemove", onDialogDragMove);
  window.removeEventListener("mouseup", stopDialogDrag);
}

function onDialogDragMove(ev) {
  if (!activeDialogDrag) return;
  const { dialog, offsetX, offsetY } = activeDialogDrag;
  setDialogPosition(dialog, ev.clientX - offsetX, ev.clientY - offsetY);
}

function startDialogDrag(ev, dialog) {
  if (!dialog || ev.button !== 0) return;
  if (ev.target.closest("button, input, select, textarea, a, label")) return;
  ev.preventDefault();
  const rect = dialog.getBoundingClientRect();
  activeDialogDrag = {
    dialog,
    offsetX: ev.clientX - rect.left,
    offsetY: ev.clientY - rect.top,
  };
  dialog.classList.add("dragging-dialog");
  dialog.style.margin = "0";
  dialog.style.left = `${Math.round(rect.left)}px`;
  dialog.style.top = `${Math.round(rect.top)}px`;
  window.addEventListener("mousemove", onDialogDragMove);
  window.addEventListener("mouseup", stopDialogDrag);
}

function resetDialogPosition(dialog) {
  if (!dialog) return;
  dialog.style.left = "";
  dialog.style.top = "";
  dialog.style.margin = "";
}

function initDraggableDialog(dialog) {
  if (!dialog || dialog.dataset.dragInit === "1") return;
  const handle = dialog.querySelector("h2, .row-between");
  if (!handle) return;
  handle.classList.add("dialog-drag-handle");
  if (!handle.getAttribute("title")) {
    handle.setAttribute("title", "Drag to move dialog");
  }
  handle.addEventListener("mousedown", (ev) => startDialogDrag(ev, dialog));
  handle.addEventListener("dblclick", (ev) => {
    if (ev.target.closest("button, input, select, textarea, a, label")) return;
    resetDialogPosition(dialog);
  });
  dialog.addEventListener("close", () => {
    if (activeDialogDrag?.dialog === dialog) stopDialogDrag();
  });
  dialog.dataset.dragInit = "1";
}

function toggleTabActions(groupName, anchorBtn = null) {
  if (!el.tabActions) return;
  const shouldOpen = !el.tabActions.classList.contains("open");
  const visibleBtn = el.tabActions.querySelector("button[style*='inline-flex']");
  const visibleGroup = String(visibleBtn?.dataset.group || "").toLowerCase();
  const requestedGroup = String(groupName || "").toLowerCase();
  if (shouldOpen || visibleGroup !== requestedGroup) {
    showTabActions(requestedGroup, anchorBtn);
  } else {
    hideTabActions();
  }
}

function logLine(direction, text, ts = null) {
  const row = document.createElement("div");
  row.className = "log-line";
  const dir = String(direction || "SYS").toUpperCase();
  if (dir === "TX") row.classList.add("log-tx");
  else if (dir === "RX") row.classList.add("log-rx");
  else if (dir === "ERR") row.classList.add("log-err");
  else row.classList.add("log-sys");
  const stamp = ts || new Date().toLocaleTimeString("en-GB", { hour12: false });
  row.textContent = `[${stamp}] ${dir}: ${text}`;
  el.logBox.appendChild(row);
  while (el.logBox.childElementCount > maxLogLines) {
    el.logBox.removeChild(el.logBox.firstElementChild);
  }
  el.logBox.scrollTop = el.logBox.scrollHeight;
}

function parseNum(v) {
  if (v === null || v === undefined) return null;
  const m = String(v).match(/[-+]?\d*\.?\d+/);
  if (!m) return null;
  const n = Number(m[0]);
  return Number.isFinite(n) ? n : null;
}

function normalizeThemeName(themeValue) {
  const txt = String(themeValue || "").trim().toLowerCase();
  return txt === "light" ? "light" : "dark";
}

function applyTheme(themeValue, persist = true) {
  const nextTheme = normalizeThemeName(themeValue);
  state.theme = nextTheme;
  document.body.classList.toggle("theme-light", nextTheme === "light");
  document.body.classList.toggle("theme-dark", nextTheme !== "light");
  document.documentElement.style.colorScheme = nextTheme;
  if (el.themeSelect) el.themeSelect.value = nextTheme;
  scheduleDrawGraphs();
  if (persist) {
    try {
      window.localStorage.setItem(themeStorageKey, nextTheme);
    } catch (_err) {
      // Ignore storage errors in restricted environments.
    }
  }
}

function loadThemePreference() {
  let savedTheme = "dark";
  try {
    savedTheme = normalizeThemeName(window.localStorage.getItem(themeStorageKey) || "dark");
  } catch (_err) {
    savedTheme = "dark";
  }
  applyTheme(savedTheme, false);
}

function getGraphUiColors() {
  return state.theme === "light" ? graphThemeColors.light : graphThemeColors.dark;
}

function calcRange(values, fallbackMin, fallbackMax) {
  const finite = values.filter((value) => Number.isFinite(value));
  let lo = Number(fallbackMin);
  let hi = Number(fallbackMax);
  if (finite.length > 0) {
    lo = Math.min(lo, ...finite);
    hi = Math.max(hi, ...finite);
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo >= hi) return [0, 1];
  if (Math.abs(hi - lo) < 1e-9) hi = lo + 1;
  return [lo, hi];
}

function resolveInputModePresentation(modeValue = null) {
  const mode = String(modeValue ?? el.cfgMode?.value ?? "").trim().toUpperCase();
  if (mode.includes("RESIST")) {
    return {
      unit: "k\u03A9",
      title: "INPUT SIGNAL (k\u03A9)",
      hint: "Input Resistance",
      axis: "Input Resistance (k\u03A9)",
    };
  }
  if (mode.includes("MA")) {
    return {
      unit: "mA",
      title: "INPUT SIGNAL (mA)",
      hint: "Input Current",
      axis: "Input Current (mA)",
    };
  }
  if (mode.includes("V")) {
    return {
      unit: "V",
      title: "INPUT SIGNAL (V)",
      hint: "Input Voltage",
      axis: "Input Voltage (V)",
    };
  }
  return {
    unit: "",
    title: "INPUT SIGNAL",
    hint: "Input Measurement",
    axis: "Input",
  };
}

function getInputSignalModeInfo(modeValue = null) {
  const info = resolveInputModePresentation(modeValue);
  return { unit: info.unit, title: info.title, hint: info.hint };
}

function formatInputSignalValue(value, unit = "") {
  const txt = String(value ?? "").trim();
  if (!txt || txt === "-" || txt === "--") return "--";
  if (!unit) return txt;
  const upper = txt.toUpperCase();
  if (upper.includes("V") || upper.includes("MA") || upper.includes("OHM") || txt.includes("\u03A9")) {
    return txt;
  }
  const parsed = parseNum(txt);
  if (parsed === null) return txt;
  return `${txt} ${unit}`;
}

function forceOutputUnitCurrent() {
  if (el.cfgUnit) el.cfgUnit.value = "mA";
  return "mA";
}

function getMeasuredOutputUnit() {
  return forceOutputUnitCurrent();
}

function formatMeasuredOutputValue(value, unit = "mA") {
  const txt = String(value ?? "").trim();
  if (!txt || txt === "-" || txt === "--") return "--";
  if (!unit) return txt;
  if (/[A-Za-z\u03A9]/.test(txt)) return txt;
  const parsed = parseNum(txt);
  if (parsed === null) return txt;
  return `${txt} ${unit}`;
}

function updateInputSignalCardPresentation(modeValue = null) {
  const info = getInputSignalModeInfo(modeValue);
  if (el.teleInputTitle) el.teleInputTitle.textContent = info.title;
  if (el.teleInputHint) el.teleInputHint.textContent = info.hint;

  const inputUnitSuffix = info.unit ? ` (${info.unit})` : "";
  if (el.cfgInMinLabel) el.cfgInMinLabel.textContent = `Input Minimum${inputUnitSuffix}`;
  if (el.cfgInMaxLabel) el.cfgInMaxLabel.textContent = `Input Maximum${inputUnitSuffix}`;

  const outputUnit = getMeasuredOutputUnit();
  const outputUnitSuffix = outputUnit ? ` (${outputUnit})` : "";
  if (el.cfgOutMinLabel) el.cfgOutMinLabel.textContent = `Output Minimum${outputUnitSuffix}`;
  if (el.cfgOutMaxLabel) el.cfgOutMaxLabel.textContent = `Output Maximum${outputUnitSuffix}`;
  if (el.bpInputHead) el.bpInputHead.textContent = `Input${inputUnitSuffix}`;
  if (el.bpOutputHead) el.bpOutputHead.textContent = `Output${outputUnitSuffix}`;

  return info;
}

function getGraphInputAxisLabel() {
  return resolveInputModePresentation().axis;
}

function getGraphOutputAxisLabel() {
  const outputUnit = String(getMeasuredOutputUnit() || "").trim();
  return outputUnit ? `Measured Output (${outputUnit})` : "Measured Output";
}

function getLinearityReferencePoints(inMin, inMax, outMin, outMax) {
  const points = [];
  for (const row of el.bpRows.querySelectorAll(".bp-row")) {
    const pointX = parseNum(row.querySelector(".bp-in").value);
    const pointY = parseNum(row.querySelector(".bp-out").value);
    if (pointX !== null && pointY !== null) points.push([pointX, pointY]);
  }

  const eps = 1e-9;
  const hasMinPoint = points.some(([x]) => Math.abs(x - inMin) < eps);
  const hasMaxPoint = points.some(([x]) => Math.abs(x - inMax) < eps);
  if (!hasMinPoint) points.push([inMin, outMin]);
  if (!hasMaxPoint) points.push([inMax, outMax]);
  points.sort((a, b) => a[0] - b[0]);

  const unique = [];
  for (const [x, y] of points) {
    if (!unique.length || Math.abs(unique[unique.length - 1][0] - x) >= eps) {
      unique.push([x, y]);
    }
  }
  return unique;
}

function interpolateCurveY(points, xValue) {
  if (!Array.isArray(points) || points.length === 0) return null;
  const x = Number(xValue);
  if (!Number.isFinite(x)) return null;

  if (x <= Number(points[0][0])) return Number(points[0][1]);
  if (x >= Number(points[points.length - 1][0])) return Number(points[points.length - 1][1]);

  for (let i = 1; i < points.length; i++) {
    const x0 = Number(points[i - 1][0]);
    const y0 = Number(points[i - 1][1]);
    const x1 = Number(points[i][0]);
    const y1 = Number(points[i][1]);
    if (x <= x1) {
      if (Math.abs(x1 - x0) < 1e-12) return y1;
      const t = (x - x0) / (x1 - x0);
      return y0 + ((y1 - y0) * t);
    }
  }

  return Number(points[points.length - 1][1]);
}

function getCanvasMousePoint(canvas, event) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
  };
}

function onLinearityMouseMove(event) {
  if (!el.linearityCanvas) return;
  state.linearityHover = getCanvasMousePoint(el.linearityCanvas, event);
  drawLinearityCanvas();
}

function onLinearityMouseLeave() {
  state.linearityHover = null;
  drawLinearityCanvas();
}

function scheduleDrawGraphs() {
  if (drawRaf) return;
  drawRaf = window.requestAnimationFrame(() => {
    drawRaf = null;
    drawGraphs();
  });
}

function normalizeProfileField(value) {
  const txt = String(value ?? "").trim();
  if (!txt || txt === "-" || txt.toLowerCase() === "none" || txt.toLowerCase() === "null") {
    return "";
  }
  return txt;
}

function setSelectValueOrAppend(selectEl, value) {
  if (!selectEl) return;
  const txt = normalizeProfileField(value);
  if (!txt) return;
  const has = [...selectEl.options].some((opt) => opt.value === txt || opt.textContent === txt);
  if (!has) {
    const opt = document.createElement("option");
    opt.value = txt;
    opt.textContent = txt;
    selectEl.appendChild(opt);
  }
  selectEl.value = txt;
}

function syncDitherToggleState() {
  if (!el.advDitherEnable) return;
  const on = !!el.advDitherEnable.checked;
  if (el.advDitherToggleWrap) {
    el.advDitherToggleWrap.classList.toggle("on", on);
  }
  if (el.advDitherState) {
    el.advDitherState.textContent = on ? "ON" : "OFF";
  }
}

function setDeveloperStatus(text, isError = false) {
  if (!el.devStatusText) return;
  el.devStatusText.textContent = String(text || "");
  el.devStatusText.style.color = isError ? "#ff9d9d" : "";
}

function setFirmwareStatus(text, isError = false) {
  if (!el.fwStatusText) return;
  el.fwStatusText.textContent = String(text || "");
  el.fwStatusText.style.color = isError ? "#ff9d9d" : "";
}

function setFirmwareUploadBusy(busy) {
  const next = !!busy;
  state.firmwareUploadBusy = next;
  if (el.btnFirmwareUpload) el.btnFirmwareUpload.disabled = next;
  if (el.btnPickFirmware) el.btnPickFirmware.disabled = next;
  if (el.btnFirmwareCancel) el.btnFirmwareCancel.disabled = firmwareCancelRequestPending;
}

function clearFirmwareAuthPassword() {
  state.firmwareAuthPassword = "";
}

function syncFirmwareFileSelectionUi() {
  const path = String(state.firmwarePath || "").trim();
  const size = Math.max(0, Number(state.firmwareSizeBytes) || 0);
  if (el.fwFilePath) {
    el.fwFilePath.value = path ? fileNameFromPath(path) : "";
    el.fwFilePath.title = path;
  }
  if (el.fwFileSize) {
    el.fwFileSize.value = size > 0 ? formatBytes(size) : "-";
  }
}

function clearFirmwareFileSelection() {
  state.firmwarePath = "";
  state.firmwareSizeBytes = 0;
  syncFirmwareFileSelectionUi();
  setFirmwareProgress(0, 0, 0, false);
}

function clearFirmwareSession(statusText = "Ready") {
  setFirmwareUploadBusy(false);
  clearFirmwareAuthPassword();
  clearFirmwareFileSelection();
  setFirmwareStatus(statusText);
}

function finishConfirmDialog(result) {
  const resolve = confirmDialogResolve;
  confirmDialogResolve = null;
  if (el.confirmDialog?.open) {
    el.confirmDialog.close();
  }
  if (typeof resolve === "function") {
    resolve(!!result);
  }
}

function askConfirmDialog(message, title = "Warning") {
  const text = String(message || "").trim();
  if (!el.confirmDialog || !el.confirmDialogMessage || typeof el.confirmDialog.showModal !== "function") {
    if (nativeConfirm) return Promise.resolve(nativeConfirm(text));
    nativeAlert(text);
    return Promise.resolve(true);
  }
  if (typeof confirmDialogResolve === "function") {
    confirmDialogResolve(false);
    confirmDialogResolve = null;
  }
  if (el.confirmDialogTitle) el.confirmDialogTitle.textContent = String(title || "Warning");
  el.confirmDialogMessage.textContent = text;
  if (!el.confirmDialog.open) el.confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmDialogResolve = resolve;
  });
}

async function confirmFirmwareCancelClose() {
  const question =
    "Are you sure you want to cancel firmware update?\n" +
    "Selected BIN and password session will be cleared.";
  return await askConfirmDialog(question, "Warning");
}

function cancelFirmwareDialogSession() {
  clearFirmwareSession("Firmware update cancelled");
  if (el.firmwareDialog?.open) {
    el.firmwareDialog.close();
  }
}

function getFirmwareDoNotCloseWarning() {
  return "Firmware update is in progress.\nDo not close this tab/window.\nIf closed, upload will stop and firmware update may fail.";
}

function handleFirmwareBeforeUnload(event) {
  if (!state.firmwareUploadBusy) return;
  const warning = getFirmwareDoNotCloseWarning();
  event.preventDefault();
  event.returnValue = warning;
  return warning;
}

function normalizeFirmwarePercent(percent = 0, sentBytes = 0, totalBytes = 0) {
  const total = Number(totalBytes);
  const sent = Number(sentBytes);
  if (Number.isFinite(total) && total > 0 && Number.isFinite(sent)) {
    const clampedSent = Math.max(0, Math.min(total, sent));
    return (clampedSent * 100.0) / total;
  }

  let pct = Number(percent);
  if (!Number.isFinite(pct)) return 0;
  if (pct >= 0 && pct <= 1) pct *= 100.0;
  return pct;
}

function formatFirmwarePercentLabel(percent = 0) {
  const pct = Math.max(0, Math.min(100, Number(percent) || 0));
  if (pct >= 100) return "100%";
  if (pct <= 0) return "0%";
  return `${pct.toFixed(1)}%`;
}

function normalizeFirmwareUploadState(stateName = "") {
  return String(stateName || "").trim().toLowerCase();
}

function firmwareStatusTextFromState(stateName, percent = 0) {
  const stateText = normalizeFirmwareUploadState(stateName);
  const pctLabel = formatFirmwarePercentLabel(percent);
  switch (stateText) {
    case "validating":
      return { text: "Validating firmware BIN...", isError: false };
    case "authorizing":
      return { text: "Authorizing firmware update...", isError: false };
    case "entering_fw_mode":
      return { text: "Entering firmware update mode...", isError: false };
    case "starting_transfer":
      return { text: "Starting firmware transfer...", isError: false };
    case "uploading":
      return { text: `Uploading firmware... ${pctLabel}`, isError: false };
    case "finalizing":
      return { text: "Finalizing firmware update...", isError: false };
    case "cancelling":
      return { text: `Cancelling firmware upload... ${pctLabel}`, isError: false };
    case "cancelled":
      return { text: "Firmware upload cancelled.", isError: false };
    case "completed":
      return { text: "Firmware upload completed. Device reboot pending.", isError: false };
    case "failed":
      return { text: "Firmware upload failed. Check error/event log.", isError: true };
    default:
      return null;
  }
}

function setFirmwareProgress(percent = 0, sentBytes = 0, totalBytes = 0, active = false) {
  const sent = Math.max(0, Math.floor(Number(sentBytes) || 0));
  const total = Math.max(0, Math.floor(Number(totalBytes) || 0));
  const pct = Math.max(0, Math.min(100, normalizeFirmwarePercent(percent, sent, total)));
  const pctLabel = formatFirmwarePercentLabel(pct);

  if (el.fwProgressFill) el.fwProgressFill.style.width = `${pct}%`;
  if (el.fwProgressTrack) {
    el.fwProgressTrack.setAttribute("aria-valuenow", String(Math.round(pct * 10) / 10));
    el.fwProgressTrack.setAttribute("aria-valuetext", pctLabel);
    el.fwProgressTrack.classList.toggle("active", !!active);
  }
  if (el.fwProgressText) {
    if (total > 0) {
      el.fwProgressText.textContent = `${pctLabel} (${formatBytes(sent)} / ${formatBytes(total)})`;
    } else {
      el.fwProgressText.textContent = pctLabel;
    }
  }
}

function updateFirmwareProgressUi(snapshot) {
  const info = snapshot?.firmware_upload || {};
  const total = Number(info.total_bytes || 0);
  const sent = Number(info.sent_bytes || 0);
  const percent = normalizeFirmwarePercent(info.percent, sent, total);
  const active = !!info.active;
  const hasSelectedFile = !!String(state.firmwarePath || "").trim();
  // Avoid stale size/progress when no BIN is selected and no active upload.
  if (!active && !hasSelectedFile) {
    state.firmwareSizeBytes = 0;
    setFirmwareProgress(0, 0, 0, false);
    if (el.fwFileSize) el.fwFileSize.value = "-";
  } else {
    setFirmwareProgress(percent, sent, total, active);
    if (total > 0) {
      state.firmwareSizeBytes = Math.max(0, Math.floor(total));
      if (el.fwFileSize) {
        el.fwFileSize.value = formatBytes(total);
      }
    }
  }
  const statusInfo = firmwareStatusTextFromState(info.state, percent);
  if (statusInfo) {
    setFirmwareStatus(statusInfo.text, statusInfo.isError);
  }
}

function clearPasswordPromptError() {
  if (!el.passwordPromptError) return;
  el.passwordPromptError.textContent = "";
  el.passwordPromptError.style.color = "";
}

function showPasswordPromptInfo(text) {
  if (!el.passwordPromptError) return;
  el.passwordPromptError.textContent = String(text || "");
  el.passwordPromptError.style.color = "";
}

function showPasswordPromptError(text) {
  if (!el.passwordPromptError) return;
  el.passwordPromptError.textContent = String(text || "");
  el.passwordPromptError.style.color = "#ff9d9d";
}

function showPasswordPromptSuccess(text) {
  if (!el.passwordPromptError) return;
  el.passwordPromptError.textContent = String(text || "Password correct");
  el.passwordPromptError.style.color = "#6fe3a3";
}

function setPasswordPromptBusy(busy) {
  passwordPromptBusy = !!busy;
  if (el.passwordPromptInput) el.passwordPromptInput.disabled = passwordPromptBusy;
  if (el.btnPasswordPromptOk) el.btnPasswordPromptOk.disabled = passwordPromptBusy;
  if (el.btnPasswordPromptCancel) el.btnPasswordPromptCancel.disabled = passwordPromptBusy;
}

function finishPasswordPrompt(result) {
  const resolve = passwordPromptResolve;
  passwordPromptResolve = null;
  passwordPromptValidator = null;
  setPasswordPromptBusy(false);
  if (el.passwordPromptDialog?.open) {
    el.passwordPromptDialog.close();
  }
  if (typeof resolve === "function") {
    resolve(result);
  }
}

async function submitPasswordPrompt() {
  if (passwordPromptBusy) return;
  const pwd = String(el.passwordPromptInput?.value || "").trim();
  if (!pwd) {
    showPasswordPromptError("Password required");
    el.passwordPromptInput?.focus();
    return;
  }

  if (typeof passwordPromptValidator !== "function") {
    finishPasswordPrompt(pwd);
    return;
  }

  const activeResolve = passwordPromptResolve;
  setPasswordPromptBusy(true);
  showPasswordPromptInfo("Checking password...");
  try {
    const check = await passwordPromptValidator(pwd);
    if (passwordPromptResolve !== activeResolve || typeof passwordPromptResolve !== "function") return;
    if (check?.ok) {
      showPasswordPromptSuccess(check?.success || "Password correct");
      const value = (check && Object.prototype.hasOwnProperty.call(check, "value")) ? check.value : pwd;
      window.setTimeout(() => finishPasswordPrompt(value), 360);
      return;
    }
    showPasswordPromptError(check?.error || "Wrong password");
    setPasswordPromptBusy(false);
    el.passwordPromptInput?.focus();
    el.passwordPromptInput?.select();
  } catch (err) {
    if (passwordPromptResolve !== activeResolve || typeof passwordPromptResolve !== "function") return;
    showPasswordPromptError(err?.message || "Password check failed");
    setPasswordPromptBusy(false);
    el.passwordPromptInput?.focus();
    el.passwordPromptInput?.select();
  }
}

async function askPassword(label, validator = null) {
  if (!el.passwordPromptDialog || !el.passwordPromptInput || !el.passwordPromptLabel) {
    const entered = window.prompt(String(label || "Enter password:"), "");
    if (entered === null) return null;
    const pwd = String(entered || "").trim();
    if (typeof validator === "function") {
      const check = await validator(pwd);
      if (!check?.ok) {
        return null;
      }
      return (check && Object.prototype.hasOwnProperty.call(check, "value")) ? check.value : pwd;
    }
    return pwd || "";
  }

  if (typeof passwordPromptResolve === "function") {
    passwordPromptResolve(null);
    passwordPromptResolve = null;
  }

  passwordPromptValidator = (typeof validator === "function") ? validator : null;
  el.passwordPromptLabel.textContent = String(label || "Enter password:");
  el.passwordPromptInput.value = "";
  clearPasswordPromptError();
  setPasswordPromptBusy(false);
  el.passwordPromptDialog.showModal();
  window.setTimeout(() => {
    el.passwordPromptInput?.focus();
    el.passwordPromptInput?.select();
  }, 0);

  return new Promise((resolve) => {
    passwordPromptResolve = resolve;
  });
}

function formatBytes(size) {
  const n = Number(size);
  if (!Number.isFinite(n) || n < 0) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function fileNameFromPath(pathText) {
  const txt = String(pathText || "").trim();
  if (!txt) return "";
  const normalized = txt.replace(/\\/g, "/");
  const idx = normalized.lastIndexOf("/");
  return idx >= 0 ? normalized.slice(idx + 1) : normalized;
}

function readCalibrationFromUi() {
  const payload = {};
  for (const [fieldKey, inputId] of calibrationFieldMap) {
    const input = el[inputId];
    const txt = String(input?.value || "").trim();
    const num = Number(txt);
    if (!txt || !Number.isFinite(num)) {
      throw new Error(`Invalid value for ${fieldKey}`);
    }
    payload[fieldKey] = num;
  }
  return payload;
}

function writeCalibrationToUi(calibration) {
  for (const [fieldKey, inputId] of calibrationFieldMap) {
    const input = el[inputId];
    if (!input) continue;
    const value = calibration?.[fieldKey];
    input.value = value === undefined || value === null ? "" : String(value);
  }
}

function resetDeveloperUi() {
  state.devUnlocked = false;
  setDeveloperStatus("Session locked");
}

function inferAlertLevel(levelHint, messageText) {
  const hinted = String(levelHint || "").trim().toLowerCase();
  if (hinted === "error" || hinted === "warning") return hinted;
  const txt = String(messageText || "").toLowerCase();
  if (/(error|failed|invalid|unable|cannot|can't|denied|disconnect)/.test(txt)) {
    return "error";
  }
  return "warning";
}

function syncAlertDialogVisuals(level) {
  const tone = inferAlertLevel(level, "");
  if (el.warningDialog) el.warningDialog.dataset.level = tone;
  if (el.warningDialogTitle) {
    el.warningDialogTitle.textContent = tone === "error" ? "Error" : "Warning";
  }
  if (el.warningDialogIcon) {
    el.warningDialogIcon.className = `warning-icon ${tone}`;
  }
}

function closeWarningDialog() {
  if (el.warningDialog?.open) el.warningDialog.close();
}

function finishUiAlertAndContinue() {
  const done = uiAlertActiveOnClose;
  uiAlertActiveOnClose = null;
  uiAlertOpen = false;
  if (typeof done === "function") {
    done();
  }
  showNextQueuedAlert();
}

function showNextQueuedAlert() {
  if (uiAlertOpen || uiAlertQueue.length === 0) return;
  const next = uiAlertQueue.shift();
  const message = String(next?.message ?? "").trim();
  const level = inferAlertLevel(next?.level, message);
  const onClose = (typeof next?.onClose === "function") ? next.onClose : null;
  if (!message) {
    if (onClose) onClose();
    showNextQueuedAlert();
    return;
  }
  if (!el.warningDialog || !el.warningDialogMessage || typeof el.warningDialog.showModal !== "function") {
    nativeAlert(message);
    if (onClose) onClose();
    showNextQueuedAlert();
    return;
  }
  syncAlertDialogVisuals(level);
  el.warningDialogMessage.textContent = message;
  uiAlertActiveOnClose = onClose;
  uiAlertOpen = true;
  if (!el.warningDialog.open) el.warningDialog.showModal();
}

function appAlert(message, level = "auto") {
  uiAlertQueue.push({ message: String(message ?? ""), level, onClose: null });
  showNextQueuedAlert();
}

function appAlertAsync(message, level = "auto") {
  return new Promise((resolve) => {
    uiAlertQueue.push({
      message: String(message ?? ""),
      level,
      onClose: resolve,
    });
    showNextQueuedAlert();
  });
}

function confirmWithFallback(message) {
  if (nativeConfirm) {
    return nativeConfirm(String(message ?? ""));
  }
  nativeAlert(String(message ?? ""));
  return true;
}

function startFirmwareUploadWarningText() {
  return (
    "Firmware update is starting now.\n" +
    "Do not close this tab/window until upload completes.\n" +
    "If closed, upload will stop and update may fail.\n\n" +
    "Click OK to start upload."
  );
}


function getFirmwareUploadStartWarning() {
  return startFirmwareUploadWarningText();
}

async function handleFirmwareDialogCloseAttempt() {
  if (!(await confirmFirmwareCancelClose())) {
    return false;
  }
  if (state.firmwareUploadBusy && api && typeof api.firmware_cancel_upload === "function") {
    if (firmwareCancelRequestPending) return false;
    firmwareCancelRequestPending = true;
    if (el.btnFirmwareCancel) el.btnFirmwareCancel.disabled = true;
    setFirmwareStatus("Cancelling firmware upload...");
    logLine("SYS", "Firmware upload cancel requested by user.");
    try {
      await api.firmware_cancel_upload();
    } catch (_cancelErr) {
      // Upload thread may complete on its own; ignore cancel command race errors.
    } finally {
      firmwareCancelRequestPending = false;
    }
  }
  cancelFirmwareDialogSession();
  return true;
}

function warnOnce(key, message, cooldownMs = 2500) {
  const now = Date.now();
  const prev = Number(state.protocolWarnAt?.[key] || 0);
  if ((now - prev) < cooldownMs) return;
  state.protocolWarnAt[key] = now;
  alert(message);
}

function ensureNotMonitoring(actionLabel, autoStop = false) {
  if (!state.monitorOn) return true;
  if (!autoStop) {
    alert(`Stop Monitoring first, then ${actionLabel}.`);
    return false;
  }
  stopMonitoring();
  logLine("SYS", `Monitoring stopped before ${actionLabel}.`);
  return true;
}

function applyCfgFromDevice(cfg) {
  if (!cfg || typeof cfg !== "object") return;
  const mode = String(cfg.mode || "").trim();
  if (mode) setSelectValueOrAppend(el.cfgMode, mode);

  const inputVals = Array.isArray(cfg.input) ? cfg.input : [];
  const outputVals = Array.isArray(cfg.output) ? cfg.output : [];
  if (inputVals.length > 0) el.cfgInMin.value = String(inputVals[0]);
  if (inputVals.length > 1) el.cfgInMax.value = String(inputVals[1]);
  if (outputVals.length > 0) el.cfgOutMin.value = String(outputVals[0]);
  if (outputVals.length > 1) el.cfgOutMax.value = String(outputVals[1]);
  forceOutputUnitCurrent();

  el.bpRows.innerHTML = "";
  const points = Array.isArray(cfg.points) ? cfg.points.slice(0, bpRowsMax) : [];
  for (const row of points) {
    if (!Array.isArray(row) || row.length < 2) continue;
    addBreakpointRow(String(row[0]), String(row[1]));
  }
  reindexRows();
  updateInputSignalCardPresentation();
  updateGraphMeta(el.teleInput.textContent || "--", el.teleOutput.textContent || "--");
}

function applyAdvFromDevice(adv) {
  if (!adv || typeof adv !== "object") return;
  if (adv.kp !== undefined) el.advKp.value = String(adv.kp);
  if (adv.ki !== undefined) el.advKi.value = String(adv.ki);
  if (adv.ditherFreq !== undefined) el.advDitherFreq.value = String(adv.ditherFreq);
  if (adv.ditherAmplitude !== undefined) el.advDitherAmp.value = String(adv.ditherAmplitude);
  if (adv.pwmFreq !== undefined) el.advPwmFreq.value = String(adv.pwmFreq);
  const ditherRaw = adv.ditherEnable;
  el.advDitherEnable.checked = !!(ditherRaw === true || ditherRaw === 1 || String(ditherRaw).toLowerCase() === "1");
  syncDitherToggleState();
}

function handleProtocolRxLine(lineText) {
  const txt = String(lineText || "").trim();
  if (!txt) return;
  const low = txt.toLowerCase();
  if (low === "uart_blocked_ble_active") {
    warnOnce("uart_blocked_ble_active", "Serial is blocked because Bluetooth session is active on device.");
    return;
  }
  if (low === "unknown_command" && Date.now() <= state.pendingLoadUntil) {
    const scope = state.pendingLoadScope || "both";
    if (scope === "cfg") warnOnce("unknown_cmd_cfg", "Device firmware does not support readcfg.");
    else if (scope === "adv") warnOnce("unknown_cmd_adv", "Device firmware does not support readadv.");
    else warnOnce("unknown_cmd_cfgadv", "Device firmware does not support readcfg/readadv.");
    return;
  }
  if ((low === "auth_required" || low.startsWith("error: authentication required")) && Date.now() <= state.pendingLoadUntil) {
    warnOnce("auth_required_load", "Device returned authentication required while loading config.");
  }
}

function applyOpenedProfile(profile) {
  const cfg = profile?.cfg || {};
  const adv = profile?.adv || {};

  setSelectValueOrAppend(el.cfgMode, cfg.mode);
  el.cfgInMin.value = normalizeProfileField(cfg.input_min);
  el.cfgInMax.value = normalizeProfileField(cfg.input_max);
  el.cfgOutMin.value = normalizeProfileField(cfg.output_min);
  el.cfgOutMax.value = normalizeProfileField(cfg.output_max);
  forceOutputUnitCurrent();

  el.bpRows.innerHTML = "";
  const breakpoints = Array.isArray(cfg.breakpoints) ? cfg.breakpoints.slice(0, bpRowsMax) : [];
  for (const bp of breakpoints) {
    const inVal = normalizeProfileField(bp?.input);
    const outVal = normalizeProfileField(bp?.output);
    if (inVal || outVal) addBreakpointRow(inVal, outVal);
  }
  reindexRows();
  updateInputSignalCardPresentation();
  updateGraphMeta(el.teleInput.textContent || "--", el.teleOutput.textContent || "--");

  el.advKp.value = normalizeProfileField(adv.kp) || "0.15";
  el.advKi.value = normalizeProfileField(adv.ki) || "0.00005";
  el.advDitherFreq.value = normalizeProfileField(adv.ditherFreq) || "150";
  el.advDitherAmp.value = normalizeProfileField(adv.ditherAmplitude) || "0.05";
  el.advPwmFreq.value = normalizeProfileField(adv.pwmFreq) || "2000";
  el.advDitherEnable.checked = !!adv.ditherEnable;
  syncDitherToggleState();

  drawGraphs();
}

function buildProfilePayload() {
  const rows = [...el.bpRows.querySelectorAll(".bp-row")];
  const breakpoints = [];
  for (let i = 0; i < bpRowsMax; i++) {
    if (i < rows.length) {
      breakpoints.push({
        input: rows[i].querySelector(".bp-in")?.value?.trim() || "",
        output: rows[i].querySelector(".bp-out")?.value?.trim() || "",
      });
    } else {
      breakpoints.push({ input: "", output: "" });
    }
  }
  return {
    cfg: {
      mode: el.cfgMode.value || "",
      input_min: el.cfgInMin.value.trim(),
      input_max: el.cfgInMax.value.trim(),
      output_min: el.cfgOutMin.value.trim(),
      output_max: el.cfgOutMax.value.trim(),
      unit: el.cfgUnit.value.trim(),
      breakpoints: breakpoints,
    },
    adv: {
      kp: el.advKp.value.trim(),
      ki: el.advKi.value.trim(),
      ditherEnable: !!el.advDitherEnable.checked,
      ditherFreq: el.advDitherFreq.value.trim(),
      ditherAmplitude: el.advDitherAmp.value.trim(),
      pwmFreq: el.advPwmFreq.value.trim(),
    },
  };
}

function setDeviceIdDisplay(deviceId) {
  const value = String(deviceId || "").trim() || "-";
  state.deviceId = value;
  if (el.btnDeviceName) el.btnDeviceName.textContent = value === "-" ? "Device ID" : value;
  if (el.topDeviceId) el.topDeviceId.textContent = `Device ID: ${value}`;
}

function stopDeviceNameRequestFlow() {
  if (devnameRetryTimer) clearTimeout(devnameRetryTimer);
  devnameRetryTimer = null;
}

async function sendBackgroundCommand(cmd) {
  if (!api) return;
  const txt = String(cmd || "").trim();
  if (!txt) return;
  try {
    await api.send_command(txt);
  } catch (_e) {
    // ignore best-effort background request failures
  }
}

function requestDeviceNameFlow(retries = 6) {
  if (String(state.transport || "").toUpperCase() === "BLE") {
    return;
  }
  stopDeviceNameRequestFlow();
  setTimeout(() => {
    if (state.connected) sendBackgroundCommand("uart_connected");
  }, 900);
  setTimeout(() => {
    if (state.connected) sendBackgroundCommand("version");
  }, 1200);

  let retriesLeft = Math.max(0, Number(retries) || 0);
  const attemptDevname = () => {
    if (!state.connected) {
      stopDeviceNameRequestFlow();
      return;
    }
    sendBackgroundCommand("devname");
    if (retriesLeft <= 0) {
      devnameRetryTimer = null;
      return;
    }
    retriesLeft -= 1;
    devnameRetryTimer = setTimeout(attemptDevname, 420);
  };
  setTimeout(attemptDevname, 1450);
}

async function loadAllParametersFromDevice(options = {}) {
  const showAlerts = options.showAlerts !== false;
  const source = String(options.source || "manual").trim().toLowerCase();
  const autoStopMonitoring = options.autoStopMonitoring === true;
  if (!state.connected) {
    if (showAlerts) alert("Connect device first");
    return { ok: false, error: "Not connected" };
  }
  const actionLabel = source === "auto_connect" ? "auto loading device parameters" : "click Read Device";
  if (!ensureNotMonitoring(actionLabel, autoStopMonitoring)) {
    return { ok: false, error: "Monitoring is active" };
  }
  state.pendingLoadScope = "both";
  state.pendingLoadUntil = Date.now() + 3000;
  el.panelState.textContent = "LOADING CFG+ADV";
  if (source === "auto_connect") {
    logLine("SYS", "Connected. Auto-reading configuration and advanced parameters...");
  } else {
    logLine("SYS", "Requesting saved configuration/advanced parameters from device...");
  }
  const res = await api.load_config_from_device("both");
  state.pendingLoadUntil = 0;
  if (!res?.ok) {
    const message = res?.error || "Read Device failed";
    logLine("ERR", `Read Device failed: ${message}`);
    if (showAlerts) alert(message);
    el.panelState.textContent = state.connected ? "CONNECTED" : "IDLE";
    return { ok: false, error: message };
  }
  if (res?.cfg) applyCfgFromDevice(res.cfg);
  if (res?.adv) applyAdvFromDevice(res.adv);
  if (Array.isArray(res?.warnings)) {
    for (const w of res.warnings) {
      if (!w) continue;
      logLine("SYS", w);
      if (showAlerts) warnOnce(`load_warn_${w}`, w, 0);
    }
  }
  logLine("SYS", source === "auto_connect" ? "Auto-read complete" : "Device parameters loaded");
  el.panelState.textContent = state.connected ? "CONNECTED" : "IDLE";
  return { ok: true, cfg: res?.cfg, adv: res?.adv };
}

async function autoLoadParametersOnFirstConnect() {
  if (!state.connected || state.autoLoadOnConnectBusy) return;
  state.autoLoadOnConnectBusy = true;
  try {
    await loadAllParametersFromDevice({ showAlerts: false, source: "auto_connect", autoStopMonitoring: true });
  } finally {
    state.autoLoadOnConnectBusy = false;
    if (state.connected) {
      requestDeviceNameFlow(6);
    }
  }
}

function updateConnectionUi(snapshot) {
  const snapTransport = String(snapshot?.transport || "").toUpperCase();
  state.transport = snapTransport === "BLE" ? "BLE" : "UART";
  if (state.transport !== "BLE") {
    clearFirmwareAuthPassword();
  }
  const wasConnected = state.connected;
  state.connected = !!snapshot.connected;
  const justConnected = state.connected && !wasConnected;
  const justDisconnected = !state.connected && wasConnected;

  if (state.connected) {
    if (state.transport === "BLE") {
      el.connState.textContent = `Connected Bluetooth: ${snapshot.device_name || "Unknown"}`;
    } else {
      el.connState.textContent = `Connected: ${snapshot.port} @ ${snapshot.baud}`;
    }
    el.panelState.textContent = state.monitorOn ? "READING" : "CONNECTED";
    el.btnConnect.innerHTML = '<span class="btn-icon icon-switch-close"></span>Disconnect';
    el.btnConnect.classList.remove("btn-blue");
    el.btnConnect.classList.add("btn-warn");
    if (el.topConnState) {
      el.topConnState.classList.remove("offline");
      el.topConnState.innerHTML = '<span class="dot"></span>Connected';
    }
    if (justConnected) {
      void autoLoadParametersOnFirstConnect();
    }
  } else {
    el.connState.textContent = "Disconnected";
    el.panelState.textContent = "IDLE";
    el.btnConnect.innerHTML = '<span class="btn-icon icon-switch-open"></span>Connect';
    el.btnConnect.classList.add("btn-blue");
    el.btnConnect.classList.remove("btn-warn");
    stopMonitoring();
    if (el.topConnState) {
      el.topConnState.classList.add("offline");
      el.topConnState.innerHTML = '<span class="dot"></span>Disconnected';
    }
    if (justDisconnected || state.deviceId !== "-") {
      stopDeviceNameRequestFlow();
      setDeviceIdDisplay("-");
    }
    resetDeveloperUi();
    clearFirmwareAuthPassword();
    state.autoLoadOnConnectBusy = false;
    state.pendingLoadUntil = 0;
    state.pendingLoadScope = "both";
  }
  el.errorText.textContent = snapshot.last_error ? `Error: ${snapshot.last_error}` : "";
  if (el.footerPort) {
    if (!state.connected) {
      el.footerPort.textContent = "Disconnected";
    } else if (state.transport === "BLE") {
      el.footerPort.textContent = "Bluetooth Connected";
    } else {
      el.footerPort.textContent = "Serial Connected";
    }
  }
  if (el.footerBaud) {
    el.footerBaud.textContent = "";
  }
  if (el.footerErr) el.footerErr.textContent = snapshot.last_error ? "Errors" : "No Errors";

  if (el.transportSelect) {
    const activeTransport = state.transport === "BLE" ? "BLE" : "UART";
    if (state.connected && el.transportSelect.value !== activeTransport) {
      el.transportSelect.value = activeTransport;
    }
    el.transportSelect.disabled = !!state.connected;
  }
}

function updateTelemetryUi(telemetry) {
  const inputRaw = telemetry?.input ?? "-";
  const outputRaw = telemetry?.output ?? "-";
  const outputTxt = formatMeasuredOutputValue(outputRaw, getMeasuredOutputUnit());
  const satTxt = String(telemetry?.sat ?? "-");
  const inputInfo = updateInputSignalCardPresentation();
  const inputTxt = formatInputSignalValue(inputRaw, inputInfo.unit);
  el.teleInput.textContent = inputTxt;
  el.teleOutput.textContent = outputTxt;
  el.teleSat.textContent = satTxt;
  setLimitStatusAlert(satTxt);
  updateGraphMeta(inputTxt, outputTxt);

  const x = parseNum(inputRaw);
  const y = parseNum(outputRaw);
  if (x !== null || y !== null) {
    state.monitorAwaitingRx = false;
    const now = Date.now();
    const prev = state.lastPoint;
    const sameValues = !!prev && prev.x === x && prev.y === y;
    const tooSoon = !!prev && (now - prev.t) < 260;
    if (sameValues && tooSoon) return;
    const point = {
      t: now,
      x: x,
      y: y,
    };
    state.lastPoint = point;
    state.points.push(point);
    state.trend.push(point);
    if (state.points.length > 900) state.points.shift();
    const cutoff = now - 30000;
    state.trend = state.trend.filter((p) => p.t >= cutoff);
    scheduleDrawGraphs();
  }
}

function isLimitStatusOn(value) {
  const txt = String(value ?? "").trim().toLowerCase();
  if (!txt || txt === "-") return false;
  const normalized = txt.replace(/[\s_-]+/g, "");
  if (["on", "1", "true", "yes", "high", "sat", "limiton"].includes(normalized)) return true;
  if (["off", "0", "false", "no", "low", "unsat", "limitoff"].includes(normalized)) return false;
  return /\bon\b/.test(txt) || /\bsat\b/.test(txt);
}

function setLimitStatusAlert(statusValue) {
  const card = el.limitStatusCard || el.teleSat?.closest(".tele-card");
  if (!card) return;
  card.classList.toggle("limit-alert", isLimitStatusOn(statusValue));
}

function formatGraphMetaValue(value) {
  const txt = String(value ?? "").trim();
  if (!txt || txt === "-" || txt === "--") return "--";
  const match = txt.match(/[-+]?\d*\.?\d+/);
  return match ? match[0] : txt;
}

function updateGraphMeta(inputTxt = "--", outputTxt = "--") {
  const graphInputTxt = formatGraphMetaValue(inputTxt);
  const graphOutputTxt = formatGraphMetaValue(outputTxt);
  const inputInfo = getInputSignalModeInfo();
  const inputUnitLabel = inputInfo.unit ? ` (${inputInfo.unit})` : "";
  const outputUnit = getMeasuredOutputUnit();
  const outputUnitLabel = outputUnit ? ` (${outputUnit})` : "";
  if (el.metaVolt) el.metaVolt.textContent = `Input Signal${inputUnitLabel}: ${graphInputTxt}`;
  if (el.metaCurrent) el.metaCurrent.textContent = `Measured Output${outputUnitLabel}: ${graphOutputTxt}`;
}

function updateClock() {
  if (!el.footerTime) return;
  el.footerTime.textContent = new Date().toLocaleTimeString("en-US", { hour12: true });
}

function tryUpdateDeviceIdFromRx(lineText) {
  const txt = String(lineText || "").trim();
  if (!txt) return;
  let deviceId = "";
  const upper = txt.toUpperCase();
  if (upper.startsWith("DEVNAME:") || upper.startsWith("DEVNAME=")) {
    const sep = txt.includes(":") ? ":" : "=";
    deviceId = txt.split(sep)[1]?.trim() || "";
  } else if (upper.includes("BLE DEVICE NAME:")) {
    deviceId = txt.split(":")[1]?.trim() || "";
  } else if (upper.startsWith("SERVO_")) {
    deviceId = txt;
  }
  if (!deviceId) return;
  setDeviceIdDisplay(deviceId);
}

async function refreshEndpoints() {
  const transport = String(el.transportSelect?.value || "UART").toUpperCase();
  const previousValue = String(el.endpointSelect?.value || "");
  el.endpointSelect.innerHTML = "";

  if (transport === "BLE") {
    el.endpointLabel.textContent = "Bluetooth Device";
    const res = await api.list_ble_devices();
    const devices = Array.isArray(res?.devices) ? res.devices : [];
    for (const dev of devices) {
      const address = String(dev?.address || "").trim();
      if (!address) continue;
      const name = String(dev?.name || "Unknown").trim() || "Unknown";
      const opt = document.createElement("option");
      opt.value = address;
      opt.textContent = name;
      opt.dataset.name = name;
      el.endpointSelect.appendChild(opt);
    }
    if (previousValue && [...el.endpointSelect.options].some((o) => o.value === previousValue)) {
      el.endpointSelect.value = previousValue;
    }
    if (el.endpointSelect.options.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(No Bluetooth devices)";
      el.endpointSelect.appendChild(opt);
    }
    if (!res?.ok && res?.error) {
      logLine("ERR", `Bluetooth scan failed: ${res.error}`);
      alert(`Bluetooth scan failed: ${res.error}`);
    }
    return;
  }

  el.endpointLabel.textContent = "Serial Port";
  const res = await api.list_serial_ports();
  const ports = Array.isArray(res?.ports) ? res.ports : [];
  for (const p of ports) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    el.endpointSelect.appendChild(opt);
  }
  if (previousValue && [...el.endpointSelect.options].some((o) => o.value === previousValue)) {
    el.endpointSelect.value = previousValue;
  }
  if (ports.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(No serial ports)";
    el.endpointSelect.appendChild(opt);
  }
}

async function toggleConnection() {
  if (state.connected) {
    await api.disconnect();
    return;
  }
  if (String(el.transportSelect?.value || "").toUpperCase() === "BLE") {
    const address = String(el.endpointSelect?.value || "").trim();
    const selectedOpt = el.endpointSelect?.options?.[el.endpointSelect.selectedIndex];
    const name = String(selectedOpt?.dataset?.name || selectedOpt?.textContent || "").trim();
    if (!address) {
      alert("Select a Bluetooth device first");
      return;
    }
    const res = await api.connect_ble(address, name);
    if (!res?.ok) {
      alert(res?.error || "Bluetooth connection failed");
      return;
    }
    logLine("SYS", `Bluetooth connected: ${name || "Unknown"}`);
    return;
  }
  const selectedPort = String(el.endpointSelect?.value || "").trim();
  if (!selectedPort) {
    alert("Select a serial port first");
    return;
  }
  const res = await api.connect_serial(selectedPort, 9600);
  if (!res?.ok) {
    alert(res?.error || "Connection failed");
    return;
  }
  if (res?.warning) {
    logLine("SYS", res.warning);
    alert(res.warning);
  }
  if (res?.port && res?.baud) {
    logLine("SYS", `Serial connected: ${res.port} @ ${res.baud}`);
  } else if (res?.baud) {
    logLine("SYS", `Serial connected using ${res.baud} baud`);
  }
}

async function sendCommand(cmd, options = {}) {
  const alertOnError = options.alertOnError !== false;
  const txt = String(cmd || "").trim();
  if (!txt) return;
  const res = await api.send_command(txt);
  if (!res?.ok) {
    if (alertOnError) alert(res?.error || "Send failed");
    return { ok: false, error: res?.error || "Send failed" };
  }
  return { ok: true };
}

function buildCfgPayload() {
  const mode = el.cfgMode.value;
  const inMin = parseNum(el.cfgInMin.value);
  const inMax = parseNum(el.cfgInMax.value);
  const outMin = parseNum(el.cfgOutMin.value);
  const outMax = parseNum(el.cfgOutMax.value);
  if (inMin === null || inMax === null || outMin === null || outMax === null) {
    throw new Error("Fill all CFG numeric fields");
  }
  if (inMin >= inMax) {
    throw new Error("Input range invalid: Input Min must be less than Input Max.");
  }
  if (outMin >= outMax) {
    throw new Error("Output range invalid: Output Min must be less than Output Max.");
  }
  if (outMin < 0 || outMin > 2000 || outMax < 0 || outMax > 2000) {
    throw new Error("Output current range must stay within [0, 2000] mA.");
  }
  const modeLimits = cfgModeLimits[mode];
  if (Array.isArray(modeLimits)) {
    const [modeMin, modeMax] = modeLimits;
    if (inMin < modeMin || inMax > modeMax) {
      throw new Error(`For mode '${mode}', input range must stay within [${modeMin}, ${modeMax}].`);
    }
  }

  const points = [];
  const seenInputs = new Map();
  const rows = [...el.bpRows.querySelectorAll(".bp-row")];
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const label = `BP${i + 1}`;
    const inTxt = row.querySelector(".bp-in").value.trim();
    const outTxt = row.querySelector(".bp-out").value.trim();
    if (!inTxt && !outTxt) continue;
    if (!inTxt || !outTxt) {
      throw new Error(`${label} requires both Input and Output.`);
    }
    const px = parseNum(inTxt);
    const py = parseNum(outTxt);
    if (px === null || py === null) {
      throw new Error(`${label} contains non-numeric values.`);
    }
    if (py < outMin || py > outMax) {
      throw new Error(`${label} output ${py} is out of output range [${outMin}, ${outMax}].`);
    }
    if (seenInputs.has(px)) {
      throw new Error(`Duplicate input value: ${label} input ${px} is already used in ${seenInputs.get(px)}.`);
    }
    seenInputs.set(px, label);
    points.push([px, py]);
  }
  for (let i = 1; i < points.length; i++) {
    if (points[i][0] <= points[i - 1][0]) {
      throw new Error(`Input order invalid: BP${i + 1} input must be greater than BP${i} input.`);
    }
  }
  return {
    mode: mode,
    input: [inMin, inMax],
    output: [outMin, outMax],
    unit: el.cfgUnit.value || "mA",
    points: points,
  };
}

function buildAdvPayload() {
  const kp = parseNum(el.advKp.value);
  const ki = parseNum(el.advKi.value);
  const ditherFreq = parseNum(el.advDitherFreq.value);
  const ditherAmplitude = parseNum(el.advDitherAmp.value);
  const pwmFreq = parseNum(el.advPwmFreq.value);
  if (kp === null || ki === null || ditherFreq === null || ditherAmplitude === null || pwmFreq === null) {
    throw new Error("Fill all Advanced numeric fields");
  }
  if (pwmFreq <= 0) {
    throw new Error("PWM Freq must be greater than zero.");
  }
  return {
    kp: kp,
    ki: ki,
    ditherEnable: el.advDitherEnable.checked ? 1 : 0,
    ditherFreq: ditherFreq,
    ditherAmplitude: ditherAmplitude,
    pwmFreq: Math.round(pwmFreq),
  };
}

function addBreakpointRow(inVal = "", outVal = "") {
  const current = el.bpRows.querySelectorAll(".bp-row").length;
  if (current >= bpRowsMax) return;
  const idx = current + 1;
  const row = document.createElement("div");
  row.className = "bp-row";
  row.innerHTML = `
    <span>BP${idx}</span>
    <input class="bp-in" type="text" value="${inVal}">
    <span>-></span>
    <input class="bp-out" type="text" value="${outVal}">
    <button class="btn btn-warn small bp-remove" type="button" title="Delete setpoint" aria-label="Delete setpoint">
      <span class="btn-icon icon-trash" aria-hidden="true"></span>
    </button>
  `;
  row.querySelector(".bp-remove").addEventListener("click", () => {
    row.remove();
    reindexRows();
  });
  el.bpRows.appendChild(row);
  reindexRows();
}

function reindexRows() {
  const rows = [...el.bpRows.querySelectorAll(".bp-row")];
  rows.forEach((row, i) => {
    row.querySelector("span").textContent = `BP${i + 1}`;
  });
  if (el.bpEmpty) {
    el.bpEmpty.style.display = rows.length === 0 ? "flex" : "none";
  }
  const bpBody = el.bpRows?.closest(".bp-body");
  if (bpBody) {
    bpBody.classList.toggle("has-rows", rows.length > 0);
    bpBody.classList.toggle("has-many-rows", rows.length > 5);
  }
}

function clearGraphData() {
  state.points = [];
  state.trend = [];
  state.lastPoint = null;
  state.linearityHover = null;
  scheduleDrawGraphs();
  logLine("SYS", "Cleared live graph traces");
}

function drawGraphs() {
  drawLinearityCanvas();
  drawTrendCanvas();
}

function setupCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const cssWidth = Math.max(1, Math.floor(rect.width));
  const cssHeight = Math.max(1, Math.floor(rect.height));
  const dpr = Math.max(1, Number(window.devicePixelRatio) || 1);
  const pixelWidth = Math.max(1, Math.floor(cssWidth * dpr));
  const pixelHeight = Math.max(1, Math.floor(cssHeight * dpr));
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.imageSmoothingEnabled = true;
  return { ctx, width: cssWidth, height: cssHeight };
}

function drawGrid(ctx, w, h, left, top, right, bottom, uiColors = null, showFrame = true) {
  const palette = uiColors || getGraphUiColors();
  const crisp = (value) => Math.round(value) + 0.5;
  if (showFrame) {
    ctx.strokeStyle = palette.frame;
    ctx.lineWidth = 1;
    ctx.strokeRect(crisp(left), crisp(top), Math.max(1, right - left), Math.max(1, bottom - top));
  }
  ctx.strokeStyle = palette.grid;
  for (let i = 1; i < 5; i++) {
    const x = crisp(left + ((right - left) * i) / 5);
    const y = crisp(top + ((bottom - top) * i) / 5);
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
  }
}

function drawLinearityCanvas() {
  const c = el.linearityCanvas;
  const canvasView = setupCanvas(c);
  const ctx = canvasView.ctx;
  const w = canvasView.width;
  const h = canvasView.height;
  const uiColors = getGraphUiColors();
  ctx.clearRect(0, 0, w, h);
  const left = 58;
  const top = 34;
  const right = Math.max(left + 1, w - 44);
  const bottom = Math.max(top + 1, h - 42);
  drawGrid(ctx, w, h, left, top, right, bottom, uiColors, false);

  let inMin = parseNum(el.cfgInMin.value);
  let inMax = parseNum(el.cfgInMax.value);
  const modeLimits = cfgModeLimits[String(el.cfgMode?.value || "").trim()];
  if (inMin === null || inMax === null || inMin >= inMax) {
    if (Array.isArray(modeLimits) && modeLimits.length >= 2) {
      inMin = Number(modeLimits[0]);
      inMax = Number(modeLimits[1]);
    } else {
      inMin = 0;
      inMax = 10;
    }
  }

  let outMin = parseNum(el.cfgOutMin.value);
  let outMax = parseNum(el.cfgOutMax.value);
  if (outMin === null || outMax === null || outMin >= outMax) {
    outMin = 0;
    outMax = 2000;
  }

  const showReference = true;
  const showCurrent = true;
  const showVoltage = false;
  const refPoints = getLinearityReferencePoints(inMin, inMax, outMin, outMax);
  const actualPoints = state.points.filter((point) => point.x !== null && point.y !== null);
  const voltagePoints = [];
  if (showVoltage && refPoints.length >= 2 && actualPoints.length > 0) {
    for (const point of actualPoints) {
      const vx = Number(point.x);
      const vy = interpolateCurveY(refPoints, vx);
      if (Number.isFinite(vx) && Number.isFinite(vy)) {
        voltagePoints.push({ x: vx, y: vy });
      }
    }
  }

  const xValues = [];
  const yValues = [];
  if (showReference) {
    for (const [x, y] of refPoints) {
      xValues.push(Number(x));
      yValues.push(Number(y));
    }
  }
  if (showCurrent) {
    for (const point of actualPoints) {
      xValues.push(Number(point.x));
      yValues.push(Number(point.y));
    }
  }
  if (showVoltage) {
    for (const point of voltagePoints) {
      xValues.push(Number(point.x));
      yValues.push(Number(point.y));
    }
  }
  const [xMin, xMax] = calcRange(xValues, inMin, inMax);
  const [yMin, yMax] = calcRange(yValues, outMin, outMax);
  const sx = (x) => left + ((x - xMin) * (right - left)) / Math.max(1e-9, xMax - xMin);
  const sy = (y) => bottom - ((y - yMin) * (bottom - top)) / Math.max(1e-9, yMax - yMin);

  ctx.save();
  ctx.fillStyle = uiColors.tickText;
  ctx.font = "11px Consolas";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 5; i++) {
    const yTick = yMin + ((yMax - yMin) * (5 - i)) / 5;
    const py = top + ((bottom - top) * i) / 5;
    ctx.fillText(yTick.toFixed(0), left - 8, py);
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = 0; i <= 5; i++) {
    const xTick = xMin + ((xMax - xMin) * i) / 5;
    const px = left + ((right - left) * i) / 5;
    ctx.fillText(xTick.toFixed(2), px, bottom + 6);
  }
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  const outputUnit = String(getMeasuredOutputUnit() || "mA").trim() || "mA";
  const inputUnit = String(getInputSignalModeInfo().unit || "").trim();
  ctx.restore();

  const hoverPoints = [];
  if (showReference) {
    ctx.strokeStyle = graphColors.setpoint;
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.shadowColor = "rgba(113, 209, 138, 0.25)";
    ctx.shadowBlur = 6;
    ctx.beginPath();
    refPoints.forEach(([x, y], i) => {
      const px = sx(x);
      const py = sy(y);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
      hoverPoints.push({ px, py, x: Number(x), y: Number(y), series: "Setpoint", color: graphColors.setpoint });
    });
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.setLineDash([]);

    for (const [x, y] of refPoints) {
      const px = sx(x);
      const py = sy(y);
      ctx.beginPath();
      ctx.fillStyle = graphColors.setpoint;
      ctx.strokeStyle = "#0f1419";
      ctx.lineWidth = 1;
      ctx.arc(px, py, 3, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
  }

  if (showCurrent && actualPoints.length >= 2) {
    ctx.strokeStyle = graphColors.current;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.shadowColor = "rgba(232, 86, 86, 0.24)";
    ctx.shadowBlur = 6;
    ctx.beginPath();
    actualPoints.forEach((point, i) => {
      const px = sx(point.x);
      const py = sy(point.y);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
      hoverPoints.push({ px, py, x: Number(point.x), y: Number(point.y), series: "Current", color: graphColors.current });
    });
    ctx.stroke();
    ctx.shadowBlur = 0;
  } else if (showCurrent && actualPoints.length === 1) {
    const point = actualPoints[0];
    const px = sx(point.x);
    const py = sy(point.y);
    ctx.beginPath();
    ctx.fillStyle = graphColors.current;
    ctx.arc(px, py, 2.5, 0, Math.PI * 2);
    ctx.fill();
    hoverPoints.push({ px, py, x: Number(point.x), y: Number(point.y), series: "Current", color: graphColors.current });
  }

  if (showVoltage && voltagePoints.length >= 2) {
    ctx.strokeStyle = graphColors.voltage;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.shadowColor = "rgba(87, 196, 255, 0.22)";
    ctx.shadowBlur = 6;
    ctx.beginPath();
    voltagePoints.forEach((point, i) => {
      const px = sx(point.x);
      const py = sy(point.y);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
      hoverPoints.push({ px, py, x: Number(point.x), y: Number(point.y), series: "Voltage", color: graphColors.voltage });
    });
    ctx.stroke();
    ctx.shadowBlur = 0;
  } else if (showVoltage && voltagePoints.length === 1) {
    const point = voltagePoints[0];
    const px = sx(point.x);
    const py = sy(point.y);
    ctx.beginPath();
    ctx.fillStyle = graphColors.voltage;
    ctx.arc(px, py, 2.5, 0, Math.PI * 2);
    ctx.fill();
    hoverPoints.push({ px, py, x: Number(point.x), y: Number(point.y), series: "Voltage", color: graphColors.voltage });
  }

  if (state.linearityHover && hoverPoints.length > 0) {
    let nearest = null;
    let nearestDist2 = Number.POSITIVE_INFINITY;
    for (const point of hoverPoints) {
      const dx = point.px - state.linearityHover.x;
      const dy = point.py - state.linearityHover.y;
      const dist2 = (dx * dx) + (dy * dy);
      if (dist2 < nearestDist2) {
        nearestDist2 = dist2;
        nearest = point;
      }
    }
    if (nearest && nearestDist2 <= (graphHoverMaxDistancePx * graphHoverMaxDistancePx)) {
      const tipLines = [
        nearest.series,
        `Input: ${nearest.x.toFixed(2)}${inputUnit ? ` ${inputUnit}` : ""}`,
        `Output: ${nearest.y.toFixed(1)} ${outputUnit}`,
      ];
      const pad = 4;
      const lineHeight = 14;
      ctx.save();
      ctx.font = "11px Consolas";
      const textWidth = tipLines.reduce((maxWidth, line) => Math.max(maxWidth, ctx.measureText(line).width), 0);
      const boxW = Math.ceil(textWidth) + 2;
      const boxH = (tipLines.length * lineHeight) + 2;
      let tx = state.linearityHover.x + 12;
      let ty = state.linearityHover.y - 12;
      if ((tx + boxW + (pad * 2)) > w) tx = w - (boxW + (pad * 2));
      if ((ty + boxH + (pad * 2)) > h) ty = h - (boxH + (pad * 2));
      if (tx < 0) tx = 0;
      if (ty < 0) ty = 0;

      ctx.fillStyle = uiColors.hoverBg;
      ctx.strokeStyle = nearest.color;
      ctx.lineWidth = 1;
      ctx.fillRect(tx, ty, boxW + (pad * 2), boxH + (pad * 2));
      ctx.strokeRect(tx, ty, boxW + (pad * 2), boxH + (pad * 2));

      ctx.fillStyle = uiColors.hoverText;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      tipLines.forEach((line, index) => {
        ctx.fillText(line, tx + pad, ty + pad + (index * lineHeight));
      });
      ctx.beginPath();
      ctx.fillStyle = nearest.color;
      ctx.arc(nearest.px, nearest.py, 3, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }

  ctx.save();
  ctx.fillStyle = uiColors.axisText;
  ctx.font = "11px Consolas";
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.fillText(getGraphInputAxisLabel(), (left + right) / 2, h - 4);
  ctx.textBaseline = "middle";
  ctx.translate(18, (top + bottom) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(getGraphOutputAxisLabel(), 0, 0);
  ctx.restore();

  const hasData = (
    (showReference && refPoints.length >= 2)
    || (showCurrent && actualPoints.length >= 1)
    || (showVoltage && voltagePoints.length >= 1)
  );
  if (!hasData) {
    ctx.fillStyle = uiColors.waitText;
    ctx.font = "12px Segoe UI";
    ctx.fillText("Waiting for telemetry...", left + 16, top + 18);
  }
}

function drawTrendCanvas() {
  const c = el.trendCanvas;
  const canvasView = setupCanvas(c);
  const ctx = canvasView.ctx;
  const w = canvasView.width;
  const h = canvasView.height;
  const uiColors = getGraphUiColors();
  const inputInfo = getInputSignalModeInfo();
  const inputUnit = inputInfo.unit || "";
  const outputUnit = getMeasuredOutputUnit();
  const rightAxisUnitLabel = inputUnit || "Input";

  ctx.clearRect(0, 0, w, h);
  const left = 58;
  const top = 34;
  const right = Math.max(left + 1, w - 44);
  const bottom = Math.max(top + 1, h - 42);
  drawGrid(ctx, w, h, left, top, right, bottom, uiColors, false);

  const data = state.trend.filter((p) => p.y !== null || p.x !== null);
  if (data.length === 0) {
    ctx.fillStyle = uiColors.waitText;
    ctx.font = "12px Segoe UI";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("Waiting for live trend...", (left + right) / 2, top + ((bottom - top) * 0.14));
    ctx.fillStyle = uiColors.axisText;
    ctx.font = "11px Consolas";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillText("Time (s)", (left + right) / 2, h - 4);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(outputUnit, left - 32, top - 8);
    ctx.textAlign = "left";
    ctx.fillText(rightAxisUnitLabel, right + 8, top - 8);
    return;
  }

  const t0 = data[0].t;
  const t1 = data[data.length - 1].t;
  const dt = Math.max(1, t1 - t0);

  const yVals = data.map((p) => p.y).filter((v) => Number.isFinite(v));
  const xVals = data.map((p) => p.x).filter((v) => Number.isFinite(v));

  let outMinFallback = parseNum(el.cfgOutMin?.value);
  let outMaxFallback = parseNum(el.cfgOutMax?.value);
  if (outMinFallback === null || outMaxFallback === null || outMinFallback >= outMaxFallback) {
    outMinFallback = 0;
    outMaxFallback = 2000;
  }

  let inMinFallback = parseNum(el.cfgInMin?.value);
  let inMaxFallback = parseNum(el.cfgInMax?.value);
  const modeLimits = cfgModeLimits[String(el.cfgMode?.value || "").trim()];
  if (inMinFallback === null || inMaxFallback === null || inMinFallback >= inMaxFallback) {
    if (Array.isArray(modeLimits) && modeLimits.length >= 2) {
      inMinFallback = Number(modeLimits[0]);
      inMaxFallback = Number(modeLimits[1]);
    } else {
      inMinFallback = 0;
      inMaxFallback = 10;
    }
  }

  const [curMin, curMax] = calcRange(yVals, outMinFallback, outMaxFallback);
  const [inputMin, inputMax] = calcRange(xVals, inMinFallback, inMaxFallback);

  const tx = (t) => left + ((t - t0) * (right - left)) / dt;
  const yCur = (v) => bottom - ((v - curMin) * (bottom - top)) / Math.max(1e-9, curMax - curMin);
  const yInput = (v) => bottom - ((v - inputMin) * (bottom - top)) / Math.max(1e-9, inputMax - inputMin);

  const formatTrendTick = (value) => {
    if (!Number.isFinite(value)) return "--";
    const abs = Math.abs(value);
    if (abs >= 100) return value.toFixed(0);
    if (abs >= 10) return value.toFixed(1);
    return value.toFixed(2);
  };

  ctx.save();
  ctx.font = "10.5px Consolas";
  for (let i = 0; i <= 5; i++) {
    const py = top + ((bottom - top) * i) / 5;
    const curTick = curMin + ((curMax - curMin) * (5 - i)) / 5;
    const inputTick = inputMin + ((inputMax - inputMin) * (5 - i)) / 5;
    ctx.fillStyle = graphColors.current;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(formatTrendTick(curTick), left - 8, py);
    ctx.fillStyle = graphColors.voltage;
    ctx.textAlign = "left";
    ctx.fillText(formatTrendTick(inputTick), right + 8, py);
  }

  ctx.fillStyle = uiColors.tickText;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = 0; i <= 5; i++) {
    const px = left + ((right - left) * i) / 5;
    const secTick = ((dt / 1000) * i) / 5;
    ctx.fillText(secTick.toFixed(1), px, bottom + 6);
  }

  ctx.fillStyle = graphColors.current;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.fillText(outputUnit, left - 32, top - 8);
  ctx.fillStyle = graphColors.voltage;
  ctx.textAlign = "left";
  ctx.fillText(rightAxisUnitLabel, right + 8, top - 8);
  ctx.fillStyle = uiColors.axisText;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.fillText("Time (s)", (left + right) / 2, h - 4);
  ctx.restore();

  ctx.strokeStyle = "#e85656";
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = "rgba(232, 86, 86, 0.22)";
  ctx.shadowBlur = 5;
  ctx.beginPath();
  let started = false;
  for (const p of data) {
    if (p.y === null || !Number.isFinite(p.y)) continue;
    if (!started) {
      ctx.moveTo(tx(p.t), yCur(p.y));
      started = true;
    } else {
      ctx.lineTo(tx(p.t), yCur(p.y));
    }
  }
  if (started) ctx.stroke();
  ctx.shadowBlur = 0;

  ctx.strokeStyle = "#57c4ff";
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = "rgba(87, 196, 255, 0.2)";
  ctx.shadowBlur = 5;
  ctx.beginPath();
  started = false;
  for (const p of data) {
    if (p.x === null || !Number.isFinite(p.x)) continue;
    if (!started) {
      ctx.moveTo(tx(p.t), yInput(p.x));
      started = true;
    } else {
      ctx.lineTo(tx(p.t), yInput(p.x));
    }
  }
  if (started) ctx.stroke();
  ctx.shadowBlur = 0;
}

function setMonitorButton() {
  if (state.monitorOn) {
    el.btnReadToggle.innerHTML = '<span class="btn-icon icon-stop"></span>Stop Monitoring';
    el.btnReadToggle.classList.remove("btn-green");
    el.btnReadToggle.classList.add("btn-warn");
  } else {
    el.btnReadToggle.innerHTML = '<span class="btn-icon icon-play"></span>Start Monitoring';
    el.btnReadToggle.classList.add("btn-green");
    el.btnReadToggle.classList.remove("btn-warn");
  }
}

function startMonitoring() {
  if (!state.connected) return;
  if (state.monitorOn) return;
  state.monitorOn = true;
  state.monitorAwaitingRx = false;
  state.monitorLastSentAt = 0;
  setMonitorButton();
  el.panelState.textContent = "READING";
  monitorTimer = setInterval(() => {
    const now = Date.now();
    if (state.monitorAwaitingRx && (now - state.monitorLastSentAt) < 700) return;
    state.monitorAwaitingRx = true;
    state.monitorLastSentAt = now;
    sendCommand("read", { alertOnError: false });
  }, 220);
}

function stopMonitoring() {
  state.monitorOn = false;
  state.monitorAwaitingRx = false;
  setMonitorButton();
  if (monitorTimer) clearInterval(monitorTimer);
  monitorTimer = null;
}

function toggleMonitoring() {
  if (!state.connected) {
    alert("Connect device first");
    return;
  }
  if (state.monitorOn) stopMonitoring();
  else startMonitoring();
}

async function developerLogin() {
  if (!state.connected) {
    alert("Connect device first");
    return false;
  }
  return ensureDeveloperUnlocked("Enter developer password:");
}

async function developerLoginWithPassword(password) {
  const pwd = String(password || "").trim();
  if (!pwd) {
    return { ok: false, error: "Password required" };
  }
  setDeveloperStatus("Unlocking developer mode...");
  const res = await api.developer_login(pwd);
  if (!res?.ok) {
    state.devUnlocked = false;
    const message = res?.error || "Developer login failed";
    setDeveloperStatus(message, true);
    logLine("ERR", `Developer login failed: ${message}`);
    return { ok: false, error: message };
  }
  state.devUnlocked = true;
  setDeveloperStatus("Developer mode unlocked");
  logLine("SYS", "Developer mode unlocked");
  return { ok: true };
}

async function ensureDeveloperUnlocked(promptLabel = "Enter developer password:") {
  if (state.devUnlocked) return true;
  const password = await askPassword(promptLabel, async (pwd) => {
    const unlockRes = await developerLoginWithPassword(pwd);
    if (!unlockRes?.ok) {
      return { ok: false, error: unlockRes?.error || "Developer login failed" };
    }
    return { ok: true, success: "Developer password correct", value: pwd };
  });
  if (password === null) return false;
  if (!String(password || "").trim()) {
    alert("Password required");
    return false;
  }
  return true;
}

async function openDeveloperModeDialog() {
  if (!state.connected) {
    setDeveloperStatus("Session locked");
    alert("Connect device first");
    return;
  }
  ensureNotMonitoring("opening Developer Mode", true);
  const unlocked = await ensureDeveloperUnlocked("Enter developer password to open Developer Mode:");
  if (!unlocked) return;
  el.developerDialog.showModal();
  await developerLoadCalibration({ requireUnlock: false });
}

async function developerLogout() {
  if (!state.connected) {
    alert("Connect device first");
    return;
  }
  setDeveloperStatus("Closing developer session...");
  const res = await api.developer_logout();
  if (!res?.ok) {
    state.devUnlocked = false;
    const message = res?.error || "Developer logout failed";
    setDeveloperStatus(message, true);
    logLine("ERR", `Developer logout failed: ${message}`);
    return;
  }
  state.devUnlocked = false;
  setDeveloperStatus("Session locked");
  logLine("SYS", "Developer session locked");
}

async function developerLoadCalibration(options = {}) {
  if (!state.connected) {
    alert("Connect device first");
    return false;
  }
  ensureNotMonitoring("loading calibration", true);
  const requireUnlock = options?.requireUnlock !== false;
  if (requireUnlock) {
    const unlocked = await developerLogin();
    if (!unlocked) return false;
  }
  setDeveloperStatus("Reading calibration...");
  const res = await api.developer_load_calibration();
  if (!res?.ok) {
    const message = res?.error || "Calibration read failed";
    if (/authentication required/i.test(message)) state.devUnlocked = false;
    setDeveloperStatus(message, true);
    logLine("ERR", `Calibration read failed: ${message}`);
    return false;
  }
  writeCalibrationToUi(res.calibration || {});
  setDeveloperStatus("Calibration loaded");
  logLine("SYS", "Developer calibration loaded");
  return true;
}

async function developerSaveCalibration() {
  if (!state.connected) {
    alert("Connect device first");
    return;
  }
  ensureNotMonitoring("saving calibration", true);
  const unlocked = await developerLogin();
  if (!unlocked) return false;
  let payload = null;
  try {
    payload = readCalibrationFromUi();
  } catch (err) {
    alert(err?.message || "Invalid calibration values");
    return false;
  }
  setDeveloperStatus("Saving calibration...");
  const res = await api.developer_save_calibration(payload);
  if (!res?.ok) {
    const message = res?.error || "Calibration save failed";
    if (/authentication required/i.test(message)) state.devUnlocked = false;
    setDeveloperStatus(message, true);
    logLine("ERR", `Calibration save failed: ${message}`);
    return false;
  }
  setDeveloperStatus("Calibration saved");
  logLine("SYS", "Developer calibration saved");
  return true;
}

async function pickFirmwareFile() {
  let dirHint = "";
  if (state.firmwarePath) {
    const idx = Math.max(state.firmwarePath.lastIndexOf("/"), state.firmwarePath.lastIndexOf("\\"));
    if (idx > 0) dirHint = state.firmwarePath.slice(0, idx);
  }
  const res = await api.pick_firmware_bin(dirHint);
  if (res?.cancelled) return;
  if (!res?.ok) {
    const message = res?.error || "Failed to pick firmware file";
    setFirmwareStatus(message, true);
    alert(message);
    return;
  }
  const size = Number(res.size || 0);
  state.firmwarePath = String(res.path || "").trim();
  state.firmwareSizeBytes = size;
  syncFirmwareFileSelectionUi();
  setFirmwareProgress(0, 0, size, false);
  setFirmwareStatus("Firmware file selected");
}

async function openFirmwareDialog() {
  if (!state.connected) {
    alert("Connect Bluetooth device first");
    return;
  }
  if (String(state.transport || "").toUpperCase() !== "BLE") {
    alert("Firmware update is allowed only in Bluetooth mode.\nSwitch to Bluetooth and connect first.");
    return;
  }
  if (!ensureNotMonitoring("opening Firmware Update", true)) return;

  const promptResult = await askPassword(
    "Enter AUTH password to open Firmware Update:",
    async (pwd) => {
      if (!api || typeof api.firmware_validate_password !== "function") {
        return { ok: true, success: "Password captured", value: String(pwd || "").trim() };
      }
      const verify = await api.firmware_validate_password(String(pwd || "").trim());
      if (!verify?.ok) {
        return { ok: false, error: verify?.error || "Wrong AUTH password" };
      }
      return { ok: true, success: verify?.message || "Password verified", value: String(pwd || "").trim() };
    }
  );
  if (promptResult === null) return;
  const firmwarePassword = String(promptResult || "").trim();
  if (!firmwarePassword) {
    alert("Password required");
    return;
  }

  state.firmwareAuthPassword = firmwarePassword;
  if (!String(state.firmwarePath || "").trim()) {
    clearFirmwareFileSelection();
  } else {
    syncFirmwareFileSelectionUi();
  }
  setFirmwareStatus(
    String(state.firmwarePath || "").trim()
      ? "Authorized. Selected BIN is ready. Click Upload Firmware."
      : "Authorized. Select firmware BIN and click Upload Firmware."
  );
  el.firmwareDialog.showModal();
  await pollSnapshot();
}

async function uploadFirmware() {
  if (!state.connected) {
    alert("Connect device first");
    return;
  }
  if (String(state.transport || "").toUpperCase() !== "BLE") {
    alert("Firmware update is allowed only in Bluetooth mode.");
    return;
  }
  ensureNotMonitoring("firmware upload", true);
  const path = String(state.firmwarePath || "").trim();
  if (!path) {
    alert("Select firmware BIN first");
    return;
  }
  const firmwarePassword = String(state.firmwareAuthPassword || "").trim();
  if (!firmwarePassword) {
    alert("Firmware password session is missing.\nClose and reopen Firmware Update, then enter password.");
    return;
  }

  setFirmwareUploadBusy(true);
  setFirmwareStatus("Uploading firmware... please wait. Do not disconnect power.");
  logLine("SYS", `Firmware upload started: ${path}`);
  try {
    const res = await api.firmware_upload(path, firmwarePassword);
    if (res?.cancelled) {
      setFirmwareStatus("Firmware upload cancelled");
      logLine("SYS", "Firmware upload cancelled.");
      cancelFirmwareDialogSession();
      return;
    }
    if (!res?.ok) {
      const message = res?.error || "Firmware upload failed";
      setFirmwareStatus(message, true);
      logLine("ERR", `Firmware upload failed: ${message}`);
      alert(message);
      return;
    }
    if (state.firmwareSizeBytes > 0) {
      setFirmwareProgress(100, state.firmwareSizeBytes, state.firmwareSizeBytes, false);
    } else {
      setFirmwareProgress(100, 0, 0, false);
    }
    setFirmwareStatus("Firmware upload completed");
    logLine("SYS", "Firmware upload completed");
    logLine("SYS", "Device reboot expected after OTA. Closing firmware dialog and resetting connection.");
    clearFirmwareSession("Ready");
    if (el.firmwareDialog?.open) {
      el.firmwareDialog.close();
    }
    try {
      await api.disconnect();
    } catch (_disconnectErr) {
      // Device may already reboot/disconnect right after OTA; ignore.
    }
    try {
      await refreshEndpoints();
    } catch (_refreshErr) {
      // Keep UI usable even if endpoint refresh fails once.
    }
    await pollSnapshot();
    if (el.panelState) el.panelState.textContent = "RECONNECT DEVICE";
    alert("Firmware upload completed.\nDevice will restart now.\nReconnect the device after reboot.");
  } catch (err) {
    const message = err?.message || "Firmware upload failed";
    setFirmwareStatus(message, true);
    logLine("ERR", `Firmware upload failed: ${message}`);
    alert(message);
  } finally {
    firmwareCancelRequestPending = false;
    setFirmwareUploadBusy(false);
  }
}

async function pollSnapshot() {
  try {
    const snap = await api.get_snapshot(eventCursor);
    updateConnectionUi(snap);
    updateTelemetryUi(snap.telemetry || {});
    updateFirmwareProgressUi(snap);
    if (snap?.device_name && String(snap.device_name).trim() && String(snap.device_name).trim() !== "-") {
      setDeviceIdDisplay(String(snap.device_name).trim());
    }
    for (const evt of snap.events || []) {
      logLine(evt.direction, evt.text, evt.ts);
      if (String(evt.direction || "").toUpperCase() === "RX") {
        state.lastRxMonotonic = Date.now();
        tryUpdateDeviceIdFromRx(evt.text);
        handleProtocolRxLine(evt.text);
      }
    }
    eventCursor = Number(snap.next_event_id || eventCursor);
    updateClock();
  } catch (_e) {
    // ignore transient poll errors
  }
}

function resetEditorForNewProfile() {
  el.cfgMode.value = "0-10V";
  el.cfgInMin.value = "0";
  el.cfgInMax.value = "10";
  el.cfgOutMin.value = "0";
  el.cfgOutMax.value = "2000";
  forceOutputUnitCurrent();
  el.bpRows.innerHTML = "";
  el.advKp.value = "0.15";
  el.advKi.value = "0.00005";
  el.advDitherFreq.value = "150";
  el.advDitherAmp.value = "0.05";
  el.advPwmFreq.value = "2000";
  el.advDitherEnable.checked = true;
  syncDitherToggleState();
  el.teleInput.textContent = "--";
  el.teleOutput.textContent = "--";
  el.teleSat.textContent = "--";
  setLimitStatusAlert("--");
  state.monitorAwaitingRx = false;
  state.lastPoint = null;
  reindexRows();
  clearGraphData();
  updateGraphMeta("--", "--");
  state.currentProfilePath = "";
}

async function createNewProfileFile() {
  const ok = await askConfirmDialog("Clear current GUI values and start a new profile?", "Warning");
  if (!ok) return false;
  hideTabActions();
  resetEditorForNewProfile();
  logLine("SYS", "New file: CFG cleared, ADV reset to defaults");
  return true;
}

async function openProfileFile() {
  hideTabActions();
  try {
    const res = await api.open_profile_file(state.currentProfilePath || "");
    if (res?.cancelled) return false;
    if (!res?.ok) {
      alert(res?.error || "Open File failed");
      return false;
    }
    applyOpenedProfile(res.profile || {});
    state.currentProfilePath = String(res.path || "").trim();
    if (state.currentProfilePath) logLine("SYS", `Profile loaded: ${state.currentProfilePath}`);
    return true;
  } catch (err) {
    alert(err?.message || "Open File failed");
    return false;
  }
}

async function saveProfileFile() {
  hideTabActions();
  try {
    const payload = buildProfilePayload();
    const deviceName = state.deviceId && state.deviceId !== "-" ? state.deviceId : "SERVO_EVDR";
    const res = await api.save_profile_file(payload, state.currentProfilePath || "", deviceName);
    if (res?.cancelled) return false;
    if (!res?.ok) {
      alert(res?.error || "Save File failed");
      return false;
    }
    state.currentProfilePath = String(res.path || "").trim();
    if (state.currentProfilePath) logLine("SYS", `Profile saved: ${state.currentProfilePath}`);
    return true;
  } catch (err) {
    alert(err?.message || "Save File failed");
    return false;
  }
}

function isShortcutEditableTarget(target) {
  if (!target) return false;
  const elTarget = target;
  if (elTarget.isContentEditable) return true;
  const tag = String(elTarget.tagName || "").toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

function handleGlobalShortcuts(ev) {
  if (ev.defaultPrevented) return;
  if (ev.altKey) return;
  if (!(ev.ctrlKey || ev.metaKey)) return;
  if (isShortcutEditableTarget(ev.target)) return;
  const key = String(ev.key || "").toLowerCase();
  if (key === "n") {
    ev.preventDefault();
    void createNewProfileFile();
    return;
  }
  if (key === "o") {
    ev.preventDefault();
    void openProfileFile();
    return;
  }
  if (key === "s") {
    ev.preventDefault();
    void saveProfileFile();
  }
}

function bindEvents() {
  if (el.tabFiles) {
    el.tabFiles.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleTabActions("files", ev.currentTarget);
    });
  }
  if (el.tabSettings) {
    el.tabSettings.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleTabActions("settings", ev.currentTarget);
    });
  }
  if (el.tabActions) {
    el.tabActions.addEventListener("click", (ev) => ev.stopPropagation());
  }
  document.addEventListener("click", hideTabActions);
  document.addEventListener("keydown", handleGlobalShortcuts);
  if (el.btnPasswordPromptOk) {
    el.btnPasswordPromptOk.addEventListener("click", submitPasswordPrompt);
  }
  if (el.btnPasswordPromptCancel) {
    el.btnPasswordPromptCancel.addEventListener("click", () => finishPasswordPrompt(null));
  }
  if (el.passwordPromptInput) {
    el.passwordPromptInput.addEventListener("input", clearPasswordPromptError);
    el.passwordPromptInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        submitPasswordPrompt();
      }
    });
  }
  if (el.passwordPromptDialog) {
    el.passwordPromptDialog.addEventListener("cancel", (ev) => {
      ev.preventDefault();
      finishPasswordPrompt(null);
    });
    el.passwordPromptDialog.addEventListener("close", () => {
      if (typeof passwordPromptResolve === "function") {
        passwordPromptResolve(null);
        passwordPromptResolve = null;
      }
    });
  }
  if (el.btnConfirmDialogYes) {
    el.btnConfirmDialogYes.addEventListener("click", () => finishConfirmDialog(true));
  }
  if (el.btnConfirmDialogNo) {
    el.btnConfirmDialogNo.addEventListener("click", () => finishConfirmDialog(false));
  }
  if (el.confirmDialog) {
    el.confirmDialog.addEventListener("cancel", (ev) => {
      ev.preventDefault();
      finishConfirmDialog(false);
    });
    el.confirmDialog.addEventListener("close", () => {
      if (typeof confirmDialogResolve === "function") {
        confirmDialogResolve(false);
        confirmDialogResolve = null;
      }
    });
  }
  if (el.btnWarningDialogOk) {
    el.btnWarningDialogOk.addEventListener("click", closeWarningDialog);
  }
  if (el.warningDialog) {
    el.warningDialog.addEventListener("close", () => {
      finishUiAlertAndContinue();
    });
    el.warningDialog.addEventListener("cancel", () => {
      window.setTimeout(() => {
        if (!el.warningDialog?.open && uiAlertOpen) {
          finishUiAlertAndContinue();
        }
      }, 0);
    });
  }
  window.alert = (message) => appAlert(message, "auto");
  document.querySelectorAll("dialog.adv-dialog").forEach((dialog) => initDraggableDialog(dialog));
  if (el.developerDialog) {
    el.developerDialog.addEventListener("close", async () => {
      if (!state.devUnlocked) return;
      await developerLogout();
    });
  }
  if (el.firmwareDialog) {
    el.firmwareDialog.addEventListener("cancel", (ev) => {
      ev.preventDefault();
      handleFirmwareDialogCloseAttempt();
    });
    el.firmwareDialog.addEventListener("close", () => {
      setFirmwareUploadBusy(false);
      clearFirmwareAuthPassword();
    });
  }
  window.addEventListener("beforeunload", handleFirmwareBeforeUnload);

  el.transportSelect.addEventListener("change", async () => {
    if (state.connected) {
      const activeTransport = state.transport === "BLE" ? "BLE" : "UART";
      el.transportSelect.value = activeTransport;
      alert("Disconnect first, then change interface.");
      return;
    }
    await refreshEndpoints();
  });
  if (el.cfgMode) {
    el.cfgMode.addEventListener("change", () => {
      updateInputSignalCardPresentation();
      updateGraphMeta(el.teleInput.textContent || "--", el.teleOutput.textContent || "--");
      drawLinearityCanvas();
    });
  }
  el.btnRefresh.addEventListener("click", refreshEndpoints);
  el.btnConnect.addEventListener("click", toggleConnection);
  el.btnReadToggle.addEventListener("click", toggleMonitoring);
  el.btnReadDevice.addEventListener("click", async () => {
    await loadAllParametersFromDevice({ showAlerts: true, source: "manual", autoStopMonitoring: false });
  });
  el.btnApplyCfg.addEventListener("click", async () => {
    if (!state.connected) {
      alert("Connect device first");
      return;
    }
    if (!ensureNotMonitoring("applying configuration", true)) return;
    try {
      const payload = buildCfgPayload();
      el.panelState.textContent = "AUTH";
      const res = await askPassword("Enter AUTH password to apply configuration:", async (pwd) => {
        const applyRes = await api.apply_cfg_update(payload, pwd);
        if (!applyRes?.ok) {
          return { ok: false, error: applyRes?.error || "Wrong password or apply failed" };
        }
        return { ok: true, success: "Password correct", value: applyRes };
      });
      if (res === null) {
        el.panelState.textContent = state.connected ? "CONNECTED" : "IDLE";
        return;
      }
      if (res?.warning) logLine("SYS", res.warning);
      const verify = await api.load_config_from_device("cfg");
      if (verify?.ok && verify?.cfg) {
        applyCfgFromDevice(verify.cfg);
      } else if (verify?.error) {
        logLine("ERR", `CFG verify read failed: ${verify.error}`);
      }
      logLine("SYS", "Configuration updated");
      el.panelState.textContent = state.connected ? "CONNECTED" : "IDLE";
    } catch (err) {
      alert(err.message || "Invalid CFG input");
    }
  });
  el.btnApplyAdv.addEventListener("click", async () => {
    if (!state.connected) {
      alert("Connect device first");
      return;
    }
    if (!ensureNotMonitoring("applying advanced parameters", true)) return;
    try {
      const payload = buildAdvPayload();
      el.panelState.textContent = "AUTH";
      const res = await askPassword("Enter AUTH password to apply advanced parameters:", async (pwd) => {
        const applyRes = await api.apply_adv_update(payload, pwd);
        if (!applyRes?.ok) {
          return { ok: false, error: applyRes?.error || "Wrong password or apply failed" };
        }
        return { ok: true, success: "Password correct", value: applyRes };
      });
      if (res === null) {
        el.panelState.textContent = state.connected ? "CONNECTED" : "IDLE";
        return;
      }
      if (res?.warning) logLine("SYS", res.warning);
      const verify = await api.load_config_from_device("adv");
      if (verify?.ok && verify?.adv) {
        applyAdvFromDevice(verify.adv);
      } else if (verify?.error) {
        logLine("ERR", `ADV verify read failed: ${verify.error}`);
      }
      logLine("SYS", "Advanced parameters updated");
      el.panelState.textContent = state.connected ? "CONNECTED" : "IDLE";
    } catch (err) {
      alert(err?.message || "Invalid ADV input");
    }
  });
  el.advDitherEnable.addEventListener("change", syncDitherToggleState);
  el.btnResetPlot.addEventListener("click", clearGraphData);
  el.btnClearLog.addEventListener("click", async () => {
    el.logBox.innerHTML = "";
    await api.clear_log();
  });
  el.btnAddPoint.addEventListener("click", () => addBreakpointRow());
  if (el.menuTheme) {
    el.menuTheme.addEventListener("click", () => {
      hideTabActions();
      if (el.themeSelect) el.themeSelect.value = state.theme;
      el.themeDialog.showModal();
    });
  }
  if (el.btnThemeApply) {
    el.btnThemeApply.addEventListener("click", () => {
      const selected = el.themeSelect ? el.themeSelect.value : state.theme;
      applyTheme(selected, true);
      if (el.themeDialog?.open) el.themeDialog.close();
      logLine("SYS", `Theme changed to ${state.theme}`);
    });
  }
  el.menuAdvanced.addEventListener("click", () => {
    hideTabActions();
    el.advancedDialog.showModal();
  });
  el.menuDeveloper.addEventListener("click", async () => {
    hideTabActions();
    await openDeveloperModeDialog();
  });
  el.menuFirmware.addEventListener("click", async () => {
    hideTabActions();
    await openFirmwareDialog();
  });
  el.menuEventLog.addEventListener("click", () => {
    hideTabActions();
    el.eventLogDialog.showModal();
  });
  el.menuNewFile.addEventListener("click", async () => {
    await createNewProfileFile();
  });
  el.menuOpenFile.addEventListener("click", async () => {
    await openProfileFile();
  });
  el.menuSaveFile.addEventListener("click", async () => {
    await saveProfileFile();
  });

  if (el.btnCalLoad) el.btnCalLoad.addEventListener("click", () => developerLoadCalibration());
  if (el.btnCalSave) el.btnCalSave.addEventListener("click", developerSaveCalibration);

  el.btnPickFirmware.addEventListener("click", pickFirmwareFile);
  el.btnFirmwareUpload.addEventListener("click", uploadFirmware);
  if (el.btnFirmwareCancel) {
    el.btnFirmwareCancel.addEventListener("click", handleFirmwareDialogCloseAttempt);
  }


  if (el.linearityCanvas) {
    el.linearityCanvas.addEventListener("mousemove", onLinearityMouseMove);
    el.linearityCanvas.addEventListener("mouseleave", onLinearityMouseLeave);
  }

  window.addEventListener("resize", () => {
    scheduleDrawGraphs();
    if (el.tabActions && el.tabActions.classList.contains("open")) {
      if (el.tabFiles?.classList.contains("active")) {
        showTabActions("files", el.tabFiles);
      } else if (el.tabSettings?.classList.contains("active")) {
        showTabActions("settings", el.tabSettings);
      }
    }
    document.querySelectorAll("dialog.adv-dialog[open]").forEach((dialog) => {
      if (dialog.style.left && dialog.style.top) {
        const rect = dialog.getBoundingClientRect();
        setDialogPosition(dialog, rect.left, rect.top);
      }
    });
  });
}

async function init() {
  if (!window.pywebview || !window.pywebview.api) {
    alert("Run this from pywebview: python WEBUI_APP.py");
    return;
  }
  loadThemePreference();
  api = window.pywebview.api;
  bindEvents();

  setActiveTopTab("files");
  setDeviceIdDisplay("-");
  reindexRows();
  setMonitorButton();
  syncDitherToggleState();
  resetDeveloperUi();
  setFirmwareStatus("Ready");
  setFirmwareProgress(0, 0, 0, false);
  updateInputSignalCardPresentation();
  updateGraphMeta();
  await refreshEndpoints();
  await pollSnapshot();
  drawGraphs();

  pollTimer = setInterval(pollSnapshot, 450);
  setInterval(updateClock, 1000);
  logLine("SYS", "Industrial UI v2 ready. Select Serial/Bluetooth, establish link, then click Start Acquisition.");
  logLine("SYS", "Update flow: Stop Acquisition -> Apply Configuration/Advanced Parameters -> Start Acquisition");
  logLine("SYS", "Developer Mode: use Developer Mode for calibration.");
  logLine("SYS", "Firmware Update: open from Settings -> Firmware Update.");
}

window.addEventListener("pywebviewready", init);
