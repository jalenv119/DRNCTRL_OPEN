import asyncio
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ws_mod = types.ModuleType("websocket")
ws_mod.WebSocketApp = MagicMock()
sys.modules["websocket"] = ws_mod

from cortex import CortexClient
import Drone as _drone_mod
from tello_driver import TelloDriver


def run(coro):
    return asyncio.run(coro)


def mock_drone():
    d = MagicMock()
    d.run_command = AsyncMock()
    d.close = MagicMock()
    return d


def make_client():
    client = CortexClient(mock_drone())
    client.ws = MagicMock()
    return client


class TestCortexSendingMessages(unittest.TestCase):
    def test_handle_mental_command_sends_drone_command(self):
        client = make_client()
        client.handle_mental_command({"com": ["lift", 0.90]})
        client.drone.run_command.assert_awaited_once_with("UP 20")


class TestCortexRpcCall(unittest.TestCase):
    def test_rpc_call_sends_correct_json(self):
        client = make_client()

        def fake_send(msg):
            payload = json.loads(msg)
            with client._pending_lock:
                entry = client._pending[payload["id"]]
            entry["resp"] = {"id": payload["id"], "result": {"ok": True}}
            entry["event"].set()

        client.ws.send.side_effect = fake_send
        result = client._rpc_call("testMethod", {"foo": "bar"})

        sent = json.loads(client.ws.send.call_args[0][0])
        self.assertEqual(sent["jsonrpc"], "2.0")
        self.assertEqual(sent["method"], "testMethod")
        self.assertEqual(sent["params"], {"foo": "bar"})
        self.assertEqual(result, {"ok": True})


class TestDroneMain(unittest.TestCase):
    def test_main_sends_battery_and_lands_and_closes(self):
        d = mock_drone()

        with patch("Drone.create_drone", new=AsyncMock(return_value=d)):
            run(_drone_mod.main())

        d.run_command.assert_any_await("battery?")
        d.run_command.assert_any_await("land")
        d.close.assert_called_once()


class TestTelloDriverInit(unittest.TestCase):
    def test_init_sets_expected_values(self):
        d = TelloDriver("192.168.10.1", 8889)
        self.assertEqual(d.drone_addr, ("192.168.10.1", 8889))
        self.assertIsNone(d.transport)
        self.assertIsNone(d.last_response)
        self.assertFalse(d.response_event.is_set())


class TestTelloConnections(unittest.TestCase):
    def test_connection_made_stores_transport(self):
        d = TelloDriver("192.168.10.1", 8889)
        transport = MagicMock()
        d.connection_made(transport)
        self.assertIs(d.transport, transport)

    def test_datagram_received_stores_response_and_sets_event(self):
        d = TelloDriver("192.168.10.1", 8889)
        d.datagram_received(b"ok", ("192.168.10.1", 8889))
        self.assertEqual(d.last_response, "ok")
        self.assertTrue(d.response_event.is_set())


if __name__ == "__main__":
    unittest.main()