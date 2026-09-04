from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest import mock


PACKAGE_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hermes_intercom_test"


def load_plugin(home: Path):
    constants = ModuleType("hermes_constants")
    setattr(constants, "get_hermes_home", lambda: str(home))
    sys.modules["hermes_constants"] = constants
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.home = Path(self.tmp.name) / ".hermes"
        self.home.mkdir()
        self.plugin = load_plugin(self.home)

    def tearDown(self):
        self.plugin.inbox.stop()
        for name in list(sys.modules):
            if name == PACKAGE_NAME or name.startswith(f"{PACKAGE_NAME}."):
                sys.modules.pop(name, None)
        sys.modules.pop("hermes_constants", None)
        self.tmp.cleanup()

    def test_ask_sends_request_metadata(self):
        captured = {}

        def fake_send_to(target, message, from_name, from_cwd, payload_extra=None):
            captured.update(
                target=target,
                message=message,
                from_name=from_name,
                from_cwd=from_cwd,
                payload_extra=payload_extra,
            )
            request_id = payload_extra["request_id"]
            self.plugin.inbox.resolve_ask(request_id, "pong")
            return {"ok": True, "receipt": "delivered"}

        with mock.patch.object(self.plugin.inbox, "send_to", fake_send_to):
            result = self.plugin.inbox.ask_to("peer", "ping", "sender", "/tmp", timeout=10)

        self.assertEqual(
            result,
            {"ok": True, "reply": "pong", "from": "peer", "action": "ask"},
        )
        payload = cast(dict[str, Any], captured["payload_extra"])
        self.assertEqual(payload["type"], "ask")
        self.assertTrue(payload["request_id"])

    def test_reply_action_routes_to_pending_request(self):
        captured = {}

        def fake_reply_to(target, request_id, message, from_name, from_cwd):
            captured.update(
                target=target,
                request_id=request_id,
                message=message,
                from_name=from_name,
                from_cwd=from_cwd,
            )
            return {"ok": True, "receipt": "delivered"}

        with (
            mock.patch.object(
                self.plugin.inbox,
                "self_meta",
                return_value={"name": "receiver", "cwd": "/tmp"},
            ),
            mock.patch.object(
                self.plugin.inbox,
                "reply_to",
                fake_reply_to,
                create=True,
            ),
        ):
            result = json.loads(
                self.plugin._handle(
                    {
                        "action": "reply",
                        "to": "sender",
                        "request_id": "request-123",
                        "message": "pong",
                    }
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            captured,
            {
                "target": "sender",
                "request_id": "request-123",
                "message": "pong",
                "from_name": "receiver",
                "from_cwd": "/tmp",
            },
        )

    def test_ask_envelope_tells_receiver_to_use_reply_action(self):
        delivered = {}

        def capture_delivery(text):
            delivered["text"] = text
            return "submitted_as_turn"

        server, client = self.plugin.inbox.socket.socketpair()
        with mock.patch.object(self.plugin.inbox, "_deliver_local", capture_delivery):
            worker = threading.Thread(target=self.plugin.inbox._serve, args=(server,))
            worker.start()
            client.sendall(
                (
                    json.dumps(
                        {
                            "type": "ask",
                            "from_name": "sender",
                            "from_cwd": "/tmp",
                            "message": "ping",
                            "request_id": "request-123",
                        }
                    )
                    + "\n"
                ).encode()
            )
            response = json.loads(client.makefile().readline())
            worker.join(timeout=2)
            client.close()

        self.assertTrue(response["ok"])
        self.assertIn('intercom(action="reply"', delivered["text"])
        self.assertIn('request_id="request-123"', delivered["text"])
        self.assertIn('to="sender"', delivered["text"])

    def test_rejects_peer_with_different_uid(self):
        server, client = self.plugin.inbox.socket.socketpair()
        with mock.patch.object(
            self.plugin.inbox,
            "_peer_uid",
            return_value=self.plugin.inbox.os.getuid() + 1,
        ):
            worker = threading.Thread(target=self.plugin.inbox._serve, args=(server,))
            worker.start()
            client.sendall(
                (json.dumps({"type": "send", "message": "ping"}) + "\n").encode()
            )
            response = json.loads(client.makefile().readline())
            worker.join(timeout=2)
            client.close()

        self.assertFalse(response["ok"])
        self.assertIn("uid", response["error"])

    def test_restart_recovers_spool_for_same_hermes_session(self):
        with mock.patch.dict(
            self.plugin.inbox.os.environ,
            {"HERMES_SESSION_ID": "durable-session-123"},
        ):
            self.plugin.inbox.start(name="lab")
            self.assertEqual(
                self.plugin.inbox.self_meta()["session_id"],
                "durable-session-123",
            )
            self.plugin.inbox._spool_append("survives restart")
            self.plugin.inbox.stop()
            self.plugin.inbox.start(name="lab")

        self.assertEqual(self.plugin.inbox.take_pending(), ["survives restart"])

    def test_same_uid_socket_round_trip(self):
        delivered = {}

        def capture_delivery(text):
            delivered["text"] = text
            return "submitted_as_turn"

        self.plugin.inbox.start(name="receiver", session_id="receiver-session")
        socket_path = self.plugin.inbox.self_meta()["socket"]
        with mock.patch.object(self.plugin.inbox, "_deliver_local", capture_delivery):
            with self.plugin.inbox.socket.socket(
                self.plugin.inbox.socket.AF_UNIX,
                self.plugin.inbox.socket.SOCK_STREAM,
            ) as client:
                client.connect(socket_path)
                client.sendall(
                    (
                        json.dumps(
                            {
                                "type": "send",
                                "from_name": "sender",
                                "from_cwd": "/tmp",
                                "message": "ping",
                            }
                        )
                        + "\n"
                    ).encode()
                )
                response = json.loads(client.makefile().readline())

        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], "submitted_as_turn")
        self.assertIn("ping", delivered["text"])


if __name__ == "__main__":
    unittest.main()
