import asyncio
from collections import deque
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    BleakClient = None
    BleakScanner = None

# ---------------------------------------------------------------------------
# Default payloads and protocol constants
# ---------------------------------------------------------------------------
DEFAULT_ADV = {
    "kp": 0.15,
    "ki": 0.00005,
    "ditherEnable": 1,
    "ditherFreq": 150,
    "ditherAmplitude": 0.05,
    "pwmFreq": 2000,
}

TELEMETRY_META = [
    ("input", "Input Signal", "Input Measurement"),
    ("output", "Measured Output", "Measured Current"),
    ("sat", "Limit Status", "Limit Condition"),
]
TELEMETRY_KEYS = [key for key, _, _ in TELEMETRY_META]
MODE_OPTIONS = ["Resistance (0-10)", "0-5V", "0-10V", "0-20mA"]
TRANSPORT_OPTIONS = ["UART", "BLE"]

BLE_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
BLE_LIVE_CHAR_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"
BLE_CONFIG_CHAR_UUID = "0000ffe5-0000-1000-8000-00805f9b34fb"
BLE_ADV_CHAR_UUID = "0000ffe6-0000-1000-8000-00805f9b34fb"
APP_ICON_CANDIDATES = [
    "app_icon.ico",
    "logo.ico",
    "icon.ico",
    "app_icon.png",
    "logo.png",
    "icon.png",
]
APP_USER_MODEL_ID = "servo.evdr.panel"
APP_SETTINGS_FILE = "user_preferences.json"
AUTH_RESPONSE_TIMEOUT_MS = 2600
AUTH_RETRY_COUNT = 2
UART_POST_AUTH_UPDATE_DELAY_MS = 140
FIXED_UART_BAUD = 9600
UART_BAUD_CANDIDATES = (FIXED_UART_BAUD, 115200)
AUTO_READ_INTERVAL_MS = 180
AUTO_READ_RESPONSE_GUARD_MS = 450
SMOOTH_UPDATE_WINDOW_MS = 2600
DEV_AUTH_RESPONSE_TIMEOUT_MS = 3200
FW_CMD_RESPONSE_TIMEOUT_MS = 12000
FW_CHUNK_ACK_TIMEOUT_MS = 12000
# Keep the hex command below a 512-byte BLE MTU, including offset metadata.
FW_CHUNK_BYTES = 220
FW_CHUNK_MIN_BYTES = 100
FW_CHUNK_LEGACY_BYTES = 100
FW_CHUNK_RETRY_COUNT = 2
# OTA policy: this app build currently accepts only SERVO_EVDR firmware images.
FW_ALLOWED_VARIANT = "SERVO_EVDR"
# Marker that must be present inside BIN bytes for model identity.
FW_REQUIRED_MODEL_MARKER = f"FW_MODEL={FW_ALLOWED_VARIANT}"
# Marker that must be present inside BIN bytes for project identity.
FW_REQUIRED_PROJECT_MARKER = "FW_PROJECT=FINAL_BLE_UART_CODE"
# Number of SHA-256 hex characters sent in FW_BEGIN for integrity check.
FW_HASH_PREFIX_LEN = 16

GRAPH_COLOR_VOLTAGE = "#57c4ff"
GRAPH_COLOR_CURRENT = "#e85656"
GRAPH_COLOR_SET_CURRENT = "#71d18a"
GRAPH_DASH_SET_CURRENT = (2, 4)
GRAPH_COLOR_LIVE_CURRENT = GRAPH_COLOR_CURRENT
GRAPH_HOVER_MAX_DISTANCE_PX = 14
TREND_WINDOW_SECONDS = 30.0
BLE_GRAPH_SMOOTH_TICK_MS = 60
BLE_GRAPH_SMOOTH_TAU_SECONDS = 0.22
BLE_GRAPH_SNAP_INPUT = 0.01
BLE_GRAPH_SNAP_OUTPUT = 0.6
GRAPH_HISTORY_POINTS = 900


# ---------------------------------------------------------------------------
# Main GUI controller
# ---------------------------------------------------------------------------
class DeviceTestGUI:
    """Main GUI controller for SERVO EVDR over Serial or BLE."""

    def __init__(self, root: tk.Tk) -> None:
        # Window and top-level app identity.
        """Initialize application state, communication handlers, and UI widgets."""
        self.root = root
        self.root.title("SERVO EVDR APPLICATION")
        self.root.geometry("1340x900")
        self.root.minsize(1180, 780)
        self._window_icon_ref = None
        self._apply_window_icon()

        # Runtime communication state.
        self.ser = None
        self.reader_thread = None
        self.stop_event = threading.Event()
        self.rx_queue = queue.Queue()

        # Connection and UI-bound variables.
        self.transport_var = tk.StringVar(value="UART")
        self.transport_display_var = tk.StringVar(value="Serial")
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value=str(FIXED_UART_BAUD))
        self.endpoint_label_var = tk.StringVar(value="Serial Port")
        self.refresh_btn_text_var = tk.StringVar(value="↻")
        self.auto_read_var = tk.BooleanVar(value=False)
        self.current_profile_path = ""
        self.recent_files = []
        self.file_recent_menu = None
        self.transport_combo = None
        self.port_combo = None
        self.baud_entry = None
        self.refresh_btn = None
        self.read_toggle_btn = None
        self.load_cfg_btn = None
        self.apply_cfg_btn = None
        self.apply_adv_btn = None
        self.device_name_btn = None
        self.adv_window = None
        self.dev_window = None
        self.firmware_window = None
        self.dev_window_status_var = tk.StringVar(value="Locked")
        self.fw_upload_progress_var = tk.DoubleVar(value=0.0)
        self.fw_upload_progress_text_var = tk.StringVar(value="BIN Upload Progress: Idle (0%)")
        self.fw_upload_file_var = tk.StringVar(value="")
        self.fw_upload_progressbar = None
        self.fw_upload_progress_label = None
        self.fw_upload_file_label = None
        self.fw_upload_progress_frame = None
        self.fw_update_btn = None
        self.dev_load_cal_btn = None
        self.dev_save_cal_btn = None
        self.dev_logout_btn = None
        self.fw_upload_active = False
        self.fw_window_box = None
        self.dev_cal_vars = {
            "MA_SCALE_FACTOR_4_20MA": tk.StringVar(value=""),
            "RESISTANCE_SCALE_FACTOR": tk.StringVar(value=""),
            "VOLTAGE_SCALE_FACTOR": tk.StringVar(value=""),
            "FB_TO_CURRENT_FACTOR": tk.StringVar(value=""),
            "FB_OFFSET": tk.StringVar(value=""),
        }
        self.graph_canvas = None
        self.time_graph_canvas = None
        self.clear_graph_btn = None
        self.graph_mode_var = tk.StringVar(value="both")
        self.graph_hover_points = []
        self.conn_status_var = tk.StringVar(value="Disconnected")
        self.device_name_var = tk.StringVar(value="Device ID")
        self.panel_state_var = tk.StringVar(value="IDLE")
        self.dev_status_var = tk.StringVar(value="DEV: LOCKED | STATE: NORMAL_MODE | FW: -")
        # Live telemetry and graph buffers.
        self.graph_input_points = deque(maxlen=GRAPH_HISTORY_POINTS)
        self.graph_output_points = deque(maxlen=GRAPH_HISTORY_POINTS)
        self.graph_command_points = deque(maxlen=GRAPH_HISTORY_POINTS)
        self.graph_iv_points = deque(maxlen=GRAPH_HISTORY_POINTS)
        self.graph_time_points = deque(maxlen=900)
        self.graph_time_voltage_points = deque(maxlen=900)
        self.graph_time_current_points = deque(maxlen=900)
        self.graph_time_start_monotonic = None
        self.live_input_seen = None
        self.live_output_seen = None
        self.live_output_set = None
        self._ble_target_input = None
        self._ble_target_output = None
        self._ble_target_set = None
        self._ble_smooth_last_monotonic = time.monotonic()
        self.last_rx_monotonic = time.monotonic()
        self._last_auto_read_sent_at = 0.0
        self._auto_read_waiting_rx = False
        self.last_tx_cmd = ""
        self.last_dedup_line = ""
        self.last_dedup_at = 0.0
        self.pending_load_until = 0.0
        self.pending_load_scope = "both"
        self.pending_devname_until = 0.0
        self.last_cfgadv_rx_monotonic = 0.0
        self.devname_retry_left = 0
        # Secured update/auth state machine.
        self.awaiting_auth_for_update = False
        self.pending_secured_update_cmd = ""
        self.pending_auth_cmd = ""
        self.auth_retry_left = 0
        self.auth_request_id = 0
        self.last_auth_cmd_used = ""
        self.update_reauth_retry_left = 0
        self.last_requested_update_cmd = ""
        self.pending_update_scope = "both"
        self.awaiting_update_result = False
        self.last_update_cmd_sent = ""
        self.last_update_sent_at = 0.0
        self.update_legacy_retry_done = False
        self.smooth_update_until = 0.0
        self.fw_version = ""
        self.ota_result = "none"
        self.device_mode_state = "NORMAL_MODE"
        self.dev_authenticated = False
        self.fw_update_mode = False
        self.cal_payload_cache = {}
        self._sync_wait_active = False
        self._sync_wait_line = ""
        self._sync_wait_match = None
        self._disconnecting = False
        self._is_closing = False

        # BLE runtime objects.
        self.ble_client = None
        self.ble_connected = False
        self.ble_loop = None
        self.ble_loop_thread = None
        self.ble_scan_in_progress = False
        self.ble_display_to_address = {}

        # Telemetry values shown in cards.
        self.telemetry_vars = {k: tk.StringVar(value="-") for k in TELEMETRY_KEYS}

        # CFG form fields.
        self.cfg_mode_var = tk.StringVar(value="")
        self.cfg_in_min_var = tk.StringVar(value="")
        self.cfg_in_max_var = tk.StringVar(value="")
        self.cfg_out_min_var = tk.StringVar(value="")
        self.cfg_out_max_var = tk.StringVar(value="")
        self.cfg_unit_var = tk.StringVar(value="")
        self.cfg_bp_in_vars = [tk.StringVar(value="") for _ in range(5)]
        self.cfg_bp_out_vars = [tk.StringVar(value="") for _ in range(5)]
        self.cfg_bp_row_frames = []

        # ADV form fields.
        self.adv_kp_var = tk.StringVar(value=str(DEFAULT_ADV["kp"]))
        self.adv_ki_var = tk.StringVar(value=str(DEFAULT_ADV["ki"]))
        self.adv_dither_enable_var = tk.BooleanVar(value=bool(DEFAULT_ADV["ditherEnable"]))
        self.adv_dither_freq_var = tk.StringVar(value=str(DEFAULT_ADV["ditherFreq"]))
        self.adv_dither_amp_var = tk.StringVar(value=str(DEFAULT_ADV["ditherAmplitude"]))
        self.adv_pwm_freq_var = tk.StringVar(value=str(DEFAULT_ADV["pwmFreq"]))

        self._cfg_graph_trace_vars = [
            self.cfg_mode_var,
            self.cfg_in_min_var,
            self.cfg_in_max_var,
            self.cfg_out_min_var,
            self.cfg_out_max_var,
        ] + self.cfg_bp_in_vars + self.cfg_bp_out_vars
        for var in self._cfg_graph_trace_vars:
            var.trace_add("write", self._on_cfg_graph_params_changed)

        self._load_app_preferences()

        # UI bootstrap and background loops.
        self._build_styles()
        self._build_ui()
        self._clear_cfg_form()
        self.refresh_ports()
        self._poll_rx_queue()
        self._auto_read_tick()
        self._ble_graph_smooth_tick()

    # ------------------------------------------------------------------
    # Resource and UI setup
    # ------------------------------------------------------------------
    def _resource_search_dirs(self):
        """Return candidate folders where icon/assets may be present."""
        dirs = []
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            dirs.extend([exe_dir, exe_dir / "assets"])

        script_dir = Path(__file__).resolve().parent
        dirs.extend([script_dir, script_dir / "assets", Path.cwd(), Path.cwd() / "assets"])

        unique_dirs = []
        seen = set()
        for d in dirs:
            key = str(d).lower()
            if key in seen:
                continue
            seen.add(key)
            unique_dirs.append(d)
        return unique_dirs

    def _apply_window_icon(self) -> None:
        """Apply window icon."""
        for search_dir in self._resource_search_dirs():
            for filename in APP_ICON_CANDIDATES:
                icon_path = search_dir / filename
                if not icon_path.exists():
                    continue
                try:
                    if icon_path.suffix.lower() == ".ico":
                        self.root.iconbitmap(str(icon_path))
                    else:
                        self._window_icon_ref = tk.PhotoImage(file=str(icon_path))
                        self.root.iconphoto(True, self._window_icon_ref)
                    return
                except Exception:
                    continue

    def _build_styles(self) -> None:
        """Build styles."""
        self.root.configure(bg="#0f1419")
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure("Panel.TFrame", background="#0f1419")
        style.configure("Inner.TFrame", background="#1b232c")
        style.configure(
            "Card.TLabelframe",
            background="#1b232c",
            foreground="#d8e1eb",
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background="#1b232c",
            foreground="#d8e1eb",
            font=("Segoe UI Semibold", 10),
        )
        style.configure("TopCard.TFrame", background="#131b22")
        style.configure("TopField.TLabel", background="#131b22", foreground="#91a1b2", font=("Segoe UI", 9))
        style.configure("TopValue.TLabel", background="#131b22", foreground="#f2f7ff", font=("Segoe UI Semibold", 10))
        style.configure("MenuStrip.TFrame", background="#1b1b1b", borderwidth=1, relief="solid")
        style.configure("MenuStrip.TLabel", background="#1b1b1b", foreground="#f3f3f3", font=("Segoe UI Semibold", 10))
        style.configure("Title.TLabel", background="#0f1419", foreground="#eef4fb", font=("Bahnschrift SemiBold", 18))
        style.configure("Brand.TLabel", background="#0f1419", foreground="#d64a4a", font=("Bahnschrift SemiBold", 16))
        style.configure("SubTitle.TLabel", background="#0f1419", foreground="#98a8b9", font=("Segoe UI", 10))
        style.configure("Field.TLabel", background="#1b232c", foreground="#b9c8d6", font=("Segoe UI", 10))
        style.configure("ConnField.TLabel", background="#1b232c", foreground="#c2c8d0", font=("Segoe UI Semibold", 10))
        style.configure("TelemetryHint.TLabel", background="#1b232c", foreground="#7f93a8", font=("Segoe UI", 9))
        style.configure("Value.TLabel", background="#1b232c", foreground="#57c4ff", font=("Consolas", 15, "bold"))
        style.configure("Status.TLabel", background="#0f1419", foreground="#8ed79f", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI Semibold", 10))
        style.configure("Primary.TButton", foreground="#10161d", background="#52b7ff", padding=(12, 6), borderwidth=0)
        style.map(
            "Primary.TButton",
            background=[("active", "#6cc4ff"), ("pressed", "#3da9f6"), ("disabled", "#5c7386")],
            foreground=[("disabled", "#d7e3ee")],
        )
        style.configure("Accent.TButton", foreground="#eaf6ff", background="#28825b", padding=(12, 6), borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#2f9568"), ("pressed", "#21734f")])
        style.configure("Warn.TButton", foreground="#fff5f5", background="#ba4b4b", padding=(12, 6), borderwidth=0)
        style.map("Warn.TButton", background=[("active", "#cb5a5a"), ("pressed", "#a44141")])
        style.configure("MiniWarn.TButton", foreground="#fff5f5", background="#ba4b4b", padding=(8, 2), borderwidth=0)
        style.map("MiniWarn.TButton", background=[("active", "#cb5a5a"), ("pressed", "#a44141")])
        style.configure("Neutral.TButton", foreground="#d9e6f3", background="#2f3d4b", padding=(12, 6), borderwidth=0)
        style.map("Neutral.TButton", background=[("active", "#3a4a59"), ("pressed", "#253140")])
        style.configure("Icon.TButton", foreground="#d9e6f3", background="#2f3d4b", padding=(6, 5), borderwidth=0)
        style.map("Icon.TButton", background=[("active", "#3a4a59"), ("pressed", "#253140")])
        style.configure(
            "TransportOn.TButton",
            foreground="#0d1520",
            background="#57c4ff",
            padding=(12, 6),
            borderwidth=1,
            relief="solid",
        )
        style.map(
            "TransportOn.TButton",
            background=[("active", "#6fd0ff"), ("pressed", "#42b5f4"), ("disabled", "#5c7386")],
            foreground=[("disabled", "#d7e3ee")],
        )
        style.configure(
            "TransportOff.TButton",
            foreground="#c9d8e6",
            background="#22313d",
            padding=(12, 6),
            borderwidth=1,
            relief="solid",
        )
        style.map(
            "TransportOff.TButton",
            background=[("active", "#2b3c4b"), ("pressed", "#1d2a35"), ("disabled", "#1a2530")],
            foreground=[("disabled", "#6d8296")],
        )
        style.configure("TCheckbutton", background="#1b232c", foreground="#d8e1eb")
        style.map("TCheckbutton", background=[("active", "#1b232c")])
        style.configure("TRadiobutton", background="#1b232c", foreground="#d8e1eb")
        style.map("TRadiobutton", background=[("active", "#1b232c")])
        style.configure(
            "FW.Horizontal.TProgressbar",
            troughcolor="#111920",
            background="#52b7ff",
            bordercolor="#2e3c4a",
            lightcolor="#52b7ff",
            darkcolor="#3da9f6",
            thickness=18,
        )
        style.configure(
            "TEntry",
            fieldbackground="#111920",
            foreground="#e6edf5",
            bordercolor="#2e3c4a",
            lightcolor="#2e3c4a",
            darkcolor="#2e3c4a",
            insertcolor="#e6edf5",
            padding=(4, 3),
        )
        style.configure(
            "TCombobox",
            fieldbackground="#111920",
            foreground="#e6edf5",
            bordercolor="#2e3c4a",
            lightcolor="#2e3c4a",
            darkcolor="#2e3c4a",
            padding=(3, 1),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#111920"), ("disabled", "#0f1419")],
            foreground=[("readonly", "#e6edf5"), ("disabled", "#708396")],
            selectbackground=[("readonly", "#2f3d4b")],
            selectforeground=[("readonly", "#e6edf5")],
        )
        self.root.option_add("*TCombobox*Listbox.background", "#111920")
        self.root.option_add("*TCombobox*Listbox.foreground", "#e6edf5")
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#2f3d4b")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#e6edf5")

    def _build_ui(self) -> None:
        """Create the complete application layout and connect actions."""
        root_frame = ttk.Frame(self.root, style="Panel.TFrame", padding=12)
        root_frame.pack(fill="both", expand=True)

        header = ttk.Frame(root_frame, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 10))
        header.grid_columnconfigure(0, weight=1)
        ttk.Label(header, text="SERVO EVDR - OPERATOR CONSOLE", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="SERVOCONTROLS", style="Brand.TLabel").grid(row=0, column=1, sticky="e")

        self._build_top_menu_strip(root_frame)
        self._build_connection_panel(root_frame)

        main_body = ttk.Frame(root_frame, style="Panel.TFrame")
        main_body.pack(fill="both", expand=True)

        left_col = ttk.Frame(main_body, style="Panel.TFrame")
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right_col = ttk.Frame(main_body, style="Panel.TFrame")
        right_col.pack(side="left", fill="both", expand=True, padx=(6, 0))

        self._build_telemetry_panel(left_col)
        self._build_parameter_panels(left_col)
        self._build_log_panel(left_col)
        self._build_graph_panel(right_col)
        self._set_update_buttons_enabled(True)

        self._log("Industrial UI v2 ready. Select Serial/BLE, establish link, then click Start Acquisition.")
        self._log("Update flow: Stop Acquisition -> Apply Configuration/Advanced Parameters -> Start Acquisition")
        self._log("Developer Mode: use Developer Mode for calibration.")
        self._log("Firmware Update: open from Settings -> Firmware Update.")

    def _build_top_menu_strip(self, parent: ttk.Frame) -> None:
        """Render top menu strip with File and Settings menus."""
        menu_bar = ttk.Frame(parent, style="MenuStrip.TFrame", padding=(10, 4))
        menu_bar.pack(fill="x", pady=(0, 8))

        file_btn = tk.Menubutton(
            menu_bar,
            text="File",
            font=("Segoe UI Semibold", 10),
            bg="#1b1b1b",
            fg="#f3f3f3",
            activebackground="#2a2a2a",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=2,
            pady=0,
            highlightthickness=0,
        )
        file_btn.grid(row=0, column=0, sticky="w", padx=(0, 18))

        file_menu = tk.Menu(
            file_btn,
            tearoff=0,
            bg="#1f1f1f",
            fg="#f3f3f3",
            activebackground="#2f3d4b",
            activeforeground="#ffffff",
        )
        file_btn.configure(menu=file_menu)
        file_menu.add_command(label="New File", command=self._menu_new_file)
        file_menu.add_command(label="Open File...", command=self._menu_open_file)

        self.file_recent_menu = tk.Menu(
            file_menu,
            tearoff=0,
            bg="#1f1f1f",
            fg="#f3f3f3",
            activebackground="#2f3d4b",
            activeforeground="#ffffff",
        )
        file_menu.add_cascade(label="Open Recent", menu=self.file_recent_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Save", command=self._menu_save_file)
        self._refresh_file_recent_menu()

        settings_btn = tk.Menubutton(
            menu_bar,
            text="Settings",
            font=("Segoe UI Semibold", 10),
            bg="#1b1b1b",
            fg="#f3f3f3",
            activebackground="#2a2a2a",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=2,
            pady=0,
            highlightthickness=0,
        )
        settings_btn.grid(row=0, column=1, sticky="w")

        settings_menu = tk.Menu(
            settings_btn,
            tearoff=0,
            bg="#1f1f1f",
            fg="#f3f3f3",
            activebackground="#2f3d4b",
            activeforeground="#ffffff",
        )
        settings_btn.configure(menu=settings_menu)
        settings_menu.add_command(label="Advanced Parameters", command=self._open_adv_window)
        settings_menu.add_command(label="Developer Mode", command=self.open_developer_mode_window)
        settings_menu.add_command(label="Firmware Update", command=self.open_firmware_update_window)

    def _remember_recent_file(self, file_path: Path) -> None:
        """Track a profile path in Open Recent list."""
        norm = str(file_path.resolve())
        self.recent_files = [p for p in self.recent_files if p != norm]
        self.recent_files.insert(0, norm)
        self.recent_files = self.recent_files[:8]
        self._save_app_preferences()
        self._refresh_file_recent_menu()

    def _refresh_file_recent_menu(self) -> None:
        """Rebuild the Open Recent submenu from in-memory list."""
        if self.file_recent_menu is None:
            return
        self.file_recent_menu.delete(0, "end")

        existing = []
        for raw in self.recent_files:
            txt = str(raw).strip()
            if not txt:
                continue
            if Path(txt).exists():
                existing.append(txt)

        self.recent_files = existing[:8]
        if not self.recent_files:
            self.file_recent_menu.add_command(label="(No recent files)", state="disabled")
            return

        for path_text in self.recent_files:
            self.file_recent_menu.add_command(
                label=path_text,
                command=lambda p=path_text: self._open_profile_file(Path(p)),
            )

    def _menu_new_file(self) -> None:
        """Reset GUI to a fresh profile: blank CFG, default ADV, cleared telemetry."""
        if not messagebox.askyesno("New File", "Clear current GUI values and start a new profile?"):
            return

        self._clear_cfg_form()
        self.cfg_mode_var.set("")
        self.cfg_in_min_var.set("")
        self.cfg_in_max_var.set("")
        self.cfg_out_min_var.set("")
        self.cfg_out_max_var.set("")
        self.cfg_unit_var.set("")
        for idx in range(5):
            self.cfg_bp_in_vars[idx].set("")
            self.cfg_bp_out_vars[idx].set("")
            if idx < len(self.cfg_bp_row_frames):
                self.cfg_bp_row_frames[idx].grid_remove()

        self._load_adv_json(json.dumps(DEFAULT_ADV, separators=(",", ":")))
        for key in TELEMETRY_KEYS:
            if key in self.telemetry_vars:
                self.telemetry_vars[key].set("-")
        self._clear_graph_live_values()
        self.current_profile_path = ""
        self._log("New file: CFG cleared, ADV reset to defaults")

    def _menu_open_file(self) -> None:
        """Open a saved text profile and load values into GUI only."""
        initial_dir = ""
        if self.current_profile_path:
            initial_dir = str(Path(self.current_profile_path).parent)
        elif self.recent_files:
            initial_dir = str(Path(self.recent_files[0]).parent)
        else:
            initial_dir = str(Path.cwd())

        file_path = filedialog.askopenfilename(
            title="Open Profile File",
            initialdir=initial_dir,
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not file_path:
            return
        self._open_profile_file(Path(file_path))

    def _open_profile_file(self, file_path: Path) -> None:
        """Load profile text file values into GUI fields without sending to device."""
        try:
            text = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Open File", f"Failed to read profile file.\n\n{exc}")
            return

        try:
            values = self._parse_profile_text_values(text)
            is_valid, validation_message = self._validate_profile_text_values(values)
            if not is_valid:
                raise ValueError(validation_message)
            self._apply_profile_text_values(values)
        except Exception as exc:
            messagebox.showerror("Open File", f"Profile format is invalid.\n\n{exc}")
            return

        self.current_profile_path = str(file_path.resolve())
        self._remember_recent_file(file_path)
        self._log(f"Profile loaded: {self.current_profile_path}")

    def _menu_save_file(self) -> None:
        """Save current GUI CFG/ADV values as a text profile."""
        suggested_name = f"{self._safe_export_filename_base()}_profile.txt"
        initial_dir = str(Path.cwd())
        initial_file = suggested_name

        if self.current_profile_path:
            try:
                current_path = Path(self.current_profile_path)
                if current_path.parent.exists():
                    initial_dir = str(current_path.parent)
                if current_path.name:
                    initial_file = current_path.name
            except Exception:
                pass

        chosen = filedialog.asksaveasfilename(
            title="Save Profile File",
            initialdir=initial_dir,
            initialfile=initial_file,
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not chosen:
            return
        target_path = Path(chosen)

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(self._build_profile_text_lines()).rstrip() + "\n"
            target_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Save File", f"Failed to save profile file.\n\n{exc}")
            return

        self.current_profile_path = str(target_path.resolve())
        self._log(f"Profile saved: {self.current_profile_path}")

    def _normalize_profile_value(self, value: str) -> str:
        """Convert placeholder text values back to empty GUI field text."""
        txt = str(value).strip()
        if txt in ("", "-", "None", "none", "null", "NULL"):
            return ""
        return txt

    def _build_profile_text_lines(self) -> list[str]:
        """Build text profile lines from current GUI field values."""
        device_name = self.device_name_var.get().strip() or "SERVO_EVDR"
        lines = [
            f"device_name={device_name}",
            f"saved_at={time.strftime('%Y-%m-%d %H:%M:%S')}",
            "format=SERVO_EVDR_PROFILE_TEXT",
            "",
            "[Configuration]",
            f"mode={self.cfg_mode_var.get().strip() or '-'}",
            f"input_min={self.cfg_in_min_var.get().strip() or '-'}",
            f"input_max={self.cfg_in_max_var.get().strip() or '-'}",
            f"output_min={self.cfg_out_min_var.get().strip() or '-'}",
            f"output_max={self.cfg_out_max_var.get().strip() or '-'}",
            f"unit={self.cfg_unit_var.get().strip() or '-'}",
        ]
        for idx in range(5):
            bp_in = self.cfg_bp_in_vars[idx].get().strip() or "-"
            bp_out = self.cfg_bp_out_vars[idx].get().strip() or "-"
            lines.append(f"bp{idx + 1}_input={bp_in}")
            lines.append(f"bp{idx + 1}_output={bp_out}")

        lines.extend(
            [
                "",
                "[Advanced Parameters]",
                f"kp={self.adv_kp_var.get().strip() or '-'}",
                f"ki={self.adv_ki_var.get().strip() or '-'}",
                f"ditherEnable={1 if self.adv_dither_enable_var.get() else 0}",
                f"ditherFreq={self.adv_dither_freq_var.get().strip() or '-'}",
                f"ditherAmplitude={self.adv_dither_amp_var.get().strip() or '-'}",
                f"pwmFreq={self.adv_pwm_freq_var.get().strip() or '-'}",
            ]
        )
        return lines

    def _parse_profile_text_values(self, content: str) -> dict[str, str]:
        """Parse `key=value` text profile content into a value map."""
        values = {}
        for raw_line in str(content).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key_txt = key.strip()
            if not key_txt:
                continue
            values[key_txt] = value.strip()
        return values

    def _validate_profile_text_values(self, values: dict[str, str]) -> tuple[bool, str]:
        """Validate text profile key set before applying to GUI."""
        if not isinstance(values, dict) or not values:
            return False, "File is empty or does not contain key=value entries."

        required_keys = [
            "mode",
            "input_min",
            "input_max",
            "output_min",
            "output_max",
            "unit",
            "kp",
            "ki",
            "ditherEnable",
            "ditherFreq",
            "ditherAmplitude",
            "pwmFreq",
        ]
        for idx in range(1, 6):
            required_keys.append(f"bp{idx}_input")
            required_keys.append(f"bp{idx}_output")

        missing = [k for k in required_keys if k not in values]
        if missing:
            sample = ", ".join(missing[:6])
            if len(missing) > 6:
                sample += ", ..."
            return False, f"Not a valid SERVO profile text file.\nMissing keys: {sample}"

        return True, ""

    def _apply_profile_text_values(self, values: dict[str, str]) -> None:
        """Apply parsed text profile values to GUI fields only."""
        self.cfg_mode_var.set(self._normalize_profile_value(values.get("mode", "")))
        self.cfg_in_min_var.set(self._normalize_profile_value(values.get("input_min", "")))
        self.cfg_in_max_var.set(self._normalize_profile_value(values.get("input_max", "")))
        self.cfg_out_min_var.set(self._normalize_profile_value(values.get("output_min", "")))
        self.cfg_out_max_var.set(self._normalize_profile_value(values.get("output_max", "")))
        self.cfg_unit_var.set(self._normalize_profile_value(values.get("unit", "")))

        for idx in range(5):
            bp_in_txt = self._normalize_profile_value(values.get(f"bp{idx + 1}_input", ""))
            bp_out_txt = self._normalize_profile_value(values.get(f"bp{idx + 1}_output", ""))
            self.cfg_bp_in_vars[idx].set(bp_in_txt)
            self.cfg_bp_out_vars[idx].set(bp_out_txt)
            if idx < len(self.cfg_bp_row_frames):
                if bp_in_txt or bp_out_txt:
                    self.cfg_bp_row_frames[idx].grid()
                else:
                    self.cfg_bp_row_frames[idx].grid_remove()

        self.adv_kp_var.set(self._normalize_profile_value(values.get("kp", str(DEFAULT_ADV["kp"]))) or str(DEFAULT_ADV["kp"]))
        self.adv_ki_var.set(self._normalize_profile_value(values.get("ki", str(DEFAULT_ADV["ki"]))) or str(DEFAULT_ADV["ki"]))
        dither_enable_txt = self._normalize_profile_value(values.get("ditherEnable", str(DEFAULT_ADV["ditherEnable"])))
        if not dither_enable_txt:
            dither_enable_txt = str(DEFAULT_ADV["ditherEnable"])
        self.adv_dither_enable_var.set(str(dither_enable_txt).strip() in ("1", "true", "True", "on", "ON"))
        self.adv_dither_freq_var.set(
            self._normalize_profile_value(values.get("ditherFreq", str(DEFAULT_ADV["ditherFreq"]))) or str(DEFAULT_ADV["ditherFreq"])
        )
        self.adv_dither_amp_var.set(
            self._normalize_profile_value(values.get("ditherAmplitude", str(DEFAULT_ADV["ditherAmplitude"])))
            or str(DEFAULT_ADV["ditherAmplitude"])
        )
        self.adv_pwm_freq_var.set(
            self._normalize_profile_value(values.get("pwmFreq", str(DEFAULT_ADV["pwmFreq"]))) or str(DEFAULT_ADV["pwmFreq"])
        )

        self._draw_output_graph()
        self._refresh_live_telemetry_units()

    def _build_connection_panel(self, parent: ttk.Frame) -> None:
        """Top control row: transport, endpoint, connect, and read controls."""
        frame = ttk.LabelFrame(parent, text="Communication Interface", style="Card.TLabelframe", padding=10)
        frame.pack(fill="x", pady=(0, 10))

        ttk.Label(frame, text="Interface Type", style="ConnField.TLabel").grid(row=0, column=0, sticky="w")
        self.transport_combo = ttk.Combobox(
            frame,
            textvariable=self.transport_display_var,
            state="readonly",
            values=("Serial", "BLE"),
            width=9,
        )
        self.transport_combo.grid(row=0, column=1, padx=(8, 14), sticky="w")
        self.transport_combo.bind("<<ComboboxSelected>>", self._on_transport_combo_changed)

        ttk.Label(frame, textvariable=self.endpoint_label_var, style="ConnField.TLabel").grid(row=0, column=2, sticky="w")
        self.port_combo = ttk.Combobox(frame, textvariable=self.port_var, state="readonly", width=22)
        self.port_combo.grid(row=0, column=3, padx=(8, 14), sticky="w")

        self.refresh_btn = ttk.Button(
            frame,
            textvariable=self.refresh_btn_text_var,
            style="Icon.TButton",
            command=self.refresh_ports,
            width=2,
        )
        self.refresh_btn.grid(row=0, column=4, padx=(0, 12), sticky="w")

        self.connect_btn = ttk.Button(frame, text="Connect", style="Primary.TButton", width=14, command=self.toggle_connection)
        self.connect_btn.grid(row=0, column=5, padx=(0, 12), sticky="w")

        self.read_toggle_btn = ttk.Button(
            frame,
            text="Start Monitoring",
            style="Accent.TButton",
            width=16,
            command=self.toggle_reading,
        )
        self.read_toggle_btn.grid(row=0, column=6, padx=(0, 8), sticky="w")

        # Keep Device ID anchored to the far-right corner.
        frame.grid_columnconfigure(7, weight=1)

        self.device_name_btn = ttk.Button(
            frame,
            textvariable=self.device_name_var,
            style="Neutral.TButton",
            width=16,
            command=lambda: None,
        )
        self.device_name_btn.grid(row=0, column=8, padx=(8, 0), sticky="e")
        self._set_device_name_display()

        self._update_read_toggle_button()
        self._on_transport_changed()

    def _select_transport(self, transport: str) -> None:
        """Select Serial/BLE transport mode."""
        mode = (transport or "").strip().upper()
        if mode not in TRANSPORT_OPTIONS:
            return
        if self._is_connected():
            return
        if self.transport_var.get().strip().upper() == mode:
            return
        self.transport_var.set(mode)
        self._on_transport_changed()

    def _on_transport_combo_changed(self, _event=None) -> None:
        """Handle transport dropdown selection."""
        if self._is_connected():
            return
        selected = (self.transport_display_var.get() or "").strip().upper()
        self._select_transport("BLE" if selected == "BLE" else "UART")

    def _refresh_transport_toggle_styles(self) -> None:
        """Refresh transport selector visual state."""
        mode = self.transport_var.get().strip().upper()
        if mode not in TRANSPORT_OPTIONS:
            mode = "UART"
            self.transport_var.set(mode)
        if self.transport_combo is not None and self.transport_combo.winfo_exists():
            self.transport_display_var.set("BLE" if mode == "BLE" else "Serial")

    def _set_transport_toggle_enabled(self, enabled: bool) -> None:
        """Enable/disable transport selector."""
        if self.transport_combo is not None and self.transport_combo.winfo_exists():
            state = "readonly" if bool(enabled) else "disabled"
            self.transport_combo.configure(state=state)

    def _set_device_name_display(self, value: str = "") -> None:
        """Set device name display."""
        if value and value.strip():
            self.device_name_var.set(value.strip())
            return

        self.device_name_var.set("Device ID")

    def _is_ble_mode(self) -> bool:
        """Return whether BLE mode."""
        return self.transport_var.get().strip().upper() == "BLE"

    def _is_uart_connected(self) -> bool:
        """Return whether Serial connected."""
        return bool(self.ser and self.ser.is_open)

    def _is_ble_connected(self) -> bool:
        """Return whether BLE connected."""
        return bool(self.ble_client is not None and self.ble_connected)

    def _is_connected(self) -> bool:
        """Return whether connected."""
        return self._is_uart_connected() or self._is_ble_connected()

    def _resolve_ble_address(self, selected: str) -> str:
        """Resolve BLE address."""
        text = selected.strip()
        if not text:
            return ""
        if text in self.ble_display_to_address:
            return self.ble_display_to_address[text]
        if "|" in text:
            return text.rsplit("|", 1)[1].strip()
        return text

    def _resolve_ble_name(self, selected: str) -> str:
        """Resolve BLE name."""
        text = selected.strip()
        if not text:
            return ""
        if "|" in text:
            return text.split("|", 1)[0].strip()
        if text in self.ble_display_to_address:
            return text
        parts = text.split(":")
        if len(parts) == 6 and all(len(p) == 2 for p in parts):
            return ""
        return text

    def _build_legacy_update_cmd(self, cmd: str) -> str:
        """Build legacy update cmd."""
        txt = cmd.strip()
        if not txt.lower().startswith("update:"):
            return ""
        parts = txt.split(":", 2)
        if len(parts) < 3:
            return ""
        prefix = parts[1].strip().upper()
        payload = parts[2].strip()
        if prefix == "CFG":
            return f"CFG:{payload}"
        if prefix == "ADV":
            return f"ADV:{payload}"
        return ""

    def _send_update_command(self, cmd: str) -> None:
        """Send update command."""
        self.last_update_cmd_sent = cmd
        self.last_update_sent_at = time.monotonic()
        self.update_legacy_retry_done = False
        self.smooth_update_until = time.monotonic() + (SMOOTH_UPDATE_WINDOW_MS / 1000.0)
        self.panel_state_var.set("SAFE UPDATE")
        self._log("Safety ramp active: update uses virtual 0V input and transitions current smoothly.")
        self._send_line(cmd, expect_response=False)

    def _infer_update_scope(self, cmd: str) -> str:
        """Infer update scope."""
        txt = cmd.strip().upper()
        if txt.startswith("UPDATE:CFG:") or txt.startswith("CFG:"):
            return "cfg"
        if txt.startswith("UPDATE:ADV:") or txt.startswith("ADV:"):
            return "adv"
        return "both"

    def _on_transport_changed(self, _event=None) -> None:
        """Switch endpoint labels/controls when transport selection changes."""
        mode = self.transport_var.get().strip().upper()
        if mode not in TRANSPORT_OPTIONS:
            self.transport_var.set("UART")
        if self._is_ble_mode():
            self.endpoint_label_var.set("BLE Device")
            self.refresh_btn_text_var.set("↻")
            if self.port_combo is not None:
                self.port_combo.configure(state="readonly")
            if self.baud_entry is not None:
                self.baud_entry.configure(state="disabled")
        else:
            self.endpoint_label_var.set("Serial Port")
            self.refresh_btn_text_var.set("↻")
            if self.port_combo is not None:
                self.port_combo.configure(state="readonly")
            if self.baud_entry is not None:
                self.baud_entry.configure(state="disabled")

        self._refresh_transport_toggle_styles()
        if not self._is_connected() and not self._is_closing:
            self.refresh_ports()

    def _on_cfg_graph_params_changed(self, *_args) -> None:
        """Handle configuration graph params changed."""
        if self.graph_canvas is None:
            return
        self.root.after_idle(self._draw_output_graph)

    def _clear_cfg_form(self) -> None:
        """Clear configuration form."""
        self.cfg_mode_var.set("")
        self.cfg_in_min_var.set("")
        self.cfg_in_max_var.set("")
        self.cfg_out_min_var.set("")
        self.cfg_out_max_var.set("")
        self.cfg_unit_var.set("")
        for i in range(5):
            self.cfg_bp_in_vars[i].set("")
            self.cfg_bp_out_vars[i].set("")
            if i < len(self.cfg_bp_row_frames):
                self.cfg_bp_row_frames[i].grid_remove()
        self._draw_output_graph()
        self._refresh_live_telemetry_units()

    def _format_sat_value(self, value: str) -> str:
        """Format sat value."""
        raw = value.strip()
        v = raw.lower()

        if v in ("1", "on", "true", "yes", "high", "sat"):
            return "ON"
        if v in ("0", "off", "false", "no", "low", "unsat"):
            return "OFF"

        try:
            return "ON" if float(v) > 0 else "OFF"
        except ValueError:
            return raw

    def _request_device_name(self, retries: int = 5) -> None:
        """Request device name."""
        if not self._is_uart_connected():
            return

        self.pending_devname_until = time.monotonic() + 4.0
        self.devname_retry_left = retries
        self._send_line("devname", expect_response=False)
        self.root.after(420, self._retry_device_name)

    def _retry_device_name(self) -> None:
        """Retry device name."""
        if not self._is_uart_connected():
            return
        if self.device_name_var.get().strip().upper().startswith("SERVO_"):
            return
        if self.devname_retry_left <= 0:
            return

        self.devname_retry_left -= 1
        self._send_line("devname", expect_response=False)
        self.root.after(420, self._retry_device_name)

    def _app_settings_path(self) -> Path:
        """Return per-user settings path for desktop app preferences."""
        if sys.platform == "win32":
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            if local_app_data:
                return Path(local_app_data) / "SERVO_EVDR_APPLICATION" / APP_SETTINGS_FILE
        return Path.home() / ".servo_evdr_application" / APP_SETTINGS_FILE

    def _save_app_preferences(self) -> bool:
        """Persist small UI preferences."""
        normalized_recent = []
        for path_text in self.recent_files:
            txt = str(path_text).strip()
            if txt and txt not in normalized_recent:
                normalized_recent.append(txt)
        data = {
            "recent_files": normalized_recent[:8],
        }
        try:
            settings_path = self._app_settings_path()
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        except Exception as exc:
            if hasattr(self, "log_text"):
                self._log(f"Settings save failed: {exc}")
            return False

    def _load_app_preferences(self) -> None:
        """Load saved UI preferences if available."""
        try:
            settings_path = self._app_settings_path()
            if not settings_path.exists():
                return
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            recent = data.get("recent_files", [])
            if isinstance(recent, list):
                self.recent_files = [str(item).strip() for item in recent if str(item).strip()]
        except Exception:
            # Ignore broken/missing settings file and continue with defaults.
            pass

    def _safe_export_filename_base(self) -> str:
        """Build a Windows-safe file name prefix from device name."""
        raw_name = self.device_name_var.get().strip()
        if not raw_name or raw_name.lower() == "device id":
            raw_name = "SERVO_EVDR"

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._-")
        if not safe:
            safe = "SERVO_EVDR"
        return safe[:64]

    def _on_update_success_ack(self) -> None:
        """Handle successful update acknowledgment from device."""
        self.awaiting_update_result = False
        self.update_legacy_retry_done = False
        self._log("Device update acknowledged")
        verify_scope = self.pending_update_scope if self.pending_update_scope in ("cfg", "adv", "both") else "both"

        wait_ms = 250
        now = time.monotonic()
        if self.smooth_update_until > now:
            wait_ms = min(1200, max(350, int((self.smooth_update_until - now) * 1000)))
            self.panel_state_var.set("RAMPING")
            self._log("Waiting for smooth ramp completion before verify read...")
        self.root.after(wait_ms, lambda s=verify_scope: self.load_config_from_device(scope=s))

    # ------------------------------------------------------------------
    # Panel builders and graph helpers
    # ------------------------------------------------------------------
    def _build_telemetry_panel(self, parent: ttk.Frame) -> None:
        """Build telemetry panel."""
        panel = ttk.LabelFrame(parent, text="Real-Time Telemetry", style="Card.TLabelframe", padding=10)
        panel.pack(fill="x", pady=(0, 10))

        telemetry_grid = ttk.Frame(panel, style="Card.TLabelframe")
        telemetry_grid.pack(fill="x")

        for i, (key, title, hint) in enumerate(TELEMETRY_META):
            card = ttk.LabelFrame(telemetry_grid, text=title, style="Card.TLabelframe", padding=8)
            card.grid(row=i // 4, column=i % 4, padx=6, pady=6, sticky="nsew")
            ttk.Label(card, textvariable=self.telemetry_vars[key], style="Value.TLabel", width=14).pack(anchor="w")
            ttk.Label(card, text=hint, style="TelemetryHint.TLabel").pack(anchor="w", pady=(2, 0))

        for c in range(4):
            telemetry_grid.grid_columnconfigure(c, weight=1)

    def _add_labeled_entry(
        self,
        parent: ttk.Frame,
        row: int,
        col: int,
        label: str,
        var: tk.Variable,
        width: int = 12,
        readonly: bool = False,
    ) -> None:
        """Add labeled entry."""
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 4), pady=4)
        entry = ttk.Entry(parent, textvariable=var, width=width)
        if readonly:
            entry.state(["readonly"])
        entry.grid(row=row, column=col + 1, sticky="w", padx=(0, 12), pady=4)

    def _build_parameter_panels(self, parent: ttk.Frame) -> None:
        """Build parameter panels."""
        cfg = ttk.LabelFrame(parent, text="Configuration Parameters", style="Card.TLabelframe", padding=10)
        cfg.pack(fill="x", pady=(0, 10))

        cfg_body = ttk.Frame(cfg, style="Inner.TFrame")
        cfg_body.pack(fill="both", expand=True)
        cfg_body.grid_columnconfigure(0, weight=1)
        cfg_body.grid_columnconfigure(1, weight=1)
        cfg_body.grid_rowconfigure(0, weight=1)

        input_box = ttk.LabelFrame(cfg_body, text="Input Parameters", style="Card.TLabelframe", padding=8)
        input_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        bp_box = ttk.LabelFrame(cfg_body, text="Setpoint Table", style="Card.TLabelframe", padding=8)
        bp_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        ttk.Label(input_box, text="Mode", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 4), pady=4)
        mode_combo = ttk.Combobox(
            input_box, textvariable=self.cfg_mode_var, values=MODE_OPTIONS, state="readonly", width=18
        )
        mode_combo.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=4)
        mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_live_telemetry_units())

        self._add_labeled_entry(input_box, 1, 0, "Input Min", self.cfg_in_min_var)
        self._add_labeled_entry(input_box, 1, 2, "Input Max", self.cfg_in_max_var)
        self._add_labeled_entry(input_box, 2, 0, "Output Min", self.cfg_out_min_var)
        self._add_labeled_entry(input_box, 2, 2, "Output Max", self.cfg_out_max_var)
        self._add_labeled_entry(input_box, 3, 0, "Unit", self.cfg_unit_var, readonly=True)
        for c in range(4):
            input_box.grid_columnconfigure(c, weight=1 if c in (1, 3) else 0)

        bp_top = ttk.Frame(bp_box, style="Inner.TFrame")
        bp_top.grid(row=0, column=0, columnspan=5, sticky="new", pady=(0, 6))
        bp_box.grid_columnconfigure(0, weight=1)
        bp_top.grid_columnconfigure(0, weight=1)
        ttk.Label(
            bp_top,
            text="Input -> Output mapping (up to 5 setpoints)",
            style="TelemetryHint.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            bp_top,
            text="+ Add",
            style="Neutral.TButton",
            command=self._add_breakpoint_row,
            width=9,
        ).grid(row=0, column=1, sticky="ne", padx=(10, 0))

        bp_body = ttk.Frame(bp_box, style="Inner.TFrame")
        bp_body.grid(row=1, column=0, columnspan=5, sticky="ew")
        bp_body.grid_columnconfigure(0, weight=1)

        bp_header_row = ttk.Frame(bp_body, style="Inner.TFrame")
        bp_header_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(bp_header_row, text="Point", style="Field.TLabel", width=6).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Label(bp_header_row, text="Input", style="Field.TLabel", width=10).grid(
            row=0, column=1, sticky="w", padx=(0, 8)
        )
        ttk.Label(bp_header_row, text="", style="Field.TLabel", width=3).grid(row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Label(bp_header_row, text="Output", style="Field.TLabel", width=10).grid(
            row=0, column=3, sticky="w", padx=(0, 8)
        )
        ttk.Label(bp_header_row, text="Action", style="Field.TLabel", width=8).grid(row=0, column=4, sticky="w")

        self.cfg_bp_row_frames = []
        for i in range(5):
            row_frame = ttk.Frame(bp_body, style="Inner.TFrame")
            row_frame.grid(row=i + 1, column=0, sticky="ew", pady=2)

            ttk.Label(row_frame, text=f"BP{i+1}", style="Field.TLabel", width=6).grid(
                row=0, column=0, sticky="w", padx=(0, 8)
            )
            ttk.Entry(row_frame, textvariable=self.cfg_bp_in_vars[i], width=10).grid(
                row=0, column=1, sticky="w", padx=(0, 8)
            )
            ttk.Label(row_frame, text="->", style="Field.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
            ttk.Entry(row_frame, textvariable=self.cfg_bp_out_vars[i], width=10).grid(
                row=0, column=3, sticky="w", padx=(0, 8)
            )
            ttk.Button(
                row_frame,
                text="Remove",
                style="MiniWarn.TButton",
                command=lambda idx=i: self._remove_breakpoint_row(idx),
                width=8,
            ).grid(row=0, column=4, sticky="w")

            row_frame.grid_remove()
            self.cfg_bp_row_frames.append(row_frame)
            row_frame.grid_columnconfigure(5, weight=1)

        cfg_actions = ttk.Frame(cfg, style="Inner.TFrame")
        cfg_actions.pack(fill="x", pady=(8, 0))
        self.apply_cfg_btn = ttk.Button(cfg_actions, text="Apply Configuration", style="Primary.TButton", command=self.send_cfg)
        self.apply_cfg_btn.pack(side="right")
        self.load_cfg_btn = ttk.Button(
            cfg_actions,
            text="Read Device",
            style="Primary.TButton",
            command=self.load_config_from_device,
        )
        self.load_cfg_btn.pack(side="right", padx=(0, 12))

    def _open_adv_window(self) -> None:
        """Open advanced window."""
        if self.adv_window is not None and self.adv_window.winfo_exists():
            self.adv_window.deiconify()
            self.adv_window.lift()
            self.adv_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.adv_window = win
        win.title("Advanced Parameters")
        win.configure(bg="#0f1419")
        win.resizable(False, False)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._close_adv_window)

        adv = ttk.LabelFrame(win, text="Advanced Parameters", style="Card.TLabelframe", padding=10)
        adv.pack(fill="both", expand=True, padx=10, pady=10)
        adv.grid_columnconfigure(0, weight=1)

        form = ttk.Frame(adv, style="Inner.TFrame")
        form.grid(row=0, column=0, sticky="ew")
        form.grid_columnconfigure(0, weight=1)
        form.grid_columnconfigure(1, weight=1)

        left_col = ttk.Frame(form, style="Inner.TFrame")
        left_col.grid(row=0, column=0, sticky="nw", padx=(0, 16))
        right_col = ttk.Frame(form, style="Inner.TFrame")
        right_col.grid(row=0, column=1, sticky="nw", padx=(16, 0))

        self._add_labeled_entry(left_col, 0, 0, "Kp", self.adv_kp_var, width=12)
        self._add_labeled_entry(left_col, 1, 0, "Dither Freq", self.adv_dither_freq_var, width=12)
        self._add_labeled_entry(left_col, 2, 0, "PWM Freq", self.adv_pwm_freq_var, width=12)

        self._add_labeled_entry(right_col, 0, 0, "Ki", self.adv_ki_var, width=12)
        self._add_labeled_entry(right_col, 1, 0, "Dither Amp", self.adv_dither_amp_var, width=12)
        ttk.Checkbutton(right_col, text="Dither Enable", variable=self.adv_dither_enable_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 4)
        )

        actions = ttk.Frame(adv, style="Inner.TFrame")
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        actions.grid_columnconfigure(0, weight=1)
        self.apply_adv_btn = ttk.Button(actions, text="Apply Advanced", style="Primary.TButton", command=self.send_adv)
        self.apply_adv_btn.grid(row=0, column=0, sticky="e")

        win.update_idletasks()
        fit_w = max(560, win.winfo_reqwidth() + 8)
        fit_h = max(220, win.winfo_reqheight() + 8)
        win.geometry(f"{fit_w}x{fit_h}")
        win.minsize(fit_w, fit_h)
        win.maxsize(fit_w, fit_h)

        if self.auto_read_var.get():
            self.apply_adv_btn.configure(state="disabled")

    def _close_adv_window(self) -> None:
        """Close advanced window."""
        if self.adv_window is not None:
            try:
                self.adv_window.destroy()
            except Exception:
                pass
        self.adv_window = None
        self.apply_adv_btn = None

    def _close_developer_window(self) -> None:
        """Close developer window."""
        if self.dev_window is not None:
            try:
                self.dev_window.destroy()
            except Exception:
                pass
        self.dev_window = None
        self.dev_load_cal_btn = None
        self.dev_save_cal_btn = None
        self.dev_logout_btn = None
        self.dev_window_status_var.set("Locked")

    def _set_dev_window_cal_values(self, values: dict) -> None:
        """Set dev window cal values."""
        for key, var in self.dev_cal_vars.items():
            if key in values:
                var.set(self._format_number(values[key]))

    def _refresh_dev_window_status(self) -> None:
        """Refresh dev window status."""
        if self.dev_authenticated:
            state_txt = self.device_mode_state if self.device_mode_state else "DEV_MODE_UNLOCKED"
            self.dev_window_status_var.set(f"Unlocked | {state_txt}")
        else:
            self.dev_window_status_var.set("Locked")

    def _load_calibration_to_window(self) -> bool:
        """Load calibration to window."""
        if not self._ensure_developer_unlocked():
            self._refresh_dev_window_status()
            return False

        enter_reply = self._send_and_wait_line(
            "CAL_ENTER",
            lambda txt: txt.strip().upper() in ("CAL_MODE_READY", "DEV_REQUIRED", "BUSY_FW_UPDATE"),
            FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not enter_reply or enter_reply.strip().upper() != "CAL_MODE_READY":
            self._refresh_dev_window_status()
            return False

        cal_line = self._send_and_wait_line(
            "CAL_GET",
            lambda txt: txt.strip().upper().startswith("CAL:") or txt.strip().upper().startswith("CAL_ERROR"),
            FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        self._send_and_wait_line(
            "CAL_EXIT",
            lambda txt: txt.strip().upper() == "CAL_MODE_OFF",
            FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=0,
        )
        if not cal_line or not cal_line.strip().upper().startswith("CAL:"):
            self._refresh_dev_window_status()
            return False

        current_vals = self._parse_cal_payload(cal_line.strip())
        self.cal_payload_cache = dict(current_vals)
        self._set_dev_window_cal_values(current_vals)
        self._refresh_dev_window_status()
        return True

    def _developer_window_load_calibration(self) -> None:
        """Handle developer window load calibration."""
        if not self._is_connected():
            messagebox.showwarning("Not Connected", "Establish Serial/BLE link first.")
            return
        if self.auto_read_var.get():
            self.stop_reading()

        ok = self._load_calibration_to_window()
        if ok:
            self._log("Developer calibration values loaded")
        else:
            messagebox.showwarning("Calibration", "Failed to load calibration values from device.")

    def _developer_window_apply_calibration(self) -> None:
        """Handle developer window apply calibration."""
        if not self._is_connected():
            messagebox.showwarning("Not Connected", "Establish Serial/BLE link first.")
            return
        if self.auto_read_var.get():
            self.stop_reading()
        if not self._ensure_developer_unlocked():
            self._refresh_dev_window_status()
            return

        payload_obj = {}
        for key, var in self.dev_cal_vars.items():
            raw = var.get().strip()
            if not raw:
                messagebox.showwarning("Calibration", f"{key} cannot be empty.")
                return
            try:
                payload_obj[key] = float(raw)
            except ValueError:
                messagebox.showwarning("Calibration", f"{key} must be numeric.")
                return

        enter_reply = self._send_and_wait_line(
            "CAL_ENTER",
            lambda txt: txt.strip().upper() in ("CAL_MODE_READY", "DEV_REQUIRED", "BUSY_FW_UPDATE"),
            FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not enter_reply or enter_reply.strip().upper() != "CAL_MODE_READY":
            self._refresh_dev_window_status()
            messagebox.showwarning("Calibration", f"CAL_ENTER failed: {enter_reply or 'no response'}")
            return

        payload = json.dumps(payload_obj, separators=(",", ":"))
        save_reply = self._send_and_wait_line(
            f"CAL_SET:{payload}",
            lambda txt: txt.strip().upper().startswith("CAL_") or txt.strip().upper() in ("DEV_REQUIRED", "BUSY_FW_UPDATE"),
            FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        self._send_and_wait_line(
            "CAL_EXIT",
            lambda txt: txt.strip().upper() == "CAL_MODE_OFF",
            FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=0,
        )
        if not save_reply or save_reply.strip().upper() != "CAL_SAVED":
            self._refresh_dev_window_status()
            messagebox.showwarning("Calibration", f"Save failed: {save_reply or 'no response'}")
            return

        self.cal_payload_cache = dict(payload_obj)
        self._refresh_dev_window_status()
        self._log("Calibration updated successfully")
        messagebox.showinfo("Calibration", "Calibration values saved successfully.")

    def open_developer_mode_window(self) -> None:
        """Open developer mode window."""
        if not self._is_connected():
            messagebox.showwarning("Not Connected", "Establish Serial/BLE link first.")
            return
        if self.auto_read_var.get():
            self.stop_reading()
        if not self._ensure_developer_unlocked(force_prompt=True):
            self._refresh_dev_window_status()
            return

        if self.dev_window is not None and self.dev_window.winfo_exists():
            self.dev_window.deiconify()
            self.dev_window.lift()
            self.dev_window.focus_force()
            self._autosize_dev_window()
            self.root.after(80, self._developer_window_load_calibration)
            return

        win = tk.Toplevel(self.root)
        self.dev_window = win
        win.title("Developer Mode")
        win.configure(bg="#0f1419")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._close_developer_window)

        wrapper = ttk.Frame(win, style="Panel.TFrame", padding=12)
        wrapper.pack(fill="both", expand=True)

        head = ttk.Frame(wrapper, style="Panel.TFrame")
        head.pack(fill="x", pady=(0, 8))
        head.grid_columnconfigure(1, weight=1)
        ttk.Label(head, text="Developer Mode", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, textvariable=self.dev_window_status_var, style="SubTitle.TLabel").grid(
            row=0, column=1, sticky="e"
        )

        cal_box = ttk.LabelFrame(wrapper, text="Calibration Parameters", style="Card.TLabelframe", padding=10)
        cal_box.pack(fill="x", pady=(0, 10))
        cal_box.grid_columnconfigure(1, weight=1)

        row = 0
        for key in [
            "MA_SCALE_FACTOR_4_20MA",
            "RESISTANCE_SCALE_FACTOR",
            "VOLTAGE_SCALE_FACTOR",
            "FB_TO_CURRENT_FACTOR",
            "FB_OFFSET",
        ]:
            ttk.Label(cal_box, text=key, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            ttk.Entry(cal_box, textvariable=self.dev_cal_vars[key], width=20).grid(row=row, column=1, sticky="w", pady=4)
            row += 1

        cal_actions = ttk.Frame(wrapper, style="Panel.TFrame")
        cal_actions.pack(fill="x", pady=(0, 10))
        self.dev_load_cal_btn = ttk.Button(
            cal_actions,
            text="Load Calibration",
            style="Neutral.TButton",
            command=self._developer_window_load_calibration,
        )
        self.dev_load_cal_btn.pack(side="left")
        self.dev_save_cal_btn = ttk.Button(
            cal_actions,
            text="Save Calibration",
            style="Primary.TButton",
            command=self._developer_window_apply_calibration,
        )
        self.dev_save_cal_btn.pack(side="left", padx=(8, 0))

        footer = ttk.Frame(wrapper, style="Panel.TFrame")
        footer.pack(fill="x")
        self.dev_logout_btn = ttk.Button(
            footer, text="Developer Logout", style="Neutral.TButton", command=self._developer_logout
        )
        self.dev_logout_btn.pack(side="right")

        if self.cal_payload_cache:
            self._set_dev_window_cal_values(self.cal_payload_cache)
        self._refresh_dev_window_status()
        self._autosize_dev_window()
        self.root.after(120, self._developer_window_load_calibration)

    def open_firmware_update_window(self) -> None:
        """Open standalone firmware update window."""
        if self.firmware_window is not None and self.firmware_window.winfo_exists():
            self.firmware_window.deiconify()
            self.firmware_window.lift()
            self.firmware_window.focus_force()
            self._ensure_fw_progress_widgets()
            self._hide_fw_upload_progress_ui()
            self._autosize_firmware_window()
            return

        win = tk.Toplevel(self.root)
        self.firmware_window = win
        win.title("Firmware Update")
        win.configure(bg="#0f1419")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._close_firmware_window)

        wrapper = ttk.Frame(win, style="Panel.TFrame", padding=12)
        wrapper.pack(fill="both", expand=True)

        head = ttk.Frame(wrapper, style="Panel.TFrame")
        head.pack(fill="x", pady=(0, 8))
        head.grid_columnconfigure(1, weight=1)
        ttk.Label(head, text="Firmware Update", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, textvariable=self.dev_status_var, style="SubTitle.TLabel").grid(row=0, column=1, sticky="e")

        fw_box = ttk.LabelFrame(wrapper, text="OTA Firmware Upload", style="Card.TLabelframe", padding=10)
        self.fw_window_box = fw_box
        fw_box.pack(fill="x", pady=(0, 10))
        ttk.Label(
            fw_box,
            text="Upload .bin over BLE. Requires BLE connection and AUTH password.",
            style="TelemetryHint.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        self.fw_update_btn = ttk.Button(
            fw_box,
            text="Firmware Update",
            style="Warn.TButton",
            command=self.open_developer_firmware_flow,
            width=20,
        )
        self.fw_update_btn.pack(anchor="w")

        self._ensure_fw_progress_widgets()
        self._hide_fw_upload_progress_ui()
        self._reset_fw_upload_progress()

        self._autosize_firmware_window()

    def _close_firmware_window(self, force: bool = False) -> None:
        """Close standalone firmware update window."""
        if self.fw_upload_active and not force:
            messagebox.showwarning(
                "Firmware Update In Progress",
                "Firmware upload is running.\nDo not close Firmware Update window until upload finishes.",
            )
            return
        if self.firmware_window is not None:
            try:
                self.firmware_window.destroy()
            except Exception:
                pass
        self.firmware_window = None
        self.fw_window_box = None
        self.fw_update_btn = None
        self.fw_upload_progressbar = None
        self.fw_upload_progress_label = None
        self.fw_upload_file_label = None
        self.fw_upload_progress_frame = None

    def _build_graph_panel(self, parent: ttk.Frame) -> None:
        """Build graph panel."""
        panel = ttk.LabelFrame(parent, text="Real-Time Graph", style="Card.TLabelframe", padding=10)
        panel.pack(fill="both", expand=True)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=3)
        panel.grid_rowconfigure(4, weight=2)

        ttk.Label(
            panel,
            text="Linearity View (X=Input, Y=Current): Setpoint, Actual, or Both",
            style="TelemetryHint.TLabel",
        ).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        controls = ttk.Frame(panel, style="Inner.TFrame")
        controls.grid(row=1, column=0, sticky="w", pady=(0, 6))
        modes = [
            ("Setpoint", "setpoint"),
            ("Actual", "actual"),
            ("Both", "both"),
        ]
        for idx, (label, value) in enumerate(modes):
            ttk.Radiobutton(
                controls,
                text=label,
                value=value,
                variable=self.graph_mode_var,
                command=self._draw_output_graph,
            ).grid(row=0, column=idx, sticky="w", padx=(0, 12))

        self.clear_graph_btn = ttk.Button(
            controls,
            text="Reset Plot",
            style="Neutral.TButton",
            command=self._clear_graph_live_values,
        )
        self.clear_graph_btn.grid(row=0, column=len(modes), sticky="w", padx=(8, 0))

        self.graph_canvas = tk.Canvas(
            panel,
            bg="#10161d",
            highlightthickness=1,
            highlightbackground="#2f3d4b",
            bd=0,
        )
        self.graph_canvas.grid(row=2, column=0, sticky="nsew")
        self.graph_canvas.bind("<Configure>", lambda _e: self._draw_output_graph())
        self.graph_canvas.bind("<Motion>", self._on_graph_mouse_move)
        self.graph_canvas.bind("<Leave>", lambda _e: self._clear_graph_hover_tip())
        ttk.Label(
            panel,
            text="Real-Time Trend (X=Time, Y=Current + Voltage)",
            style="TelemetryHint.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=(10, 4))
        self.time_graph_canvas = tk.Canvas(
            panel,
            bg="#10161d",
            highlightthickness=1,
            highlightbackground="#2f3d4b",
            bd=0,
        )
        self.time_graph_canvas.grid(row=4, column=0, sticky="nsew")
        self.time_graph_canvas.bind("<Configure>", lambda _e: self._draw_time_graph())
        self._draw_output_graph()
        self._draw_time_graph()

    def _clear_graph_hover_tip(self) -> None:
        """Clear graph hover tip."""
        if self.graph_canvas is None:
            return
        self.graph_canvas.delete("graph_hover_tip")

    def _clear_graph_live_values(self) -> None:
        """Clear live graph traces/markers while keeping configured setpoint curve."""
        self.graph_input_points.clear()
        self.graph_output_points.clear()
        self.graph_command_points.clear()
        self.graph_iv_points.clear()
        self.graph_time_points.clear()
        self.graph_time_voltage_points.clear()
        self.graph_time_current_points.clear()
        self.graph_time_start_monotonic = None
        self.graph_hover_points = []
        self.live_input_seen = None
        self.live_output_seen = None
        self.live_output_set = None
        self._draw_output_graph()
        self._draw_time_graph()
        self._log("Cleared live graph traces")

    def _append_time_graph_sample(self) -> None:
        """Append time graph sample."""
        if self.live_input_seen is None and self.live_output_seen is None:
            return
        now = time.monotonic()
        if self.graph_time_start_monotonic is None:
            self.graph_time_start_monotonic = now
        t_rel = now - self.graph_time_start_monotonic
        self.graph_time_points.append(float(t_rel))
        self.graph_time_voltage_points.append(
            float(self.live_input_seen) if self.live_input_seen is not None else float("nan")
        )
        self.graph_time_current_points.append(
            float(self.live_output_seen) if self.live_output_seen is not None else float("nan")
        )

    def _draw_time_graph(self) -> None:
        """Draw time graph."""
        if self.time_graph_canvas is None:
            return

        c = self.time_graph_canvas
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        c.delete("all")

        left = 52
        top = 24
        right = max(left + 1, w - 52)
        bottom = max(top + 1, h - 30)
        c.create_rectangle(left, top, right, bottom, outline="#2f3d4b")

        if not self.graph_time_points:
            c.create_text(
                (left + right) // 2,
                (top + bottom) // 2,
                text="Waiting for live trend...",
                fill="#7f93a8",
                font=("Segoe UI", 10),
            )
            c.create_text((left + right) / 2, h - 10, text="Time (s)", fill="#9cb6cf", font=("Consolas", 8))
            return

        latest_t = float(self.graph_time_points[-1])
        x_max = max(TREND_WINDOW_SECONDS, latest_t)
        x_min = max(0.0, x_max - TREND_WINDOW_SECONDS)
        if x_max <= x_min:
            x_max = x_min + 1.0

        mode_min, mode_max = self._get_mode_input_limits(self.cfg_mode_var.get().strip())
        current_vals = []
        voltage_vals = []
        for t, cur, vol in zip(self.graph_time_points, self.graph_time_current_points, self.graph_time_voltage_points):
            if t < x_min:
                continue
            if math.isfinite(cur):
                current_vals.append(float(cur))
            if math.isfinite(vol):
                voltage_vals.append(float(vol))

        cur_min, cur_max = self._calc_range(current_vals, 0.0, 2000.0)
        vol_min, vol_max = self._calc_range(voltage_vals, float(mode_min), float(mode_max))

        def x_to_px(t_val: float) -> float:
            """Map an X-axis value to canvas pixel coordinates."""
            return left + (t_val - x_min) * (right - left) / (x_max - x_min)

        def y_cur_to_px(cur_val: float) -> float:
            """Map a current value to canvas Y pixel coordinates."""
            return bottom - (cur_val - cur_min) * (bottom - top) / (cur_max - cur_min)

        def y_vol_to_px(vol_val: float) -> float:
            """Map a voltage value to canvas Y pixel coordinates."""
            return bottom - (vol_val - vol_min) * (bottom - top) / (vol_max - vol_min)

        for i in range(1, 5):
            gx = left + (right - left) * i / 5.0
            gy = top + (bottom - top) * i / 5.0
            c.create_line(gx, top, gx, bottom, fill="#1a2632")
            c.create_line(left, gy, right, gy, fill="#1a2632")

        for i in range(6):
            py = top + (bottom - top) * i / 5.0
            cur_tick = cur_min + (cur_max - cur_min) * (5 - i) / 5.0
            vol_tick = vol_min + (vol_max - vol_min) * (5 - i) / 5.0
            c.create_text(left - 6, py, text=f"{cur_tick:.0f}", anchor="e", fill=GRAPH_COLOR_CURRENT, font=("Consolas", 8))
            c.create_text(right + 6, py, text=f"{vol_tick:.2f}", anchor="w", fill=GRAPH_COLOR_VOLTAGE, font=("Consolas", 8))
        c.create_text(left - 32, top - 8, text="mA", fill=GRAPH_COLOR_CURRENT, font=("Consolas", 8))
        c.create_text(right + 26, top - 8, text="V", fill=GRAPH_COLOR_VOLTAGE, font=("Consolas", 8))

        for i in range(6):
            px = left + (right - left) * i / 5.0
            t_tick = x_min + (x_max - x_min) * i / 5.0
            c.create_text(px, bottom + 12, text=f"{t_tick:.1f}", fill="#8397ab", font=("Consolas", 8))

        def draw_segment(values, y_mapper, color):
            """Draw a polyline segment for the provided graph series."""
            pts = []
            drawn = False
            for t, v in zip(self.graph_time_points, values):
                if t < x_min:
                    continue
                if not math.isfinite(v):
                    if len(pts) >= 4:
                        c.create_line(pts, fill=color, width=2, smooth=True)
                        drawn = True
                    pts = []
                    continue
                pts.extend([x_to_px(float(t)), y_mapper(float(v))])
            if len(pts) >= 4:
                c.create_line(pts, fill=color, width=2, smooth=True)
                drawn = True
            return drawn

        have_current = draw_segment(self.graph_time_current_points, y_cur_to_px, GRAPH_COLOR_CURRENT)
        have_voltage = draw_segment(self.graph_time_voltage_points, y_vol_to_px, GRAPH_COLOR_VOLTAGE)

        seen_cur = "-" if self.live_output_seen is None else f"{self.live_output_seen:.1f} mA"
        seen_vol = "-" if self.live_input_seen is None else f"{self.live_input_seen:.2f} V"
        header_parts = [
            ("Real-Time Trend | ", "#9cb6cf"),
            ("Current", GRAPH_COLOR_CURRENT),
            (f": {seen_cur} | ", "#9cb6cf"),
            ("Voltage", GRAPH_COLOR_VOLTAGE),
            (f": {seen_vol}", "#9cb6cf"),
        ]
        x_cursor = left
        for text_value, color in header_parts:
            item = c.create_text(x_cursor, 10, anchor="w", text=text_value, fill=color, font=("Consolas", 8))
            bbox = c.bbox(item)
            if not bbox:
                continue
            x_cursor = bbox[2] + 2
            if x_cursor >= (right - 4):
                break
        c.create_text((left + right) / 2, h - 10, text="Time (s)", fill="#9cb6cf", font=("Consolas", 8))

        # Legend labels intentionally hidden per UI preference.

    def _on_graph_mouse_move(self, event) -> None:
        """Handle graph mouse move."""
        if self.graph_canvas is None:
            return
        if not self.graph_hover_points:
            self._clear_graph_hover_tip()
            return

        nearest = None
        nearest_dist2 = float("inf")
        for point in self.graph_hover_points:
            dx = point["px"] - float(event.x)
            dy = point["py"] - float(event.y)
            dist2 = dx * dx + dy * dy
            if dist2 < nearest_dist2:
                nearest_dist2 = dist2
                nearest = point

        if nearest is None or nearest_dist2 > (GRAPH_HOVER_MAX_DISTANCE_PX * GRAPH_HOVER_MAX_DISTANCE_PX):
            self._clear_graph_hover_tip()
            return

        c = self.graph_canvas
        c.delete("graph_hover_tip")

        tip_text = (
            f"{nearest['series']}\n"
            f"Input: {nearest['x']:.2f}\n"
            f"Current: {nearest['y']:.1f} mA"
        )
        tx = float(event.x) + 12
        ty = float(event.y) - 12

        text_id = c.create_text(
            tx,
            ty,
            text=tip_text,
            anchor="nw",
            fill="#eaf3ff",
            font=("Consolas", 8),
            tags=("graph_hover_tip",),
        )
        bbox = c.bbox(text_id)
        if bbox is None:
            return

        x1, y1, x2, y2 = bbox
        pad = 4
        # Keep tooltip inside graph canvas bounds.
        dx = 0
        dy = 0
        if x2 + pad > c.winfo_width():
            dx = c.winfo_width() - (x2 + pad)
        if y1 - pad < 0:
            dy = -(y1 - pad)
        if y2 + pad > c.winfo_height():
            dy = c.winfo_height() - (y2 + pad)
        if dx or dy:
            c.move(text_id, dx, dy)
            bbox = c.bbox(text_id)
            if bbox is not None:
                x1, y1, x2, y2 = bbox

        c.create_rectangle(
            x1 - pad,
            y1 - pad,
            x2 + pad,
            y2 + pad,
            fill="#0f1a24",
            outline=nearest["color"],
            width=1,
            tags=("graph_hover_tip",),
        )
        c.tag_raise(text_id)
        c.create_oval(
            nearest["px"] - 3,
            nearest["py"] - 3,
            nearest["px"] + 3,
            nearest["py"] + 3,
            fill=nearest["color"],
            outline="",
            tags=("graph_hover_tip",),
        )

    def _extract_number(self, text: str):
        """Extract number."""
        if text is None:
            return None
        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(text))
        if not match:
            return None
        try:
            value = float(match.group(0))
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        return value

    def _get_visible_graph_flags(self):
        """Return visible graph flags."""
        mode = self.graph_mode_var.get().strip().lower()
        if mode in ("setcurrent", "setpoint"):
            return False, True
        if mode in ("current", "measured", "actual", "voltage"):
            return True, False
        return True, True

    def _calc_range(self, values, fallback_min: float, fallback_max: float):
        """Handle calc range."""
        vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
        lo = float(fallback_min)
        hi = float(fallback_max)
        if vals:
            lo = min(lo, min(vals))
            hi = max(hi, max(vals))
        if not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi:
            lo, hi = 0.0, 1.0
        if abs(hi - lo) < 1e-9:
            hi = lo + 1.0
        return lo, hi

    def _get_cfg_reference_points(self, in_min: float, in_max: float, out_min: float, out_max: float):
        """Return configuration reference points."""
        points = []
        for i in range(5):
            in_val = self._extract_number(self.cfg_bp_in_vars[i].get())
            out_val = self._extract_number(self.cfg_bp_out_vars[i].get())
            if in_val is None or out_val is None:
                continue
            x = float(in_val)
            y = float(out_val)
            if not math.isfinite(x) or not math.isfinite(y):
                continue
            if x < in_min or x > in_max:
                continue
            if y < out_min or y > out_max:
                continue
            points.append((x, y))

        points.sort(key=lambda p: p[0])

        eps = 1e-9
        has_min_point = any(abs(x - float(in_min)) < eps for x, _y in points)
        has_max_point = any(abs(x - float(in_max)) < eps for x, _y in points)

        # Add default boundary points only when user didn't provide a breakpoint
        # at the same input. This prevents default max/min outputs from
        # overriding explicit breakpoint values.
        if not has_min_point:
            points.insert(0, (float(in_min), float(out_min)))
        if not has_max_point:
            points.append((float(in_max), float(out_max)))

        unique_points = []
        for x, y in points:
            if not unique_points or abs(unique_points[-1][0] - x) >= eps:
                unique_points.append((x, y))
        return unique_points

    def _get_graph_input_axis_label(self) -> str:
        """Return graph input axis label."""
        unit = self._get_input_live_unit_label()
        if unit == "k\u03A9":
            return "X Axis: Input (k\u03A9)"
        if unit == "mA":
            return "X Axis: Input (mA)"
        return "X Axis: Input Voltage (V)"

    def _interpolate_curve_y(self, points, x_value: float):
        """Handle interpolate curve y."""
        if not points:
            return None

        x = float(x_value)
        if x <= float(points[0][0]):
            return float(points[0][1])
        if x >= float(points[-1][0]):
            return float(points[-1][1])

        for i in range(1, len(points)):
            x0, y0 = points[i - 1]
            x1, y1 = points[i]
            x0 = float(x0)
            y0 = float(y0)
            x1 = float(x1)
            y1 = float(y1)
            if x <= x1:
                if abs(x1 - x0) < 1e-12:
                    return y1
                t = (x - x0) / (x1 - x0)
                return y0 + (y1 - y0) * t

        return float(points[-1][1])

    def _push_graph_sample(self, input_seen=None, output_seen=None, output_set=None) -> None:
        """Handle push graph sample."""
        if self._is_ble_connected():
            if input_seen is not None:
                self._ble_target_input = float(input_seen)
            if output_seen is not None:
                self._ble_target_output = float(output_seen)
            if output_set is not None:
                self._ble_target_set = float(output_set)

            # Seed first point immediately, then let smoother animate updates.
            if (
                self.live_input_seen is None
                and self.live_output_seen is None
                and self._ble_target_input is not None
                and self._ble_target_output is not None
            ):
                self._commit_graph_sample(
                    input_seen=float(self._ble_target_input),
                    output_seen=float(self._ble_target_output),
                    output_set=self._ble_target_set,
                )
            return

        self._commit_graph_sample(input_seen=input_seen, output_seen=output_seen, output_set=output_set)

    def _commit_graph_sample(self, input_seen=None, output_seen=None, output_set=None) -> None:
        """Commit one graph sample directly to live buffers and redraw."""
        if input_seen is not None:
            self.live_input_seen = float(input_seen)
            self.graph_input_points.append(self.live_input_seen)
        if output_seen is not None:
            self.live_output_seen = float(output_seen)
            self.graph_output_points.append(self.live_output_seen)
        if output_set is not None:
            self.live_output_set = float(output_set)
            self.graph_command_points.append(self.live_output_set)

        if self.live_input_seen is not None and self.live_output_seen is not None:
            pair = (float(self.live_input_seen), float(self.live_output_seen))
            if not self.graph_iv_points:
                self.graph_iv_points.append(pair)
            else:
                last_x, last_y = self.graph_iv_points[-1]
                if abs(last_x - pair[0]) > 1e-9 or abs(last_y - pair[1]) > 1e-9:
                    self.graph_iv_points.append(pair)

        self._append_time_graph_sample()
        self._draw_output_graph()
        self._draw_time_graph()

    def _ble_graph_smooth_tick(self) -> None:
        """Animate BLE graph points toward latest notify values for smooth flow."""
        now = time.monotonic()
        dt = max(0.0, now - self._ble_smooth_last_monotonic)
        self._ble_smooth_last_monotonic = now

        if self._is_ble_connected():
            target_in = self._ble_target_input
            target_out = self._ble_target_output
            target_set = self._ble_target_set

            if target_set is not None and (
                self.live_output_set is None or abs(float(self.live_output_set) - float(target_set)) > 1e-9
            ):
                self.live_output_set = float(target_set)
                self.graph_command_points.append(self.live_output_set)
                self._draw_output_graph()

            if target_in is not None and target_out is not None:
                alpha = 1.0
                if BLE_GRAPH_SMOOTH_TAU_SECONDS > 1e-9 and dt > 0.0:
                    alpha = 1.0 - math.exp(-dt / BLE_GRAPH_SMOOTH_TAU_SECONDS)
                    alpha = max(0.08, min(1.0, alpha))

                if self.live_input_seen is None:
                    next_in = float(target_in)
                else:
                    next_in = float(self.live_input_seen) + (float(target_in) - float(self.live_input_seen)) * alpha

                if self.live_output_seen is None:
                    next_out = float(target_out)
                else:
                    next_out = float(self.live_output_seen) + (float(target_out) - float(self.live_output_seen)) * alpha

                if abs(float(target_in) - next_in) <= BLE_GRAPH_SNAP_INPUT:
                    next_in = float(target_in)
                if abs(float(target_out) - next_out) <= BLE_GRAPH_SNAP_OUTPUT:
                    next_out = float(target_out)

                moved = (
                    self.live_input_seen is None
                    or self.live_output_seen is None
                    or abs(next_in - float(self.live_input_seen)) > 1e-9
                    or abs(next_out - float(self.live_output_seen)) > 1e-9
                )
                if moved:
                    self._commit_graph_sample(input_seen=next_in, output_seen=next_out)

        self.root.after(BLE_GRAPH_SMOOTH_TICK_MS, self._ble_graph_smooth_tick)

    def _draw_output_graph(self) -> None:
        """Draw output graph."""
        if self.graph_canvas is None:
            return

        c = self.graph_canvas
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        c.delete("all")
        self.graph_hover_points = []

        left = 52
        top = 44
        right = max(left + 1, w - 52)
        bottom = max(top + 1, h - 34)
        c.create_rectangle(left, top, right, bottom, outline="#2f3d4b")

        show_measured, show_reference = self._get_visible_graph_flags()
        in_min = self._extract_number(self.cfg_in_min_var.get())
        in_max = self._extract_number(self.cfg_in_max_var.get())
        out_min = self._extract_number(self.cfg_out_min_var.get())
        out_max = self._extract_number(self.cfg_out_max_var.get())
        if in_min is None or in_max is None or in_min >= in_max:
            mode_min, mode_max = self._get_mode_input_limits(self.cfg_mode_var.get().strip())
            in_min, in_max = float(mode_min), float(mode_max)
        if out_min is None or out_max is None or out_min >= out_max:
            out_min, out_max = 0.0, 2000.0

        ref_points = self._get_cfg_reference_points(float(in_min), float(in_max), float(out_min), float(out_max))

        x_values = []
        y_values = []
        if show_measured:
            for x, y in self.graph_iv_points:
                x_values.append(float(x))
                y_values.append(float(y))
        if show_reference:
            for x, y in ref_points:
                x_values.append(float(x))
                y_values.append(float(y))

        x_min, x_max = self._calc_range(x_values, float(in_min), float(in_max))
        y_min, y_max = self._calc_range(y_values, float(out_min), float(out_max))

        def x_to_px(val: float) -> float:
            """Map an X-axis value to canvas pixel coordinates."""
            return left + (val - x_min) * (right - left) / (x_max - x_min)

        def y_current_to_px(val: float) -> float:
            """Map a current value to canvas Y pixel coordinates."""
            return bottom - (val - y_min) * (bottom - top) / (y_max - y_min)

        for i in range(1, 5):
            x = left + (right - left) * i / 5.0
            y = top + (bottom - top) * i / 5.0
            c.create_line(x, top, x, bottom, fill="#1a2632")
            c.create_line(left, y, right, y, fill="#1a2632")

        for i in range(6):
            ty = y_min + (y_max - y_min) * (5 - i) / 5.0
            py = top + (bottom - top) * i / 5.0
            c.create_text(left - 6, py, text=f"{ty:.0f}", anchor="e", fill="#8397ab", font=("Consolas", 8))
        c.create_text(left - 34, top - 8, text="mA", fill="#8397ab", font=("Consolas", 8))

        for i in range(6):
            px = left + (right - left) * i / 5.0
            tx = x_min + (x_max - x_min) * i / 5.0
            c.create_text(px, bottom + 12, text=f"{tx:.2f}", fill="#8397ab", font=("Consolas", 8))

        legend_items = []
        current_legend_added = False
        voltage_legend_added = False

        if show_reference and len(ref_points) >= 2:
            points = []
            point_pixels = []
            for x, y in ref_points:
                px = x_to_px(float(x))
                py = y_current_to_px(float(y))
                points.extend([px, py])
                point_pixels.append((px, py))
                self.graph_hover_points.append(
                    {"px": px, "py": py, "x": float(x), "y": float(y), "series": "Setpoint", "color": GRAPH_COLOR_SET_CURRENT}
                )
            c.create_line(points, fill=GRAPH_COLOR_SET_CURRENT, width=2, dash=GRAPH_DASH_SET_CURRENT)
            for px, py in point_pixels:
                c.create_oval(
                    px - 3,
                    py - 3,
                    px + 3,
                    py + 3,
                    fill=GRAPH_COLOR_SET_CURRENT,
                    outline="#0f1419",
                    width=1,
                )
            legend_items.append(("Setpoint", GRAPH_COLOR_SET_CURRENT, GRAPH_DASH_SET_CURRENT))

        if show_measured and len(self.graph_iv_points) >= 2:
            points = []
            for x, y in self.graph_iv_points:
                px = x_to_px(float(x))
                py = y_current_to_px(float(y))
                points.extend([px, py])
                self.graph_hover_points.append(
                    {"px": px, "py": py, "x": float(x), "y": float(y), "series": "Actual", "color": GRAPH_COLOR_CURRENT}
                )
            c.create_line(points, fill=GRAPH_COLOR_CURRENT, width=2, smooth=True)
            legend_items.append(("Live Current", GRAPH_COLOR_CURRENT, None))
            current_legend_added = True

            # Voltage path trail: for each live voltage sample, plot the
            # corresponding point on the setpoint curve so it moves similarly
            # to the current curve and is easy to compare.
            if len(ref_points) >= 2:
                vpath_points = []
                for x, _y in self.graph_iv_points:
                    vx = float(x)
                    vy = self._interpolate_curve_y(ref_points, vx)
                    if vy is None:
                        continue
                    px = x_to_px(vx)
                    py = y_current_to_px(float(vy))
                    vpath_points.extend([px, py])
                    self.graph_hover_points.append(
                        {"px": px, "py": py, "x": vx, "y": float(vy), "series": "Live Voltage", "color": GRAPH_COLOR_VOLTAGE}
                    )
                if len(vpath_points) >= 4:
                    c.create_line(vpath_points, fill=GRAPH_COLOR_VOLTAGE, width=2)
                    legend_items.append(("Live Voltage", GRAPH_COLOR_VOLTAGE, None))
                    voltage_legend_added = True

        if show_measured and len(self.graph_iv_points) == 1:
            x, y = self.graph_iv_points[0]
            px = x_to_px(float(x))
            py = y_current_to_px(float(y))
            c.create_oval(px - 2, py - 2, px + 2, py + 2, fill=GRAPH_COLOR_CURRENT, outline="")
            self.graph_hover_points.append(
                {"px": px, "py": py, "x": float(x), "y": float(y), "series": "Actual", "color": GRAPH_COLOR_CURRENT}
            )
        elif show_measured and self.live_input_seen is not None and self.live_output_seen is not None:
            px = x_to_px(float(self.live_input_seen))
            py = y_current_to_px(float(self.live_output_seen))
            c.create_oval(px - 3, py - 3, px + 3, py + 3, fill=GRAPH_COLOR_CURRENT, outline="")

        # For short captures (single sample), still show one blue voltage point
        # on setpoint curve.
        if show_measured and len(self.graph_iv_points) >= 1 and not voltage_legend_added and len(ref_points) >= 2:
            vx = float(self.graph_iv_points[-1][0])
            vy = self._interpolate_curve_y(ref_points, vx)
            if vy is not None:
                px = x_to_px(vx)
                py = y_current_to_px(float(vy))
                c.create_oval(px - 2, py - 2, px + 2, py + 2, fill=GRAPH_COLOR_VOLTAGE, outline="")
                self.graph_hover_points.append(
                    {"px": px, "py": py, "x": vx, "y": float(vy), "series": "Live Voltage", "color": GRAPH_COLOR_VOLTAGE}
                )
                legend_items.append(("Live Voltage", GRAPH_COLOR_VOLTAGE, None))
                voltage_legend_added = True

        live_current_overlay_drawn = False
        live_input_overlay_drawn = False

        # Always show a clear live-current sight line/marker for quick reading.
        if self.live_output_seen is not None:
            live_current = float(self.live_output_seen)
            py = y_current_to_px(live_current)
            py = max(top, min(bottom, py))
            c.create_line(left, py, right, py, fill=GRAPH_COLOR_LIVE_CURRENT, width=1, dash=(6, 3))
            live_current_overlay_drawn = True

            if self.live_input_seen is not None:
                live_voltage = float(self.live_input_seen)
                px = x_to_px(live_voltage)
                px = max(left, min(right, px))
                c.create_line(px, top, px, bottom, fill=GRAPH_COLOR_VOLTAGE, width=1, dash=(3, 5))
                live_input_overlay_drawn = True
                c.create_oval(px - 4, py - 4, px + 4, py + 4, fill=GRAPH_COLOR_LIVE_CURRENT, outline="")
                c.create_text(
                    min(right - 6, px + 10),
                    max(top + 6, py - 10),
                    anchor="w",
                    text=f"{live_voltage:.2f} V | {live_current:.1f} mA",
                    fill="#dbe5ef",
                    font=("Consolas", 8, "bold"),
                )
        if live_current_overlay_drawn and not current_legend_added:
            legend_items.append(("Live Current", GRAPH_COLOR_LIVE_CURRENT, (6, 3)))
            current_legend_added = True
        if live_input_overlay_drawn and not voltage_legend_added:
            legend_items.append(("Live Voltage", GRAPH_COLOR_VOLTAGE, (3, 5)))

        seen_x = "-" if self.live_input_seen is None else f"{self.live_input_seen:.2f}"
        seen_y = "-" if self.live_output_seen is None else f"{self.live_output_seen:.1f}"
        set_y = "-" if self.live_output_set is None else f"{self.live_output_set:.1f}"
        mode_value = self.graph_mode_var.get().strip().lower()
        mode_display = {
            "current": "Actual",
            "measured": "Actual",
            "actual": "Actual",
            "setcurrent": "Setpoint",
            "setpoint": "Setpoint",
            "voltage": "Actual",
            "both": "Both",
            "all": "Both",
        }
        mode_txt = mode_display.get(mode_value, mode_value.title())
        header_font = ("Consolas", 8)
        x_cursor = left
        y_cursor = 10
        head_items = [
            (f"Mode: {mode_txt} | ", "#9cb6cf"),
            ("Live Voltage", GRAPH_COLOR_VOLTAGE),
            (f": {seen_x} | ", "#9cb6cf"),
            ("Live Current", GRAPH_COLOR_LIVE_CURRENT),
            (f": {seen_y} mA | ", "#9cb6cf"),
            ("Setpoint I", GRAPH_COLOR_SET_CURRENT),
            (f": {set_y} mA", "#9cb6cf"),
        ]

        def _fit_text_to_width(base_text: str, start_x: float, start_y: float, color: str, max_right: float) -> int:
            """Render text clipped so it fits inside the graph bounds."""
            item_local = c.create_text(start_x, start_y, anchor="w", text=base_text, fill=color, font=header_font)
            bbox_local = c.bbox(item_local)
            if bbox_local and bbox_local[2] <= max_right:
                return item_local

            txt = base_text.rstrip()
            while txt:
                candidate = txt + "..."
                c.itemconfigure(item_local, text=candidate)
                bbox_local = c.bbox(item_local)
                if bbox_local and bbox_local[2] <= max_right:
                    return item_local
                txt = txt[:-1]

            c.itemconfigure(item_local, text="")
            return item_local

        # Single-line header only: fit/truncate text, never wrap.
        for text_value, color in head_items:
            if x_cursor >= (right - 4):
                break
            item = _fit_text_to_width(text_value, x_cursor, y_cursor, color, right - 2)
            bbox = c.bbox(item)
            if not bbox:
                break
            x_cursor = bbox[2] + 2
            if bbox[2] >= (right - 2):
                break
        c.create_text(
            (left + right) / 2,
            h - 10,
            text=self._get_graph_input_axis_label(),
            fill="#9cb6cf",
            font=("Consolas", 8),
        )

        # Legend labels intentionally hidden per UI preference.

        has_data = bool(
            (show_reference and len(ref_points) >= 2)
            or (show_measured and len(self.graph_iv_points) >= 1)
        )
        if not has_data:
            c.create_text(
                (left + right) // 2,
                (top + bottom) // 2,
                text="Waiting for telemetry... (X=Input, Y=Current)",
                fill="#7f93a8",
                font=("Segoe UI", 10),
            )

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        """Build log panel."""
        panel = ttk.LabelFrame(parent, text="Event & Communication Log", style="Card.TLabelframe", padding=10)
        panel.pack(fill="both", expand=True)

        toolbar_box = tk.Frame(
            panel,
            bg="#1b232c",
            highlightthickness=1,
            highlightbackground="#6f7f90",
            highlightcolor="#6f7f90",
            bd=0,
        )
        toolbar_box.pack(fill="x", pady=(0, 8))

        top = ttk.Frame(toolbar_box, style="Inner.TFrame", padding=(8, 6))
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=1)
        ttk.Label(top, text="Real-Time TX/RX Trace", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(top, text="Clear Log", style="Neutral.TButton", command=self.clear_log).grid(
            row=0, column=1, sticky="e"
        )

        self.log_text = scrolledtext.ScrolledText(
            panel,
            height=12,
            wrap="word",
            bg="#11151a",
            fg="#dbe5ef",
            insertbackground="#dbe5ef",
            font=("Consolas", 10),
            relief="flat",
            borderwidth=0,
        )
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # Transport discovery and background runtime helpers
    # ------------------------------------------------------------------
    def clear_log(self) -> None:
        """Clear log."""
        self.log_text.delete("1.0", "end")
        self._log("Log cleared")

    def refresh_ports(self) -> None:
        """Refresh Serial ports or BLE devices based on selected transport."""
        if self._is_ble_mode():
            self._scan_ble_devices()
            return

        if list_ports is None:
            self.port_combo["values"] = []
            self.port_var.set("")
            return

        ports = [p.device for p in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and (self.port_var.get() not in ports):
            self.port_var.set(ports[0])
        elif not ports:
            self.port_var.set("")

    def _scan_ble_devices(self) -> None:
        """Scan for BLE devices."""
        if BleakScanner is None:
            messagebox.showerror(
                "Missing Dependency",
                f"Install bleak for this interpreter:\n{sys.executable} -m pip install bleak",
            )
            return
        if self.ble_scan_in_progress:
            return

        self.ble_scan_in_progress = True
        if self.refresh_btn is not None:
            self.refresh_btn.configure(state="disabled")
        self._log("Scanning BLE devices (SERVO/EVDR only)...")
        threading.Thread(target=self._run_ble_scan, daemon=True).start()

    def _is_target_ble_device(self, dev) -> bool:
        """Return whether target BLE device."""
        metadata = getattr(dev, "metadata", {}) or {}
        details = getattr(dev, "details", None)

        name_candidates = [
            getattr(dev, "name", ""),
            metadata.get("local_name", ""),
            getattr(details, "local_name", "") if details is not None else "",
        ]
        merged_name = " ".join([str(x).strip().upper() for x in name_candidates if str(x).strip()])
        if "SERVO_EVDR" in merged_name or "SERVO_" in merged_name or "EVDR" in merged_name:
            return True

        uuid_list = metadata.get("uuids") or metadata.get("service_uuids") or []
        service_uuid = BLE_SERVICE_UUID.lower()
        for uuid in uuid_list:
            uuid_txt = str(uuid).strip().lower().replace("{", "").replace("}", "")
            if uuid_txt == service_uuid:
                return True
            if uuid_txt in ("ffe0", "0xffe0"):
                return True
            if len(uuid_txt) == 4 and ("0000" + uuid_txt + "-0000-1000-8000-00805f9b34fb") == service_uuid:
                return True
        return False

    def _run_ble_scan(self) -> None:
        """Run BLE discovery in worker thread and filter to target devices."""
        devices = []
        error = ""
        discovered_total = 0
        try:
            discovered = asyncio.run(BleakScanner.discover(timeout=4.0))
            discovered_total = len(discovered)
            seen_addresses = set()
            for dev in discovered:
                if not self._is_target_ble_device(dev):
                    continue
                address = (getattr(dev, "address", "") or "").strip()
                if not address or address in seen_addresses:
                    continue
                seen_addresses.add(address)
                name = (getattr(dev, "name", "") or "Unknown").strip() or "Unknown"
                devices.append((name, address))
            devices.sort(key=lambda item: (item[0].lower(), item[1]))
        except Exception as exc:
            error = str(exc)

        self.root.after(0, lambda: self._complete_ble_scan(devices, error, discovered_total))

    def _complete_ble_scan(self, devices, error: str, discovered_total: int = 0) -> None:
        """Handle complete BLE scan."""
        self.ble_scan_in_progress = False
        if self.refresh_btn is not None:
            self.refresh_btn.configure(state="normal")
        if not self._is_ble_mode():
            return

        self.ble_display_to_address = {}
        values = []
        addresses = set()
        name_counts = {}
        for name, address in devices:
            base_name = name or "Unknown"
            count = name_counts.get(base_name, 0) + 1
            name_counts[base_name] = count
            display = base_name if count == 1 else f"{base_name} ({count})"
            self.ble_display_to_address[display] = address
            values.append(display)
            addresses.add(address)

        self.port_combo["values"] = values
        selected = self.port_var.get().strip()
        selected_addr = self._resolve_ble_address(selected)
        if values and selected_addr not in addresses:
            self.port_var.set(values[0])
        elif not values and selected_addr not in addresses:
            self.port_var.set("")

        if error:
            self._log(f"BLE scan failed: {error}")
            messagebox.showerror("BLE Scan Error", error)
            return

        if values:
            self._log(f"BLE scan complete: {len(values)} device(s) found")
        else:
            if discovered_total == 0:
                self._log(
                    "BLE scan complete: no advertisements found. Check client Bluetooth + Location permissions."
                )
            else:
                self._log(
                    f"BLE scan complete: {discovered_total} device(s) seen, but no SERVO/EVDR target found."
                )
            messagebox.showwarning(
                "BLE Device Not Found",
                "SERVO/EVDR device not detected.\n"
                "1) Ensure SERVO Device is not in Serial session (BLE is disabled during active Serial)\n"
                "2) Keep device nearby and powered\n"
                "3) On client PC, turn ON Bluetooth + Location, then rescan",
            )

    def _ensure_ble_loop(self) -> None:
        """Start a dedicated asyncio loop thread for BLE operations."""
        if self.ble_loop is not None and self.ble_loop.is_running():
            return

        self.ble_loop = asyncio.new_event_loop()
        self.ble_loop_thread = threading.Thread(target=self._ble_loop_main, daemon=True)
        self.ble_loop_thread.start()

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.ble_loop is not None and self.ble_loop.is_running():
                return
            time.sleep(0.02)

    def _ble_loop_main(self) -> None:
        """Handle BLE loop main."""
        loop = self.ble_loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)
        loop.run_forever()

        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _stop_ble_loop(self) -> None:
        """Stop BLE loop."""
        loop = self.ble_loop
        thread = self.ble_loop_thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self.ble_loop = None
        self.ble_loop_thread = None

    def _run_ble_coro(self, coro, timeout: float = 8.0):
        """Run BLE coro."""
        if self.ble_loop is None or not self.ble_loop.is_running():
            raise RuntimeError("BLE runtime is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self.ble_loop)
        return future.result(timeout=timeout)

    def _queue_ble_payload(self, payload) -> None:
        """Queue BLE payload."""
        try:
            if isinstance(payload, (bytes, bytearray)):
                text = bytes(payload).decode("utf-8", errors="replace")
            else:
                text = str(payload)
        except Exception:
            return
        line = text.replace("\x00", "").strip()
        if line:
            self.rx_queue.put(line)

    def _on_ble_live_notify(self, _sender: int, data: bytearray) -> None:
        """Handle BLE live notify."""
        self._queue_ble_payload(data)

    def _on_ble_config_notify(self, _sender: int, data: bytearray) -> None:
        """Handle BLE config notify."""
        self._queue_ble_payload(data)

    def _on_ble_adv_notify(self, _sender: int, data: bytearray) -> None:
        """Handle BLE advanced notify."""
        self._queue_ble_payload(data)

    def _on_ble_disconnected(self, _client) -> None:
        """Handle BLE disconnected."""
        self.ble_connected = False
        if not self._disconnecting and not self._is_closing:
            self.rx_queue.put("[BLE_DISCONNECTED]")

    async def _ble_connect_async(self, address: str) -> None:
        """Handle BLE connect async."""
        client = BleakClient(address, disconnected_callback=self._on_ble_disconnected)
        try:
            await client.connect(timeout=10.0)
            await client.start_notify(BLE_LIVE_CHAR_UUID, self._on_ble_live_notify)
            await client.start_notify(BLE_CONFIG_CHAR_UUID, self._on_ble_config_notify)
            await client.start_notify(BLE_ADV_CHAR_UUID, self._on_ble_adv_notify)
        except Exception:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
            raise

        self.ble_client = client
        self.ble_connected = True

    async def _ble_disconnect_async(self) -> None:
        """Handle BLE disconnect async."""
        client = self.ble_client
        if client is None:
            return
        try:
            if client.is_connected:
                for uuid in (BLE_LIVE_CHAR_UUID, BLE_CONFIG_CHAR_UUID, BLE_ADV_CHAR_UUID):
                    try:
                        await client.stop_notify(uuid)
                    except Exception:
                        pass
                await client.disconnect()
        finally:
            self.ble_connected = False
            self.ble_client = None

    async def _ble_read_text_async(self, char_uuid: str) -> str:
        """Handle BLE read text async."""
        client = self.ble_client
        if client is None or not client.is_connected:
            raise RuntimeError("BLE device is not connected.")
        data = await client.read_gatt_char(char_uuid)
        return bytes(data).decode("utf-8", errors="replace").replace("\x00", "").strip()

    async def _ble_write_text_async(self, char_uuid: str, text: str) -> None:
        """Handle BLE write text async."""
        client = self.ble_client
        if client is None or not client.is_connected:
            raise RuntimeError("BLE device is not connected.")
        await client.write_gatt_char(char_uuid, text.encode("utf-8"), response=True)

    def toggle_connection(self) -> None:
        """Toggle connection."""
        if self._is_connected():
            self.disconnect()
        else:
            self.connect()

    def _set_update_buttons_enabled(self, enabled: bool) -> None:
        """Set update buttons enabled."""
        state = "normal" if enabled else "disabled"
        if self.apply_cfg_btn is not None:
            self.apply_cfg_btn.configure(state=state)
        if self.apply_adv_btn is not None:
            self.apply_adv_btn.configure(state=state)
        if self.load_cfg_btn is not None:
            self.load_cfg_btn.configure(state=state)

    def connect(self) -> None:
        """Connect using the currently selected transport."""
        if self._is_ble_mode():
            self._connect_ble()
            return

        self._connect_uart()

    def _connect_uart(self) -> None:
        """Validate COM selection and start Serial session + reader thread."""
        if serial is None:
            messagebox.showerror("Missing Dependency", "Install pyserial: pip install pyserial")
            return

        self.refresh_ports()
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("No Port", "Select a serial port first.")
            return

        if list_ports is not None:
            available_ports = [p.device for p in list_ports.comports()]
            if not available_ports:
                messagebox.showwarning("No Serial Port", "No serial ports detected. Plug device and click Refresh.")
                return
            if port not in available_ports:
                messagebox.showwarning("Invalid Port", f"{port} is not available now. Select a valid serial port.")
                return

        baud_candidates = []
        for item in UART_BAUD_CANDIDATES:
            try:
                val = int(item)
            except Exception:
                continue
            if val > 0 and val not in baud_candidates:
                baud_candidates.append(val)
        if not baud_candidates:
            baud_candidates = [FIXED_UART_BAUD]

        try:
            selected_baud = None
            for idx, baud_try in enumerate(baud_candidates):
                try:
                    self.ser = serial.Serial(port=port, baudrate=baud_try, timeout=0.2, write_timeout=0.5)
                except Exception:
                    self.ser = None
                    continue

                try:
                    self.ser.reset_input_buffer()
                    self.ser.reset_output_buffer()
                except Exception:
                    pass

                probe_status = self._uart_preconnect_check()
                if probe_status == "blocked":
                    try:
                        if self.ser and self.ser.is_open:
                            self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
                    return

                if probe_status == "ready":
                    selected_baud = baud_try
                    break

                try:
                    if self.ser and self.ser.is_open:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None

                if idx < len(baud_candidates) - 1:
                    self._log(f"No Serial response @ {baud_try}. Trying {baud_candidates[idx + 1]}...")

            if self.ser is None or selected_baud is None:
                fallback_baud = baud_candidates[0]
                try:
                    self.ser = serial.Serial(port=port, baudrate=fallback_baud, timeout=0.2, write_timeout=0.5)
                    try:
                        self.ser.reset_input_buffer()
                        self.ser.reset_output_buffer()
                    except Exception:
                        pass
                    selected_baud = fallback_baud
                    messagebox.showwarning(
                        "Limited Serial Probe",
                        "No immediate Serial response during baud probe.\n"
                        "Connected anyway at "
                        + str(fallback_baud)
                        + " baud.\n"
                        "If data stays empty, try BLE or verify wiring/active session.",
                    )
                    self._log("Serial probe had no immediate response; continuing with fallback connect")
                except Exception:
                    messagebox.showwarning(
                        "No Serial Data",
                        "No response from device on Serial.\n"
                        "Tried baud rates: " + ", ".join(str(b) for b in baud_candidates) + ".\n"
                        "Check port selection, wiring, and whether BLE session is active.",
                    )
                    self._log("Serial connect failed: no valid response from device")
                    return

            baud = selected_baud
            self.baud_var.set(str(baud))

            self.stop_event.clear()
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()
            self._set_device_name_display()
            self._clear_cfg_form()
            self.connect_btn.configure(text="Disconnect")
            self.connect_btn.configure(style="Warn.TButton")
            self._set_transport_toggle_enabled(False)
            self.conn_status_var.set(f"Connected: {port} @ {baud}")
            self.panel_state_var.set("CONNECTED")
            self._log(f"Connected to {port} @ {baud}")
            # Keep one delayed sync attempt for robustness on boards that reset on COM open.
            self.root.after(900, lambda: self._send_line("uart_connected", expect_response=False))
            self.root.after(1200, lambda: self._send_line("version", expect_response=False))
            self.root.after(1450, lambda: self._request_device_name(retries=6))
            self.root.after(1850, self.load_config_from_device)
        except Exception as exc:
            messagebox.showerror("Connection Error", str(exc))
            self._log(f"Connection failed: {exc}")

    def _uart_preconnect_check(self) -> str:
        """Probe Serial link and return one of: ready, blocked, no_response."""
        if not self.ser or not self.ser.is_open:
            return "no_response"

        probe_cmds = ("uart_connected", "version", "devname", "read")
        saw_any_rx = False

        for probe_cmd in probe_cmds:
            try:
                self.ser.write((probe_cmd + "\n").encode("utf-8"))
            except Exception:
                break

            wait_until = time.monotonic() + 0.65
            while time.monotonic() < wait_until:
                try:
                    raw = self.ser.readline()
                except Exception:
                    break

                if not raw:
                    time.sleep(0.03)
                    continue

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                saw_any_rx = True
                low = line.lower()

                if low == "uart_blocked_ble_active":
                    self._log("Serial connect blocked: BLE client is active")
                    messagebox.showwarning(
                        "Serial Blocked",
                        "Device is currently connected over BLE.\nDisconnect BLE first, then connect Serial.",
                    )
                    return "blocked"

                if low in ("uart_mode_on", "uart_mode_off"):
                    self.rx_queue.put(line)
                    return "ready"

                if (
                    line.startswith("FW:")
                    or line.startswith("CFG:")
                    or line.startswith("ADV:")
                    or low.startswith("devname:")
                    or ("input:" in low and "|output:" in low)
                    or low in ("unknown_command", "auth_required", "auth_success", "auth_failed", "ok")
                ):
                    self.rx_queue.put(line)
                    return "ready"

                # Preserve unrelated startup lines for the UI log.
                self.rx_queue.put(line)

            time.sleep(0.05)

        return "ready" if saw_any_rx else "no_response"

    def _connect_ble(self) -> None:
        """Connect to selected BLE endpoint and prime GUI state."""
        if BleakClient is None:
            messagebox.showerror(
                "Missing Dependency",
                f"Install bleak for this interpreter:\n{sys.executable} -m pip install bleak",
            )
            return

        target = self.port_var.get().strip()
        if not target:
            messagebox.showwarning(
                "No BLE Device",
                "Scan BLE and select a device first.\n"
                "If device is missing, make sure Serial session is disconnected and client Bluetooth + Location are ON.",
            )
            return

        address = self._resolve_ble_address(target)
        if not address:
            messagebox.showwarning("Invalid BLE Address", "Select a valid BLE address.")
            return

        try:
            self._ensure_ble_loop()
            self._run_ble_coro(self._ble_connect_async(address), timeout=12.0)
            self._ble_target_input = None
            self._ble_target_output = None
            self._ble_target_set = None
            self._ble_smooth_last_monotonic = time.monotonic()
            self._set_device_name_display(self._resolve_ble_name(target) or address)
            self._clear_cfg_form()
            self.connect_btn.configure(text="Disconnect")
            self.connect_btn.configure(style="Warn.TButton")
            self._set_transport_toggle_enabled(False)
            self.conn_status_var.set(f"Connected BLE: {address}")
            self.panel_state_var.set("CONNECTED")
            self._log(f"Connected to BLE device {address}")
            self.root.after(650, self.load_config_from_device)
        except Exception as exc:
            self.ble_connected = False
            self.ble_client = None
            self._stop_ble_loop()
            messagebox.showerror("BLE Connection Error", str(exc))
            self._log(f"BLE connection failed: {exc}")

    def disconnect(self) -> None:
        """Close active transport and return UI to disconnected state."""
        if self._disconnecting:
            return
        self._disconnecting = True

        if self._is_uart_connected():
            try:
                self.ser.write(b"uart_disconnected\n")
                self._log("TX: uart_disconnected")
            except Exception:
                pass

        self.stop_event.set()
        try:
            self._close_adv_window()
            self._close_developer_window()
            self._close_firmware_window(force=True)

            if self.reader_thread and self.reader_thread.is_alive():
                self.reader_thread.join(timeout=1.0)
            self.reader_thread = None
            self.auto_read_var.set(False)
            self._update_read_toggle_button()
            self._set_update_buttons_enabled(True)

            if self.ser:
                try:
                    if self.ser.is_open:
                        self.ser.close()
                except Exception:
                    pass
            self.ser = None

            if self.ble_client is not None:
                try:
                    self._run_ble_coro(self._ble_disconnect_async(), timeout=5.0)
                except Exception:
                    pass
            self.ble_connected = False
            self.ble_client = None
            self._stop_ble_loop()

            self.fw_version = ""
            self.ota_result = "none"
            self.device_mode_state = "NORMAL_MODE"
            self.dev_authenticated = False
            self.fw_update_mode = False
            self.cal_payload_cache = {}
            self.dev_status_var.set("DEV: LOCKED | STATE: NORMAL_MODE | FW: -")
            self._clear_sync_waiter()
            self.last_dedup_line = ""
            self.last_dedup_at = 0.0
            self.pending_devname_until = 0.0
            self.pending_load_scope = "both"
            self.devname_retry_left = 0
            self.awaiting_auth_for_update = False
            self.pending_secured_update_cmd = ""
            self.pending_auth_cmd = ""
            self.auth_retry_left = 0
            self.last_auth_cmd_used = ""
            self.update_reauth_retry_left = 0
            self.last_requested_update_cmd = ""
            self.pending_update_scope = "both"
            self.awaiting_update_result = False
            self.last_update_cmd_sent = ""
            self.last_update_sent_at = 0.0
            self.update_legacy_retry_done = False
            self.smooth_update_until = 0.0
            self.graph_input_points.clear()
            self.graph_output_points.clear()
            self.graph_command_points.clear()
            self.graph_iv_points.clear()
            self.graph_time_points.clear()
            self.graph_time_voltage_points.clear()
            self.graph_time_current_points.clear()
            self.graph_time_start_monotonic = None
            self.live_input_seen = None
            self.live_output_seen = None
            self.live_output_set = None
            self._ble_target_input = None
            self._ble_target_output = None
            self._ble_target_set = None
            self._ble_smooth_last_monotonic = time.monotonic()
            self._auto_read_waiting_rx = False
            self._last_auto_read_sent_at = 0.0
            self._draw_output_graph()
            self._draw_time_graph()
            self._set_device_name_display()
            self._clear_cfg_form()
            self.connect_btn.configure(text="Establish Link")
            self.connect_btn.configure(style="Primary.TButton")
            self._set_transport_toggle_enabled(True)
            self._on_transport_changed()
            self.conn_status_var.set("Disconnected")
            self.panel_state_var.set("IDLE")
            self._log("Disconnected")
        finally:
            self._disconnecting = False

    # ------------------------------------------------------------------
    # RX processing, validation, and payload builders
    # ------------------------------------------------------------------
    def _reader_loop(self) -> None:
        """Handle reader loop."""
        while not self.stop_event.is_set():
            if not self.ser or not self.ser.is_open:
                time.sleep(0.1)
                continue
            try:
                raw = self.ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self.rx_queue.put(line)
            except Exception as exc:
                self.rx_queue.put(f"[SERIAL_ERROR] {exc}")
                time.sleep(0.2)

    def _poll_rx_queue(self) -> None:
        """Drain queued device lines and route each line to parser logic."""
        while not self.rx_queue.empty():
            line = self.rx_queue.get()
            self.last_rx_monotonic = time.monotonic()
            if self._is_duplicate_status_rx(line):
                continue
            if self.auto_read_var.get():
                self.panel_state_var.set("READING")
            elif self._is_connected():
                self.panel_state_var.set("CONNECTED")
            if not self._should_suppress_rx_log(line):
                self._log(f"RX: {line}")
            self._handle_device_line(line)
        self.root.after(80, self._poll_rx_queue)

    def _is_duplicate_status_rx(self, line: str) -> bool:
        """Return whether duplicate status RX."""
        if self._sync_wait_active:
            return False

        text = line.strip()
        if not text:
            return False

        low = text.lower()
        is_candidate = (
            text.startswith("CFG:")
            or text.startswith("ADV:")
            or text.startswith("DEV_STATUS:")
            or text.startswith("FW_STATUS:")
            or low in (
                "auth_success",
                "auth ok",
                "auth_failed",
                "auth_required",
                "device updated",
                "uart_mode_on",
                "uart_mode_off",
                "dev_ok",
                "dev_fail",
                "dev_locked",
                "cal_mode_ready",
                "cal_mode_off",
                "cal_saved",
                "fw_mode_off",
                "fw_aborted",
            )
        )
        if not is_candidate:
            return False

        now = time.monotonic()
        if text == self.last_dedup_line and (now - self.last_dedup_at) <= 0.9:
            return True

        self.last_dedup_line = text
        self.last_dedup_at = now
        return False

    def _should_suppress_rx_log(self, line: str) -> bool:
        """Handle should suppress RX log."""
        low = line.strip().lower()
        if low.startswith("fw_chunk_ack:"):
            return True
        if low != "unknown_command":
            return False

        now = time.monotonic()
        if now <= self.pending_devname_until:
            return True
        if now <= self.pending_load_until:
            return True
        if self.last_cfgadv_rx_monotonic > 0 and (now - self.last_cfgadv_rx_monotonic) <= 1.2:
            return True
        return False

    def _get_mode_input_limits(self, mode: str):
        """Return mode input limits."""
        limits = {
            "0-5V": (0.0, 5.0),
            "0-10V": (0.0, 10.0),
            "Resistance (0-10)": (0.0, 10.0),
            "0-20mA": (0.0, 20.0),
        }
        return limits.get(mode, (0.0, 10.0))

    def _parse_number(self, text: str, field_name: str):
        """Parse number."""
        raw = text.strip()
        if not raw:
            raise ValueError(f"{field_name} is required.")
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{field_name} must be numeric.")
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must be a finite number.")
        if value.is_integer():
            return int(value)
        return value

    def _format_number(self, value) -> str:
        """Format number."""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return str(value)
        if not math.isfinite(f):
            return str(value)
        if abs(f - round(f)) < 1e-12:
            return str(int(round(f)))
        return f"{f:.12g}"

    def _get_input_live_unit_label(self) -> str:
        """Return input live unit label."""
        mode = self.cfg_mode_var.get().strip().upper()
        if "RESISTANCE" in mode:
            return "k\u03A9"
        if mode == "0-20MA":
            return "mA"
        if mode in ("0-5V", "0-10V"):
            return "V"
        return ""

    def _format_live_input_value(self, value_text: str) -> str:
        """Format live input value."""
        raw = str(value_text).strip()
        if not raw or raw == "-":
            return "-"
        num = self._extract_number(raw)
        if num is None:
            return raw

        unit = self._get_input_live_unit_label()
        if unit in ("V", "mA", "k\u03A9"):
            return f"{num:.2f} {unit}"
        return self._format_number(num)

    def _format_live_output_value(self, value_text: str) -> str:
        """Format live output value."""
        raw = str(value_text).strip()
        if not raw or raw == "-":
            return "-"
        num = self._extract_number(raw)
        if num is None:
            return raw
        return f"{self._format_number(num)} mA"

    def _refresh_live_telemetry_units(self) -> None:
        """Refresh live telemetry units."""
        current_input = self.telemetry_vars["input"].get()
        current_output = self.telemetry_vars["output"].get()
        self.telemetry_vars["input"].set(self._format_live_input_value(current_input))
        self.telemetry_vars["output"].set(self._format_live_output_value(current_output))

    def _build_cfg_json(self) -> str:
        """Validate CFG form fields and build compact JSON payload."""
        mode = self.cfg_mode_var.get().strip()
        in_min_num = self._parse_number(self.cfg_in_min_var.get(), "Input Min")
        in_max_num = self._parse_number(self.cfg_in_max_var.get(), "Input Max")
        out_min_num = self._parse_number(self.cfg_out_min_var.get(), "Output Min")
        out_max_num = self._parse_number(self.cfg_out_max_var.get(), "Output Max")

        in_min = float(in_min_num)
        in_max = float(in_max_num)
        out_min = float(out_min_num)
        out_max = float(out_max_num)

        if in_min >= in_max:
            raise ValueError("Input range invalid: Input Min must be less than Input Max.")
        if out_min >= out_max:
            raise ValueError("Output range invalid: Output Min must be less than Output Max.")

        mode_min, mode_max = self._get_mode_input_limits(mode)
        if in_min < mode_min or in_max > mode_max:
            raise ValueError(
                f"For mode '{mode}', input range must stay within [{mode_min:g}, {mode_max:g}]."
            )

        payload = {
            "mode": mode,
            "input": [in_min_num, in_max_num],
            "output": [out_min_num, out_max_num],
            "unit": self.cfg_unit_var.get().strip() or "mA",
            "points": self._collect_breakpoints(in_min, in_max, out_min, out_max),
        }
        return json.dumps(payload, separators=(",", ":"))

    def _add_breakpoint_row(self) -> None:
        """Add breakpoint row."""
        for row in self.cfg_bp_row_frames:
            if not row.winfo_ismapped():
                row.grid()
                return
        messagebox.showinfo("Limit Reached", "Maximum 5 target points allowed.")

    def _remove_breakpoint_row(self, idx: int) -> None:
        """Remove breakpoint row."""
        if idx < 0 or idx >= len(self.cfg_bp_row_frames):
            return
        self.cfg_bp_in_vars[idx].set("")
        self.cfg_bp_out_vars[idx].set("")
        self.cfg_bp_row_frames[idx].grid_remove()

    def _collect_breakpoints(self, in_min: float, in_max: float, out_min: float, out_max: float):
        """Collect breakpoints."""
        points = []
        seen_inputs = {}
        for i in range(5):
            in_text = self.cfg_bp_in_vars[i].get().strip()
            out_text = self.cfg_bp_out_vars[i].get().strip()

            if not in_text and not out_text:
                continue
            if not in_text or not out_text:
                raise ValueError(f"BP{i+1} requires both Input and Output.")

            bp_in_num = self._parse_number(in_text, f"BP{i+1} input")
            bp_out_num = self._parse_number(out_text, f"BP{i+1} output")
            bp_in = float(bp_in_num)
            bp_out = float(bp_out_num)

            if bp_out < out_min or bp_out > out_max:
                raise ValueError(
                    f"BP{i+1} output {bp_out:g} is out of output range [{out_min:g}, {out_max:g}]."
                )

            if bp_in in seen_inputs:
                first_idx = seen_inputs[bp_in]
                raise ValueError(
                    f"Duplicate input value: BP{i+1} input {bp_in:g} is already used in BP{first_idx}."
                )
            seen_inputs[bp_in] = i + 1
            points.append([bp_in_num, bp_out_num])

        for i in range(1, len(points)):
            prev_in, _prev_out = points[i - 1]
            curr_in, _curr_out = points[i]
            if curr_in <= prev_in:
                raise ValueError(
                    f"Input order invalid: BP{i+1} input must be greater than BP{i} input."
                )
        return points

    def _build_adv_json(self) -> str:
        """Build ADV payload JSON from current advanced controls."""
        payload = {
            "kp": float(self.adv_kp_var.get().strip()),
            "ki": float(self.adv_ki_var.get().strip()),
            "ditherEnable": 1 if self.adv_dither_enable_var.get() else 0,
            "ditherFreq": float(self.adv_dither_freq_var.get().strip()),
            "ditherAmplitude": float(self.adv_dither_amp_var.get().strip()),
            "pwmFreq": int(float(self.adv_pwm_freq_var.get().strip())),
        }
        return json.dumps(payload, separators=(",", ":"))

    def _load_cfg_json(self, cfg_json: str) -> None:
        """Load configuration json."""
        data = json.loads(cfg_json)
        self.cfg_mode_var.set(str(data.get("mode", self.cfg_mode_var.get())))
        in_vals = data.get("input", [0, 10])
        out_vals = data.get("output", [0, 2000])
        self.cfg_in_min_var.set(self._format_number(in_vals[0] if len(in_vals) > 0 else 0))
        self.cfg_in_max_var.set(self._format_number(in_vals[1] if len(in_vals) > 1 else 10))
        self.cfg_out_min_var.set(self._format_number(out_vals[0] if len(out_vals) > 0 else 0))
        self.cfg_out_max_var.set(self._format_number(out_vals[1] if len(out_vals) > 1 else 2000))
        self.cfg_unit_var.set(str(data.get("unit", "mA")))

        for i in range(5):
            self.cfg_bp_in_vars[i].set("")
            self.cfg_bp_out_vars[i].set("")
            if i < len(self.cfg_bp_row_frames):
                self.cfg_bp_row_frames[i].grid_remove()

        points = data.get("points", [])
        for i, p in enumerate(points[:5]):
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                self.cfg_bp_in_vars[i].set(self._format_number(p[0]))
                self.cfg_bp_out_vars[i].set(self._format_number(p[1]))
                if i < len(self.cfg_bp_row_frames):
                    self.cfg_bp_row_frames[i].grid()
        self._draw_output_graph()
        self._refresh_live_telemetry_units()

    def _load_adv_json(self, adv_json: str) -> None:
        """Load advanced json."""
        data = json.loads(adv_json)
        self.adv_kp_var.set(str(data.get("kp", self.adv_kp_var.get())))
        self.adv_ki_var.set(str(data.get("ki", self.adv_ki_var.get())))
        self.adv_dither_enable_var.set(int(data.get("ditherEnable", 0)) == 1)
        self.adv_dither_freq_var.set(str(data.get("ditherFreq", self.adv_dither_freq_var.get())))
        self.adv_dither_amp_var.set(str(data.get("ditherAmplitude", self.adv_dither_amp_var.get())))
        self.adv_pwm_freq_var.set(str(data.get("pwmFreq", self.adv_pwm_freq_var.get())))

    def _parse_pipe_kv(self, line: str):
        """Parse pipe kv."""
        parsed = {}
        for part in line.split("|"):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            parsed[key.strip().upper()] = value.strip()
        return parsed

    def _update_dev_status_label(self) -> None:
        """Update dev status label."""
        auth_text = "UNLOCKED" if self.dev_authenticated else "LOCKED"
        fw_text = self.fw_version if self.fw_version else "-"
        self.dev_status_var.set(f"DEV: {auth_text} | STATE: {self.device_mode_state} | FW: {fw_text}")
        if self.dev_window is not None and self.dev_window.winfo_exists():
            self._refresh_dev_window_status()

    def _set_fw_upload_progress(self, percent: float, state_text: str = "Idle") -> None:
        """Set firmware upload progress."""
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            pct = 0.0
        pct = max(0.0, min(100.0, pct))
        pct_round = int(round(pct))
        self.fw_upload_progress_var.set(pct)
        label = (
            f"BIN Upload Progress: {state_text} ({pct_round}%)"
            if state_text
            else f"BIN Upload Progress: {pct_round}%"
        )
        self.fw_upload_progress_text_var.set(label)
        if self.firmware_window is not None and self.firmware_window.winfo_exists():
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass

    def _reset_fw_upload_progress(self) -> None:
        """Handle reset firmware upload progress."""
        self._set_fw_upload_progress(0.0, "Idle")
        self.fw_upload_file_var.set("")

    def _ensure_fw_progress_widgets(self) -> None:
        """Ensure firmware progress widgets exist in the firmware window."""
        if self.firmware_window is None or not self.firmware_window.winfo_exists():
            return
        if self.fw_upload_progress_frame is not None and self.fw_upload_progress_frame.winfo_exists():
            return
        if self.fw_window_box is None or not self.fw_window_box.winfo_exists():
            return
        self.fw_upload_progress_frame = ttk.Frame(self.fw_window_box, style="Inner.TFrame")
        self.fw_upload_progressbar = ttk.Progressbar(
            self.fw_upload_progress_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100.0,
            variable=self.fw_upload_progress_var,
            style="FW.Horizontal.TProgressbar",
        )
        self.fw_upload_progressbar.pack(fill="x", pady=(0, 4), ipady=2)
        self.fw_upload_progress_label = ttk.Label(
            self.fw_upload_progress_frame,
            textvariable=self.fw_upload_progress_text_var,
            style="Field.TLabel",
        )
        self.fw_upload_progress_label.pack(anchor="w", pady=(0, 8))
        self.fw_upload_file_label = ttk.Label(
            self.fw_upload_progress_frame,
            textvariable=self.fw_upload_file_var,
            style="TelemetryHint.TLabel",
        )
        self.fw_upload_file_label.pack(anchor="w", pady=(0, 8))
        self._reset_fw_upload_progress()

    def _show_fw_upload_progress_ui(self) -> None:
        """Handle show firmware upload progress UI."""
        self._ensure_fw_progress_widgets()
        if self.fw_upload_progress_frame is None or not self.fw_upload_progress_frame.winfo_exists():
            return
        if self.fw_upload_progress_frame.winfo_manager():
            return
        if self.fw_update_btn is not None and self.fw_update_btn.winfo_exists():
            self.fw_upload_progress_frame.pack(fill="x", pady=(0, 8), before=self.fw_update_btn)
        else:
            self.fw_upload_progress_frame.pack(fill="x", pady=(0, 8))
        self._autosize_firmware_window()

    def _hide_fw_upload_progress_ui(self) -> None:
        """Handle hide firmware upload progress UI."""
        if self.fw_upload_progress_frame is not None and self.fw_upload_progress_frame.winfo_exists():
            try:
                self.fw_upload_progress_frame.pack_forget()
            except tk.TclError:
                pass
        self._autosize_firmware_window()

    def _autosize_dev_window(self) -> None:
        """Handle autosize dev window."""
        if self.dev_window is None or not self.dev_window.winfo_exists():
            return
        try:
            self.dev_window.update_idletasks()
            req_w = max(620, self.dev_window.winfo_reqwidth() + 6)
            req_h = max(380, self.dev_window.winfo_reqheight() + 6)
            cur_w = max(1, self.dev_window.winfo_width())
            cur_h = max(1, self.dev_window.winfo_height())
            # Grow/shrink to current content instead of using a fixed large size.
            if cur_w != req_w or cur_h != req_h:
                self.dev_window.geometry(f"{req_w}x{req_h}")
            self.dev_window.minsize(req_w, req_h)
        except tk.TclError:
            return

    def _autosize_firmware_window(self) -> None:
        """Auto-size firmware window around current content."""
        if self.firmware_window is None or not self.firmware_window.winfo_exists():
            return
        try:
            self.firmware_window.update_idletasks()
            req_w = max(1, self.firmware_window.winfo_reqwidth() + 6)
            req_h = max(1, self.firmware_window.winfo_reqheight() + 6)
            cur_w = max(1, self.firmware_window.winfo_width())
            cur_h = max(1, self.firmware_window.winfo_height())
            if cur_w != req_w or cur_h != req_h:
                self.firmware_window.geometry(f"{req_w}x{req_h}")
        except tk.TclError:
            return

    def _set_firmware_update_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable firmware window controls while upload runs."""
        state = "normal" if enabled else "disabled"
        if self.fw_update_btn is None:
            return
        try:
            if self.fw_update_btn.winfo_exists():
                self.fw_update_btn.configure(state=state)
        except tk.TclError:
            return

    def _set_sync_waiter(self, matcher) -> None:
        """Set sync waiter."""
        self._sync_wait_active = True
        self._sync_wait_line = ""
        self._sync_wait_match = matcher

    def _clear_sync_waiter(self) -> None:
        """Clear sync waiter."""
        self._sync_wait_active = False
        self._sync_wait_line = ""
        self._sync_wait_match = None

    def _wait_for_sync_line(self, timeout_ms: int) -> str:
        """Wait for sync line."""
        end = time.monotonic() + (max(100, timeout_ms) / 1000.0)
        while time.monotonic() < end:
            if self._sync_wait_line:
                line = self._sync_wait_line
                self._clear_sync_waiter()
                return line
            try:
                self.root.update()
            except tk.TclError:
                self._clear_sync_waiter()
                return ""
            time.sleep(0.01)
        self._clear_sync_waiter()
        return ""

    def _send_and_wait_line(self, cmd: str, matcher, timeout_ms: int, retries: int = 0) -> str:
        """Send and wait line."""
        attempts = max(0, retries) + 1
        for idx in range(attempts):
            self._set_sync_waiter(matcher)
            self._send_line(cmd, expect_response=False)
            matched = self._wait_for_sync_line(timeout_ms)
            if matched:
                return matched
            if idx < attempts - 1:
                self._log(f"No reply for '{cmd}', retrying ({idx + 1}/{attempts - 1})...")
        return ""

    def _request_developer_password(self) -> str:
        """Request developer password."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Developer Login")
        dialog.configure(bg="#0f1419")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        result = {"password": ""}

        wrapper = ttk.Frame(dialog, style="Panel.TFrame", padding=(18, 16, 18, 14))
        wrapper.pack(fill="both", expand=True)
        wrapper.grid_columnconfigure(0, weight=1)

        ttk.Label(wrapper, text="Developer Login", style="SubTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(wrapper, text="Enter developer password", style="Field.TLabel").grid(
            row=1, column=0, sticky="w", pady=(12, 6)
        )

        password_var = tk.StringVar()
        entry = ttk.Entry(wrapper, textvariable=password_var, show="*", width=34)
        entry.grid(row=2, column=0, sticky="ew")

        buttons = ttk.Frame(wrapper, style="Panel.TFrame")
        buttons.grid(row=3, column=0, sticky="e", pady=(16, 0))

        def submit() -> None:
            """Handle dialog submit action."""
            result["password"] = password_var.get().strip()
            dialog.destroy()

        def cancel() -> None:
            """Handle dialog cancel action."""
            result["password"] = ""
            dialog.destroy()

        ttk.Button(buttons, text="OK", style="Primary.TButton", width=10, command=submit).pack(side="left")
        ttk.Button(buttons, text="Cancel", style="Neutral.TButton", width=10, command=cancel).pack(
            side="left", padx=(8, 0)
        )

        dialog.bind("<Return>", lambda _event: submit())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

        dialog.update_idletasks()
        width = 360
        height = 178
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        entry.focus_set()
        self.root.wait_window(dialog)

        return result["password"]

    def _query_firmware_snapshot(self) -> str:
        """Handle query firmware snapshot."""
        if self._is_uart_connected():
            line = self._send_and_wait_line(
                "version",
                lambda txt: txt.strip().upper().startswith("FW:"),
                FW_CMD_RESPONSE_TIMEOUT_MS,
                retries=1,
            )
            return line.strip()

        line = self._send_and_wait_line(
            "DEV_STATUS",
            lambda txt: txt.strip().upper().startswith("DEV_STATUS:"),
            DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        return line.strip()

    def _ensure_developer_unlocked(self, force_prompt: bool = False) -> bool:
        """Ensure developer unlocked."""
        status_line = self._send_and_wait_line(
            "DEV_STATUS",
            lambda txt: txt.strip().upper().startswith("DEV_STATUS:"),
            DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=0,
        )
        if status_line:
            status_kv = self._parse_pipe_kv(status_line)
            self.dev_authenticated = status_kv.get("DEV_STATUS", "").upper() == "UNLOCKED"
            self.device_mode_state = status_kv.get("STATE", self.device_mode_state)
            self.fw_version = status_kv.get("FW_VERSION", self.fw_version)
            self.ota_result = status_kv.get("FW_RESULT", self.ota_result)
            self.fw_update_mode = self.device_mode_state == "FW_UPDATE_MODE"
            self._update_dev_status_label()
            if self.dev_authenticated and not force_prompt:
                return True

        password = self._request_developer_password()
        if not password:
            self._log("Developer login cancelled")
            return False

        reply = self._send_and_wait_line(
            f"DEV_LOGIN:{password}",
            lambda txt: txt.strip().upper() in ("DEV_OK", "DEV_FAIL"),
            DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not reply:
            messagebox.showwarning("Developer Login", "No response from device for DEV_LOGIN.")
            return False

        if reply.strip().upper() != "DEV_OK":
            self.dev_authenticated = False
            self.device_mode_state = "NORMAL_MODE"
            self._update_dev_status_label()
            messagebox.showerror("Developer Login Failed", "Invalid developer password.")
            return False

        self.dev_authenticated = True
        self.device_mode_state = "DEV_MODE_UNLOCKED"
        self._update_dev_status_label()
        self._log("Developer mode unlocked")
        self._send_and_wait_line(
            "DEV_STATUS",
            lambda txt: txt.strip().upper().startswith("DEV_STATUS:"),
            DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=0,
        )
        return True

    def _developer_logout(self) -> None:
        """Handle developer logout."""
        if self._is_connected():
            reply = self._send_and_wait_line(
                "DEV_LOGOUT",
                lambda txt: txt.strip().upper() in ("DEV_LOCKED", "FW_MODE_OFF"),
                DEV_AUTH_RESPONSE_TIMEOUT_MS,
                retries=0,
            )
            if reply:
                self._log(f"Developer session closed: {reply}")
        self.dev_authenticated = False
        self.fw_update_mode = False
        self.device_mode_state = "NORMAL_MODE"
        self._update_dev_status_label()
        self._refresh_dev_window_status()

    def _parse_cal_payload(self, line: str):
        """Parse cal payload."""
        if not line.startswith("CAL:"):
            return {}
        try:
            data = json.loads(line[4:].strip())
        except Exception:
            return {}
        fields = [
            "MA_SCALE_FACTOR_4_20MA",
            "RESISTANCE_SCALE_FACTOR",
            "VOLTAGE_SCALE_FACTOR",
            "FB_TO_CURRENT_FACTOR",
            "FB_OFFSET",
        ]
        result = {}
        for key in fields:
            if key in data:
                result[key] = float(data[key])
        return result

    def _transfer_firmware_chunks(self, image_bytes: bytes, fast_protocol: bool) -> bool:
        """Handle transfer firmware chunks."""
        total = len(image_bytes)
        if total <= 0:
            self._set_fw_upload_progress(0.0, "No data")
            return False
        sent = 0
        chunk_size = FW_CHUNK_BYTES if fast_protocol else FW_CHUNK_LEGACY_BYTES
        last_progress_log = 0
        self._set_fw_upload_progress(0.0, "Uploading")
        while sent < total:
            chunk = image_bytes[sent : sent + chunk_size]
            hex_payload = chunk.hex().upper()
            expected_total = sent + len(chunk)
            chunk_cmd = f"FW_CHUNK:{sent}:{hex_payload}" if fast_protocol else f"FW_CHUNK:{hex_payload}"

            ack_ok = False
            for _retry in range(FW_CHUNK_RETRY_COUNT + 1):
                ack_line = self._send_and_wait_line(
                    chunk_cmd,
                    lambda txt: txt.strip().upper().startswith("FW_CHUNK_ACK:")
                    or txt.strip().upper().startswith("FW_ERROR:")
                    or txt.strip().upper() in ("DEV_REQUIRED", "FW_MODE_REQUIRED", "BUSY_FW_UPDATE"),
                    FW_CHUNK_ACK_TIMEOUT_MS,
                    retries=0,
                )
                if not ack_line:
                    continue
                upper = ack_line.strip().upper()
                if upper.startswith("FW_CHUNK_ACK:"):
                    try:
                        ack_bytes = int(ack_line.split(":", 1)[1].strip())
                    except Exception:
                        ack_bytes = -1
                    if ack_bytes >= expected_total:
                        expected_total = min(ack_bytes, total)
                        ack_ok = True
                        break
                    self._log(f"FW chunk ack mismatch: got {ack_bytes}, expected >= {expected_total}")
                    continue

                self._log(f"FW chunk failed: {ack_line}")
                return False

            if not ack_ok:
                if fast_protocol and chunk_size > FW_CHUNK_MIN_BYTES:
                    chunk_size = max(FW_CHUNK_MIN_BYTES, chunk_size // 2)
                    self._log(f"FW chunk timeout; reducing chunk size to {chunk_size} bytes")
                    continue
                self._log("FW chunk retries exhausted")
                return False

            sent = expected_total
            progress_pct = (sent / total) * 100.0
            self.panel_state_var.set(f"FW {int(progress_pct)}%")
            self._set_fw_upload_progress(progress_pct, "Uploading")
            if sent >= total or (sent - last_progress_log) >= 10240:
                self._log(f"FW progress: {sent}/{total} bytes")
                last_progress_log = sent
        return True

    def _parse_partition_size(self, token: str) -> int:
        """Parse partition size."""
        text = token.strip().lower()
        if not text:
            return 0
        try:
            if text.startswith("0x"):
                return int(text, 16)
            if text.endswith("k"):
                return int(float(text[:-1]) * 1024)
            if text.endswith("m"):
                return int(float(text[:-1]) * 1024 * 1024)
            return int(text, 10)
        except ValueError:
            return 0

    def _detect_device_variant(self, device_name: str) -> str:
        """Infer device variant from DEVNAME text."""
        txt = str(device_name or "").strip().upper()
        if "SERVO_EVDR_DUAL" in txt:
            return "SERVO_EVDR_DUAL"
        if FW_ALLOWED_VARIANT in txt:
            return FW_ALLOWED_VARIANT
        return ""

    def _detect_bin_variant(self, image_bytes: bytes) -> str:
        """Infer firmware variant from BIN content markers."""
        data = bytes(image_bytes or b"")
        if not data:
            return ""
        # Explicit dual marker check first so overlapping substrings do not misclassify.
        if b"FW_MODEL=SERVO_EVDR_DUAL" in data:
            return "SERVO_EVDR_DUAL"
        if FW_REQUIRED_MODEL_MARKER.encode("ascii") in data:
            return FW_ALLOWED_VARIANT
        return ""

    def _firmware_hash_prefix(self, image_bytes: bytes) -> str:
        """Return short SHA-256 prefix for FW_BEGIN integrity check."""
        # Keep prefix short to limit FW_BEGIN length, full hash is still derived from entire BIN.
        digest = hashlib.sha256(bytes(image_bytes or b"")).hexdigest().upper()
        return digest[:FW_HASH_PREFIX_LEN]

    def _validate_firmware_variant_match(self, image_bytes: bytes) -> tuple[bool, str, str, str, str]:
        """Validate BIN variant against connected device variant."""
        dev_name = self.device_name_var.get().strip()
        device_variant = self._detect_device_variant(dev_name)
        if not device_variant:
            return False, "", "", "", (
                "Unable to detect connected device variant from DEVNAME.\n"
                f"Connect device and confirm DEVNAME includes {FW_ALLOWED_VARIANT}."
            )
        if device_variant != FW_ALLOWED_VARIANT:
            return False, device_variant, "", "", (
                f"Unsupported device variant for this app version.\n"
                f"Allowed device variant: {FW_ALLOWED_VARIANT}\n"
                f"Detected device variant: {device_variant}"
            )

        bin_variant = self._detect_bin_variant(image_bytes)
        if not bin_variant:
            return False, device_variant, "", "", (
                "Unable to detect firmware variant from selected BIN.\n"
                f"BIN must include marker: {FW_REQUIRED_MODEL_MARKER}."
            )
        if bin_variant != FW_ALLOWED_VARIANT:
            return False, device_variant, bin_variant, "", (
                f"Unsupported BIN variant for this app version.\n"
                f"Allowed BIN variant: {FW_ALLOWED_VARIANT}\n"
                f"Detected BIN variant: {bin_variant}"
            )
        # Enforce that this BIN belongs to this firmware project line.
        if FW_REQUIRED_PROJECT_MARKER.encode("ascii") not in image_bytes:
            return False, device_variant, bin_variant, "", (
                "Firmware identity marker missing in selected BIN.\n"
                f"BIN must include marker: {FW_REQUIRED_PROJECT_MARKER}."
            )

        if device_variant != bin_variant:
            return False, device_variant, bin_variant, "", (
                f"Firmware variant mismatch.\nDevice: {device_variant}\nBIN: {bin_variant}"
            )

        # Return hash prefix that firmware verifies across received chunks at FW_END.
        return True, device_variant, bin_variant, self._firmware_hash_prefix(image_bytes), ""

    def _validate_ota_bin_build_context(self, bin_path: Path, image_size: int) -> str:
        """Validate OTA bin build context."""
        build_dir = bin_path.parent
        partitions_path = build_dir / "partitions.csv"
        options_path = build_dir / "build.options.json"
        partition_scheme = ""

        if options_path.exists():
            try:
                options = json.loads(options_path.read_text(encoding="utf-8"))
                fqbn = str(options.get("fqbn", ""))
                match = re.search(r"PartitionScheme=([^,]+)", fqbn)
                if match:
                    partition_scheme = match.group(1)
            except Exception:
                partition_scheme = ""

        if not partitions_path.exists():
            return ""

        ota_slots = []
        try:
            for raw_line in partitions_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if not line:
                    continue
                cols = [part.strip() for part in line.split(",")]
                if len(cols) < 5:
                    continue
                part_type = cols[1].lower()
                subtype = cols[2].lower()
                if part_type == "app" and subtype in ("ota_0", "ota_1"):
                    ota_slots.append((cols[0], subtype, self._parse_partition_size(cols[4])))
        except Exception as exc:
            self._log(f"Partition validation skipped: {exc}")
            return ""

        subtypes = {subtype for _name, subtype, _size in ota_slots}
        if "ota_0" not in subtypes or "ota_1" not in subtypes:
            scheme_text = f" ({partition_scheme})" if partition_scheme else ""
            return (
                "Selected BIN was built with a non-OTA partition layout"
                f"{scheme_text}.\n\n"
                "Rebuild both device firmware and OTA target using an OTA partition scheme:\n"
                "No FS 4MB (2MB APP x2) or Minimal SPIFFS (1.9MB APP with OTA)."
            )

        slot_sizes = [size for _name, _subtype, size in ota_slots if size > 0]
        if slot_sizes and image_size > min(slot_sizes):
            return (
                f"Selected BIN is {image_size} bytes, but the smallest OTA slot is {min(slot_sizes)} bytes.\n\n"
                "Choose a larger OTA partition scheme or reduce firmware size."
            )

        return ""

    def open_developer_firmware_flow(self) -> None:
        """Run firmware update flow from Firmware Update window."""
        if self.fw_upload_active:
            messagebox.showwarning(
                "Firmware Update In Progress",
                "Firmware upload is already running.\nPlease wait for completion.",
            )
            return

        if self.firmware_window is None or not self.firmware_window.winfo_exists():
            self.open_firmware_update_window()

        if not self._is_ble_mode():
            messagebox.showwarning(
                "Firmware Update",
                "Firmware update is BLE-only.\nSwitch transport to BLE and connect device first.",
            )
            return

        if not self._is_ble_connected():
            messagebox.showwarning("Not Connected", "Connect BLE device first.")
            return
        if self.auto_read_var.get():
            self.stop_reading()

        if not self._ensure_firmware_update_authorized():
            return

        self._ensure_fw_progress_widgets()
        self._hide_fw_upload_progress_ui()
        self._reset_fw_upload_progress()

        bin_path = filedialog.askopenfilename(
            title="Select Firmware BIN",
            filetypes=[("Firmware Binary", "*.bin"), ("All Files", "*.*")],
            parent=self.root,
        )
        if not bin_path:
            self._log("Firmware update cancelled: no file selected")
            return

        try:
            image_bytes = Path(bin_path).read_bytes()
        except Exception as exc:
            messagebox.showerror("Firmware Update", f"Cannot read firmware file:\n{exc}")
            return

        if not image_bytes:
            messagebox.showerror("Firmware Update", "Selected firmware file is empty.")
            return

        validation_error = self._validate_ota_bin_build_context(Path(bin_path), len(image_bytes))
        if validation_error:
            self._log(validation_error.replace("\n", " "))
            if "non-OTA partition layout" in validation_error:
                proceed = messagebox.askyesno(
                    "Firmware Update",
                    validation_error
                    + "\n\nContinue only if the ESP32 was already USB-flashed with an OTA partition scheme.",
                )
                if not proceed:
                    return
            else:
                messagebox.showerror("Firmware Update", validation_error)
                return

        variant_ok, device_variant, bin_variant, bin_hash_prefix, variant_error = self._validate_firmware_variant_match(image_bytes)
        if not variant_ok:
            self._log(variant_error.replace("\n", " "))
            messagebox.showwarning("Firmware Update", variant_error)
            return
        self._log(
            f"Firmware variant check passed: device={device_variant}, bin={bin_variant}, sha16={bin_hash_prefix}"
        )

        self._show_fw_upload_progress_ui()
        self.fw_upload_file_var.set(f"File: {Path(bin_path).name}")
        self._set_fw_upload_progress(1.0, "Preparing upload")
        self.fw_upload_active = True
        self._set_firmware_update_controls_enabled(False)
        messagebox.showinfo(
            "Firmware Upload Started",
            "Firmware upload has started.\nDo not close Firmware Update window and do not press any buttons until it completes.",
        )

        try:
            pre_snapshot = self._query_firmware_snapshot()
            if pre_snapshot:
                self._log(f"FW before update: {pre_snapshot}")

            fw_enter = self._send_and_wait_line(
                "FW_ENTER",
                lambda txt: txt.strip().upper().startswith("FW_MODE_READY")
                or txt.strip().upper().startswith("FW_ERROR:")
                or txt.strip().upper() in ("DEV_REQUIRED", "BUSY_FW_UPDATE", "FW_BLE_ONLY"),
                FW_CMD_RESPONSE_TIMEOUT_MS,
                retries=1,
            )
            if not fw_enter or not fw_enter.strip().upper().startswith("FW_MODE_READY"):
                self._set_fw_upload_progress(0.0, "FW_ENTER failed")
                messagebox.showwarning("Firmware Update", f"FW_ENTER failed: {fw_enter or 'no response'}")
                return

            self.fw_update_mode = True
            self.device_mode_state = "FW_UPDATE_MODE"
            self._update_dev_status_label()
            self._set_fw_upload_progress(3.0, "OTA mode ready")

            target_version = Path(bin_path).stem.replace(",", "_")
            fw_begin = self._send_and_wait_line(
                f"FW_BEGIN:{len(image_bytes)},{target_version},{bin_variant},{bin_hash_prefix}",
                lambda txt: txt.strip().upper().startswith("FW_READY:")
                or txt.strip().upper().startswith("FW_ERROR:")
                or txt.strip().upper() in ("DEV_REQUIRED", "FW_MODE_REQUIRED", "FW_BLE_ONLY"),
                FW_CMD_RESPONSE_TIMEOUT_MS,
                retries=1,
            )
            if not fw_begin or not fw_begin.strip().upper().startswith("FW_READY:"):
                self._set_fw_upload_progress(0.0, "FW_BEGIN failed")
                messagebox.showwarning("Firmware Update", f"FW_BEGIN failed: {fw_begin or 'no response'}")
                self._send_and_wait_line(
                    "FW_ABORT",
                    lambda txt: txt.strip().upper().startswith("FW_"),
                    FW_CMD_RESPONSE_TIMEOUT_MS,
                    retries=0,
                )
                self.fw_update_mode = False
                self.device_mode_state = "DEV_MODE_UNLOCKED"
                self._update_dev_status_label()
                return

            fast_protocol = "V1.9_BLE_OTA_FAST" in (pre_snapshot + fw_enter + self.fw_version).upper()
            if fast_protocol:
                self._log(f"Starting fast FW transfer: {len(image_bytes)} bytes from {Path(bin_path).name}")
            else:
                self._log("Device firmware does not advertise fast OTA; using legacy 100-byte chunks")
                self._log(f"Starting FW transfer: {len(image_bytes)} bytes from {Path(bin_path).name}")

            if not self._transfer_firmware_chunks(image_bytes, fast_protocol):
                self._send_and_wait_line(
                    "FW_ABORT",
                    lambda txt: txt.strip().upper().startswith("FW_"),
                    FW_CMD_RESPONSE_TIMEOUT_MS,
                    retries=0,
                )
                fail_pct = self.fw_upload_progress_var.get()
                self._set_fw_upload_progress(fail_pct, "Upload failed")
                messagebox.showerror("Firmware Update", "Firmware transfer failed during chunk upload.")
                self.fw_update_mode = False
                self.device_mode_state = "DEV_MODE_UNLOCKED"
                self._update_dev_status_label()
                return

            fw_end = self._send_and_wait_line(
                "FW_END",
                lambda txt: txt.strip().upper().startswith("FW_TRANSFER_DONE")
                or txt.strip().upper().startswith("FW_ERROR:")
                or txt.strip().upper() in ("DEV_REQUIRED", "FW_MODE_REQUIRED", "FW_BLE_ONLY"),
                FW_CMD_RESPONSE_TIMEOUT_MS,
                retries=1,
            )
            if not fw_end or not fw_end.strip().upper().startswith("FW_TRANSFER_DONE"):
                self._set_fw_upload_progress(100.0, "Finalize failed")
                messagebox.showerror("Firmware Update", f"FW_END failed: {fw_end or 'no response'}")
                self.fw_update_mode = False
                self.device_mode_state = "DEV_MODE_UNLOCKED"
                self._update_dev_status_label()
                return

            self._set_fw_upload_progress(100.0, "Upload complete")
            self._log(fw_end.strip())
            self._log("Firmware transfer completed. Device will reboot to validate new slot.")
            self.fw_update_mode = False
            self.device_mode_state = "RETURN_TO_NORMAL"
            self._update_dev_status_label()

            self.root.after(4500, self._post_firmware_reboot_check)
            messagebox.showinfo(
                "Firmware Update",
                "Firmware upload completed.\nWaiting for reboot, then app will read new firmware version.",
            )
        finally:
            self.fw_upload_active = False
            self._set_firmware_update_controls_enabled(True)
            self._hide_fw_upload_progress_ui()

    def _ensure_firmware_update_authorized(self) -> bool:
        """Authenticate firmware update flow using AUTH password prompt."""
        password = self._request_update_password("Enter AUTH password for firmware update:")
        if not password:
            self._log("Firmware update cancelled: password not entered")
            return False

        self.panel_state_var.set("AUTH")
        auth_reply = self._send_and_wait_line(
            f"auth:{password}",
            lambda txt: txt.strip().lower() in ("auth_success", "auth_failed", "auth ok"),
            AUTH_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not auth_reply:
            self.panel_state_var.set("CONNECTED" if self._is_connected() else "IDLE")
            messagebox.showwarning("Auth Timeout", "No authentication response from device.")
            return False
        if auth_reply.strip().lower() not in ("auth_success", "auth ok"):
            self.panel_state_var.set("CONNECTED" if self._is_connected() else "IDLE")
            messagebox.showerror("Authentication Failed", "Wrong AUTH password.")
            return False

        # Firmware commands are gated by DEV session in device code; send DEV_LOGIN
        # with same password (firmware accepts AUTH password for DEV unlock).
        dev_reply = self._send_and_wait_line(
            f"DEV_LOGIN:{password}",
            lambda txt: txt.strip().upper() in ("DEV_OK", "DEV_FAIL"),
            DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        self.panel_state_var.set("CONNECTED" if self._is_connected() else "IDLE")
        if not dev_reply:
            messagebox.showwarning("Firmware Access", "No response for firmware access unlock.")
            return False
        if dev_reply.strip().upper() != "DEV_OK":
            messagebox.showerror("Firmware Access", "Firmware access unlock failed.")
            return False
        return True

    def _post_firmware_reboot_check(self) -> None:
        """Run post-firmware reboot checks."""
        if not self._is_connected():
            self._log("Post-OTA check skipped: transport disconnected")
            self.dev_authenticated = False
            self.fw_update_mode = False
            self.device_mode_state = "NORMAL_MODE"
            self._update_dev_status_label()
            return
        snapshot = self._query_firmware_snapshot()
        if snapshot:
            self._log(f"FW after update: {snapshot}")
        status_line = self._send_and_wait_line(
            "DEV_STATUS",
            lambda txt: txt.strip().upper().startswith("DEV_STATUS:"),
            DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=0,
        )
        if status_line:
            self._log(status_line)
        self._developer_logout()

    # ------------------------------------------------------------------
    # Device protocol parsing and command handling
    # ------------------------------------------------------------------
    def _handle_device_line(self, line: str) -> None:
        """Parse one RX line and update state/UI based on protocol messages."""
        if line.strip() == "[BLE_DISCONNECTED]":
            self._log("BLE link disconnected")
            self.disconnect()
            return

        # Immediate transport-level error handling.
        if line.startswith("[SERIAL_ERROR]"):
            self.conn_status_var.set("Serial error")
            self.panel_state_var.set("ERROR")
            return

        # Authentication and update acknowledgement flow.
        low_line = line.strip().lower()
        line_upper = line.strip().upper()

        if self._sync_wait_active and callable(self._sync_wait_match):
            try:
                if self._sync_wait_match(line):
                    self._sync_wait_line = line
            except Exception:
                pass

        if line_upper.startswith("DEV_STATUS:"):
            status_kv = self._parse_pipe_kv(line)
            self.dev_authenticated = status_kv.get("DEV_STATUS", "").upper() == "UNLOCKED"
            self.device_mode_state = status_kv.get("STATE", self.device_mode_state)
            self.ota_result = status_kv.get("FW_RESULT", self.ota_result)
            self.fw_version = status_kv.get("FW_VERSION", self.fw_version)
            self.fw_update_mode = self.device_mode_state == "FW_UPDATE_MODE"
            self._update_dev_status_label()
            return

        if line_upper.startswith("FW_STATUS:"):
            fw_kv = self._parse_pipe_kv(line)
            self.device_mode_state = fw_kv.get("STATE", self.device_mode_state)
            self.fw_version = fw_kv.get("FW", self.fw_version)
            self.ota_result = fw_kv.get("LAST", self.ota_result)
            self.fw_update_mode = fw_kv.get("FW_STATUS", "").upper() == "ON"
            self._update_dev_status_label()
            return

        if line_upper.startswith("CAL:"):
            self.cal_payload_cache = self._parse_cal_payload(line)
            return

        if low_line == "dev_ok":
            self.dev_authenticated = True
            self.device_mode_state = "DEV_MODE_UNLOCKED"
            self._update_dev_status_label()
            return

        if low_line in ("dev_fail", "dev_locked"):
            self.dev_authenticated = False
            self.fw_update_mode = False
            self.device_mode_state = "NORMAL_MODE"
            self._update_dev_status_label()
            return

        if low_line == "dev_required":
            self.dev_authenticated = False
            self.device_mode_state = "NORMAL_MODE"
            self._update_dev_status_label()
            return

        if low_line in ("fw_mode_ready", "fw_mode_off", "fw_aborted"):
            if low_line == "fw_mode_ready":
                self.fw_update_mode = True
                self.device_mode_state = "FW_UPDATE_MODE"
            else:
                self.fw_update_mode = False
                self.device_mode_state = "DEV_MODE_UNLOCKED" if self.dev_authenticated else "NORMAL_MODE"
            self._update_dev_status_label()
            return

        if line_upper.startswith("FW_MODE_READY|"):
            fw_kv = self._parse_pipe_kv(line)
            self.fw_update_mode = True
            self.device_mode_state = "FW_UPDATE_MODE"
            self.fw_version = fw_kv.get("FW", self.fw_version)
            self.ota_result = fw_kv.get("LAST", self.ota_result)
            self._update_dev_status_label()
            return

        if line_upper.startswith("FW_TRANSFER_DONE:"):
            self.fw_update_mode = False
            self.device_mode_state = "RETURN_TO_NORMAL"
            self.ota_result = "pending_reboot"
            self._update_dev_status_label()
            return

        if line_upper.startswith("FW_ERROR:") or low_line == "busy_fw_update":
            if low_line == "busy_fw_update":
                self._log("Device busy: firmware update mode has priority")
                if self.awaiting_update_result:
                    self.awaiting_update_result = False
                    self.smooth_update_until = 0.0
            elif line_upper.startswith("FW_ERROR:"):
                self._log(line.strip())
            self._update_dev_status_label()
            return

        if low_line in ("auth_success", "auth ok") or "auth success" in low_line:
            self._log("Authentication success")
            if self.awaiting_auth_for_update and self.pending_secured_update_cmd:
                secured_cmd = self.pending_secured_update_cmd
                verify_scope = self._infer_update_scope(secured_cmd)
                self.awaiting_auth_for_update = False
                self.pending_secured_update_cmd = ""
                self.pending_auth_cmd = ""
                self.auth_retry_left = 0
                self.pending_update_scope = verify_scope
                self.awaiting_update_result = True
                if self._is_uart_connected():
                    self.root.after(
                        UART_POST_AUTH_UPDATE_DELAY_MS,
                        lambda c=secured_cmd: self._send_update_command(c),
                    )
                else:
                    self._send_update_command(secured_cmd)
            return

        if low_line == "auth_failed" or "auth failed" in low_line:
            was_update_auth = self.awaiting_auth_for_update
            self.awaiting_auth_for_update = False
            self.pending_secured_update_cmd = ""
            self.pending_auth_cmd = ""
            self.auth_retry_left = 0
            self.awaiting_update_result = False
            self.update_reauth_retry_left = 0
            self.smooth_update_until = 0.0
            self.panel_state_var.set("CONNECTED" if self._is_connected() else "IDLE")
            self._log("Authentication failed")
            if was_update_auth:
                messagebox.showerror("Authentication Failed", "Wrong password. Update not allowed.")
            else:
                messagebox.showerror("Authentication Failed", "Wrong password.")
            return

        if low_line == "auth_required" or low_line.startswith("error: authentication required"):
            was_update_auth = self.awaiting_auth_for_update
            is_load_auth_case = (
                time.monotonic() <= self.pending_load_until
                and self.last_tx_cmd.strip().lower() in ("readcfg", "readadv")
            )
            if is_load_auth_case and not self.awaiting_auth_for_update:
                self.pending_load_until = 0.0
                self.pending_load_scope = "both"
                self.panel_state_var.set("CONNECTED" if self._is_connected() else "IDLE")
                self._log("Read from device blocked by auth policy; app will not ask password for reading.")
                messagebox.showwarning(
                    "Load Blocked",
                    "Device returned authentication required while loading config.\n"
                    "This app will not ask password for Read Device.\n"
                    "Try reconnecting, or use Serial mode for loading.",
                )
                return

            self.awaiting_auth_for_update = False
            self.pending_secured_update_cmd = ""
            self.pending_auth_cmd = ""
            self.auth_retry_left = 0
            self.awaiting_update_result = False
            self.update_reauth_retry_left = 0
            self.smooth_update_until = 0.0
            self.panel_state_var.set("CONNECTED" if self._is_connected() else "IDLE")
            self._log("Update blocked: authentication required")
            if was_update_auth or self.last_tx_cmd.strip().lower().startswith(("update:", "cfg:", "adv:")):
                messagebox.showwarning(
                    "Authentication Required",
                    "Enter correct password to apply Configuration/Advanced Parameters.",
                )
            return

        if low_line == "device updated":
            self._on_update_success_ack()
            return

        if (
            low_line == "ok"
            and self.awaiting_update_result
            and self.last_tx_cmd
            and self.last_tx_cmd.strip().lower().startswith(("update:", "cfg:", "adv:"))
        ):
            self._on_update_success_ack()
            return

        if low_line == "uart_blocked_ble_active":
            self._log("Serial blocked: BLE client is connected to device")
            messagebox.showwarning(
                "Serial Blocked",
                "Device is currently connected over BLE.\nDisconnect BLE first, then use Serial.",
            )
            if self._is_uart_connected():
                self.disconnect()
            return

        if low_line in ("uart_mode_on", "uart_mode_off"):
            self._log(f"Device Serial mode status: {low_line}")
            return

        if self.awaiting_auth_for_update and low_line.startswith("auth:"):
            self._log("Auth command echo received; waiting for auth response...")
            return

        if self.last_tx_cmd and line.strip().lower() == self.last_tx_cmd.strip().lower():
            self._log("Echo ignored")
            return

        # CFG/ADV payloads from readcfg/readadv or BLE characteristics.
        if line.startswith("CFG:"):
            cfg_json = line[4:].strip()
            try:
                self._load_cfg_json(cfg_json)
                self.last_cfgadv_rx_monotonic = time.monotonic()
                self._log("CFG loaded from device")
                self.pending_load_until = 0.0
                if self.pending_load_scope == "cfg":
                    self.pending_load_scope = "both"
            except Exception as exc:
                self._log(f"CFG parse error: {exc}")
            return

        if line.startswith("ADV:"):
            adv_json = line[4:].strip()
            try:
                self._load_adv_json(adv_json)
                self.last_cfgadv_rx_monotonic = time.monotonic()
                self._log("ADV loaded from device")
                self.pending_load_until = 0.0
                if self.pending_load_scope == "adv":
                    self.pending_load_scope = "both"
            except Exception as exc:
                self._log(f"ADV parse error: {exc}")
            return

        if line.upper().startswith("FW:"):
            fw_kv = self._parse_pipe_kv(line)
            self.fw_version = fw_kv.get("FW", line[3:].strip())
            self.ota_result = fw_kv.get("OTA_RESULT", self.ota_result)
            self.device_mode_state = fw_kv.get("STATE", self.device_mode_state)
            self.fw_update_mode = self.device_mode_state == "FW_UPDATE_MODE"
            self._update_dev_status_label()
            self._log(f"Firmware: {self.fw_version}")
            return

        # Device name responses (multiple firmware formats supported).
        line_upper = line.upper()
        if line_upper.startswith("DEVNAME:") or line_upper.startswith("DEVNAME="):
            sep = ":" if ":" in line else "="
            dev_name = line.split(sep, 1)[1].strip()
            if dev_name:
                self._set_device_name_display(dev_name)
                self.pending_devname_until = 0.0
                self._log(f"Device ID: {dev_name}")
            return

        if "BLE DEVICE NAME:" in line_upper:
            dev_name = line.split(":", 1)[1].strip() if ":" in line else ""
            if dev_name:
                self._set_device_name_display(dev_name)
                self.pending_devname_until = 0.0
                self._log(f"Device ID: {dev_name}")
            return

        if line.strip().upper().startswith("SERVO_"):
            self._set_device_name_display(line.strip())
            self.pending_devname_until = 0.0
            self._log(f"Device ID: {line.strip()}")
            return

        if line.strip().lower() == "unknown_command" and time.monotonic() <= self.pending_devname_until:
            self.pending_devname_until = 0.0
            self._log("Device name command not supported in this firmware")
            return

        if (
            line.strip().lower() == "unknown_command"
            and self.awaiting_update_result
            and self.last_update_cmd_sent
            and self.last_update_cmd_sent.lower().startswith("update:")
            and (time.monotonic() - self.last_update_sent_at) <= 2.0
            and self.last_tx_cmd
            and self.last_tx_cmd.strip().lower().startswith(("update:", "cfg:", "adv:"))
        ):
            if not self.update_legacy_retry_done:
                legacy_cmd = self._build_legacy_update_cmd(self.last_update_cmd_sent)
                self.update_legacy_retry_done = True
                if legacy_cmd:
                    self._log("Update command not recognized. Retrying with legacy CFG/ADV format...")
                    self._send_line(legacy_cmd, expect_response=False)
                    self.last_update_sent_at = time.monotonic()
                    self.awaiting_update_result = True
                    return
            if self.update_reauth_retry_left > 0 and self.last_auth_cmd_used and self.last_requested_update_cmd:
                self.update_reauth_retry_left -= 1
                self._log("Legacy retry failed. Re-authenticating and retrying update once...")
                self.awaiting_auth_for_update = True
                self.pending_secured_update_cmd = self.last_requested_update_cmd
                self.pending_auth_cmd = self.last_auth_cmd_used
                self.auth_retry_left = AUTH_RETRY_COUNT
                self.auth_request_id += 1
                self.awaiting_update_result = False
                self.update_legacy_retry_done = False
                self.panel_state_var.set("AUTH")
                self._send_line(self.pending_auth_cmd, expect_response=False)
                request_id = self.auth_request_id
                self.root.after(
                    AUTH_RESPONSE_TIMEOUT_MS,
                    lambda rid=request_id: self._handle_auth_timeout(rid),
                )
                return
            self._log("Update rejected by firmware (unknown_command).")
            self.awaiting_update_result = False
            self.smooth_update_until = 0.0
            messagebox.showwarning(
                "Update Rejected",
                "Firmware did not accept the update command.\nPlease flash the latest compatible firmware.",
            )
            return

        if line.strip().lower() == "unknown_command" and time.monotonic() <= self.pending_load_until:
            self.pending_load_until = 0.0
            scope_txt = self.pending_load_scope
            self.pending_load_scope = "both"
            if scope_txt == "cfg":
                message = "Device firmware does not support readcfg.\nUpload the latest compatible firmware."
                self._log("Load CFG failed: firmware missing readcfg support")
            elif scope_txt == "adv":
                message = "Device firmware does not support readadv.\nUpload the latest compatible firmware."
                self._log("Load ADV failed: firmware missing readadv support")
            else:
                message = "Device firmware does not support readcfg/readadv.\nUpload the latest compatible firmware."
                self._log("Read from device failed: firmware missing readcfg/readadv support")
                messagebox.showwarning("Firmware Command Missing", message)
                return

        # Telemetry format #1: "VOLT=...,CURR=...,SAT=..."
        upper_line = line.upper()
        if "VOLT=" in upper_line and "CURR=" in upper_line:
            kv = {}
            for part in line.split(","):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                kv[key.strip().upper()] = value.strip()

            volt_num = None
            curr_num = None
            if "VOLT" in kv:
                self.telemetry_vars["input"].set(self._format_live_input_value(kv["VOLT"]))
                volt_num = self._extract_number(kv["VOLT"])
            if "CURR" in kv:
                self.telemetry_vars["output"].set(self._format_live_output_value(kv["CURR"]))
                curr_num = self._extract_number(kv["CURR"])
            if "SAT" in kv:
                self.telemetry_vars["sat"].set(self._format_sat_value(kv["SAT"]))
            if "MODE" in kv and "command" in self.telemetry_vars:
                self.telemetry_vars["command"].set(kv["MODE"])

            if volt_num is not None or curr_num is not None:
                self._auto_read_waiting_rx = False
                self._push_graph_sample(input_seen=volt_num, output_seen=curr_num)
            return

        if "|" not in line or ":" not in line:
            return

        # Telemetry format #2: "input:...|output:...|sat:..."
        parsed = {}
        for part in line.split("|"):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            parsed[key.strip().lower()] = value.strip()

        for key in TELEMETRY_KEYS:
            if key in parsed:
                if key == "sat":
                    self.telemetry_vars[key].set(self._format_sat_value(parsed[key]))
                elif key == "input":
                    self.telemetry_vars[key].set(self._format_live_input_value(parsed[key]))
                elif key == "output":
                    self.telemetry_vars[key].set(self._format_live_output_value(parsed[key]))
                else:
                    self.telemetry_vars[key].set(parsed[key])

        input_num = self._extract_number(parsed.get("input"))
        output_num = self._extract_number(parsed.get("output"))
        command_num = self._extract_number(parsed.get("command"))
        if input_num is not None or output_num is not None or command_num is not None:
            self._auto_read_waiting_rx = False
            self._push_graph_sample(input_seen=input_num, output_seen=output_num, output_set=command_num)

    def _send_line(self, cmd: str, expect_response: bool = True) -> None:
        """Send command on active transport and optionally check for no-RX hint."""
        cmd = cmd.strip()
        if not cmd:
            return

        if not self._is_connected():
            messagebox.showwarning("Not Connected", "Establish Serial/BLE link first.")
            return

        try:
            if self._is_ble_connected():
                self._send_ble_line(cmd)
            else:
                self.ser.write((cmd + "\n").encode("utf-8"))
            self.last_tx_cmd = cmd
            self.panel_state_var.set("TX")
            if cmd.strip().upper().startswith("FW_CHUNK:"):
                pass
            else:
                self._log(f"TX: {cmd}")
            sent_at = time.monotonic()
            if expect_response:
                self.root.after(1500, lambda s=sent_at, c=cmd: self._check_no_response_hint(s, c))
        except Exception as exc:
            self._log(f"TX ERROR: {exc}")
            if not cmd.strip().upper().startswith("FW_CHUNK:"):
                messagebox.showerror("Send Error", str(exc))

    def _send_ble_line(self, cmd: str) -> None:
        """Send BLE line."""
        upper = cmd.strip().upper()

        if upper == "READ":
            try:
                live_data = self._run_ble_coro(self._ble_read_text_async(BLE_LIVE_CHAR_UUID), timeout=3.5)
            except Exception:
                return
            if live_data:
                self.rx_queue.put(live_data)
            return

        if upper == "READCFG":
            cfg_data = self._run_ble_coro(self._ble_read_text_async(BLE_CONFIG_CHAR_UUID), timeout=4.0)
            if cfg_data:
                self.rx_queue.put(cfg_data)
            return

        if upper == "READADV":
            adv_data = self._run_ble_coro(self._ble_read_text_async(BLE_ADV_CHAR_UUID), timeout=4.0)
            if adv_data:
                self.rx_queue.put(adv_data)
            return

        if upper.startswith("AUTH:"):
            payload = f"AUTH:{cmd.split(':', 1)[1]}"
            self._run_ble_coro(self._ble_write_text_async(BLE_CONFIG_CHAR_UUID, payload), timeout=5.0)
            return

        if (
            upper.startswith("DEV_LOGIN:")
            or upper in ("DEV_LOGOUT", "DEV_STATUS")
            or upper.startswith("CAL_")
            or upper.startswith("FW_")
            or upper == "FIRMWARE_UPDATE"
            or upper == "CALIBRATION"
        ):
            self._run_ble_coro(self._ble_write_text_async(BLE_CONFIG_CHAR_UUID, cmd), timeout=8.0)
            return

        if upper.startswith("UPDATE:CFG:"):
            payload = f"CFG:{cmd.split(':', 2)[2]}"
            self._run_ble_coro(self._ble_write_text_async(BLE_CONFIG_CHAR_UUID, payload), timeout=5.0)
            return

        if upper.startswith("UPDATE:ADV:"):
            payload = f"ADV:{cmd.split(':', 2)[2]}"
            self._run_ble_coro(self._ble_write_text_async(BLE_ADV_CHAR_UUID, payload), timeout=5.0)
            return

        if upper == "DEVNAME":
            if self.device_name_var.get().strip():
                self.rx_queue.put(f"DEVNAME:{self.device_name_var.get().strip()}")
            return

        if upper == "VERSION":
            self._log("Firmware version query over BLE command path is not available in this GUI.")
            return

        self._run_ble_coro(self._ble_write_text_async(BLE_CONFIG_CHAR_UUID, cmd), timeout=5.0)

    def _check_no_response_hint(self, sent_at: float, sent_cmd: str = "") -> None:
        """Handle check no response hint."""
        if self.last_rx_monotonic > sent_at:
            return
        cmd_low = sent_cmd.strip().lower()
        now = time.monotonic()
        if self.awaiting_auth_for_update:
            return
        if now <= self.pending_load_until:
            return
        if (
            cmd_low.startswith("auth:")
            or cmd_low.startswith("update:")
            or cmd_low.startswith("dev_login:")
            or cmd_low.startswith("dev_")
            or cmd_low.startswith("cal_")
            or cmd_low.startswith("fw_")
            or cmd_low in ("readcfg", "readadv", "uart_connected", "uart_disconnected")
        ):
            return
        if self._is_ble_connected():
            self._log("No RX after BLE command. Check BLE range, pairing permission, and notifications.")
        else:
            self._log("No RX after command. Check COM/baud and wiring. USB Serial=115200, Cable Serial=9600.")

    def load_config_from_device(self, scope: str = "both") -> None:
        """Request CFG/ADV snapshots from device to sync GUI forms."""
        if not self._is_connected():
            messagebox.showwarning("Not Connected", "Establish Serial/BLE link first.")
            return
        if self.auto_read_var.get():
            messagebox.showwarning("Stop Acquisition First", "Stop acquisition, then click Read Device.")
            return

        scope_txt = (scope or "both").strip().lower()
        if scope_txt not in ("cfg", "adv", "both"):
            scope_txt = "both"

        self.pending_load_scope = scope_txt
        self.pending_load_until = time.monotonic() + 2.0
        if scope_txt == "cfg":
            self.panel_state_var.set("LOADING CFG")
            self._log("Requesting saved CFG from device...")
            self._send_line("readcfg", expect_response=False)
            return

        if scope_txt == "adv":
            self.panel_state_var.set("LOADING ADV")
            self._log("Requesting saved ADV from device...")
            self._send_line("readadv", expect_response=False)
            return

        self.panel_state_var.set("LOADING CFG+ADV")
        self._log("Requesting saved configuration/advanced parameters from device...")
        self._send_line("readcfg", expect_response=False)
        self.root.after(180, lambda: self._send_line("readadv", expect_response=False))

    def _update_read_toggle_button(self) -> None:
        """Update read toggle button."""
        if self.read_toggle_btn is None:
            return
        if self.auto_read_var.get():
            self.read_toggle_btn.configure(text="Stop Monitoring", style="Warn.TButton")
        else:
            self.read_toggle_btn.configure(text="Start Monitoring", style="Accent.TButton")

    def toggle_reading(self) -> None:
        """Toggle reading."""
        if self.auto_read_var.get():
            self.stop_reading()
        else:
            self.start_reading()

    def start_reading(self) -> None:
        """Start reading."""
        if not self._is_connected():
            messagebox.showwarning("Not Connected", "Establish Serial/BLE link first.")
            return
        if self.auto_read_var.get():
            return
        self.auto_read_var.set(True)
        self._update_read_toggle_button()
        self._set_update_buttons_enabled(False)
        self.panel_state_var.set("READING")
        self._log("Acquisition started")
        self._auto_read_waiting_rx = False
        self._last_auto_read_sent_at = 0.0
        self._send_line("read", expect_response=False)

    def stop_reading(self) -> None:
        """Stop reading."""
        if not self.auto_read_var.get():
            return
        self.auto_read_var.set(False)
        self._auto_read_waiting_rx = False
        self._last_auto_read_sent_at = 0.0
        self._update_read_toggle_button()
        self._set_update_buttons_enabled(True)
        if self._is_connected():
            self.panel_state_var.set("CONNECTED")
        else:
            self.panel_state_var.set("IDLE")
        self._log("Acquisition stopped")

    def _can_update_now(self) -> bool:
        """Return whether update now."""
        if self.auto_read_var.get():
            messagebox.showwarning(
                "Stop Acquisition First",
                "Please stop acquisition before updating parameters.\n\nFlow: Stop Acquisition -> Update -> Start Acquisition.",
            )
            self._log("Update blocked: stop reading first")
            return False
        return True

    def _request_update_password(self, prompt_text: str = "Enter device password to apply settings:") -> str:
        """Request update password."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Device Password")
        dialog.configure(bg="#0f1419")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        result = {"password": ""}

        wrapper = ttk.Frame(dialog, style="Panel.TFrame", padding=(18, 16, 18, 14))
        wrapper.pack(fill="both", expand=True)
        wrapper.grid_columnconfigure(0, weight=1)

        ttk.Label(wrapper, text="Device Password", style="SubTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(wrapper, text=prompt_text, style="Field.TLabel").grid(
            row=1, column=0, sticky="w", pady=(12, 6)
        )

        password_var = tk.StringVar()
        entry = ttk.Entry(wrapper, textvariable=password_var, show="*", width=34)
        entry.grid(row=2, column=0, sticky="ew")

        buttons = ttk.Frame(wrapper, style="Panel.TFrame")
        buttons.grid(row=3, column=0, sticky="e", pady=(16, 0))

        def submit() -> None:
            """Handle dialog submit action."""
            result["password"] = password_var.get().strip()
            dialog.destroy()

        def cancel() -> None:
            """Handle dialog cancel action."""
            result["password"] = ""
            dialog.destroy()

        ttk.Button(buttons, text="OK", style="Primary.TButton", width=10, command=submit).pack(side="left")
        ttk.Button(buttons, text="Cancel", style="Neutral.TButton", width=10, command=cancel).pack(
            side="left", padx=(8, 0)
        )

        dialog.bind("<Return>", lambda _event: submit())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

        dialog.update_idletasks()
        width = 420
        height = 182
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        entry.focus_set()
        self.root.wait_window(dialog)

        return result["password"]

    def _send_secured_update(self, update_cmd: str, state_text: str) -> None:
        """Perform password-authenticated update flow with retry metadata."""
        password = self._request_update_password()
        if not password:
            self._log("Update cancelled: password not entered")
            return

        self.awaiting_auth_for_update = True
        self.pending_secured_update_cmd = update_cmd
        self.pending_auth_cmd = f"auth:{password}"
        self.last_auth_cmd_used = self.pending_auth_cmd
        self.last_requested_update_cmd = update_cmd
        self.pending_update_scope = self._infer_update_scope(update_cmd)
        self.awaiting_update_result = False
        self.update_reauth_retry_left = 1
        self.auth_retry_left = AUTH_RETRY_COUNT
        self.auth_request_id += 1
        self.last_update_cmd_sent = update_cmd
        self.last_update_sent_at = 0.0
        self.update_legacy_retry_done = False
        self.panel_state_var.set("AUTH")
        self._send_line(self.pending_auth_cmd, expect_response=False)
        request_id = self.auth_request_id
        self.root.after(
            AUTH_RESPONSE_TIMEOUT_MS,
            lambda rid=request_id: self._handle_auth_timeout(rid),
        )

        # Keep user intent visible while auth is in progress.
        self._log(f"{state_text}: awaiting password verification")

    def _handle_auth_timeout(self, request_id: int) -> None:
        """Handle auth timeout."""
        if request_id != self.auth_request_id:
            return
        if not self.awaiting_auth_for_update:
            return

        if self.auth_retry_left > 0 and self.pending_auth_cmd:
            self.auth_retry_left -= 1
            self._log("No auth response yet. Retrying authentication...")
            if self._is_uart_connected():
                # Re-assert Serial session before retry, so auth is not missed on marginal links.
                self._send_line("uart_connected", expect_response=False)
                self.root.after(120, lambda: self._send_line(self.pending_auth_cmd, expect_response=False))
            else:
                self._send_line(self.pending_auth_cmd, expect_response=False)
            self.root.after(
                AUTH_RESPONSE_TIMEOUT_MS,
                lambda rid=request_id: self._handle_auth_timeout(rid),
            )
            return

        self.awaiting_auth_for_update = False
        self.pending_secured_update_cmd = ""
        self.pending_auth_cmd = ""
        self.auth_retry_left = 0
        self.awaiting_update_result = False
        self.update_reauth_retry_left = 0
        self.smooth_update_until = 0.0
        self.panel_state_var.set("CONNECTED" if self._is_connected() else "IDLE")
        self._log("Authentication timeout: no auth response from device")
        messagebox.showwarning(
            "Auth Timeout",
            "No auth response from device after retries.\n"
            "Check Serial cable stability/baud and try again.",
        )

    def send_cfg(self) -> None:
        """Send configuration."""
        if not self._can_update_now():
            return
        try:
            cfg_json = self._build_cfg_json()
        except Exception as exc:
            messagebox.showerror("Invalid CFG Input", str(exc))
            return
        self._send_secured_update(f"update:CFG:{cfg_json}", "UPDATING CFG")

    def send_adv(self) -> None:
        """Send advanced."""
        if not self._can_update_now():
            return
        try:
            adv_json = self._build_adv_json()
        except Exception as exc:
            messagebox.showerror("Invalid ADV Input", str(exc))
            return
        self._send_secured_update(f"update:ADV:{adv_json}", "UPDATING ADV")

    def _auto_read_tick(self) -> None:
        """Run automatic read tick."""
        if self.auto_read_var.get() and self._is_connected():
            now = time.monotonic()
            interval_s = AUTO_READ_INTERVAL_MS / 1000.0
            guard_s = AUTO_READ_RESPONSE_GUARD_MS / 1000.0

            if self._auto_read_waiting_rx:
                if (now - self._last_auto_read_sent_at) < guard_s:
                    self.root.after(AUTO_READ_INTERVAL_MS, self._auto_read_tick)
                    return
                self._auto_read_waiting_rx = False

            if (now - self._last_auto_read_sent_at) >= interval_s:
                self._auto_read_waiting_rx = True
                self._last_auto_read_sent_at = now
                self._send_line("read", expect_response=False)
        else:
            self._auto_read_waiting_rx = False
        self.root.after(AUTO_READ_INTERVAL_MS, self._auto_read_tick)

    def _log(self, msg: str) -> None:
        """Handle log."""
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")

    def on_close(self) -> None:
        """Handle close."""
        self._is_closing = True
        self.disconnect()
        self.root.destroy()


def main() -> None:
    """Run the desktop application entry point."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except Exception:
            pass
    root = tk.Tk()
    app = DeviceTestGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
