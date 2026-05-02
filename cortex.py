import json
import ssl
import time
import threading
import asyncio
import os
from dotenv import load_dotenv

import websocket
from tello_driver import create_drone

load_dotenv()
CORTEX_URL = "wss://localhost:6868"

DRONE_IP   = "192.168.10.1"
DRONE_PORT = 8889


CLIENT_ID = "REPLACE".strip()
CLIENT_SECRET = "REPLACE".strip()

# trained profile name in Emotiv
PROFILE_NAME = "REPLACE"

# Output mapping:

# Arbitrary values for now
ACTION_TO_OUTPUT = {
    "lift": "takeoff",
    "push": "forward 40",
    "pull": "backward 40",
    "neutral": "",
}

CONFIDENCE_THRESHOLD = 0.40 # tweak 0.55-0.75
PRINT_NEUTRAL_ALWAYS = False# if False, only prints HOVER when confidence passes threshold too

# Cooldown so it doesn't spam output
OUTPUT_COOLDOWN_SEC = 0.2

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

    def _fetch_token(self, force_refresh=False):
        token = None if force_refresh else os.getenv("AUTH")
        if token:
            token = token.strip() or None
        if not token:
            # requestAccess registers/prompts approval in Emotiv Launcher
            access = self._rpc_call("requestAccess", {
                "clientId": CLIENT_ID,
                "clientSecret": CLIENT_SECRET,
            })
            if not access.get("accessGranted"):
                print("[Auth] App not yet approved. Open Emotiv Launcher and click 'Allow'…")
                for _ in range(60):  # up to 5 minutes
                    time.sleep(5)
                    access = self._rpc_call("requestAccess", {
                        "clientId": CLIENT_ID,
                        "clientSecret": CLIENT_SECRET,
                    })
                    if access.get("accessGranted"):
                        print("[Auth] Access granted!")
                        break
                else:
                    raise RuntimeError("Emotiv Launcher access not granted after 5 minutes.")

            # authorize — retry once if Cortex says not approved yet
            auth = None
            for attempt in range(2):
                try:
                    auth = self._rpc_call("authorize", {
                        "clientId": CLIENT_ID,
                        "clientSecret": CLIENT_SECRET,
                        "debit": 1,
                    })
                    break
                except RuntimeError as e:
                    if "-32102" in str(e) and attempt == 0:
                        print("[Auth] Cortex rejected authorize — waiting for Emotiv Launcher approval…")
                        for _ in range(60):
                            time.sleep(5)
                            access = self._rpc_call("requestAccess", {
                                "clientId": CLIENT_ID,
                                "clientSecret": CLIENT_SECRET,
                            })
                            if access.get("accessGranted"):
                                break
                    else:
                        raise

            token = auth["cortexToken"]
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            with open(env_path, "w") as f:
                f.write(f"AUTH={token}\n")
            os.environ["AUTH"] = token
        return token

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

    def _run_drone_command(self, cmd: str) -> None:
        """Execute a drone command. Subclasses can override for different event-loop contexts.
           Now featuring thread saftey!
        """
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(self.drone.run_command(cmd), loop)

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
        self._run_drone_command(out)


    # Setup flow
  
    def setup_flow(self):
        try:
            # 1) basic info (optional but good sanity check)
            info = self._rpc_call("getCortexInfo", {})
            print("[CortexInfo]", info)

            # 2) request access (may prompt approval in Launcher)
            # self._rpc_call("requestAccess", {
            #     "clientId": CLIENT_ID,
            #     "clientSecret": CLIENT_SECRET
            # })

            # 3) authorize -> token
            self.cortex_token = self._fetch_token()
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

            # 6) create session (retry on stale token or session limit)
            for attempt in range(3):
                try:
                    session = self._rpc_call("createSession", {
                        "cortexToken": self.cortex_token,
                        "headset": self.headset_id,
                        "status": "active",
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
    loop = asyncio.get_event_loop()
    drone = await create_drone(DRONE_IP, DRONE_PORT)
    print("[Drone] Connected.")
    client = CortexClient(drone)

    ws_app = websocket.WebSocketApp(
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

    await drone.run_command("command")
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
