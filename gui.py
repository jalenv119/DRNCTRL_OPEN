"""
gui.py  -  Dear PyGui dashboard for BCI Drone Control (Team20)

Run with:
    python gui.py

Displays:
  - Connection status (Cortex WS, headset, session, profile, drone)
  - Headset ID + battery level
  - Current mental command + confidence bar (colour-coded against threshold)
  - Last command sent to the drone + drone response
  - Scrolling confidence history plot with threshold line
"""

import asyncio
import collections
import os
import ssl
import threading
import time

import dearpygui.dearpygui as dpg
import websocket
from dotenv import load_dotenv

from cortex import (
    ACTION_TO_OUTPUT,
    CLIENT_ID,
    CLIENT_SECRET,
    CONFIDENCE_THRESHOLD,
    CORTEX_URL,
    DRONE_IP,
    DRONE_PORT,
    OUTPUT_COOLDOWN_SEC,
    CLIENT_ID,
    CLIENT_SECRET,
    PROFILE_NAME,
    CortexClient,
)
from tello_driver import create_drone

load_dotenv()

# Palette
_GREEN  = (80,  220,  80)
_RED    = (220,  60,  60)
_YELLOW = (220, 200,  60)
_BLUE   = (100, 180, 255)
_GREY   = (130, 130, 130)
_WHITE  = (220, 220, 220)

_HISTORY = 300   # number of confidence samples kept for the plot


# Shared state (written by WS/drone threads, read by GUI thread)
# Plain class attributes — the GIL makes simple reads/writes atomic enough here.

class _S:
    # Connection flags
    cortex_connected  = False
    headset_connected = False
    session_active    = False
    profile_loaded    = False
    drone_connected   = False
    # Headset
    headset_id      = "—"
    headset_battery = 5          # 0-100
    # Mental command (latest)
    current_action     = "—"
    current_confidence = 0.0
    # Confidence history
    _t0    = time.time()
    conf_x: collections.deque = collections.deque(maxlen=_HISTORY)
    conf_y: collections.deque = collections.deque(maxlen=_HISTORY)
    # Drone
    last_cmd_sent       = "—"
    last_drone_response = "—"

    @classmethod
    def push_conf(cls, action: str, conf: float) -> None:
        cls.current_action     = action
        cls.current_confidence = conf
        cls.conf_x.append(time.time() - cls._t0)
        cls.conf_y.append(conf)


# Drone proxy

class _GUIDrone:
    """Wraps TelloDriver to capture the last sent command and drone response."""

    def __init__(self, driver):
        self._d = driver

    async def run_command(self, cmd: str):
        _S.last_cmd_sent = cmd
        resp = await self._d.run_command(cmd)
        _S.last_drone_response = resp or "—"
        return resp

    def close(self):
        self._d.close()


# Extended CortexClient

class _GUICortex(CortexClient):
    """Subclass of CortexClient that mirrors state into _S for the GUI."""

    _loop: asyncio.AbstractEventLoop = None  # set by _run_backend before WS starts

    def _run_drone_command(self, cmd: str) -> None:
        """Schedule the drone coroutine on the backend event loop (called from WS thread)."""
        if _GUICortex._loop is not None:
            asyncio.run_coroutine_threadsafe(self.drone.run_command(cmd), _GUICortex._loop)

    # WebSocket lifecycle

    def on_open(self, ws):
        _S.cortex_connected = True
        super().on_open(ws)

    def on_close(self, ws, code, msg):
        _S.cortex_connected  = False
        _S.headset_connected = False
        _S.session_active    = False
        _S.profile_loaded    = False
        super().on_close(ws, code, msg)

    # Stream data

    def handle_mental_command(self, data):
        com = data.get("com")
        if com and len(com) >= 2:
            _S.push_conf(com[0], com[1])
        if self.drone is not None:
            super().handle_mental_command(data)

    # Setup (mirrors cortex.py setup_flow, updating _S along the way)

    def setup_flow(self):
        try:
            self._rpc_call("getCortexInfo", {})
            # 2) authorize -> token (try RPC first, fall back to ENV)

            try:
                self._rpc_call("requestAccess", {
                    "clientId": CLIENT_ID,
                    "clientSecret": CLIENT_SECRET
                })
                auth = self._rpc_call("authorize", {
                    "clientId": CLIENT_ID,
                    "clientSecret": CLIENT_SECRET,
                    "debit": 0
                })
                self.cortex_token = auth["cortexToken"]
                with open(".env", "w") as file:
                    file.write(f"AUTH={self.cortex_token}")
                print("[Auth] Got cortexToken via RPC.")
            except Exception as e:
                print(f"[Auth] RPC auth failed ({e}), falling back to ENV...")
                self.cortex_token = os.getenv("AUTH")
                if not self.cortex_token:
                    raise RuntimeError("No cortexToken available: RPC failed and AUTH env var is not set.") from e
                print("[Auth] Got cortexToken from environment.")

            # Discover headset
            headsets = self._rpc_call("queryHeadsets", {})
            if not headsets:
                raise RuntimeError(
                    "No headsets found. Turn on headset and connect in Emotiv Launcher first."
                )
            h = headsets[0]
            self.headset_id     = h["id"]
            _S.headset_id       = h["id"]
            # Battery field name varies by Cortex version
            _S.headset_battery  = h.get("Battery") or h.get("batteryPercent") or 0
            _S.headset_connected = True

            self._rpc_call("controlDevice", {
                "command": "connect",
                "headset": self.headset_id,
            })
            time.sleep(1.5)

            for attempt in range(3):
                try:
                    session = self._rpc_call("createSession", {
                        "cortexToken": self.cortex_token,
                        "headset":     self.headset_id,
                        "status":      "active",
                    })
                    break
                except RuntimeError as e:
                    err = str(e)
                    if ("-32602" in err or "-32050" in err) and attempt == 0:
                        print("[Auth] Token rejected — re-authenticating…")
                        os.environ.pop("AUTH", None)
                        self.cortex_token = self._fetch_token(force_refresh=True)
                    elif "-32019" in err and attempt < 2:
                        print("[Session] Session limit reached — closing existing sessions…")
                        try:
                            sessions = self._rpc_call("querySessions", {
                                "cortexToken": self.cortex_token,
                            })
                            for s in (sessions or []):
                                if s.get("status") != "closed":
                                    self._rpc_call("updateSession", {
                                        "cortexToken": self.cortex_token,
                                        "session":     s["id"],
                                        "status":      "closed",
                                    })
                                    print(f"[Session] Closed {s['id']}")
                        except Exception as se:
                            print(f"[Session] Could not close sessions: {se}")
                    else:
                        raise
            self.session_id  = session["id"]
            _S.session_active = True

            # Load profile (unload + reload if already loaded)
            for attempt in range(2):
                try:
                    self._rpc_call("setupProfile", {
                        "cortexToken": self.cortex_token,
                        "headset":     self.headset_id,
                        "profile":     PROFILE_NAME,
                        "status":      "load",
                    })
                    break
                except Exception as e:
                    if attempt == 0 and "-32127" in str(e):
                        self._rpc_call("setupProfile", {
                            "cortexToken": self.cortex_token,
                            "headset":     self.headset_id,
                            "profile":     PROFILE_NAME,
                            "status":      "unload",
                        })
                        time.sleep(1)
                    else:
                        raise
            _S.profile_loaded = True

            self._rpc_call("subscribe", {
                "cortexToken": self.cortex_token,
                "session":     self.session_id,
                "streams":     ["dev","com"],
            })
            print("\n[GUI] All systems ready. Waiting for mental commands…\n")

        except Exception as e:
            print(f"[Setup] Failed: {e}")
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass


# Build the GUI layout

def _build_gui():
    dpg.create_context()
    dpg.create_viewport(
        title="BCI Drone Control - Team20",
        width=920, height=640,
        resizable=True,
        min_width=720, min_height=520,
    )
    dpg.setup_dearpygui()

    with dpg.window(tag="root", no_title_bar=True, no_move=True,
                    no_resize=True, no_scrollbar=True):

        # Title row
        dpg.add_text("  BCI Drone Control  ·  Team 20  ·  UNO 2026", color=_WHITE)
        dpg.add_separator()
        dpg.add_spacer(height=5)

        # Three-column body
        with dpg.group(horizontal=True):

            # LEFT: connection status + headset info
            with dpg.child_window(tag="left", width=215, height=375,
                                  border=True, no_scrollbar=True):
                dpg.add_text("CONNECTION STATUS", color=_GREY)
                dpg.add_separator()
                dpg.add_spacer(height=3)

                for attr, label in [
                    ("cortex_connected",  "Cortex WS"),
                    ("headset_connected", "Headset"),
                    ("session_active",    "Session"),
                    ("profile_loaded",    f"Profile ({PROFILE_NAME})"),
                    ("drone_connected",   "Drone UDP"),
                ]:
                    with dpg.group(horizontal=True):
                        dpg.add_text("●", tag=f"dot_{attr}", color=_RED)
                        dpg.add_text(f"  {label}", color=_WHITE)

                dpg.add_spacer(height=12)
                dpg.add_separator()
                dpg.add_text("HEADSET INFO", color=_GREY)
                dpg.add_separator()
                dpg.add_spacer(height=3)

                dpg.add_text("ID", color=_GREY)
                dpg.add_text("—", tag="hid_text")
                dpg.add_spacer(height=8)
                dpg.add_text("Battery", color=_GREY)
                dpg.add_progress_bar(
                    tag="bat_bar", default_value=0.0,
                    width=-1, overlay="—",
                )

            dpg.add_spacer(width=6)

            # CENTER: mental command display
            with dpg.child_window(tag="center", width=385, height=375,
                                  border=True, no_scrollbar=True):
                dpg.add_text("MENTAL COMMAND", color=_GREY)
                dpg.add_separator()
                dpg.add_spacer(height=20)

                # Command "display box"
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=75)
                    with dpg.child_window(width=220, height=62,
                                         border=True, no_scrollbar=True):
                        dpg.add_spacer(height=10)
                        with dpg.group(horizontal=True):
                            dpg.add_spacer(width=16)
                            dpg.add_text("—", tag="cmd_text", color=_GREY)

                dpg.add_spacer(height=16)
                dpg.add_text("Confidence", color=_GREY)
                dpg.add_progress_bar(
                    tag="conf_bar", default_value=0.0,
                    width=-1, overlay="0.00",
                )
                dpg.add_spacer(height=4)
                with dpg.group(horizontal=True):
                    dpg.add_text("Threshold:", color=_GREY)
                    dpg.add_text(f"  {CONFIDENCE_THRESHOLD:.2f}", color=_YELLOW)

                dpg.add_spacer(height=20)
                dpg.add_separator()
                dpg.add_text("LAST SENT TO DRONE", color=_GREY)
                dpg.add_spacer(height=5)
                dpg.add_text("—", tag="sent_text", color=_BLUE)

            dpg.add_spacer(width=6)

            # RIGHT: drone readouts
            with dpg.child_window(tag="right", width=215, height=375,
                                  border=True, no_scrollbar=True):
                dpg.add_text("DRONE STATUS", color=_GREY)
                dpg.add_separator()
                dpg.add_spacer(height=6)

                dpg.add_text("Target IP", color=_GREY)
                dpg.add_text(DRONE_IP, color=_WHITE)
                dpg.add_text(f"Port  {DRONE_PORT}", color=_GREY)
                dpg.add_spacer(height=12)
                dpg.add_separator()

                dpg.add_text("Last Command", color=_GREY)
                dpg.add_text("—", tag="dcmd_text", color=_BLUE)
                dpg.add_spacer(height=10)

                dpg.add_text("Last Response", color=_GREY)
                dpg.add_text("—", tag="dresp_text", color=_GREEN)
                dpg.add_spacer(height=12)
                dpg.add_separator()

                dpg.add_text("Cooldown", color=_GREY)
                dpg.add_text(f"{OUTPUT_COOLDOWN_SEC} s", color=_GREY)
                dpg.add_spacer(height=6)
                dpg.add_text("Actions", color=_GREY)
                for bci_act, drone_cmd in ACTION_TO_OUTPUT.items():
                    dpg.add_text(
                        f"  {bci_act} → {drone_cmd or '(none)'}",
                        color=_GREY,
                    )

        # Confidence history plot
        dpg.add_spacer(height=6)
        with dpg.plot(label="Confidence History", height=160,
                      width=-1, tag="cplot", no_menus=True):
            dpg.add_plot_legend()
            dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="cx_axis")
            dpg.add_plot_axis(dpg.mvYAxis, label="",         tag="cy_axis")
            dpg.set_axis_limits("cy_axis", 0.0, 1.05)

            dpg.add_line_series(
                [0.0], [0.0],
                label="Confidence",
                parent="cy_axis", tag="conf_series",
            )
            dpg.add_line_series(
                [0.0, 1.0], [CONFIDENCE_THRESHOLD, CONFIDENCE_THRESHOLD],
                label=f"Threshold ({CONFIDENCE_THRESHOLD})",
                parent="cy_axis", tag="thresh_series",
            )

    dpg.set_primary_window("root", True)
    dpg.show_viewport()


# Per-frame update (always called from the main thread)

def _update_gui():
    # Connection dots
    for attr in ("cortex_connected", "headset_connected", "session_active",
                 "profile_loaded",   "drone_connected"):
        dpg.configure_item(
            f"dot_{attr}",
            color=_GREEN if getattr(_S, attr) else _RED,
        )

    # Headset
    raw_id = _S.headset_id
    dpg.set_value("hid_text", raw_id[:24] if len(raw_id) > 24 else raw_id)
    batt_frac = min(max(_S.headset_battery / 100.0, 0.0), 1.0)
    dpg.set_value("bat_bar", batt_frac)
    dpg.configure_item("bat_bar", overlay=f"{_S.headset_battery}%")

    # Command text + colour
    action  = _S.current_action
    display = action.upper() if action != "—" else "—"
    dpg.set_value("cmd_text", display)
    if action in ("—", "neutral"):
        cmd_col = _GREY
    elif _S.current_confidence >= CONFIDENCE_THRESHOLD:
        cmd_col = _GREEN
    else:
        cmd_col = _YELLOW
    dpg.configure_item("cmd_text", color=cmd_col)

    # Confidence bar
    conf = _S.current_confidence
    dpg.set_value("conf_bar", conf)
    dpg.configure_item("conf_bar", overlay=f"{conf:.2f}")

    # Sent command
    dpg.set_value("sent_text", _S.last_cmd_sent)

    # Drone panel
    dpg.set_value("dcmd_text",  _S.last_cmd_sent)
    resp = _S.last_drone_response
    dpg.set_value("dresp_text", resp)
    if resp == "ok":
        resp_col = _GREEN
    elif resp in ("TIMEOUT", "error"):
        resp_col = _RED
    else:
        resp_col = _WHITE
    dpg.configure_item("dresp_text", color=resp_col)

    # Confidence plot (scrolling 30-second window)
    xs = list(_S.conf_x)
    ys = list(_S.conf_y)
    if xs:
        dpg.set_value("conf_series", [xs, ys])
        x_max = xs[-1]
        x_min = max(0.0, x_max - 30.0)
        dpg.set_axis_limits("cx_axis", x_min, x_max + 0.5)
        dpg.set_value(
            "thresh_series",
            [[x_min, x_max + 0.5],
             [CONFIDENCE_THRESHOLD, CONFIDENCE_THRESHOLD]],
        )


# Backend: WebSocket + drone (runs in its own daemon thread)

def _run_backend():
    async def _async_main():
        try:
            _GUICortex._loop = asyncio.get_running_loop()

            # Connect drone (non-fatal if unavailable)
            drone = None
            try:
                raw = await create_drone(DRONE_IP, DRONE_PORT)
                drone = _GUIDrone(raw)
                _S.drone_connected = True
                await drone.run_command("command")
                print("[Drone] Connected.")
                await asyncio.sleep(1)
                # await drone.run_command("takeoff")
            except Exception as e:
                print(f"[Drone] Could not connect: {e}. Running in headset-only mode.")

            # Cortex reconnect loop — retries automatically after any disconnect/auth failure
            while dpg.is_dearpygui_running():
                client = _GUICortex(drone)

                ws_app = websocket.WebSocketApp(
                    CORTEX_URL,
                    on_open=client.on_open,
                    on_message=client.on_message,
                    on_error=client.on_error,
                    on_close=client.on_close,
                )
                ws_thread = threading.Thread(
                    target=ws_app.run_forever,
                    kwargs={
                        "sslopt": {
                            "cert_reqs":      ssl.CERT_NONE,
                            "check_hostname": False,
                        },
                        "ping_interval": 10,
                        "ping_timeout":  5,
                    },
                    daemon=True,
                )
                ws_thread.start()

                # Wait for this WS session to finish
                while ws_thread.is_alive() and dpg.is_dearpygui_running():
                    await asyncio.sleep(0.5)

                if dpg.is_dearpygui_running():
                    print("[Backend] Cortex disconnected — retrying in 10 s…")
                    await asyncio.sleep(10)
        finally:
            # Graceful shutdown
            if drone:
                print("[Drone] Landing…")
                await drone.run_command("land")
                drone.close()

    asyncio.run(_async_main())


# Entry point

if __name__ == "__main__":
    _build_gui()

    backend = threading.Thread(target=_run_backend, daemon=True)
    backend.start()

    # Dear PyGui render loop on the main thread
    while dpg.is_dearpygui_running():
        _update_gui()
        dpg.render_dearpygui_frame()

    dpg.destroy_context()
