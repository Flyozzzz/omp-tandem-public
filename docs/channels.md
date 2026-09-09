# Claude Code Channels and local webhooks

[Back to README](../README.md) · [Русский README](../README.ru.md) · [中文 README](../README.zh-CN.md)

This reference describes optional delivery for OMP Tandem 3.0.1. The normal MCP polling workflow works without Channels. Channels are a Claude Code feature, not a portable MCP guarantee and not a permission-approval relay.

## Choose the delivery mode

Run the launcher from the project directory. Set `TANDEM_ROOT` to the actual installed package or checkout, not a guessed cache version.

```sh
# Explicit polling: no channel probes or webhook listener.
uv run --no-project --python '>=3.12' python -I \
  "$TANDEM_ROOT/scripts/launch.py" --delivery poll

# Installed Claude plugin, subject to client consent and organization policy.
uv run --no-project --python '>=3.12' python -I \
  "$TANDEM_ROOT/scripts/launch.py" --approved-plugin omp-tandem@omp-tandem

# Standalone local MCP registration: development-channel opt-in.
uv run --no-project --python '>=3.12' python -I \
  "$TANDEM_ROOT/scripts/launch.py"
```

`--no-webhook` requests task/question events without HTTP input. `--check` prints the exact command and environment changes without launching Claude. Arguments after `--` are forwarded to Claude.

The launcher sets `MCP_PROTOCOL_NEGOTIATION=legacy` for its push path. It does not add `--dangerously-skip-permissions`, alter managed settings, or approve untrusted hooks.

The plugin declares its `omp-tandem` MCP server as a channel. Standalone and plugin server identities differ in the client: do not enable a bare standalone name when you intend the installed plugin.

## Delivery must be demonstrated

1. The server advertises the Claude channel capability.
2. After discovery/first use, it sends a `channel_probe` notification with a random `probe_token`.
3. The token is not returned in an ordinary probe tool response.
4. The coordinator acknowledges only a token actually received from that channel event, using `tandem_channel(action="ack", probe_token=...)`.
5. Only successful receipt confirmation sets `delivery="push"` and permits webhook startup.

A connected MCP server, manifest declaration, or launch flag is not evidence of working delivery. Until confirmed, use polling.

When `next_action="await_event"`, keep the CLI open and stop repetitive polling. Fetch the task result once when the event arrives. Notification delivery may wait for the client to become available; it is not a guarantee that another operation is interrupted immediately.

The bridge advertises no `claude/channel/permission` capability. A probe token never approves filesystem actions or provider access.

## Organization and client restrictions

Team/Enterprise administrators may need to enable `channelsEnabled` and approve the marketplace/plugin through managed settings. Installing a local plugin does not bypass those controls.

A real previously tested Team account reported:

```text
Channels are not enabled for your org · have an administrator set channelsEnabled: true in managed settings
```

That restriction was not bypassed. It does not mean every Team account is blocked; it means each deployment needs its own authorized positive delivery check.

Use polling when the organization or client does not support the channel. Ordinary MCP support in a desktop/IDE surface does not automatically imply support for Claude CLI Channels. Codex uses the ordinary polling path here.

## Webhook endpoint and recipient

The listener starts only after channel receipt confirmation. It binds to `127.0.0.1`, chooses a per-session port by default, and accepts `POST /webhook` with a bearer token.

Call `tandem_channel(action="status")` in the intended session. The webhook status includes its URL and descriptor path. The descriptor contains the listener URL, PID, session ID, and path to a separate token file. Descriptor/token files use private permissions.

Default location:

```text
~/.local/state/omp-tandem/projects/<scope_id>/channels/
```

**Select the exact descriptor returned by that session. Never pick the newest file across all windows.** A bridge channel session ID is not a Claude chat ID. A webhook URL is not an HTTP MCP endpoint.

Keep tokens out of command-line arguments, logs, screenshots, commits, and product snapshots. A token authorizes event submission to that listener, not arbitrary host execution.

## Event request format

```json
{
  "id": "build-42-completed",
  "content": "The authorized CI run completed. Review its report before accepting changes.",
  "meta": {
    "source": "local-ci",
    "result": "passed"
  }
}
```

| Field | Rules |
|---|---|
| `content` | Required nonblank string, at most 8,000 characters |
| `id` | Optional nonblank producer key, at most 128 characters; use one stable ID for one real transition |
| `meta` | Optional map with at most 16 entries |
| Metadata keys | ASCII letters/digits/underscore, length 1–64 |
| Metadata values | Strings, at most 512 characters |

The complete body is limited to 32,768 bytes. Unknown fields, duplicate JSON keys, nonfinite JSON constants, unsupported content types, and malformed framing are rejected. Use `Content-Type: application/json`.

The server deliberately rejects browser `Origin` requests and unexpected `Host` values. It is a local script interface, not a browser API or a remotely exposed service.

## Safe local sender example

Set `TANDEM_DESCRIPTOR` to the descriptor returned by the intended MCP session. This example reads the bearer from its private file and does not put it in argv or follow redirects:

```python
import http.client
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

path = Path(os.environ["TANDEM_DESCRIPTOR"])
descriptor = json.loads(path.read_text())
endpoint = urlsplit(descriptor["url"])
if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1":
    raise ValueError("Expected the selected loopback webhook")
token = Path(descriptor["token_file"]).read_text().strip()
body = json.dumps(
    {"id": "build-42-completed", "content": "CI completed; inspect the actual report."}
).encode()
connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=5)
try:
    connection.request(
        "POST",
        endpoint.path,
        body=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    response = connection.getresponse()
    result = response.read().decode()
    print(response.status, result)
finally:
    connection.close()
```

HTTP 202 means the event was durably queued, **not that the model has handled it or executed any side effect**. The coordinator acknowledges a handled webhook using its returned `event_id`.

An identical repeated producer ID is idempotent for the same owner/payload. Reusing that ID for different content returns a conflict. Do not resubmit the same real transition under new IDs to work around a conflict.

## Durable queue and recovery

Events are stored in the project's `channel_events` table. Task status and its terminal event are committed together. Sending and acknowledgment are separate states.

- Reading a task result acknowledges the corresponding observed task event.
- Webhook handling uses explicit `ack` with `event_id`.
- `pending` lists the current channel owner's unacknowledged events.
- `include_previous=true` also includes other owners **within the current project only**; it does not prove those sessions have ended.
- `recover` explicitly adopts an event or terminal task's events for the current session.
- Recovery does not rerun OMP, modify its outcome, or permit takeover of an active task's events.

On normal close, the listener stops and its descriptor/token files are removed. After an abrupt process kill, stale files may remain: request a current descriptor rather than trusting an old PID or URL.

Legacy migration does not copy old channel tokens/queues into a new project namespace. Preserved task results remain readable; absence of a push event is not evidence that a task did not finish.

## Hooks are not the delivery mechanism

The plugin's `SessionStart` hook only diagnoses missing executables. It does not forward every tool result, poll the task database, block Stop, approve permissions, or maintain an independent notification queue.

Task events already come from the MCP bridge. A CI system should submit one event for a real state transition, using a stable producer ID and the selected descriptor. External event contents are untrusted data, not higher-priority instructions.

## HTTP and delivery diagnostics

| Observation | Meaning/action |
|---|---|
| Connected but polling | No confirmed receipt; inspect client consent and organization policy |
| Probe pending | The client has not proved receipt; do not invent the token |
| Webhook URL absent | Confirm push first; also check `--no-webhook`/environment settings |
| 401 | Missing or incorrect bearer; do not print it while debugging |
| 403 | Invalid Host/Origin; browser requests are intentionally rejected |
| 409 | The same producer ID was used for a different event |
| 413 | Body exceeds the limit |
| 429 | The owner's pending webhook queue is full; handle existing events |
| 503 | Delivery unavailable/closed or handler capacity exhausted |
| Early connection close under load | Rejection can happen before HTTP body parsing; a full HTTP exchange is not guaranteed under overload |
| Event accepted but no model action | Storage acknowledgment is not processing acknowledgment; inspect pending state and client lifecycle |

For configuration/field details, inspect the actual MCP tool schemas. Tests exercise local stdio push, HTTP delivery, ownership, acknowledgments, queue limits, and failure handling. They do not replace a positive test in your organization's allowed client environment.

## Official references

- [Claude Code Channels](https://code.claude.com/docs/en/channels)
- [Channels reference](https://code.claude.com/docs/en/channels-reference)
- [Plugin reference](https://code.claude.com/docs/en/plugins-reference)
- [Plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces)
- [MCP reference](https://code.claude.com/docs/en/mcp)
