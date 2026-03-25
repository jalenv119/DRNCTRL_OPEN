import json
import ssl
import time
import threading
import asyncio 

from websocket import WebSocketApp
from tello_driver import create_drone

CORTEX_URL = "wss://localhost:6868"

DRONE_IP   = "192.168.10.1"
DRONE_PORT = 8889

CLIENT_ID = "xxx".strip()
CLIENT_SECRET = "xxx".strip()

# trained profile name in Emotiv
PROFILE_NAME = "jalen"

# Output mapping:

# Arbitrary values for now
ACTION_TO_OUTPUT = {
    "lift": "UP 20",
    "drop": "DOWN 20",
    "neutral": "HOVER",
}

CONFIDENCE_THRESHOLD = 0.65  # tweak 0.55-0.75
PRINT_NEUTRAL_ALWAYS = True  # if False, only prints HOVER when confidence passes threshold too

# Cooldown so it doesn't spam output
OUTPUT_COOLDOWN_SEC = 0.35

# Cortex JSON-RPC Client

class CortexClient:
    def __init__(self, drone):
        self.ws = None
        self.drone = drone
        self.next_id = 1

        self._pending = {}         # id -> {"event": Event, "resp": dict}
        self._pending_lock = threading.Lock()

        self.cortex_token = None
        self.headset_id = None
        self.session_id = None

        self._last_output_time = 0.0
        self._last_output = None

    def _rpc_call(self, method, params=None, timeout=12):
        """Send a JSON-RPC request and wait for its response."""
        if params is None:
            params = {}

        req_id = self.next_id
        self.next_id += 1

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        ev = threading.Event()
        with self._pending_lock:
            self._pending[req_id] = {"event": ev, "resp": None}

        self.ws.send(json.dumps(payload))

        ok = ev.wait(timeout=timeout)
        if not ok:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"Timed out waiting for response to {method}")

        with self._pending_lock:
            resp = self._pending.pop(req_id)["resp"]

        if "error" in resp:
            raise RuntimeError(f"{method} error: {resp['error']}")
        return resp.get("result")

    def on_open(self, ws):
        print("[WS] Connected to Cortex.")
        self.ws = ws

        # Kick off setup in a thread so we don't block websocket callbacks
        threading.Thread(target=self.setup_flow, daemon=True).start()

    def on_close(self, ws, code, msg):
        print(f"[WS] Closed. code={code}, msg={msg}")
        self.ws = None
        self.cortex_token = None
        self.headset_id = None
        self.session_id = None

    def on_error(self, ws, err):
        print(f"[WS] Error: {err}")

    def on_message(self, ws, message):
        data = json.loads(message)

        # Response to an RPC call
        if "id" in data:
            req_id = data["id"]
            with self._pending_lock:
                if req_id in self._pending:
                    self._pending[req_id]["resp"] = data
                    self._pending[req_id]["event"].set()
            return

        # Stream data (mentalCommand)
        # Typical format: {"sid":"...","time":...,"com":["push",0.82],"met":[...]}
        if "com" in data:
            self.handle_mental_command(data)

    # Mental command handling

    def handle_mental_command(self, data):
        com = data.get("com")
        if not com or len(com) < 2:
            return

        action = com[0]
        confidence = com[1]

        # Map to UP/DOWN/HOVER
        out = ACTION_TO_OUTPUT.get(action)
        if out is None:
            # Unknown action; ignore (or print for debugging)
            # print(f"[MC] Unmapped action: {action} ({confidence:.2f})")
            return

        # Confidence filtering:
        if out != "HOVER":
            if confidence < CONFIDENCE_THRESHOLD:
                return
        else:
            if (not PRINT_NEUTRAL_ALWAYS) and (confidence < CONFIDENCE_THRESHOLD):
                return

        # Cooldown + de-dup spam reduction
        now = time.time()
        if now - self._last_output_time < OUTPUT_COOLDOWN_SEC and out == self._last_output:
            return

        self._last_output_time = now
        self._last_output = out

        print(f"[Drone] Sending: {out}")
        asyncio.run(self.drone.run_command(out))


    # Setup flow
  
    def setup_flow(self):
        try:
            # 1) basic info (optional but good sanity check)
            info = self._rpc_call("getCortexInfo", {})
            # print("[CortexInfo]", info)

            # 2) request access (may prompt approval in Launcher)
            self._rpc_call("requestAccess", {
                "clientId": CLIENT_ID,
                "clientSecret": CLIENT_SECRET
            })

            # 3) authorize -> token
            auth = self._rpc_call("authorize", {
                "clientId": CLIENT_ID,
                "clientSecret": CLIENT_SECRET,
                "debit": 1
            })
            self.cortex_token = auth["cortexToken"]
            print("[Auth] Got cortexToken.")

            # 4) find headset
            headsets = self._rpc_call("queryHeadsets", {})
            if not headsets:
                raise RuntimeError("No headsets found. Turn on headset + connect in Emotiv Launcher first.")
            # pick first headset
            self.headset_id = headsets[0]["id"]
            print(f"[Headset] Using headset id: {self.headset_id}")

            # 5) connect headset (controlDevice)
            self._rpc_call("controlDevice", {
                "command": "connect",
                "headset": self.headset_id
            })
            print("[Headset] connect command sent.")

            # Give it time to fully connect
            time.sleep(1.5)

            # 6) create session
            session = self._rpc_call("createSession", {
                "cortexToken": self.cortex_token,
                "headset": self.headset_id,
                "status": "active"
            })
            self.session_id = session["id"]
            print(f"[Session] Active session: {self.session_id}")

            # 7) load profile
            try:
                self._rpc_call("setupProfile", {
                    "cortexToken": self.cortex_token,
                    "headset": self.headset_id,
                    "profile": PROFILE_NAME,
                    "status": "load"
                })
                print(f"[Profile] Loaded profile: {PROFILE_NAME}")

            except Exception as e:
                if "-32127" in str(e):
                    print("[Profile] Already loaded. Unloading first...")

                    # unload
                    self._rpc_call("setupProfile", {
                        "cortexToken": self.cortex_token,
                        "headset": self.headset_id,
                        "profile": PROFILE_NAME,
                        "status": "unload"
                    })

                    time.sleep(1)

                    # load again
                    self._rpc_call("setupProfile", {
                        "cortexToken": self.cortex_token,
                        "headset": self.headset_id,
                        "profile": PROFILE_NAME,
                        "status": "load"
                    })

                    print(f"[Profile] Reloaded profile: {PROFILE_NAME}")
                else:
                    raise

            # 8) subscribe to mentalCommand stream
            self._rpc_call("subscribe", {
                "cortexToken": self.cortex_token,
                "session": self.session_id,
                "streams": ["com"]
            })
            print("[Subscribe] mentalCommand stream subscribed.")
            print("\n--- Put on the headset. Think your trained actions. Output will be: UP / DOWN / HOVER ---\n")

        except Exception as e:
            print(f"[Setup] Failed: {e}")
            print("Tip: Make sure Emotiv Launcher shows the headset connected, and your profile name matches exactly.")
            # Close websocket to trigger (future) reconnect loop
            try:
                if self.ws:
                    self.ws.close()
            except:
                pass


async def run_once():
    drone = await create_drone(DRONE_IP, DRONE_PORT)
    print("[Drone] Connected.")

    client = CortexClient(drone)

    ws_app = WebSocketApp(
        CORTEX_URL,
        on_open=client.on_open,
        on_message=client.on_message,
        on_error=client.on_error,
        on_close=client.on_close,
    )

    ws_thread = threading.Thread(
        target=ws_app.run_forever,
        kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE, "check_hostname": False},
                "ping_interval": 10, "ping_timeout": 5},
        daemon=True
    )
    ws_thread.start()

    try:
        ws_thread.join()  # block until websocket closes
    finally:
        print("[Drone] Landing...")
        await drone.run_command("land")
        drone.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_once())
    except KeyboardInterrupt:
        pass