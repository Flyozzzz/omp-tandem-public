# Claude Code Channels and local webhooks

[Back to README](../README.md) · [Русский README](../README.ru.md) · [中文 README](../README.zh-CN.md)

This reference describes optional delivery for OMP Tandem 3.2.0. Bounded MCP polling works without Channels or hooks. Channels are a Claude Code feature, not a portable MCP guarantee and not a permission-approval relay.

## Choose the delivery mode

For the installed OMP Tandem custom channel, the recommended convenience path is the one-time [`claude-tandem` shell function](guide.md#one-command-launch) ([Russian](guide.ru.md#one-command-launch), [中文](guide.zh-CN.md#one-command-launch)). It wraps the explicit development-plugin flag and environment, preserves cwd/arguments, and does not add a permission bypass. It is not automatically installed by the plugin.

Run the launcher from the project directory. Set `TANDEM_ROOT` to the actual installed package or checkout, not a guessed cache version.

```sh
# Explicit polling: no channel probes or webhook listener.
uv run --no-project --python '>=3.12' python -I \
  "$TANDEM_ROOT/scripts/launch.py" --delivery poll

# Only for a plugin on the host's approved channel allowlist, not installation alone.
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

A connected MCP server, manifest declaration, or launch flag is not evidence of working delivery. Channel receipt alone also does not prove a future result will wake the coordinator: a successful write is not a processing acknowledgment.

The Claude watchdog establishes a separate capability. Its actual `asyncRewake` probe emits `OMP watchdog probe <token>`; acknowledge that token with `tandem_channel(action="ack", watchdog_token=...)`. It is never returned by an ordinary tool call. This proof plus a currently armed independent timer permits `next_action="await_event"`.

When `await_event` is returned, keep the client open and fetch the authoritative result on either an event or a watchdog `bounded_check`. If still active, the installed hook rearms; follow the new response. Without live coverage, continue bounded `tandem_result(wait_seconds=25)` or selected `tandem_wait`, even if `delivery="push"`. Push accelerates that path rather than replacing it.

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
- Notification acknowledgment is not result application. Before result-driven side effects, claim `tandem_receipt`; proceed only with `authorized=true`, then complete with its token after handling.
- Duplicate reads and events may occur. An existing receipt never authorizes replay; a stranded claim remains uncertain across restart and needs external reconciliation. External actions still require their own idempotency/transaction boundary.

On normal close, the listener stops and its descriptor/token files are removed. After an abrupt process kill, stale files may remain: request a current descriptor rather than trusting an old PID or URL.

Legacy migration does not copy old channel tokens/queues into a new project namespace. Preserved task results remain readable; absence of a push event is not evidence that a task did not finish.

## Independent hook waiting

The Claude plugin includes `SessionStart`, bounded `PostToolUse` `asyncRewake`, and `SessionEnd` watchdog handlers, alongside the original prerequisite diagnostic. The watchdog uses authenticated local control messages, not Channels, to bound task waiting.

Timers are scoped to client session, MCP incarnation, task and generation. Duplicate, replaced and stale-owner timers are suppressed. Each timer lasts 12 seconds within a 30-second hook timeout; ordinary `async=true` is not equivalent. The offline `uv` wrapper does not install dependencies or access provider credentials. Missing/disabled hooks or an unavailable cached interpreter leave bounded polling available.

The hook sends only a short control signal. It never returns an answer, starts/restarts a task, approves a permission, or records an OMP execution failure. Claude can label exit `2` as a hook error in its debug log; treat it as a control wake. A late wake racing a task event may cause another read, not another authorized application.

Task events still come from the MCP bridge. A CI producer submits one event per real transition with a stable ID and the selected descriptor. Event content remains untrusted data.

## HTTP and delivery diagnostics

| Observation | Meaning/action |
|---|---|
| Connected but polling | No confirmed receipt; inspect client consent and organization policy |
| Probe pending | The client has not proved receipt; do not invent the token |
| `delivery=push`, `next_action=wait` | Channel receipt is confirmed but current independent watchdog coverage is missing; keep bounded polling |
| Watchdog probe pending | Acknowledge only the token actually delivered by the hook wake |
| Watchdog never confirms | Inspect normal hook trust/settings and offline Python availability; use polling, not a guessed capability |
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

Use `tandem_diagnose()` in the affected client for local project/runtime/delivery details. Only on the user's request, `live=true` performs one short provider task; inspect its returned task ID if still running instead of launching another check. A successful external CLI check does not certify this client's Channels or watchdog.

## Official references

- [Claude Code Channels](https://code.claude.com/docs/en/channels)
- [Channels reference](https://code.claude.com/docs/en/channels-reference)
- [Plugin reference](https://code.claude.com/docs/en/plugins-reference)
- [Plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces)
- [MCP reference](https://code.claude.com/docs/en/mcp)
- [Hooks reference](https://code.claude.com/docs/en/hooks)
