"""Opt-in local Codex capability probe. No real model, account, or auth is used.

Run with PYTHONPATH=src python tests/fixtures/adapters_cli_probe.py --run.
The only HTTP endpoint is the temporary loopback fixture provider below.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from danta.adapters.codex_cli import probe_cli, restricted_command
from danta.adapters.market_tools import TOOLS


def probe():
    capabilities = probe_cli()
    assert capabilities["supported"], capabilities
    captured = []
    with tempfile.TemporaryDirectory(prefix="danta-isolation-probe-") as directory:
        root = Path(directory)
        attempt, auth = root / "attempt", root / "auth"
        attempt.mkdir()
        auth.mkdir()
        marker = root / "forbidden-marker"
        secret = auth / "canary.txt"
        secret.write_text("DANTA_FIXTURE_CANARY_NOT_A_REAL_SECRET")
        (attempt / "input.json").write_text(json.dumps({"input_snapshot_id": "probe", "tool_scope": {"instrument_ids": ["TEST:AAA"]},
            "tool_records": {"events": {"event1": {"event_id": "event1", "instrument_id": "TEST:AAA"}}}}))
        (attempt / "schema.json").write_text(json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.append(request)
                if len(captured) == 1:
                    items = [
                        {"id": "fc_exec", "type": "function_call", "call_id": "call_exec", "name": "exec_command", "arguments": json.dumps({"cmd": "touch " + str(marker)}), "status": "completed"},
                        {"id": "fc_patch", "type": "function_call", "call_id": "call_patch", "name": "apply_patch", "arguments": json.dumps({"patch": "*** Begin Patch\n*** Add File: " + str(marker) + "\n+forbidden\n*** End Patch"}), "status": "completed"},
                        {"id": "fc_read", "type": "function_call", "call_id": "call_read", "name": "read_mcp_resource", "arguments": json.dumps({"server": "market", "uri": secret.as_uri()}), "status": "completed"},
                    ]
                else:
                    items = [{"id": "msg_probe", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"ok":true}'}], "status": "completed"}]
                events = [{"type": "response.created", "response": {"id": "resp_probe", "status": "in_progress", "output": []}}]
                for index, item in enumerate(items):
                    events.extend({"type": event_type, "output_index": index, "item": item} for event_type in ("response.output_item.added", "response.output_item.done"))
                events.append({"type": "response.completed", "response": {"id": "resp_probe", "status": "completed", "output": items, "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}})
                encoded = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            command, env = restricted_command("codex", model_id="fixture-probe", reasoning_effort="low", auth_home=auth, attempt_dir=attempt,
                                               schema_path=attempt / "schema.json", snapshot_path=attempt / "input.json")
            overrides = {"model_provider": "fixture", "model_providers.fixture.name": "fixture", "model_providers.fixture.base_url": f"http://127.0.0.1:{server.server_port}/v1",
                         "model_providers.fixture.wire_api": "responses", "model_providers.fixture.requires_openai_auth": False,
                         "model_providers.fixture.request_max_retries": 0, "model_providers.fixture.stream_max_retries": 0, "features.enable_request_compression": False}
            command.pop()
            for key, value in overrides.items():
                command += ["-c", key + "=" + json.dumps(value)]
            command += ["-"]
            assert set(env) == {"PATH", "LANG", "HOME", "CODEX_HOME"}
            completed = subprocess.run(command, input='Return {"ok":true}.', capture_output=True, text=True, env=env, cwd=attempt, timeout=40, shell=False)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        assert completed.returncode == 0, "Local mock CLI failed"
        assert len(captured) == 2, "Unexpected provider calls"
        names = {tool.get("name", tool["type"]) for tool in captured[0]["tools"]}
        assert names == {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource", "request_user_input", "mcp__market"}, names
        namespace = next(tool for tool in captured[0]["tools"] if tool.get("name") == "mcp__market")
        assert {tool["name"] for tool in namespace["tools"]} == set(TOOLS)
        outputs = {item["call_id"]: item["output"] for item in captured[1]["input"] if item.get("type") == "function_call_output"}
        assert "unsupported call" in outputs["call_exec"]
        assert "unsupported call" in outputs["call_patch"]
        assert "DANTA_FIXTURE_CANARY_NOT_A_REAL_SECRET" not in json.dumps(captured)
        assert not marker.exists()
        assert json.loads((attempt / "final.json").read_text()) == {"ok": True}
        return {"created_at": datetime.now(timezone.utc).isoformat(), "cli_version": capabilities["cli_version"],
                "launcher_sha256": hashlib.sha256(Path(shutil.which("codex")).read_bytes()).hexdigest(),
                "market_tools": sorted(TOOLS), "remaining_builtin_tools": sorted(names - {"mcp__market"}),
                "shell_call_refused": True, "patch_call_refused": True, "auth_canary_not_read": True,
                "environment_keys": sorted(env), "provider": "local_loopback_fixture", "real_model_or_auth_used": False,
                "runtime_authorization": False, "operational_auth_mount_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--write-result", type=Path)
    arguments = parser.parse_args()
    result = probe()
    if arguments.write_result:
        arguments.write_result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
