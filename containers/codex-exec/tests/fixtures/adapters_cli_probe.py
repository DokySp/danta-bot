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


def probe(model_id="gpt-5.6-sol"):
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
        secret.chmod(0o600)
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
                    code = '''text({globals: Object.fromEntries(
                        ["process", "require", "fetch", "Deno", "Bun", "XMLHttpRequest", "WebSocket"]
                        .map(k => [k, typeof globalThis[k]])), tools: ALL_TOOLS.map(t => t.name)});
                    try { await import("node:fs"); text("IMPORT_ALLOWED"); }
                    catch { text("IMPORT_DENIED"); }
                    for (const name of ["apply_patch", "exec_command", "read_mcp_resource"]) {
                        try { await tools[name]({}); text(name + ":ALLOWED"); }
                        catch { text(name + ":DENIED"); }
                    }
                    text(await tools.mcp__market__get_event({event_id: "event1"}));'''
                    items = [
                        {"id": "fc_code", "type": "custom_tool_call", "call_id": "call_code", "namespace": "functions", "name": "exec", "input": code, "status": "completed"},
                        {"id": "fc_exec", "type": "function_call", "call_id": "call_exec", "name": "exec_command", "arguments": json.dumps({"cmd": "touch " + str(marker)}), "status": "completed"},
                        {"id": "fc_patch", "type": "custom_tool_call", "call_id": "call_patch", "name": "apply_patch", "input": "*** Begin Patch\n*** Add File: " + str(marker) + "\n+forbidden\n*** End Patch", "status": "completed"},
                        {"id": "fc_auth_patch", "type": "custom_tool_call", "call_id": "call_auth_patch", "name": "apply_patch", "input": "*** Begin Patch\n*** Update File: " + str(secret) + "\n@@\n-not-a-matching-line\n+forbidden\n*** End Patch", "status": "completed"},
                        {"id": "fc_read", "type": "function_call", "call_id": "call_read", "name": "read_mcp_resource", "arguments": json.dumps({"server": "market", "uri": secret.as_uri()}), "status": "completed"},
                        {"id": "fc_agents", "type": "function_call", "call_id": "call_agents", "namespace": "collaboration", "name": "list_agents", "arguments": "{}", "status": "completed"},
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
            command, env = restricted_command("codex", model_id=model_id, reasoning_effort="low", auth_home=auth, attempt_dir=attempt,
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
        exposed = [tool for item in captured[0]["input"] if item.get("type") == "additional_tools" for tool in item["tools"]]
        assert [tool["name"] for tool in exposed] == ["functions"]
        names = {tool["name"] for tool in exposed[0]["tools"]}
        assert names == {"exec", "wait", "request_user_input"}, names
        outputs = {item["call_id"]: item["output"] for item in captured[1]["input"] if item.get("type") in {"function_call_output", "custom_tool_call_output"}}
        code_output = [item["text"] for item in outputs["call_code"] if item.get("type") == "input_text"]
        inventory = json.loads(code_output[1])
        assert set(inventory["tools"]) == {"mcp__market__" + tool for tool in TOOLS}
        assert all(value == "undefined" for value in inventory["globals"].values())
        assert set(code_output[2:6]) == {"IMPORT_DENIED", "apply_patch:DENIED", "exec_command:DENIED", "read_mcp_resource:DENIED"}
        assert "unsupported call" in outputs["call_exec"]
        assert "unsupported call" in outputs["call_agents"]
        assert "writing is blocked by read-only sandbox" in outputs["call_patch"]
        assert "Failed to read file to update" in outputs["call_auth_patch"]
        auth_read_denial = "SANDBOX_SETUP_REJECTED" if "bwrap: No permissions" in outputs["call_auth_patch"] else "FILESYSTEM_READ_REJECTED"
        assert "DANTA_FIXTURE_CANARY_NOT_A_REAL_SECRET" not in json.dumps(captured) + completed.stdout + completed.stderr
        assert secret.read_text() == "DANTA_FIXTURE_CANARY_NOT_A_REAL_SECRET"
        assert not marker.exists()
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        assert not any(event.get("item", {}).get("type") in {"error", "file_change"} for event in events)
        lookups = [json.loads(line) for line in (attempt / "lookup-manifest.jsonl").read_text().splitlines()]
        assert len(lookups) == 1 and lookups[0]["tool"] == "get_event" and lookups[0]["data"]["event_id"] == "event1"
        assert json.loads((attempt / "final.json").read_text()) == {"ok": True}
        host = Path(shutil.which("codex")).resolve().with_name("codex-code-mode-host")
        return {"created_at": datetime.now(timezone.utc).isoformat(), "cli_version": capabilities["cli_version"],
                "launcher_sha256": hashlib.sha256(Path(shutil.which("codex")).read_bytes()).hexdigest(),
                "code_mode_host_sha256": hashlib.sha256(host.read_bytes()).hexdigest(), "model_id": model_id,
                "market_tools": sorted(TOOLS), "remaining_builtin_tools": sorted(names), "read_tool_verified": True,
                "code_mode_no_filesystem_or_network": True, "subagent_call_refused": True,
                "auth_read_denial": auth_read_denial, "filesystem_profile": "minimal_and_attempt_read_only_auth_denied",
                "shell_call_refused": True, "patch_call_refused": True, "auth_canary_not_read": True,
                "environment_keys": sorted(env), "provider": "local_loopback_fixture", "real_model_or_auth_used": False,
                "runtime_authorization": False, "operational_auth_mount_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--write-result", type=Path)
    parser.add_argument("--model-id", default="gpt-5.6-sol")
    arguments = parser.parse_args()
    result = probe(arguments.model_id)
    if arguments.write_result:
        arguments.write_result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
