import asyncio
import json
import math
import re
import threading
import time
import hashlib
import sys
import os
import traceback
from collections import deque
from pathlib import Path

import webview

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

UART_BAUD_CANDIDATES = (9600, 115200)
AUTH_RESPONSE_TIMEOUT_MS = 4500
DEV_AUTH_RESPONSE_TIMEOUT_MS = 5500
FW_CMD_RESPONSE_TIMEOUT_MS = 12000
UART_POST_AUTH_UPDATE_DELAY_MS = 140
FW_CHUNK_ACK_TIMEOUT_MS = 12000
FW_CHUNK_BYTES = 220
FW_CHUNK_MIN_BYTES = 100
FW_CHUNK_RETRY_COUNT = 2

FW_ALLOWED_VARIANT = "SERVO_EVDR"
FW_DEVICE_NAME_PREFIX = f"{FW_ALLOWED_VARIANT}_"
FW_REQUIRED_MODEL_MARKER = f"FW_MODEL={FW_ALLOWED_VARIANT}"
FW_REQUIRED_PROJECT_MARKER = "FW_PROJECT=FINAL_BLE_UART_CODE"
FW_HASH_PREFIX_LEN = 16

BLE_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
BLE_LIVE_CHAR_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"
BLE_CONFIG_CHAR_UUID = "0000ffe5-0000-1000-8000-00805f9b34fb"
BLE_ADV_CHAR_UUID = "0000ffe6-0000-1000-8000-00805f9b34fb"
APP_DIR_NAME = "SERVO_EVDR_APPLICATION"


def _app_user_root_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / APP_DIR_NAME
    return Path.home() / ".servo_evdr_application"


def _startup_log_dir() -> Path:
    return _app_user_root_dir() / "LOGS"


def _default_dialog_dir() -> Path:
    """Return a stable user-writable folder, especially for packaged EXE runs."""
    if not getattr(sys, "frozen", False):
        try:
            return Path.cwd()
        except Exception:
            pass
    preferred = _app_user_root_dir() / "FILES"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if preferred.exists():
        return preferred
    docs = Path.home() / "Documents"
    if docs.exists():
        return docs
    return Path.home()


def _write_startup_error_log(error_text: str) -> Path | None:
    try:
        log_dir = _startup_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "STARTUP_ERROR.log"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8", errors="ignore") as fp:
            fp.write(f"[{stamp}]\n{error_text}\n\n")
        return log_path
    except Exception:
        return None


def _show_startup_error_dialog(message: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        MB_OK = 0x00000000
        MB_ICONERROR = 0x00000010
        ctypes.windll.user32.MessageBoxW(0, str(message), "SERVO EVDR APPLICATION - STARTUP ERROR", MB_OK | MB_ICONERROR)
    except Exception:
        pass


def _hide_windows_console_when_frozen() -> None:
    """Hide console window for packaged EXE while keeping dev-console behavior."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            SW_HIDE = 0
            user32.ShowWindow(hwnd, SW_HIDE)
            # Keep console attached to avoid invalid stdio handles in some environments.
    except Exception:
        # If hiding fails, keep running normally.
        pass


def _resource_base_dirs() -> list[Path]:
    """Candidate folders for bundled/static assets in source and frozen modes."""
    raw_dirs = []
    if getattr(sys, "frozen", False):
        meipass_dir = getattr(sys, "_MEIPASS", "")
        if meipass_dir:
            try:
                raw_dirs.append(Path(str(meipass_dir)))
            except Exception:
                pass
        try:
            raw_dirs.append(Path(sys.executable).resolve().parent)
        except Exception:
            pass
    raw_dirs.append(Path(__file__).resolve().parent)

    deduped = []
    seen = set()
    for folder in raw_dirs:
        try:
            resolved = folder.resolve()
        except Exception:
            resolved = folder
        key = str(resolved).lower() if sys.platform == "win32" else str(resolved)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(resolved)
    return deduped


def _resolve_resource_path(*relative_parts: str) -> Path | None:
    rel_path = Path(*relative_parts)
    for base_dir in _resource_base_dirs():
        candidate = (base_dir / rel_path).resolve(strict=False)
        if candidate.exists():
            return candidate
    return None


def _resolve_ui_resource_path(*relative_parts: str) -> Path | None:
    # In dev/source mode, prefer UI_FILES so python run picks latest editable UI.
    # In frozen EXE mode, prefer WEBUI because PyInstaller packs UI_FILES -> WEBUI.
    ordered_ui_dirs = ("WEBUI", "UI_FILES") if getattr(sys, "frozen", False) else ("UI_FILES", "WEBUI")

    for ui_dir in ordered_ui_dirs:
        hit = _resolve_resource_path(ui_dir, *relative_parts)
        if hit is not None:
            return hit

    # Also support sibling layout: GUI_FILES/PYTHON_FILES + GUI_FILES/UI_FILES.
    rel_path = Path(*relative_parts)
    for base_dir in _resource_base_dirs():
        for ui_dir in ordered_ui_dirs:
            candidate = (base_dir.parent / ui_dir / rel_path).resolve(strict=False)
            if candidate.exists():
                return candidate

    return _resolve_resource_path(*relative_parts)


def _app_base_dir() -> Path:
    """Return app base folder for source and frozen executable modes."""
    for base_dir in _resource_base_dirs():
        if (
            (base_dir / "WEBUI" / "INDEX.HTML").exists()
            or (base_dir / "UI_FILES" / "INDEX.HTML").exists()
            or (base_dir.parent / "WEBUI" / "INDEX.HTML").exists()
            or (base_dir.parent / "UI_FILES" / "INDEX.HTML").exists()
            or (base_dir / "INDEX.HTML").exists()
        ):
            return base_dir
    roots = _resource_base_dirs()
    return roots[0] if roots else Path(__file__).resolve().parent


def _apply_windows_window_icon(window_title: str, icon_path: Path, timeout_s: float = 6.0) -> None:
    """Set window/taskbar icon at runtime for script-launched Windows apps."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except Exception:
        return

    try:
        icon_file = Path(icon_path).resolve()
        if not icon_file.exists():
            return

        # Use a stable app id so taskbar grouping/icon resolves to this app.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SERVOCONTROLS.SERVO_EVDR.APP")
        except Exception:
            pass

        user32 = ctypes.windll.user32
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040

        hicon = user32.LoadImageW(
            None,
            str(icon_file),
            IMAGE_ICON,
            0,
            0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )
        if not hicon:
            return

        deadline = time.time() + max(0.5, float(timeout_s))
        hwnd = 0
        while time.time() < deadline and not hwnd:
            hwnd = user32.FindWindowW(None, str(window_title))
            if not hwnd:
                time.sleep(0.08)
        if not hwnd:
            # Fallback: app window is typically foreground right after start.
            hwnd = user32.GetForegroundWindow()
        if hwnd:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
    except Exception:
        # Keep launch robust even if icon assignment fails in some environments.
        pass


class SerialRuntime:
    """UART/BLE runtime shared with the web UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ser = None
        self._reader_thread = None
        self._stop_event = threading.Event()
        self._events = deque(maxlen=2000)
        self._next_event_id = 1
        self._last_event_id = 0
        self._disconnecting = False
        self._ble_client = None
        self._ble_connected = False
        self._ble_loop = None
        self._ble_loop_thread = None

        self.connected = False
        self.transport = "UART"
        self.port = ""
        self.baud = 9600
        self.last_error = ""
        self.last_rx_ts = 0.0
        self.last_tx_cmd = ""
        self.device_name = "-"
        self.fw_version = ""
        self.device_mode_state = "NORMAL_MODE"
        self.dev_authenticated = False
        self.telemetry = {
            "input": "-",
            "output": "-",
            "sat": "-",
            "updated_at": 0.0,
        }
        self.firmware_upload = {
            "active": False,
            "state": "idle",
            "sent_bytes": 0,
            "total_bytes": 0,
            "percent": 0.0,
        }

    def _set_fw_upload_progress(
        self,
        *,
        state=None,
        active=None,
        sent_bytes=None,
        total_bytes=None,
    ) -> None:
        with self._state_lock:
            info = self.firmware_upload

            if state is not None:
                info["state"] = str(state)
            if active is not None:
                info["active"] = bool(active)

            if total_bytes is not None:
                try:
                    next_total = max(0, int(total_bytes))
                except Exception:
                    next_total = 0
                info["total_bytes"] = next_total

            if sent_bytes is not None:
                try:
                    next_sent = max(0, int(sent_bytes))
                except Exception:
                    next_sent = 0
                info["sent_bytes"] = next_sent

            total = int(info.get("total_bytes") or 0)
            sent = int(info.get("sent_bytes") or 0)
            if total > 0 and sent > total:
                sent = total
                info["sent_bytes"] = sent

            if total <= 0:
                percent = 100.0 if sent > 0 else 0.0
            else:
                percent = (float(sent) * 100.0) / float(total)
            percent = max(0.0, min(100.0, percent))
            info["percent"] = round(percent, 2)

    def _push_event(self, direction: str, text: str) -> None:
        with self._state_lock:
            event = {
                "id": self._next_event_id,
                "ts": time.strftime("%H:%M:%S"),
                "direction": direction,
                "text": str(text or ""),
            }
            self._next_event_id += 1
            self._last_event_id = event["id"]
            self._events.append(event)

    def list_serial_ports(self) -> list[str]:
        if list_ports is None:
            return []
        return [p.device for p in list_ports.comports()]

    @staticmethod
    def _is_servo_device_name(name_text: str) -> bool:
        txt = str(name_text or "").strip().upper()
        if not txt:
            return False
        return ("SERVO_EVDR" in txt) or ("SERVO" in txt and "EVDR" in txt)

    @staticmethod
    def _extract_devname_from_line(line_text: str) -> str:
        txt = str(line_text or "").strip()
        if not txt:
            return ""
        low = txt.lower()
        if low.startswith("devname:"):
            return txt.split(":", 1)[1].strip()
        if low.startswith("devname="):
            return txt.split("=", 1)[1].strip()
        if txt.upper().startswith("SERVO_"):
            return txt
        return ""

    def _auto_find_servo_serial_port(self, preferred_baud: int = 9600) -> dict:
        if serial is None:
            return {"ok": False, "error": "pyserial not available"}
        if list_ports is None:
            return {"ok": False, "error": "Serial port scanner is not available"}

        infos = [p for p in list_ports.comports() if str(getattr(p, "device", "") or "").strip()]
        if not infos:
            return {"ok": False, "error": "No serial ports detected"}

        def _rank_port(info) -> tuple[int, str]:
            blob = " ".join(
                [
                    str(getattr(info, "device", "") or ""),
                    str(getattr(info, "description", "") or ""),
                    str(getattr(info, "manufacturer", "") or ""),
                    str(getattr(info, "hwid", "") or ""),
                ]
            ).upper()
            if self._is_servo_device_name(blob):
                return (0, str(getattr(info, "device", "") or ""))
            if any(key in blob for key in ("CP210", "CH340", "USB SERIAL", "UART", "SERIAL")):
                return (1, str(getattr(info, "device", "") or ""))
            return (2, str(getattr(info, "device", "") or ""))

        ordered_infos = sorted(infos, key=_rank_port)
        baud_candidates = []
        for val in (preferred_baud, *UART_BAUD_CANDIDATES):
            try:
                b = int(val)
            except Exception:
                continue
            if b > 0 and b not in baud_candidates:
                baud_candidates.append(b)
        if not baud_candidates:
            baud_candidates = [9600]

        blocked_detected = False
        for info in ordered_infos:
            port_name = str(getattr(info, "device", "") or "").strip()
            if not port_name:
                continue
            for baud_try in baud_candidates:
                ser_obj = None
                try:
                    ser_obj = serial.Serial(
                        port=port_name,
                        baudrate=baud_try,
                        timeout=0.2,
                        write_timeout=0.5,
                    )
                    ser_obj.reset_input_buffer()
                    ser_obj.reset_output_buffer()
                    probe = self._uart_preconnect_check(ser_obj, push_events=False)
                except Exception:
                    probe = {"status": "no_response", "is_servo": False, "device_name": ""}
                finally:
                    try:
                        if ser_obj is not None and ser_obj.is_open:
                            ser_obj.close()
                    except Exception:
                        pass

                status = str(probe.get("status", "no_response"))
                if status == "blocked":
                    blocked_detected = True
                    continue
                if status == "ready" and probe.get("is_servo"):
                    return {
                        "ok": True,
                        "port": port_name,
                        "baud": int(baud_try),
                        "device_name": str(probe.get("device_name", "")).strip(),
                    }

        if blocked_detected:
            return {
                "ok": False,
                "error": "SERVO EVDR is currently connected over BLE. Disconnect BLE first, then connect Serial.",
            }
        return {
            "ok": False,
            "error": "SERVO EVDR device not found on serial ports. Check cable/power and try again.",
        }

    def _is_target_ble_device(self, dev) -> bool:
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

    def list_ble_devices(self) -> dict:
        if BleakScanner is None:
            return {"ok": False, "devices": [], "error": "bleak is not installed"}

        try:
            discovered = asyncio.run(BleakScanner.discover(timeout=4.0))
        except Exception as exc:
            return {"ok": False, "devices": [], "error": str(exc)}

        devices = []
        seen_addresses = set()
        for dev in discovered:
            if not self._is_target_ble_device(dev):
                continue
            address = (getattr(dev, "address", "") or "").strip()
            if not address or address in seen_addresses:
                continue
            seen_addresses.add(address)
            name = (getattr(dev, "name", "") or "Unknown").strip() or "Unknown"
            devices.append({"name": name, "address": address})

        devices.sort(key=lambda item: (item.get("name", "").lower(), item.get("address", "")))
        for idx, dev in enumerate(devices, start=1):
            dev["display"] = f"{dev['name']} ({dev['address']})"
            dev["index"] = idx
        return {"ok": True, "devices": devices}

    def _push_rx_line(self, line: str) -> None:
        txt = str(line or "").strip()
        if not txt:
            return
        with self._state_lock:
            self.last_rx_ts = time.time()
        self._push_event("RX", txt)
        self._update_telemetry_from_line(txt)
        self._update_runtime_markers_from_line(txt)

    def _ensure_ble_loop(self) -> None:
        if self._ble_loop is not None and self._ble_loop.is_running():
            return
        self._ble_loop = asyncio.new_event_loop()
        self._ble_loop_thread = threading.Thread(target=self._ble_loop_main, daemon=True)
        self._ble_loop_thread.start()

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self._ble_loop is not None and self._ble_loop.is_running():
                return
            time.sleep(0.02)
        raise RuntimeError("Failed to start BLE runtime loop")

    def _ble_loop_main(self) -> None:
        loop = self._ble_loop
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
        loop = self._ble_loop
        thread = self._ble_loop_thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._ble_loop = None
        self._ble_loop_thread = None

    def _run_ble_coro(self, coro, timeout: float = 8.0):
        if self._ble_loop is None or not self._ble_loop.is_running():
            raise RuntimeError("BLE runtime is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._ble_loop)
        return future.result(timeout=timeout)

    def _queue_ble_payload(self, payload) -> None:
        try:
            if isinstance(payload, (bytes, bytearray)):
                text = bytes(payload).decode("utf-8", errors="replace")
            else:
                text = str(payload)
        except Exception:
            return
        for raw_line in text.replace("\x00", "").splitlines():
            line = raw_line.strip()
            if line:
                self._push_rx_line(line)

    def _on_ble_live_notify(self, _sender: int, data: bytearray) -> None:
        self._queue_ble_payload(data)

    def _on_ble_config_notify(self, _sender: int, data: bytearray) -> None:
        self._queue_ble_payload(data)

    def _on_ble_adv_notify(self, _sender: int, data: bytearray) -> None:
        self._queue_ble_payload(data)

    def _on_ble_disconnected(self, _client) -> None:
        with self._state_lock:
            was_connected = self.connected
            self.connected = False
            self._ble_connected = False
            self.port = ""
            self.dev_authenticated = False
        if was_connected and not self._disconnecting:
            self._push_event("SYS", "BLE disconnected")

    async def _ble_connect_async(self, address: str) -> None:
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

        self._ble_client = client
        self._ble_connected = True

    async def _ble_disconnect_async(self) -> None:
        client = self._ble_client
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
            self._ble_connected = False
            self._ble_client = None

    async def _ble_read_text_async(self, char_uuid: str) -> str:
        client = self._ble_client
        if client is None or not client.is_connected:
            raise RuntimeError("BLE device is not connected.")
        data = await client.read_gatt_char(char_uuid)
        return bytes(data).decode("utf-8", errors="replace").replace("\x00", "").strip()

    async def _ble_write_text_async(self, char_uuid: str, text: str) -> None:
        client = self._ble_client
        if client is None or not client.is_connected:
            raise RuntimeError("BLE device is not connected.")
        await client.write_gatt_char(char_uuid, text.encode("utf-8"), response=True)

    def _uart_preconnect_check(self, ser_obj, push_events: bool = True) -> dict:
        """Probe serial link and return status plus optional device identity."""
        probe_cmds = ("uart_connected", "version", "devname", "read")
        saw_any_rx = False
        saw_ready_hint = False
        detected_devname = ""
        detected_servo = False
        for probe_cmd in probe_cmds:
            try:
                ser_obj.write((probe_cmd + "\n").encode("utf-8"))
            except Exception:
                break
            wait_until = time.monotonic() + 0.65
            while time.monotonic() < wait_until:
                try:
                    raw = ser_obj.readline()
                except Exception:
                    break
                if not raw:
                    time.sleep(0.03)
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                saw_any_rx = True
                if push_events:
                    self._push_rx_line(line)

                low = line.lower()
                if low == "uart_blocked_ble_active":
                    return {"status": "blocked", "is_servo": False, "device_name": detected_devname}

                devname = self._extract_devname_from_line(line)
                if devname:
                    detected_devname = devname
                    detected_servo = self._is_servo_device_name(devname)
                    if detected_servo:
                        return {"status": "ready", "is_servo": True, "device_name": detected_devname}

                if low in ("uart_mode_on", "uart_mode_off"):
                    saw_ready_hint = True
                if (
                    line.startswith("FW:")
                    or line.startswith("CFG:")
                    or line.startswith("ADV:")
                    or low.startswith("devname:")
                    or low.startswith("devname=")
                    or ("input:" in low and "|output:" in low)
                    or low in ("unknown_command", "auth_required", "auth_success", "auth_failed", "ok")
                ):
                    saw_ready_hint = True
            time.sleep(0.05)

        status = "ready" if (saw_any_rx or saw_ready_hint) else "no_response"
        return {"status": status, "is_servo": detected_servo, "device_name": detected_devname}

    def connect(self, port: str, baud: int = 9600) -> dict:
        if serial is None:
            return {"ok": False, "error": "pyserial not available"}
        port_txt = str(port or "").strip()
        auto_port_info = None
        if not port_txt:
            auto_pick = self._auto_find_servo_serial_port(preferred_baud=baud)
            if not auto_pick.get("ok"):
                return {"ok": False, "error": auto_pick.get("error", "SERVO EVDR auto-detect failed")}
            port_txt = str(auto_pick.get("port", "")).strip()
            auto_port_info = auto_pick
            try:
                baud = int(auto_pick.get("baud", baud))
            except Exception:
                pass
        available_ports = self.list_serial_ports()
        if available_ports and port_txt not in available_ports:
            return {"ok": False, "error": f"{port_txt} is not available now. Select a valid serial port."}

        with self._lock:
            self.disconnect_locked()
            warnings = []
            if auto_port_info:
                auto_name = str(auto_port_info.get("device_name", "")).strip()
                if auto_name:
                    warnings.append(f"Auto-selected SERVO EVDR port: {port_txt} ({auto_name})")
                else:
                    warnings.append(f"Auto-selected SERVO EVDR port: {port_txt}")
            selected_baud = None
            candidates = []
            for val in (baud, *UART_BAUD_CANDIDATES):
                try:
                    baud_i = int(val)
                except Exception:
                    continue
                if baud_i > 0 and baud_i not in candidates:
                    candidates.append(baud_i)
            if not candidates:
                candidates = [9600]

            for idx, baud_try in enumerate(candidates):
                try:
                    self._ser = serial.Serial(
                        port=port_txt,
                        baudrate=baud_try,
                        timeout=0.2,
                        write_timeout=0.5,
                    )
                    self._ser.reset_input_buffer()
                    self._ser.reset_output_buffer()
                except Exception:
                    self._ser = None
                    continue

                probe = self._uart_preconnect_check(self._ser)
                probe_status = str(probe.get("status", "no_response"))
                if probe_status == "blocked":
                    try:
                        if self._ser and self._ser.is_open:
                            self._ser.close()
                    except Exception:
                        pass
                    self._ser = None
                    with self._state_lock:
                        self.connected = False
                        self.transport = "UART"
                        self.last_error = "uart_blocked_ble_active"
                    return {
                        "ok": False,
                        "error": "Device is currently connected over BLE. Disconnect BLE first, then connect Serial.",
                    }
                if probe_status == "ready":
                    detected_name = str(probe.get("device_name", "")).strip()
                    if detected_name:
                        with self._state_lock:
                            self.device_name = detected_name
                    selected_baud = baud_try
                    break

                try:
                    if self._ser and self._ser.is_open:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
                if idx < len(candidates) - 1:
                    warnings.append(f"No immediate response @ {baud_try}. Trying {candidates[idx + 1]}...")

            if self._ser is None or selected_baud is None:
                fallback_baud = candidates[0]
                try:
                    self._ser = serial.Serial(
                        port=port_txt,
                        baudrate=fallback_baud,
                        timeout=0.2,
                        write_timeout=0.5,
                    )
                    self._ser.reset_input_buffer()
                    self._ser.reset_output_buffer()
                    selected_baud = fallback_baud
                    warnings.append(
                        "No immediate serial response during baud probe. Connected anyway; if data stays empty, verify wiring/session."
                    )
                except Exception as exc:
                    self._ser = None
                    with self._state_lock:
                        self.connected = False
                        self.last_error = str(exc)
                    self._push_event("ERR", f"Connect failed: {exc}")
                    return {"ok": False, "error": str(exc)}

            with self._state_lock:
                self.transport = "UART"
                self.port = port_txt
                self.baud = int(selected_baud)
                self.connected = True
                self.last_error = ""
            self._stop_event.clear()
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()
            self._push_event("SYS", f"Connected: {self.port} @ {self.baud}")
            if warnings:
                self._push_event("SYS", "; ".join(warnings))
                return {
                    "ok": True,
                    "warning": " ".join(warnings),
                    "baud": int(selected_baud),
                    "port": port_txt,
                    "auto_selected": bool(auto_port_info),
                }
            return {"ok": True, "baud": int(selected_baud), "port": port_txt, "auto_selected": bool(auto_port_info)}

    def connect_ble(self, address: str, display_name: str = "") -> dict:
        if BleakClient is None:
            return {"ok": False, "error": "bleak is not installed"}

        address_txt = str(address or "").strip()
        if not address_txt:
            return {"ok": False, "error": "Select a BLE device"}

        with self._lock:
            self.disconnect_locked()
            try:
                self._ensure_ble_loop()
                self._run_ble_coro(self._ble_connect_async(address_txt), timeout=12.0)
            except Exception as exc:
                self._ble_connected = False
                self._ble_client = None
                self._stop_ble_loop()
                with self._state_lock:
                    self.connected = False
                    self.last_error = str(exc)
                self._push_event("ERR", f"BLE connect failed: {exc}")
                return {"ok": False, "error": str(exc)}

            name_txt = str(display_name or "").strip()
            with self._state_lock:
                self.connected = True
                self.transport = "BLE"
                self.port = address_txt
                self.baud = 0
                self.last_error = ""
                self.device_name = name_txt or address_txt
            self._push_event("SYS", f"Connected BLE: {address_txt}")
            return {"ok": True}

    def disconnect(self) -> dict:
        with self._lock:
            self.disconnect_locked()
        return {"ok": True}

    def disconnect_locked(self) -> None:
        self._disconnecting = True
        self._stop_event.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=0.8)
        self._reader_thread = None

        if self._ser is not None:
            try:
                if self._ser.is_open:
                    self._ser.close()
            except Exception:
                pass
        self._ser = None

        if self._ble_client is not None:
            try:
                self._run_ble_coro(self._ble_disconnect_async(), timeout=5.0)
            except Exception:
                pass
        self._ble_connected = False
        self._ble_client = None
        self._stop_ble_loop()

        if self.connected:
            self._push_event("SYS", "Disconnected")
        with self._state_lock:
            self.connected = False
            self.transport = "UART"
            self.port = ""
            self.baud = 9600
            self.dev_authenticated = False
            if self.device_mode_state == "FW_UPDATE_MODE":
                self.device_mode_state = "NORMAL_MODE"
        self._disconnecting = False

    def shutdown(self) -> None:
        with self._lock:
            self.disconnect_locked()

    def send(self, cmd: str) -> dict:
        txt = str(cmd or "").strip()
        if not txt:
            return {"ok": False, "error": "Empty command"}
        with self._lock:
            if not self.connected:
                return {"ok": False, "error": "Not connected"}
            try:
                if self.transport == "BLE":
                    if not self._ble_connected or self._ble_client is None:
                        return {"ok": False, "error": "BLE device is not connected"}
                    self._send_ble_line(txt)
                else:
                    if self._ser is None or not self._ser.is_open:
                        return {"ok": False, "error": "Serial device is not connected"}
                    self._ser.write((txt + "\n").encode("utf-8"))
                with self._state_lock:
                    self.last_tx_cmd = txt
                self._push_event("TX", txt)
                return {"ok": True}
            except Exception as exc:
                with self._state_lock:
                    self.last_error = str(exc)
                self._push_event("ERR", f"TX error: {exc}")
                return {"ok": False, "error": str(exc)}

    def _send_ble_line(self, cmd: str) -> None:
        upper = cmd.strip().upper()

        if upper == "READ":
            live_data = self._run_ble_coro(self._ble_read_text_async(BLE_LIVE_CHAR_UUID), timeout=3.5)
            if live_data:
                self._queue_ble_payload(live_data)
            return

        if upper == "READCFG":
            cfg_data = self._run_ble_coro(self._ble_read_text_async(BLE_CONFIG_CHAR_UUID), timeout=4.0)
            if cfg_data:
                self._queue_ble_payload(cfg_data)
            return

        if upper == "READADV":
            adv_data = self._run_ble_coro(self._ble_read_text_async(BLE_ADV_CHAR_UUID), timeout=4.0)
            if adv_data:
                self._queue_ble_payload(adv_data)
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
            with self._state_lock:
                name = str(self.device_name or "").strip()
            if name:
                self._queue_ble_payload(f"DEVNAME:{name}")
            return

        if upper == "VERSION":
            with self._state_lock:
                fw = str(self.fw_version or "").strip()
                mode_state = str(self.device_mode_state or "NORMAL_MODE")
            if fw:
                self._queue_ble_payload(f"FW:{fw}|STATE:{mode_state}")
            return

        self._run_ble_coro(self._ble_write_text_async(BLE_CONFIG_CHAR_UUID, cmd), timeout=5.0)

    def clear_events(self) -> dict:
        with self._state_lock:
            self._events.clear()
        self._push_event("SYS", "Log cleared")
        return {"ok": True}

    def _extract_number(self, text: str):
        if text is None:
            return None
        match = re.search(r"[-+]?\d*\.?\d+", str(text))
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    def _format_sat(self, value: str) -> str:
        txt = str(value or "").strip().lower()
        if txt in ("1", "on", "true", "yes", "high", "sat"):
            return "ON"
        if txt in ("0", "off", "false", "no", "low", "unsat"):
            return "OFF"
        return str(value or "").strip() or "-"

    def _update_telemetry_from_line(self, line: str) -> None:
        up = line.upper()
        updated = False
        with self._state_lock:
            if "VOLT=" in up and "CURR=" in up:
                kv = {}
                for part in line.split(","):
                    if "=" not in part:
                        continue
                    k, v = part.split("=", 1)
                    kv[k.strip().upper()] = v.strip()
                if "VOLT" in kv:
                    self.telemetry["input"] = kv["VOLT"]
                    updated = True
                if "CURR" in kv:
                    self.telemetry["output"] = kv["CURR"]
                    updated = True
                if "SAT" in kv:
                    self.telemetry["sat"] = self._format_sat(kv["SAT"])
                    updated = True
            elif "|" in line and ":" in line:
                parsed = {}
                for part in line.split("|"):
                    if ":" not in part:
                        continue
                    k, v = part.split(":", 1)
                    parsed[k.strip().lower()] = v.strip()
                if "input" in parsed:
                    self.telemetry["input"] = parsed["input"]
                    updated = True
                if "output" in parsed:
                    self.telemetry["output"] = parsed["output"]
                    updated = True
                if "sat" in parsed:
                    self.telemetry["sat"] = self._format_sat(parsed["sat"])
                    updated = True
                if "pwm_sat" in parsed:
                    self.telemetry["pwm_sat"] = self._format_sat(parsed["pwm_sat"])
                    updated = True
            if updated:
                self.telemetry["updated_at"] = time.time()

    def _update_runtime_markers_from_line(self, line: str) -> None:
        txt = str(line or "").strip()
        up = txt.upper()
        if up.startswith("DEVNAME:") or up.startswith("DEVNAME="):
            sep = ":" if ":" in txt else "="
            name = txt.split(sep, 1)[1].strip() if sep in txt else ""
            if name:
                with self._state_lock:
                    self.device_name = name
            return
        if "BLE DEVICE NAME:" in up:
            name = txt.split(":", 1)[1].strip() if ":" in txt else ""
            if name:
                with self._state_lock:
                    self.device_name = name
            return
        if txt.upper().startswith("SERVO_"):
            with self._state_lock:
                self.device_name = txt
            return
        if up.startswith("FW:"):
            kv = WebApi._parse_pipe_kv(txt)
            fw_value = kv.get("FW", txt[3:].strip())
            if fw_value:
                with self._state_lock:
                    self.fw_version = fw_value
            return
        if up.startswith("DEV_STATUS:"):
            kv = WebApi._parse_pipe_kv(txt)
            with self._state_lock:
                self.dev_authenticated = kv.get("DEV_STATUS", "").upper() == "UNLOCKED"
                self.device_mode_state = kv.get("STATE", self.device_mode_state)
                if kv.get("FW_VERSION"):
                    self.fw_version = kv.get("FW_VERSION", self.fw_version)
            return
        if up.startswith("FW_STATUS:"):
            kv = WebApi._parse_pipe_kv(txt)
            with self._state_lock:
                if kv.get("STATE"):
                    self.device_mode_state = kv.get("STATE", self.device_mode_state)
                if kv.get("FW"):
                    self.fw_version = kv.get("FW", self.fw_version)
            return
        low = txt.lower()
        if low == "dev_ok":
            with self._state_lock:
                self.dev_authenticated = True
                self.device_mode_state = "DEV_MODE_UNLOCKED"
            return
        if low in ("dev_fail", "dev_locked", "dev_required"):
            with self._state_lock:
                self.dev_authenticated = False
                self.device_mode_state = "NORMAL_MODE"
            return
        if low == "fw_mode_ready":
            with self._state_lock:
                self.device_mode_state = "FW_UPDATE_MODE"
            return
        if low in ("fw_mode_off", "fw_aborted"):
            with self._state_lock:
                self.device_mode_state = "DEV_MODE_UNLOCKED" if self.dev_authenticated else "NORMAL_MODE"

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                ser_obj = self._ser
            if ser_obj is None or not ser_obj.is_open:
                time.sleep(0.08)
                continue
            try:
                raw = ser_obj.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                self._push_rx_line(line)
            except Exception as exc:
                with self._state_lock:
                    self.last_error = str(exc)
                self._push_event("ERR", f"RX error: {exc}")
                time.sleep(0.15)

    def snapshot(self, since_event_id: int = 0) -> dict:
        try:
            sid = int(since_event_id)
        except Exception:
            sid = 0
        with self._state_lock:
            new_events = [evt for evt in self._events if evt["id"] > sid]
            return {
                "connected": self.connected,
                "transport": self.transport,
                "port": self.port,
                "baud": self.baud,
                "last_error": self.last_error,
                "last_rx_ts": self.last_rx_ts,
                "last_tx_cmd": self.last_tx_cmd,
                "device_name": self.device_name,
                "fw_version": self.fw_version,
                "device_mode_state": self.device_mode_state,
                "dev_authenticated": self.dev_authenticated,
                "telemetry": dict(self.telemetry),
                "firmware_upload": dict(self.firmware_upload),
                "events": new_events,
                "next_event_id": self._last_event_id,
            }


class WebApi:
    """API methods exposed to the frontend via pywebview."""

    CAL_FIELDS = [
        "MA_SCALE_FACTOR_4_20MA",
        "RESISTANCE_SCALE_FACTOR",
        "VOLTAGE_SCALE_FACTOR",
        "FB_TO_CURRENT_FACTOR",
        "FB_OFFSET",
    ]
    MODE_LIMITS = {
        "0-5V": (0.0, 5.0),
        "0-10V": (0.0, 10.0),
        "Resistance (0-10)": (0.0, 10.0),
        "0-20mA": (0.0, 20.0),
    }

    def __init__(self) -> None:
        self.runtime = SerialRuntime()
        self.current_profile_path = ""
        self.recent_files = []
        self._fw_cancel_event = threading.Event()

    @staticmethod
    def _normalize_profile_value(value) -> str:
        txt = str(value or "").strip()
        if txt in ("", "-", "None", "none", "null", "NULL"):
            return ""
        return txt

    @staticmethod
    def _dialog_path(dialog_result) -> str:
        if not dialog_result:
            return ""
        if isinstance(dialog_result, (list, tuple)):
            return str(dialog_result[0]).strip() if dialog_result else ""
        return str(dialog_result).strip()

    def _open_file_dialog(
        self,
        initial_dir: str,
        title: str = "Open File",
        filetypes=None,
    ) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            resolved_filetypes = filetypes or [("All Files", "*.*")]
            chosen = filedialog.askopenfilename(
                title=title,
                initialdir=initial_dir,
                filetypes=resolved_filetypes,
            )
            root.destroy()
            return str(chosen or "").strip()
        except Exception:
            return ""

    def _save_file_dialog(self, initial_dir: str, initial_name: str) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.asksaveasfilename(
                title="Save Profile File",
                initialdir=initial_dir,
                initialfile=initial_name,
                defaultextension=".txt",
                filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
            )
            root.destroy()
            return str(chosen or "").strip()
        except Exception:
            return ""

    def _wait_for_rx_line(self, matcher, timeout_ms: int = 5000, since_event_id: int = 0):
        deadline = time.monotonic() + (max(100, int(timeout_ms)) / 1000.0)
        cursor = int(since_event_id or 0)
        while time.monotonic() < deadline:
            snap = self.runtime.snapshot(since_event_id=cursor)
            for evt in snap.get("events", []):
                try:
                    cursor = max(cursor, int(evt.get("id", cursor)))
                except Exception:
                    pass
                if str(evt.get("direction", "")).upper() != "RX":
                    continue
                text = str(evt.get("text", "")).strip()
                try:
                    if matcher(text):
                        return text, cursor
                except Exception:
                    continue
            time.sleep(0.03)
        return "", cursor

    def _send_and_wait_line(self, cmd: str, matcher, timeout_ms: int = 5000, retries: int = 0) -> dict:
        attempts = max(0, int(retries)) + 1
        for attempt in range(attempts):
            start_event_id = int(getattr(self.runtime, "_last_event_id", 0))
            tx_res = self.runtime.send(cmd)
            if not tx_res.get("ok"):
                return {"ok": False, "error": tx_res.get("error", "Send failed")}
            line, _cursor = self._wait_for_rx_line(matcher, timeout_ms=timeout_ms, since_event_id=start_event_id)
            if line:
                return {"ok": True, "line": line}
            if attempt < (attempts - 1):
                continue
        return {"ok": False, "error": f"No response for {cmd}"}

    @staticmethod
    def _build_legacy_update_cmd(cmd: str) -> str:
        txt = str(cmd or "").strip()
        low = txt.lower()
        if not low.startswith("update:"):
            return txt
        if low.startswith("update:cfg:"):
            return "CFG:" + txt.split(":", 2)[2]
        if low.startswith("update:adv:"):
            return "ADV:" + txt.split(":", 2)[2]
        return txt

    @staticmethod
    def _is_auth_required_line(text: str) -> bool:
        low = str(text or "").strip().lower()
        return low == "auth_required" or low.startswith("error: authentication required")

    @staticmethod
    def _is_update_ack_line(text: str) -> bool:
        low = str(text or "").strip().lower()
        up = str(text or "").strip().upper()
        return (
            low in (
                "device updated",
                "ok",
                "auth_required",
                "auth_failed",
                "unknown_command",
                "update_failed",
                "busy_fw_update",
                "uart_blocked_ble_active",
            )
            or up.startswith("FW_ERROR:")
            or up.startswith("ERROR:")
        )

    def _authenticate_for_update(self, password: str) -> dict:
        pwd = str(password or "").strip()
        if not pwd:
            return {"ok": False, "error": "Password required"}

        auth_res = self._send_and_wait_line(
            f"auth:{pwd}",
            lambda txt: txt.strip().lower() in ("auth_success", "auth_failed", "auth ok", "auth_required")
            or self._is_auth_required_line(txt),
            timeout_ms=AUTH_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not auth_res.get("ok"):
            return {"ok": False, "error": "No authentication response from device."}
        auth_line = str(auth_res.get("line", "")).strip()
        auth_low = auth_line.lower()
        if auth_low in ("auth_success", "auth ok"):
            return {"ok": True}
        if auth_low == "auth_failed":
            return {"ok": False, "error": "Wrong AUTH password. Update not allowed."}
        if self._is_auth_required_line(auth_line):
            return {"ok": False, "error": "Authentication required by device. Enter correct password."}
        return {"ok": False, "error": f"Authentication failed: {auth_line}"}

    def _send_secured_update_command(self, update_cmd: str, password: str) -> dict:
        auth = self._authenticate_for_update(password)
        if not auth.get("ok"):
            return auth
        is_uart = str(getattr(self.runtime, "transport", "")).upper() == "UART"
        if is_uart:
            # Device UART path briefly ignores RX after sending auth reply.
            time.sleep(UART_POST_AUTH_UPDATE_DELAY_MS / 1000.0)

        update_res = self._send_and_wait_line(
            update_cmd,
            self._is_update_ack_line,
            timeout_ms=AUTH_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not update_res.get("ok"):
            # One controlled re-auth + retry improves reliability on noisy UART links.
            reauth = self._authenticate_for_update(password)
            if reauth.get("ok"):
                if is_uart:
                    time.sleep(UART_POST_AUTH_UPDATE_DELAY_MS / 1000.0)
                update_res = self._send_and_wait_line(
                    update_cmd,
                    self._is_update_ack_line,
                    timeout_ms=AUTH_RESPONSE_TIMEOUT_MS,
                    retries=1,
                )
            if not update_res.get("ok"):
                return {"ok": False, "error": f"No response for {update_cmd}"}
        reply = str(update_res.get("line", "")).strip()
        reply_low = reply.lower()
        if reply_low in ("device updated", "ok"):
            return {"ok": True, "line": reply}
        if self._is_auth_required_line(reply):
            reauth = self._authenticate_for_update(password)
            if reauth.get("ok"):
                if is_uart:
                    time.sleep(UART_POST_AUTH_UPDATE_DELAY_MS / 1000.0)
                retry_res = self._send_and_wait_line(
                    update_cmd,
                    self._is_update_ack_line,
                    timeout_ms=AUTH_RESPONSE_TIMEOUT_MS,
                    retries=1,
                )
                if retry_res.get("ok"):
                    retry_reply = str(retry_res.get("line", "")).strip()
                    retry_low = retry_reply.lower()
                    if retry_low in ("device updated", "ok"):
                        return {
                            "ok": True,
                            "line": retry_reply,
                            "warning": "Update required re-auth retry.",
                        }
                    if retry_low == "update_failed":
                        return {"ok": False, "error": "Update failed on device."}
            return {"ok": False, "error": "Authentication required. Enter correct AUTH password and retry."}
        if reply_low == "auth_failed":
            return {"ok": False, "error": "Wrong AUTH password. Update not allowed."}
        if reply_low == "uart_blocked_ble_active":
            return {"ok": False, "error": "Serial is blocked because BLE is active on device."}
        if reply_low == "busy_fw_update":
            return {"ok": False, "error": "Device is busy in firmware update mode."}
        if reply_low == "update_failed":
            return {"ok": False, "error": "Update failed on device."}

        if reply_low == "unknown_command":
            legacy_cmd = self._build_legacy_update_cmd(update_cmd)
            if legacy_cmd != update_cmd:
                if is_uart:
                    time.sleep(UART_POST_AUTH_UPDATE_DELAY_MS / 1000.0)
                legacy_res = self._send_and_wait_line(
                    legacy_cmd,
                    self._is_update_ack_line,
                    timeout_ms=AUTH_RESPONSE_TIMEOUT_MS,
                    retries=1,
                )
                if legacy_res.get("ok"):
                    legacy_reply = str(legacy_res.get("line", "")).strip()
                    legacy_low = legacy_reply.lower()
                    if legacy_low in ("device updated", "ok"):
                        return {
                            "ok": True,
                            "line": legacy_reply,
                            "warning": "Update required legacy command fallback.",
                        }
                    if self._is_auth_required_line(legacy_reply):
                        return {"ok": False, "error": "Authentication required. Enter correct password and retry."}
                    if legacy_low == "auth_failed":
                        return {"ok": False, "error": "Wrong AUTH password. Update not allowed."}
                    if legacy_low == "busy_fw_update":
                        return {"ok": False, "error": "Device is busy in firmware update mode."}
                    if legacy_low == "update_failed":
                        return {"ok": False, "error": "Update failed on device."}
            return {
                "ok": False,
                "error": "Firmware did not accept update command. Upload latest compatible firmware.",
            }

        if reply.upper().startswith("FW_ERROR:"):
            return {"ok": False, "error": reply}
        return {"ok": False, "error": f"Update failed: {reply}"}

    def _authorize_firmware_update(self, password: str) -> dict:
        pwd = str(password or "").strip()
        if not pwd:
            return {"ok": False, "error": "Password required"}

        # Current firmware requires developer session for FW_ENTER/FW_BEGIN.
        # DEV_LOGIN accepts AUTH password too, so one prompt remains enough.
        dev_res = self._send_and_wait_line(
            f"DEV_LOGIN:{pwd}",
            lambda txt: txt.strip().upper() in ("DEV_OK", "DEV_FAIL", "UNKNOWN_COMMAND"),
            timeout_ms=DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if dev_res.get("ok"):
            dev_line = str(dev_res.get("line", "")).strip().upper()
            self.runtime._push_event("SYS", f"FW auth DEV_LOGIN -> {dev_line or 'EMPTY'}")
            if dev_line == "DEV_OK":
                return {"ok": True, "line": dev_line}
            if dev_line == "DEV_FAIL":
                return {"ok": False, "error": "Wrong AUTH password. Update not allowed."}
            # UNKNOWN_COMMAND -> older firmware path, fallback to AUTH flow.
            self.runtime._push_event("SYS", "FW auth fallback -> AUTH")
            auth_res = self._authenticate_for_update(pwd)
            if not auth_res.get("ok"):
                return auth_res
            return {"ok": True, "line": dev_line}

        # If DEV_LOGIN did not return a definitive line, do not blindly continue.
        # Verify whether developer session is actually unlocked.
        self.runtime._push_event("SYS", "FW auth DEV_LOGIN -> no definitive response, checking DEV_STATUS")
        dev_status = self._send_and_wait_line(
            "DEV_STATUS",
            lambda txt: txt.strip().upper().startswith("DEV_STATUS:") or txt.strip().lower() == "unknown_command",
            timeout_ms=DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=0,
        )
        if dev_status.get("ok"):
            status_line = str(dev_status.get("line", "")).strip().upper()
            self.runtime._push_event("SYS", f"FW auth DEV_STATUS -> {status_line or 'EMPTY'}")
            if status_line.startswith("DEV_STATUS:UNLOCKED"):
                return {"ok": True, "line": status_line}
            if status_line.startswith("DEV_STATUS:LOCKED"):
                return {
                    "ok": False,
                    "error": "Developer session is locked. Re-enter AUTH password and retry firmware update.",
                }
            if status_line == "UNKNOWN_COMMAND":
                self.runtime._push_event("SYS", "FW auth fallback -> AUTH (legacy DEV_STATUS)")
                auth_res = self._authenticate_for_update(pwd)
                if not auth_res.get("ok"):
                    return auth_res
                return {"ok": True, "line": status_line}

        return {
            "ok": False,
            "error": "Developer login did not complete. Reconnect BLE and retry firmware update.",
        }

    @staticmethod
    def _parse_pipe_kv(line: str) -> dict:
        parsed = {}
        for part in str(line or "").split("|"):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            parsed[key.strip().upper()] = value.strip()
        return parsed

    def _parse_cal_payload(self, line: str) -> dict:
        if not str(line or "").startswith("CAL:"):
            return {}
        try:
            data = json.loads(str(line)[4:].strip())
        except Exception:
            return {}
        out = {}
        for key in self.CAL_FIELDS:
            if key in data:
                try:
                    out[key] = float(data[key])
                except Exception:
                    pass
        return out

    @staticmethod
    def _parse_cfg_payload(line: str):
        txt = str(line or "").strip()
        if not txt.upper().startswith("CFG:"):
            return None
        try:
            data = json.loads(txt[4:].strip())
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return data

    @staticmethod
    def _parse_adv_payload(line: str):
        txt = str(line or "").strip()
        if not txt.upper().startswith("ADV:"):
            return None
        try:
            data = json.loads(txt[4:].strip())
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _remember_recent_file(self, file_path: Path) -> None:
        try:
            norm = str(file_path.resolve())
        except Exception:
            norm = str(file_path)
        self.recent_files = [p for p in self.recent_files if p != norm]
        self.recent_files.insert(0, norm)
        self.recent_files = self.recent_files[:8]

    @staticmethod
    def _parse_profile_text_values(content: str) -> dict:
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

    @staticmethod
    def _validate_profile_text_values(values: dict) -> tuple[bool, str]:
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
        for idx in range(1, 11):
            required_keys.append(f"bp{idx}_input")
            required_keys.append(f"bp{idx}_output")

        missing = [k for k in required_keys if k not in values]
        if missing:
            sample = ", ".join(missing[:6])
            if len(missing) > 6:
                sample += ", ..."
            return False, f"Not a valid SERVO profile text file. Missing keys: {sample}"

        return True, ""

    def _profile_from_values(self, values: dict) -> dict:
        def txt(key: str, default: str = "") -> str:
            return self._normalize_profile_value(values.get(key, default))

        cfg = {
            "mode": txt("mode"),
            "input_min": txt("input_min"),
            "input_max": txt("input_max"),
            "output_min": txt("output_min"),
            "output_max": txt("output_max"),
            "unit": txt("unit"),
            "breakpoints": [],
        }
        for idx in range(1, 11):
            cfg["breakpoints"].append(
                {
                    "input": txt(f"bp{idx}_input"),
                    "output": txt(f"bp{idx}_output"),
                }
            )

        dither_txt = txt("ditherEnable", "1")
        adv = {
            "kp": txt("kp", "0.15") or "0.15",
            "ki": txt("ki", "0.00005") or "0.00005",
            "ditherEnable": str(dither_txt).strip().lower() in ("1", "true", "on", "yes"),
            "ditherFreq": txt("ditherFreq", "150") or "150",
            "ditherAmplitude": txt("ditherAmplitude", "0.05") or "0.05",
            "pwmFreq": txt("pwmFreq", "2000") or "2000",
        }

        return {"cfg": cfg, "adv": adv}

    @staticmethod
    def _safe_export_filename_base(device_name: str) -> str:
        raw_name = str(device_name or "").strip() or "SERVO_EVDR"
        safe = re.sub(r"[\\/:*?\"<>|]+", "_", raw_name).strip(" .")
        return safe or "SERVO_EVDR"

    @staticmethod
    def _to_line_value(value) -> str:
        txt = str(value or "").strip()
        return txt if txt else "-"

    def _build_profile_text_lines(self, profile: dict, device_name: str) -> list[str]:
        cfg = dict((profile or {}).get("cfg") or {})
        adv = dict((profile or {}).get("adv") or {})
        breakpoints = list(cfg.get("breakpoints") or [])

        dither_raw = adv.get("ditherEnable", True)
        if isinstance(dither_raw, bool):
            dither_enable = 1 if dither_raw else 0
        else:
            dither_enable = 1 if str(dither_raw).strip().lower() in ("1", "true", "on", "yes") else 0

        lines = [
            f"device_name={device_name}",
            f"saved_at={time.strftime('%Y-%m-%d %H:%M:%S')}",
            "format=SERVO_EVDR_PROFILE_TEXT",
            "",
            "[Configuration]",
            f"mode={self._to_line_value(cfg.get('mode'))}",
            f"input_min={self._to_line_value(cfg.get('input_min'))}",
            f"input_max={self._to_line_value(cfg.get('input_max'))}",
            f"output_min={self._to_line_value(cfg.get('output_min'))}",
            f"output_max={self._to_line_value(cfg.get('output_max'))}",
            f"unit={self._to_line_value(cfg.get('unit'))}",
        ]

        for idx in range(10):
            bp = breakpoints[idx] if idx < len(breakpoints) and isinstance(breakpoints[idx], dict) else {}
            lines.append(f"bp{idx + 1}_input={self._to_line_value(bp.get('input'))}")
            lines.append(f"bp{idx + 1}_output={self._to_line_value(bp.get('output'))}")

        lines.extend(
            [
                "",
                "[Advanced Parameters]",
                f"kp={self._to_line_value(adv.get('kp', '0.15'))}",
                f"ki={self._to_line_value(adv.get('ki', '0.00005'))}",
                f"ditherEnable={dither_enable}",
                f"ditherFreq={self._to_line_value(adv.get('ditherFreq', '150'))}",
                f"ditherAmplitude={self._to_line_value(adv.get('ditherAmplitude', '0.05'))}",
                f"pwmFreq={self._to_line_value(adv.get('pwmFreq', '2000'))}",
            ]
        )
        return lines

    def app_info(self) -> dict:
        return {
            "name": "SERVO EVDR Web Console",
            "transport": "UART/BLE",
        }

    def list_serial_ports(self) -> dict:
        return {"ports": self.runtime.list_serial_ports()}

    def list_ble_devices(self) -> dict:
        return self.runtime.list_ble_devices()

    def connect_serial(self, port: str, baud: int = 9600) -> dict:
        return self.runtime.connect(port, baud)

    def connect_ble(self, address: str, display_name: str = "") -> dict:
        return self.runtime.connect_ble(address, display_name=display_name)

    def disconnect(self) -> dict:
        return self.runtime.disconnect()

    def send_command(self, cmd: str) -> dict:
        return self.runtime.send(cmd)

    def read_now(self) -> dict:
        return self.runtime.send("read")

    def clear_log(self) -> dict:
        return self.runtime.clear_events()

    def get_snapshot(self, since_event_id: int = 0) -> dict:
        return self.runtime.snapshot(since_event_id=since_event_id)

    @staticmethod
    def _to_float(value, field_name: str) -> float:
        try:
            num = float(value)
        except Exception as exc:
            raise ValueError(f"{field_name} must be numeric") from exc
        if not math.isfinite(num):
            raise ValueError(f"{field_name} must be finite")
        return num

    def _validate_cfg_payload(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Invalid CFG payload")

        mode = str(payload.get("mode", "")).strip()
        if not mode:
            raise ValueError("Mode is required")

        input_vals = payload.get("input")
        output_vals = payload.get("output")
        if not isinstance(input_vals, (list, tuple)) or len(input_vals) < 2:
            raise ValueError("Input range is required")
        if not isinstance(output_vals, (list, tuple)) or len(output_vals) < 2:
            raise ValueError("Output range is required")

        in_min = self._to_float(input_vals[0], "Input Min")
        in_max = self._to_float(input_vals[1], "Input Max")
        out_min = self._to_float(output_vals[0], "Output Min")
        out_max = self._to_float(output_vals[1], "Output Max")

        if in_min >= in_max:
            raise ValueError("Input range invalid: Input Min must be less than Input Max.")
        if out_min >= out_max:
            raise ValueError("Output range invalid: Output Min must be less than Output Max.")
        if out_min < 0 or out_min > 2000 or out_max < 0 or out_max > 2000:
            raise ValueError("Output current range must stay within [0, 2000] mA.")

        if mode in self.MODE_LIMITS:
            mode_min, mode_max = self.MODE_LIMITS[mode]
            if in_min < mode_min or in_max > mode_max:
                raise ValueError(f"For mode '{mode}', input range must stay within [{mode_min:g}, {mode_max:g}].")

        points_in = payload.get("points", [])
        if points_in is None:
            points_in = []
        if not isinstance(points_in, list):
            raise ValueError("Setpoint table format is invalid")
        if len(points_in) > 10:
            raise ValueError("Maximum 10 target points allowed")

        points = []
        seen_inputs = {}
        for idx, row in enumerate(points_in, start=1):
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise ValueError(f"BP{idx} requires both Input and Output")
            bp_in = self._to_float(row[0], f"BP{idx} input")
            bp_out = self._to_float(row[1], f"BP{idx} output")
            if bp_out < out_min or bp_out > out_max:
                raise ValueError(f"BP{idx} output {bp_out:g} is out of output range [{out_min:g}, {out_max:g}].")
            if bp_in in seen_inputs:
                first_idx = seen_inputs[bp_in]
                raise ValueError(f"Duplicate input value: BP{idx} input {bp_in:g} is already used in BP{first_idx}.")
            seen_inputs[bp_in] = idx
            points.append([bp_in, bp_out])

        for i in range(1, len(points)):
            prev_in = points[i - 1][0]
            curr_in = points[i][0]
            if curr_in <= prev_in:
                raise ValueError(f"Input order invalid: BP{i+1} input must be greater than BP{i} input.")

        unit = str(payload.get("unit", "")).strip() or "mA"
        return {
            "mode": mode,
            "input": [in_min, in_max],
            "output": [out_min, out_max],
            "unit": unit,
            "points": points,
        }

    def _validate_adv_payload(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Invalid ADV payload")

        kp = self._to_float(payload.get("kp"), "Kp")
        ki = self._to_float(payload.get("ki"), "Ki")
        dither_freq = self._to_float(payload.get("ditherFreq"), "Dither Freq")
        dither_amp = self._to_float(payload.get("ditherAmplitude"), "Dither Amp")
        pwm_freq_raw = self._to_float(payload.get("pwmFreq"), "PWM Freq")
        pwm_freq = int(pwm_freq_raw)
        if pwm_freq <= 0:
            raise ValueError("PWM Freq must be greater than zero")

        dither_raw = payload.get("ditherEnable", 1)
        if isinstance(dither_raw, bool):
            dither_enable = 1 if dither_raw else 0
        else:
            dither_enable = 1 if str(dither_raw).strip().lower() in ("1", "true", "on", "yes") else 0

        return {
            "kp": kp,
            "ki": ki,
            "ditherEnable": dither_enable,
            "ditherFreq": dither_freq,
            "ditherAmplitude": dither_amp,
            "pwmFreq": pwm_freq,
        }

    def load_config_from_device(self, scope: str = "both") -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}

        scope_txt = str(scope or "both").strip().lower()
        if scope_txt not in ("cfg", "adv", "both"):
            scope_txt = "both"

        def _cfg_match(line: str) -> bool:
            txt = str(line or "").strip()
            low = txt.lower()
            return (
                txt.upper().startswith("CFG:")
                or low in ("unknown_command", "busy_fw_update", "uart_blocked_ble_active")
                or self._is_auth_required_line(txt)
            )

        def _adv_match(line: str) -> bool:
            txt = str(line or "").strip()
            low = txt.lower()
            return (
                txt.upper().startswith("ADV:")
                or low in ("unknown_command", "busy_fw_update", "uart_blocked_ble_active")
                or self._is_auth_required_line(txt)
            )

        out = {"ok": True, "warnings": []}
        is_uart = str(getattr(self.runtime, "transport", "")).upper() == "UART"

        if scope_txt in ("cfg", "both"):
            cfg_res = self._send_and_wait_line("readcfg", _cfg_match, timeout_ms=AUTH_RESPONSE_TIMEOUT_MS, retries=1)
            if not cfg_res.get("ok"):
                return {"ok": False, "error": "No response for readcfg"}
            cfg_line = str(cfg_res.get("line", "")).strip()
            cfg_low = cfg_line.lower()
            if cfg_low == "unknown_command":
                out["warnings"].append("Device firmware does not support readcfg.")
            elif cfg_low == "busy_fw_update":
                return {"ok": False, "error": "Device is busy in firmware update mode."}
            elif cfg_low == "uart_blocked_ble_active":
                return {"ok": False, "error": "Serial is blocked because BLE is active on device."}
            elif self._is_auth_required_line(cfg_line):
                return {
                    "ok": False,
                    "error": "Device returned authentication required while loading config. Reconnect or use compatible firmware.",
                }
            else:
                cfg_payload = self._parse_cfg_payload(cfg_line)
                if cfg_payload is None:
                    return {"ok": False, "error": "CFG parse failed from device response."}
                out["cfg"] = cfg_payload

        if scope_txt in ("adv", "both"):
            if is_uart and scope_txt == "both":
                # Device UART path applies a short RX ignore window after TX response.
                time.sleep(0.12)
            adv_res = self._send_and_wait_line("readadv", _adv_match, timeout_ms=AUTH_RESPONSE_TIMEOUT_MS, retries=1)
            if not adv_res.get("ok"):
                return {"ok": False, "error": "No response for readadv"}
            adv_line = str(adv_res.get("line", "")).strip()
            adv_low = adv_line.lower()
            if adv_low == "unknown_command":
                out["warnings"].append("Device firmware does not support readadv.")
            elif adv_low == "busy_fw_update":
                return {"ok": False, "error": "Device is busy in firmware update mode."}
            elif adv_low == "uart_blocked_ble_active":
                return {"ok": False, "error": "Serial is blocked because BLE is active on device."}
            elif self._is_auth_required_line(adv_line):
                return {
                    "ok": False,
                    "error": "Device returned authentication required while loading advanced parameters.",
                }
            else:
                adv_payload = self._parse_adv_payload(adv_line)
                if adv_payload is None:
                    return {"ok": False, "error": "ADV parse failed from device response."}
                out["adv"] = adv_payload

        if scope_txt == "both" and "cfg" not in out and "adv" not in out:
            return {
                "ok": False,
                "error": "Device firmware does not support readcfg/readadv. Upload latest compatible firmware.",
            }
        return out

    def apply_cfg_update(self, cfg_payload: dict, password: str) -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}
        try:
            payload = self._validate_cfg_payload(cfg_payload)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        cmd = f"update:CFG:{json.dumps(payload, separators=(',', ':'))}"
        return self._send_secured_update_command(cmd, password)

    def apply_adv_update(self, adv_payload: dict, password: str) -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}
        try:
            payload = self._validate_adv_payload(adv_payload)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        cmd = f"update:ADV:{json.dumps(payload, separators=(',', ':'))}"
        return self._send_secured_update_command(cmd, password)

    def developer_status(self) -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}
        status = self._send_and_wait_line(
            "DEV_STATUS",
            lambda txt: txt.strip().upper().startswith("DEV_STATUS:"),
            timeout_ms=DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=0,
        )
        if not status.get("ok"):
            return {"ok": False, "error": "No response for DEV_STATUS."}
        line = str(status.get("line", "")).strip()
        kv = self._parse_pipe_kv(line)
        unlocked = kv.get("DEV_STATUS", "").upper() == "UNLOCKED"
        return {
            "ok": True,
            "line": line,
            "unlocked": unlocked,
            "state": kv.get("STATE", ""),
            "fw_version": kv.get("FW_VERSION", ""),
            "fw_result": kv.get("FW_RESULT", ""),
        }

    def open_profile_file(self, current_path: str = "") -> dict:
        initial_dir = str(_default_dialog_dir())
        path_txt = str(current_path or "").strip()
        if path_txt:
            try:
                cur = Path(path_txt)
                if cur.exists():
                    initial_dir = str(cur.parent if cur.is_file() else cur)
            except Exception:
                pass
        elif self.current_profile_path:
            try:
                cur = Path(self.current_profile_path)
                if cur.parent.exists():
                    initial_dir = str(cur.parent)
            except Exception:
                pass
        elif self.recent_files:
            try:
                recent = Path(self.recent_files[0])
                if recent.parent.exists():
                    initial_dir = str(recent.parent)
            except Exception:
                pass

        chosen_path = self._open_file_dialog(initial_dir=initial_dir)
        if not chosen_path:
            return {"ok": False, "cancelled": True}

        file_path = Path(chosen_path)
        try:
            text = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            return {"ok": False, "error": f"Failed to read profile file: {exc}"}

        try:
            values = self._parse_profile_text_values(text)
            is_valid, validation_message = self._validate_profile_text_values(values)
            if not is_valid:
                raise ValueError(validation_message)
            profile = self._profile_from_values(values)
        except Exception as exc:
            return {"ok": False, "error": f"Profile format is invalid: {exc}"}

        try:
            resolved = str(file_path.resolve())
        except Exception:
            resolved = str(file_path)

        self.current_profile_path = resolved
        self._remember_recent_file(file_path)
        return {
            "ok": True,
            "path": resolved,
            "profile": profile,
            "recent_files": list(self.recent_files),
        }

    def save_profile_file(self, profile: dict, current_path: str = "", device_name: str = "") -> dict:
        safe_base = self._safe_export_filename_base(device_name)
        suggested_name = f"{safe_base}_profile.txt"
        initial_dir = str(_default_dialog_dir())
        initial_file = suggested_name

        for candidate in (str(current_path or "").strip(), str(self.current_profile_path or "").strip()):
            if not candidate:
                continue
            try:
                cur = Path(candidate)
                if cur.parent.exists():
                    initial_dir = str(cur.parent)
                if cur.name:
                    initial_file = cur.name
                break
            except Exception:
                continue

        chosen_path = self._save_file_dialog(initial_dir=initial_dir, initial_name=initial_file)
        if not chosen_path:
            return {"ok": False, "cancelled": True}

        target_path = Path(chosen_path)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            lines = self._build_profile_text_lines(profile=profile or {}, device_name=safe_base)
            content = "\n".join(lines).rstrip() + "\n"
            target_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            return {"ok": False, "error": f"Failed to save profile file: {exc}"}

        try:
            resolved = str(target_path.resolve())
        except Exception:
            resolved = str(target_path)

        self.current_profile_path = resolved
        self._remember_recent_file(target_path)
        return {
            "ok": True,
            "path": resolved,
            "recent_files": list(self.recent_files),
        }

    def developer_login(self, password: str) -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}
        pwd = str(password or "").strip()
        if not pwd:
            return {"ok": False, "error": "Password required"}

        dev_res = self._send_and_wait_line(
            f"DEV_LOGIN:{pwd}",
            lambda txt: txt.strip().upper() in ("DEV_OK", "DEV_FAIL"),
            timeout_ms=DEV_AUTH_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not dev_res.get("ok"):
            return {"ok": False, "error": "No response from device for DEV_LOGIN."}
        if str(dev_res.get("line", "")).strip().upper() != "DEV_OK":
            return {"ok": False, "error": "Invalid developer password."}
        return {"ok": True, "line": dev_res.get("line", "")}

    def developer_logout(self) -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}
        start_event_id = int(getattr(self.runtime, "_last_event_id", 0))
        tx_res = self.runtime.send("DEV_LOGOUT")
        if not tx_res.get("ok"):
            return tx_res
        line, _ = self._wait_for_rx_line(
            lambda txt: txt.strip().upper() in ("DEV_LOCKED", "FW_MODE_OFF"),
            timeout_ms=3500,
            since_event_id=start_event_id,
        )
        return {"ok": True, "line": line}

    def developer_load_calibration(self) -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}

        enter = self._send_and_wait_line(
            "CAL_ENTER",
            lambda txt: txt.strip().upper() in ("CAL_MODE_READY", "DEV_REQUIRED", "BUSY_FW_UPDATE"),
            timeout_ms=FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not enter.get("ok"):
            return enter
        enter_line = str(enter.get("line", "")).strip().upper()
        if enter_line == "DEV_REQUIRED":
            return {"ok": False, "error": "Developer authentication required"}
        if enter_line == "BUSY_FW_UPDATE":
            return {"ok": False, "error": "Device is busy in firmware update mode"}
        if enter_line != "CAL_MODE_READY":
            return {"ok": False, "error": f"CAL_ENTER failed: {enter_line}"}
        if str(getattr(self.runtime, "transport", "")).upper() == "UART":
            # Device UART path applies a short RX ignore window after TX; avoid losing next command.
            time.sleep(0.12)

        cal_line = self._send_and_wait_line(
            "CAL_GET",
            lambda txt: (
                txt.strip().upper().startswith("CAL:")
                or txt.strip().upper().startswith("CAL_ERROR")
                or txt.strip().upper() in ("DEV_REQUIRED", "BUSY_FW_UPDATE")
            ),
            timeout_ms=FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        # Best-effort cleanup; do not fail whole call on CAL_EXIT timeout.
        try:
            self._send_and_wait_line(
                "CAL_EXIT",
                lambda txt: txt.strip().upper() == "CAL_MODE_OFF",
                timeout_ms=DEV_AUTH_RESPONSE_TIMEOUT_MS,
            )
        except Exception:
            pass
        if not cal_line.get("ok"):
            if str(getattr(self.runtime, "transport", "")).upper() == "BLE":
                return {
                    "ok": False,
                    "error": "No response for CAL_GET. Possible cause: UART session is active in device, so BLE calibration response is blocked.",
                }
            return cal_line
        raw = str(cal_line.get("line", "")).strip()
        raw_up = raw.upper()
        if raw_up == "DEV_REQUIRED":
            return {"ok": False, "error": "Developer authentication required"}
        if raw_up == "BUSY_FW_UPDATE":
            return {"ok": False, "error": "Device is busy in firmware update mode"}
        if not raw.upper().startswith("CAL:"):
            return {"ok": False, "error": f"Calibration read failed: {raw}"}
        payload = self._parse_cal_payload(raw)
        if not payload:
            return {"ok": False, "error": "Calibration payload parse failed"}
        return {"ok": True, "calibration": payload}

    def developer_save_calibration(self, calibration: dict) -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}
        if not isinstance(calibration, dict):
            return {"ok": False, "error": "Invalid calibration payload"}

        payload_obj = {}
        for key in self.CAL_FIELDS:
            if key not in calibration:
                return {"ok": False, "error": f"Missing calibration field: {key}"}
            try:
                payload_obj[key] = float(calibration[key])
            except Exception:
                return {"ok": False, "error": f"Calibration field must be numeric: {key}"}

        enter = self._send_and_wait_line(
            "CAL_ENTER",
            lambda txt: txt.strip().upper() in ("CAL_MODE_READY", "DEV_REQUIRED", "BUSY_FW_UPDATE"),
            timeout_ms=FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not enter.get("ok"):
            return enter
        enter_line = str(enter.get("line", "")).strip().upper()
        if enter_line == "DEV_REQUIRED":
            return {"ok": False, "error": "Developer authentication required"}
        if enter_line == "BUSY_FW_UPDATE":
            return {"ok": False, "error": "Device is busy in firmware update mode"}
        if enter_line != "CAL_MODE_READY":
            return {"ok": False, "error": f"CAL_ENTER failed: {enter_line}"}
        if str(getattr(self.runtime, "transport", "")).upper() == "UART":
            # Device UART path applies a short RX ignore window after TX; avoid losing next command.
            time.sleep(0.12)

        payload = json.dumps(payload_obj, separators=(",", ":"))
        save_line = self._send_and_wait_line(
            f"CAL_SET:{payload}",
            lambda txt: txt.strip().upper().startswith("CAL_") or txt.strip().upper() in ("DEV_REQUIRED", "BUSY_FW_UPDATE"),
            timeout_ms=FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        try:
            self._send_and_wait_line(
                "CAL_EXIT",
                lambda txt: txt.strip().upper() == "CAL_MODE_OFF",
                timeout_ms=DEV_AUTH_RESPONSE_TIMEOUT_MS,
            )
        except Exception:
            pass
        if not save_line.get("ok"):
            return save_line
        line_txt = str(save_line.get("line", "")).strip().upper()
        if line_txt != "CAL_SAVED":
            return {"ok": False, "error": f"Calibration save failed: {line_txt}"}
        return {"ok": True}

    def pick_firmware_bin(self, current_dir: str = "") -> dict:
        initial_dir = str(_default_dialog_dir())
        dir_txt = str(current_dir or "").strip()
        if dir_txt:
            try:
                p = Path(dir_txt)
                if p.exists():
                    initial_dir = str(p if p.is_dir() else p.parent)
            except Exception:
                pass
        chosen_path = self._open_file_dialog(
            initial_dir=initial_dir,
            title="Select Firmware BIN",
            filetypes=[("Firmware Binary", "*.bin"), ("All Files", "*.*")],
        )
        if not chosen_path:
            return {"ok": False, "cancelled": True}
        fp = Path(chosen_path)
        try:
            size = fp.stat().st_size
        except Exception:
            size = 0
        return {"ok": True, "path": str(fp), "name": fp.name, "size": int(size)}

    def firmware_validate_password(self, password: str) -> dict:
        """Validate AUTH password before OTA transfer starts."""
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}
        if str(getattr(self.runtime, "transport", "")).upper() != "BLE":
            return {"ok": False, "error": "Firmware update is BLE-only. Switch to Bluetooth and connect first."}
        authz = self._authorize_firmware_update(password)
        if not authz.get("ok"):
            return authz
        return {"ok": True, "message": "Password verified"}

    @staticmethod
    def _parse_partition_size(token: str) -> int:
        text = str(token or "").strip().lower()
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

    @staticmethod
    def _detect_device_variant(device_name: str) -> str:
        txt = str(device_name or "").strip().upper()
        return FW_ALLOWED_VARIANT if txt.startswith(FW_DEVICE_NAME_PREFIX) else ""

    @staticmethod
    def _detect_bin_variant(image_bytes: bytes) -> str:
        data = bytes(image_bytes or b"")
        if not data:
            return ""
        # Strictly parse firmware model marker from the BIN itself.
        marker = b"FW_MODEL="
        idx = data.find(marker)
        if idx < 0:
            return ""
        start = idx + len(marker)
        end = start
        while end < len(data):
            b = data[end]
            if (48 <= b <= 57) or (65 <= b <= 90) or b == 95:
                end += 1
                continue
            break
        if end <= start:
            return ""
        return data[start:end].decode("ascii", errors="ignore").upper()

    @staticmethod
    def _firmware_hash_prefix(image_bytes: bytes) -> str:
        digest = hashlib.sha256(bytes(image_bytes or b"")).hexdigest().upper()
        return digest[:FW_HASH_PREFIX_LEN]

    def _validate_firmware_variant_match(self, image_bytes: bytes) -> tuple[bool, str]:
        device_name = str(self.runtime.snapshot().get("device_name", "")).strip()
        device_variant = self._detect_device_variant(device_name)
        if not device_variant:
            return False, (
                "Unable to detect connected device variant from DEVNAME.\n"
                f"Connect device and confirm DEVNAME starts with {FW_DEVICE_NAME_PREFIX}."
            )
        if device_variant != FW_ALLOWED_VARIANT:
            return False, (
                f"Unsupported device variant for this app version.\n"
                f"Allowed device variant: {FW_ALLOWED_VARIANT}\n"
                f"Detected device variant: {device_variant}"
            )

        bin_variant = self._detect_bin_variant(image_bytes)
        if not bin_variant:
            return False, (
                "Unable to detect firmware variant from selected BIN.\n"
                f"BIN must include marker: {FW_REQUIRED_MODEL_MARKER}."
            )
        if bin_variant != FW_ALLOWED_VARIANT:
            return False, (
                f"Unsupported BIN variant for this app version.\n"
                f"Allowed BIN variant: {FW_ALLOWED_VARIANT}\n"
                f"Detected BIN variant: {bin_variant}"
            )
        has_project_marker = FW_REQUIRED_PROJECT_MARKER.encode("ascii") in image_bytes
        if not has_project_marker:
            return False, (
                "Firmware identity marker missing in selected BIN.\n"
                f"BIN must include marker: {FW_REQUIRED_PROJECT_MARKER}."
            )
        if device_variant != bin_variant:
            return False, f"Firmware variant mismatch.\nDevice: {device_variant}\nBIN: {bin_variant}"
        return True, ""

    def _validate_ota_bin_build_context(self, bin_path: Path, image_size: int) -> str:
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
            self.runtime._push_event("SYS", f"Partition validation skipped: {exc}")
            return ""

        subtypes = {subtype for _name, subtype, _size in ota_slots}
        if "ota_0" not in subtypes or "ota_1" not in subtypes:
            scheme_text = f" ({partition_scheme})" if partition_scheme else ""
            return (
                "Selected BIN was built with a non-OTA partition layout"
                f"{scheme_text}.\n"
                "Rebuild firmware using an OTA partition scheme."
            )

        slot_sizes = [size for _name, _subtype, size in ota_slots if size > 0]
        if slot_sizes and image_size > min(slot_sizes):
            return (
                f"Selected BIN is {image_size} bytes, but the smallest OTA slot is {min(slot_sizes)} bytes.\n"
                "Choose a larger OTA partition scheme or reduce firmware size."
            )
        return ""

    def _is_fw_cancel_requested(self) -> bool:
        return bool(self._fw_cancel_event.is_set())

    def _set_fw_cancel_requested(self) -> None:
        self._fw_cancel_event.set()

    def _clear_fw_cancel_requested(self) -> None:
        self._fw_cancel_event.clear()

    def _send_fw_abort_best_effort(self) -> None:
        try:
            self._send_and_wait_line(
                "FW_ABORT",
                lambda txt: txt.strip().upper().startswith("FW_"),
                timeout_ms=FW_CMD_RESPONSE_TIMEOUT_MS,
                retries=0,
            )
        except Exception:
            pass

    def _firmware_cancelled_result(self) -> dict:
        self.runtime._set_fw_upload_progress(state="cancelled", active=False)
        return {"ok": False, "cancelled": True, "error": "Firmware upload cancelled by user."}

    def firmware_cancel_upload(self) -> dict:
        self._set_fw_cancel_requested()
        self.runtime._set_fw_upload_progress(state="cancelling", active=True)
        self.runtime._push_event("SYS", "Firmware upload cancel requested by user.")
        return {"ok": True}

    def _transfer_firmware_chunks(self, image_bytes: bytes, fast_protocol: bool = True) -> dict:
        total = len(image_bytes or b"")
        if total <= 0:
            return {"ok": False, "error": "No firmware payload"}

        sent = 0
        self.runtime._set_fw_upload_progress(
            state="uploading",
            active=True,
            sent_bytes=sent,
            total_bytes=total,
        )
        chunk_size = FW_CHUNK_BYTES if fast_protocol else FW_CHUNK_MIN_BYTES
        while sent < total:
            if self._is_fw_cancel_requested():
                return {"ok": False, "cancelled": True, "error": "Firmware upload cancelled by user."}
            chunk = image_bytes[sent : sent + chunk_size]
            if not chunk:
                break
            expected_total = sent + len(chunk)
            chunk_hex = chunk.hex().upper()
            ack_ok = False

            for _retry in range(FW_CHUNK_RETRY_COUNT + 1):
                if self._is_fw_cancel_requested():
                    return {"ok": False, "cancelled": True, "error": "Firmware upload cancelled by user."}
                chunk_cmd = f"FW_CHUNK:{sent}:{chunk_hex}" if fast_protocol else f"FW_CHUNK:{chunk_hex}"
                ack = self._send_and_wait_line(
                    chunk_cmd,
                    lambda txt: txt.strip().upper().startswith("FW_CHUNK_ACK:")
                    or txt.strip().upper().startswith("FW_ERROR:")
                    or txt.strip().upper() in (
                        "DEV_REQUIRED",
                        "FW_MODE_REQUIRED",
                        "BUSY_FW_UPDATE",
                        "UNKNOWN_COMMAND",
                    ),
                    timeout_ms=FW_CHUNK_ACK_TIMEOUT_MS,
                    retries=0,
                )
                if not ack.get("ok"):
                    continue

                ack_line = str(ack.get("line", "")).strip()
                ack_up = ack_line.upper()
                ack_low = ack_line.lower()
                if ack_up.startswith("FW_CHUNK_ACK:"):
                    try:
                        ack_bytes = int(ack_line.split(":", 1)[1].strip())
                    except Exception:
                        ack_bytes = -1
                    if ack_bytes >= expected_total:
                        sent = min(ack_bytes, total)
                        self.runtime._set_fw_upload_progress(
                            state="uploading",
                            active=True,
                            sent_bytes=sent,
                            total_bytes=total,
                        )
                        ack_ok = True
                        break
                    if not fast_protocol:
                        sent = expected_total
                        self.runtime._set_fw_upload_progress(
                            state="uploading",
                            active=True,
                            sent_bytes=sent,
                            total_bytes=total,
                        )
                        ack_ok = True
                        break
                    continue

                if ack_low == "unknown_command" and fast_protocol:
                    fast_protocol = False
                    chunk_size = FW_CHUNK_MIN_BYTES
                    break

                if ack_up.startswith("FW_ERROR:"):
                    return {"ok": False, "error": ack_line}
                return {"ok": False, "error": f"FW_CHUNK failed: {ack_line}"}

            if sent >= total:
                break
            if ack_ok:
                continue
            if fast_protocol and chunk_size > FW_CHUNK_MIN_BYTES:
                chunk_size = max(FW_CHUNK_MIN_BYTES, chunk_size // 2)
                self.runtime._push_event("SYS", f"FW chunk timeout; reducing chunk size to {chunk_size} bytes")
                continue
            return {"ok": False, "error": "FW chunk retries exhausted"}

        if self._is_fw_cancel_requested():
            return {"ok": False, "cancelled": True, "error": "Firmware upload cancelled by user."}
        if sent < total:
            return {"ok": False, "error": "FW chunk transfer incomplete"}
        self.runtime._set_fw_upload_progress(
            state="finalizing",
            active=True,
            sent_bytes=total,
            total_bytes=total,
        )
        return {"ok": True}

    def firmware_upload(self, file_path: str, password: str) -> dict:
        if not self.runtime.connected:
            return {"ok": False, "error": "Not connected"}
        if str(getattr(self.runtime, "transport", "")).upper() != "BLE":
            return {"ok": False, "error": "Firmware update is BLE-only. Switch to Bluetooth and connect first."}
        self._clear_fw_cancel_requested()
        path_txt = str(file_path or "").strip()
        if not path_txt:
            return {"ok": False, "error": "Firmware file path required"}
        fp = Path(path_txt)
        if not fp.exists() or not fp.is_file():
            return {"ok": False, "error": "Firmware file not found"}
        try:
            image_bytes = fp.read_bytes()
        except Exception as exc:
            return {"ok": False, "error": f"Cannot read firmware file: {exc}"}
        if not image_bytes:
            return {"ok": False, "error": "Selected firmware file is empty"}
        image_size = len(image_bytes)
        self.runtime._set_fw_upload_progress(
            state="validating",
            active=True,
            sent_bytes=0,
            total_bytes=image_size,
        )
        if self._is_fw_cancel_requested():
            return self._firmware_cancelled_result()

        variant_ok, variant_error = self._validate_firmware_variant_match(image_bytes)
        if not variant_ok:
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return {"ok": False, "error": variant_error}
        ota_context_error = self._validate_ota_bin_build_context(fp, len(image_bytes))
        if ota_context_error:
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return {"ok": False, "error": ota_context_error}
        if self._is_fw_cancel_requested():
            return self._firmware_cancelled_result()

        self.runtime._set_fw_upload_progress(state="authorizing", active=True)
        authz = self._authorize_firmware_update(password)
        if not authz.get("ok"):
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return authz
        if self._is_fw_cancel_requested():
            return self._firmware_cancelled_result()

        self.runtime._set_fw_upload_progress(state="entering_fw_mode", active=True)
        enter = self._send_and_wait_line(
            "FW_ENTER",
            lambda txt: txt.strip().upper().startswith("FW_MODE_READY")
            or txt.strip().upper().startswith("FW_ERROR:")
            or txt.strip().upper() in ("DEV_REQUIRED", "BUSY_FW_UPDATE", "FW_BLE_ONLY"),
            timeout_ms=FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not enter.get("ok"):
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return enter
        enter_line = str(enter.get("line", "")).strip()
        if not enter_line.upper().startswith("FW_MODE_READY"):
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return {"ok": False, "error": f"FW_ENTER failed: {enter_line}"}
        if self._is_fw_cancel_requested():
            self._send_fw_abort_best_effort()
            return self._firmware_cancelled_result()

        target_version = fp.stem.replace(",", "_")
        digest_prefix = self._firmware_hash_prefix(image_bytes)
        bin_variant = self._detect_bin_variant(image_bytes)
        if not bin_variant:
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return {
                "ok": False,
                "error": (
                    "Unable to detect firmware variant from selected BIN.\n"
                    f"BIN must include marker: {FW_REQUIRED_MODEL_MARKER}."
                ),
            }
        self.runtime._set_fw_upload_progress(state="starting_transfer", active=True)
        begin = self._send_and_wait_line(
            f"FW_BEGIN:{len(image_bytes)},{target_version},{bin_variant},{digest_prefix}",
            lambda txt: txt.strip().upper().startswith("FW_READY:")
            or txt.strip().upper().startswith("FW_ERROR:")
            or txt.strip().upper() in ("DEV_REQUIRED", "FW_MODE_REQUIRED", "FW_BLE_ONLY"),
            timeout_ms=FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not begin.get("ok"):
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return begin
        begin_line = str(begin.get("line", "")).strip()
        if not begin_line.upper().startswith("FW_READY:"):
            self._send_fw_abort_best_effort()
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return {"ok": False, "error": f"FW_BEGIN failed: {begin_line}"}
        if self._is_fw_cancel_requested():
            self._send_fw_abort_best_effort()
            return self._firmware_cancelled_result()

        fast_protocol = "V1.0_BLE_OTA_FAST" in begin_line.upper()
        self.runtime._push_event("SYS", f"FW transfer mode: {'FAST' if fast_protocol else 'SAFE'}")
        transfer = self._transfer_firmware_chunks(image_bytes, fast_protocol=fast_protocol)
        if transfer.get("cancelled"):
            self._send_fw_abort_best_effort()
            return self._firmware_cancelled_result()
        if not transfer.get("ok"):
            self._send_fw_abort_best_effort()
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return transfer
        if self._is_fw_cancel_requested():
            self._send_fw_abort_best_effort()
            return self._firmware_cancelled_result()

        self.runtime._set_fw_upload_progress(
            state="finalizing",
            active=True,
            sent_bytes=image_size,
            total_bytes=image_size,
        )
        end = self._send_and_wait_line(
            "FW_END",
            lambda txt: txt.strip().upper().startswith("FW_TRANSFER_DONE")
            or txt.strip().upper().startswith("FW_ERROR:")
            or txt.strip().upper() in ("DEV_REQUIRED", "FW_MODE_REQUIRED", "FW_BLE_ONLY"),
            timeout_ms=FW_CMD_RESPONSE_TIMEOUT_MS,
            retries=1,
        )
        if not end.get("ok"):
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return end
        end_line = str(end.get("line", "")).strip()
        if not end_line.upper().startswith("FW_TRANSFER_DONE"):
            self.runtime._set_fw_upload_progress(state="failed", active=False)
            return {"ok": False, "error": f"FW_END failed: {end_line}"}
        self.runtime._set_fw_upload_progress(
            state="completed",
            active=False,
            sent_bytes=image_size,
            total_bytes=image_size,
        )
        return {"ok": True, "line": end_line}

    def close_app(self) -> dict:
        self.runtime.shutdown()
        return {"ok": True}


def main() -> None:
    _hide_windows_console_when_frozen()
    base_dir = _app_base_dir()
    index_path = _resolve_ui_resource_path("INDEX.HTML")
    icon_path = (
        _resolve_ui_resource_path("ASSETS", "ICONS", "APP_ICON.ICO")
        or _resolve_ui_resource_path("ASSETS", "ICONS", "APP_ICON.PNG")
        or (base_dir / "APP_ICON.ICO").resolve(strict=False)
    )
    if index_path is None:
        searched = ", ".join(str(p) for p in _resource_base_dirs())
        raise FileNotFoundError(
            f"Missing UI file: WEBUI/INDEX.HTML or UI_FILES/INDEX.HTML (searched: {searched})"
        )
    print(f"[UI] Loading INDEX from: {index_path}")
    api = WebApi()
    window = webview.create_window(
        title="SERVO EVDR APPLICATION",
        url=index_path.resolve(strict=False).as_uri(),
        js_api=api,
        width=1560,
        height=820,
        resizable=False,
    )
    if hasattr(window, "events") and hasattr(window.events, "closed"):
        window.events.closed += lambda: api.close_app()
    webview.start(
        lambda: _apply_windows_window_icon("SERVO EVDR APPLICATION", icon_path),
        debug=False,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        trace_text = traceback.format_exc()
        log_path = _write_startup_error_log(trace_text)
        log_hint = f"\n\nError log:\n{log_path}" if log_path else ""
        message = (
            "Application failed to start.\n\n"
            f"{exc.__class__.__name__}: {exc}"
            f"{log_hint}\n\n"
            "Please reopen the app. If issue continues, share the log file."
        )
        _show_startup_error_dialog(message)
        sys.exit(1)
