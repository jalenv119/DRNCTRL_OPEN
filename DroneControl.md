## Drone SDK

Tello/Ryze has some documentation [here](https://dl-cdn.ryzerobotics.com/downloads/Tello/Tello%20SDK%202.0%20User%20Guide.pdf).  
This seems to function by sending utf-8 encoded text, and all interpretation is performed on the drone, so this document is paramount. We can most likely adjust the code to be performed with a different language, since it isn’t dependent on an api or something.

### Basic driver overview (`tello_driver.py`)

`TelloDriver` is an asyncio `DatagramProtocol` that:

- Opens a UDP socket bound locally (default port `9000`)
- Sends plain-text commands to the drone (e.g. `"command"`, `"takeoff"`, `"land"`)
- Waits for a response from the drone and returns it (or `"TIMEOUT"` on no reply)

**Key methods:**
- `create_drone(ip, drone_port)` — creates the driver, binds local socket, and sends `"command"` to initialize SDK mode.
- `run_command(command, timeout=7)` — sends a command and waits for the drone response.
- `close()` — closes the UDP connection cleanly.

### Example control flow + error handling (`drone.py`)

The example script uses `try/except/finally` to ensure the drone is commanded to land and the connection is cleaned up even if an error occurs. Note that `"land"` will error if the drone is not flying, so it’s wrapped in the `finally` block as a best-effort safe shutdown.

```python
import asyncio
from tello_driver import create_drone

async def main():
    # Initialize the drone connection (assumes Station Mode on your router)
    drone1 = await create_drone("192.168.10.1", 8889)

    try:
        await drone1.run_command("battery?")
        # Add more commands here to test functionality
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Ensure the drone lands safely and the socket is cleaned up
        await drone1.run_command("land")
        drone1.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```
TODO: add automated testing and simulation?