import asyncio
from tello_driver import create_drone

async def main():
    # 1. Initialize as objects
    # (Assumes drones are in Station Mode on your router)
    drone1 = await create_drone("192.168.10.1", 8889)

    try:
        await drone1.run_command("battery?")
        # You can add more commands here to test the drone's functionality
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await drone1.run_command("land")  # Ensure the drone lands safely
            # This will throw an error if ts hasn't taken off
        drone1.close() # Cleanup connection
    

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
