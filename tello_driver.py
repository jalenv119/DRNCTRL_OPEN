import asyncio

class TelloDriver(asyncio.DatagramProtocol):
    def __init__(self, drone_ip, drone_port):
        self.drone_addr = (drone_ip, drone_port)
        self.transport = None
        self.response_event = asyncio.Event()
        self.last_response = None

    def connection_made(self, transport):
        """Represents the UDP socket connection to the drone. Called when the connection is established."""
        self.transport = transport 

    def datagram_received(self, data, addr):
        """Callback for when a response is received from the drone. Sets the last_response and triggers the event."""
        self.last_response = data.decode().strip()
        print(f"-> [{self.drone_addr[0]}] {self.last_response}")
        self.response_event.set()

    async def run_command(self, command, timeout=7):
        """The main method you will call from your control code."""
        self.response_event.clear()
        self.transport.sendto(command.encode(), self.drone_addr)
        
        try:
            await asyncio.wait_for(self.response_event.wait(), timeout=timeout)
            return self.last_response
        except asyncio.TimeoutError:
            return "TIMEOUT"
    
    def close(self):
        """Clean up the transport connection when done. Always call this when finished with the drone."""
        if self.transport:
            self.transport.close()
            print(f"[{self.drone_addr[0]}] Socket closed.")

async def create_drone(ip, drone_port):
    """Helper function to initialize a drone object properly."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: TelloDriver(ip,drone_port),
        # Using lamnbda to pass parameters to the protocol constructor
        local_addr=('0.0.0.0', 0)
    )
    # Vital: Initialize SDK mode immediately
    await protocol.run_command("command")
    return protocol