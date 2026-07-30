"""Optional OTLP/JSON receiver: Anthropic's own per-request costs (wave D).

ccmetrics works entirely without this. Turn Claude Code's telemetry on and
point it at `ccmetrics otel`, and the days it covers get shown as **exact
(OTEL)** instead of the cache-derived **floor**; everything else is unchanged.
Nothing here is ever required, and nothing here changes an uncovered figure.

Wire protocol: OTLP/JSON over HTTP (`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`),
`POST /v1/logs` and `POST /v1/metrics` on 127.0.0.1:4318. Protobuf and grpc are
NOT parsed in v1 -- a protobuf body gets a 415 with a one-line hint and the
receiver stays up.

Privacy rules, same as the rest of the tool:
  * loopback bind only, no flag exposes this on a network interface;
  * attributes are WHITELISTED by key -- an unknown key is dropped before it can
    reach the store, so `body`, `body_ref`, `tool_input`, `prompt` and friends
    (which Claude Code only emits under OTEL_LOG_RAW_API_BODIES / TOOL_DETAILS)
    can never land in the DB even if a user enables them;
  * only scalar attribute values are read; arrays and kvlists are ignored;
  * nothing is written to ~/.claude, ever.

Name provenance
---------------
VERIFIED against https://docs.claude.com/en/docs/claude-code/monitoring-usage
(fetched 2026-07-30, HTTP 200; docs.anthropic.com serves the same page):
    event name              `claude_code.api_request`
    event attributes        `event.name` = "api_request", `event.timestamp`,
                            `event.sequence`, `model`, `cost_usd`,
                            `cost_usd_micros`, `duration_ms`, `input_tokens`,
                            `output_tokens`, `cache_read_tokens`,
                            `cache_creation_tokens`, `request_id`,
                            `client_request_id`
    standard attributes     `session.id`, `app.version`, `user.id`, ...
    endpoints/protocol      `http://localhost:4318/v1/logs`, `/v1/metrics`,
                            `OTEL_EXPORTER_OTLP_PROTOCOL=grpc|http/json|http/protobuf`
SPECULATIVE (accepted defensively, never required, never the only spelling
tried): the bare `api_request` event name, `costUsd`/`cost` camelCase, the
`session_id`/`sessionId` spellings, and the Messages-API usage-block spellings
`cache_read_input_tokens` / `cache_creation_input_tokens`. If Claude Code ever
renames a field to one of those, the reader keeps working; if it renames it to
something else entirely, the event is dropped and counted, never guessed.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, constants, store

HOST = constants.value(constants.OTEL["host"])
DEFAULT_PORT = constants.value(constants.OTEL["port"])
MAX_BODY = constants.value(constants.OTEL["max_body_bytes"])
PROBE_TIMEOUT = constants.value(constants.OTEL["probe_timeout_seconds"])
EXPORT_INTERVAL_MS = constants.value(constants.OTEL["export_interval_ms"])

# --- attribute names --------------------------------------------------------
# First spelling in each tuple is the VERIFIED one; the rest are the defensive
# fallbacks described in the module docstring.

EVENT_NAMES = ("claude_code.api_request", "api_request")
COST_KEYS = ("cost_usd", "costUsd", "cost")
COST_MICROS_KEYS = ("cost_usd_micros", "costUsdMicros")
SESSION_KEYS = ("session.id", "session_id", "sessionId")
MODEL_KEYS = ("model", "model.name")
TS_KEYS = ("event.timestamp", "event_timestamp", "timestamp")
INPUT_KEYS = ("input_tokens", "inputTokens", "input_token_count")
OUTPUT_KEYS = ("output_tokens", "outputTokens", "output_token_count")
CACHE_READ_KEYS = ("cache_read_tokens", "cacheReadTokens", "cache_read_input_tokens")
CACHE_WRITE_KEYS = (
    "cache_creation_tokens", "cacheCreationTokens", "cache_creation_input_tokens",
)
REQUEST_ID_KEYS = ("request_id", "requestId")
CLIENT_REQUEST_ID_KEYS = ("client_request_id", "clientRequestId")
SEQUENCE_KEYS = ("event.sequence", "event_sequence")
NAME_KEYS = ("event.name", "event_name", "name")

# Everything the parser is allowed to keep. Any other attribute key is dropped
# where it is read, before it can reach a dict, let alone the database.
WHITELIST = frozenset(
    COST_KEYS + COST_MICROS_KEYS + SESSION_KEYS + MODEL_KEYS + TS_KEYS
    + INPUT_KEYS + OUTPUT_KEYS + CACHE_READ_KEYS + CACHE_WRITE_KEYS
    + REQUEST_ID_KEYS + CLIENT_REQUEST_ID_KEYS + SEQUENCE_KEYS + NAME_KEYS
)

MICROS_PER_USD = 1_000_000.0


# --- OTLP/JSON shredding ----------------------------------------------------


def scalar(value):
    """One OTLP AnyValue -> a python scalar, or None.

    OTLP/JSON encodes int64 as a *string* (proto3 JSON rule), so intValue has to
    go through int(str). Arrays, kvlists and bytes are deliberately unsupported:
    a structure is exactly the shape a message body would arrive in.
    """
    if isinstance(value, (str, int, float, bool)):
        return value
    if not isinstance(value, dict):
        return None
    for key, cast in (
        ("stringValue", str), ("string_value", str),
        ("doubleValue", float), ("double_value", float),
        ("intValue", int), ("int_value", int),
        ("boolValue", bool), ("bool_value", bool),
    ):
        if key in value:
            raw = value[key]
            if raw is None:
                return None
            try:
                return cast(raw)
            except (TypeError, ValueError):
                return None
    return None


def _collect(attributes, into: dict) -> None:
    for item in attributes or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or key not in WHITELIST:
            continue
        val = scalar(item.get("value"))
        if val is not None:
            into[key] = val


def _listy(container: dict, *names) -> list:
    for name in names:
        val = container.get(name)
        if isinstance(val, list):
            return val
    return []


def iter_log_records(payload: dict):
    """Yield (log_record, whitelisted attrs) over resourceLogs/scopeLogs/logRecords.

    Resource-level attributes are merged in first (Claude Code sends session.id
    both on the record and in the resource block), record attributes win.
    """
    if not isinstance(payload, dict):
        return
    for rl in _listy(payload, "resourceLogs", "resource_logs"):
        if not isinstance(rl, dict):
            continue
        base: dict = {}
        resource = rl.get("resource")
        if isinstance(resource, dict):
            _collect(resource.get("attributes"), base)
        for sl in _listy(rl, "scopeLogs", "scope_logs"):
            if not isinstance(sl, dict):
                continue
            for lr in _listy(sl, "logRecords", "log_records"):
                if not isinstance(lr, dict):
                    continue
                attrs = dict(base)
                _collect(lr.get("attributes"), attrs)
                yield lr, attrs


def _first(attrs: dict, keys):
    for key in keys:
        if key in attrs:
            return attrs[key]
    return None


def _num(attrs: dict, keys):
    val = _first(attrs, keys)
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _int(attrs: dict, keys):
    val = _num(attrs, keys)
    return None if val is None else int(val)


def record_event_name(record: dict, attrs: dict) -> str | None:
    """Event name from wherever this OTLP version puts it: the log record's own
    eventName field (OTLP >= 1.5), the `event.name` attribute (what the docs
    list), or a plain-string body."""
    for key in ("eventName", "event_name"):
        val = record.get(key)
        if isinstance(val, str) and val:
            return val
    val = _first(attrs, NAME_KEYS)
    if isinstance(val, str) and val:
        return val
    body = record.get("body")
    if isinstance(body, dict):
        val = scalar(body)
        if isinstance(val, str) and val:
            return val
    elif isinstance(body, str) and body:
        return body
    return None


def _nano_to_iso(raw) -> str | None:
    try:
        nanos = int(raw)
    except (TypeError, ValueError):
        return None
    if nanos <= 0:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(nanos / 1e9))


def record_ts(record: dict, attrs: dict) -> str | None:
    """ISO timestamp, same shape as the transcript timestamps so `substr(ts,1,10)`
    lines up with daily.date. Prefers the event's own ISO attribute, falls back
    to the OTLP unix-nano fields."""
    val = _first(attrs, TS_KEYS)
    if isinstance(val, str) and len(val) >= 10:
        return val
    for key in ("timeUnixNano", "time_unix_nano", "observedTimeUnixNano",
                "observed_time_unix_nano"):
        iso = _nano_to_iso(record.get(key))
        if iso:
            return iso
    return None


def event_hash(event: dict) -> str:
    """Dedupe key for one API request.

    `request_id` (the Anthropic `request-id` header, verified attribute) is
    unique per API request, so session+request_id is the identity whenever it is
    present -- that survives an exporter retrying the same batch with different
    OTLP envelopes. `client_request_id` is the next best. With neither, fall
    back to a digest of the full whitelisted tuple (session, ts, model, cost,
    token counts, sequence): identical replays collapse, genuinely distinct
    requests differ in at least the sequence or the counts.
    """
    ident = event.get("request_id") or event.get("client_request_id")
    if ident:
        basis = f"rid|{event.get('session_id')}|{ident}"
    else:
        basis = "tuple|" + "|".join(
            str(event.get(k))
            for k in ("session_id", "ts", "model", "cost_usd", "input_tokens",
                      "output_tokens", "cache_read", "cache_write", "sequence")
        )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def extract_events(payload: dict, received: str | None = None) -> tuple[list[dict], dict]:
    """OTLP/JSON logs payload -> cost events + a stats dict. Never raises."""
    received = received or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    stats = {"records": 0, "api_request": 0, "no_cost": 0, "other_events": 0}
    events: list[dict] = []
    try:
        records = list(iter_log_records(payload))
    except Exception:  # a malformed envelope is data, not an incident
        return [], stats
    for record, attrs in records:
        stats["records"] += 1
        name = record_event_name(record, attrs)
        if name not in EVENT_NAMES:
            stats["other_events"] += 1
            continue
        stats["api_request"] += 1
        cost = _num(attrs, COST_KEYS)
        if cost is None:
            micros = _num(attrs, COST_MICROS_KEYS)
            cost = None if micros is None else micros / MICROS_PER_USD
        if cost is None:
            # An api_request with no cost attribute is not priced by guesswork.
            stats["no_cost"] += 1
            continue
        session = _first(attrs, SESSION_KEYS)
        event = {
            "session_id": str(session) if session is not None else None,
            "ts": record_ts(record, attrs),
            "model": _first(attrs, MODEL_KEYS),
            "cost_usd": cost,
            "input_tokens": _int(attrs, INPUT_KEYS),
            "output_tokens": _int(attrs, OUTPUT_KEYS),
            "cache_read": _int(attrs, CACHE_READ_KEYS),
            "cache_write": _int(attrs, CACHE_WRITE_KEYS),
            "request_id": _first(attrs, REQUEST_ID_KEYS),
            "client_request_id": _first(attrs, CLIENT_REQUEST_ID_KEYS),
            "sequence": _first(attrs, SEQUENCE_KEYS),
            "received": received,
        }
        event["event_hash"] = event_hash(event)
        events.append(event)
    return events, stats


# --- setup block ------------------------------------------------------------

SETUP_NOTE = (
    "ccmetrics never edits your shell profile or settings.json — this block is "
    "printed for you to paste."
)


def setup_text(port: int = DEFAULT_PORT) -> str:
    endpoint = f"http://{HOST}:{port}"
    shell = "\n".join(
        [
            "export CLAUDE_CODE_ENABLE_TELEMETRY=1",
            "export OTEL_LOGS_EXPORTER=otlp",
            "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
            f"export OTEL_EXPORTER_OTLP_ENDPOINT={endpoint}",
            f"export OTEL_LOGS_EXPORT_INTERVAL={EXPORT_INTERVAL_MS}",
        ]
    )
    settings = json.dumps(
        {
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
                "OTEL_LOGS_EXPORT_INTERVAL": str(EXPORT_INTERVAL_MS),
            }
        },
        indent=2,
    )
    return "\n".join(
        [
            "ccmetrics otel — exact costs from Claude Code's own telemetry (optional)",
            "",
            "1. run the receiver in another terminal:",
            f"     ccmetrics otel --port {port}",
            "",
            "2. shell env (paste into ~/.zshrc, or export in the shell you run claude from):",
            "",
            shell,
            "",
            "   or the same thing in ~/.claude/settings.json:",
            "",
            settings,
            "",
            "3. start a new Claude Code session. Costs arrive every "
            f"{EXPORT_INTERVAL_MS // 1000}s; `ccmetrics` then labels covered days "
            "'exact (OTEL)'.",
            "",
            f"notes: http/json only (this receiver does not speak grpc or protobuf) · "
            f"loopback only ({HOST}) · counts, ids and dollars are stored, never bodies.",
            f"        {SETUP_NOTE}",
            "        leave OTEL_LOG_RAW_API_BODIES / OTEL_LOG_USER_PROMPTS / "
            "OTEL_LOG_TOOL_DETAILS off; ccmetrics drops those attributes anyway.",
        ]
    )


# --- receiver ---------------------------------------------------------------


class _State:
    """One writer connection guarded by one lock: the store stays single-writer
    even though the HTTP server is threaded."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.lock = threading.Lock()
        self.started = time.time()
        self.batches = 0
        self.events_seen = 0
        self.events_new = 0
        self.rejected = 0
        self.metric_batches = 0

    def ingest(self, payload: dict) -> dict:
        events, stats = extract_events(payload)
        with self.lock:
            new = store.insert_otel_events(self.conn, events)
            if new:
                store.refresh_exact_daily(self.conn)
        self.batches += 1
        self.events_seen += len(events)
        self.events_new += new
        stats["accepted"] = len(events)
        stats["new"] = new
        stats["duplicates"] = len(events) - new
        return stats


class Handler(BaseHTTPRequestHandler):
    server_version = f"ccmetrics-otel/{__version__}"
    protocol_version = "HTTP/1.1"
    state: _State | None = None
    verbose = True

    def log_message(self, fmt, *args):  # never echo request contents
        pass

    def _reply(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _empty(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._reply(
                {"error": "payload too large", "max_bytes": MAX_BODY}, 413
            )
            return None
        if length > 0:
            return self.rfile.read(length)
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            # Claude Code <2.1.212 used chunked encoding for http/json exports.
            chunks = bytearray()
            while True:
                line = self.rfile.readline(64).strip()
                try:
                    size = int(line.split(b";")[0] or b"0", 16)
                except ValueError:
                    break
                if size <= 0:
                    self.rfile.readline(4)
                    break
                if len(chunks) + size > MAX_BODY:
                    self._reply({"error": "payload too large", "max_bytes": MAX_BODY}, 413)
                    return None
                chunks += self.rfile.read(size)
                self.rfile.readline(4)
            return bytes(chunks)
        return b""

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/health", "/healthz"):
            self._empty(204)
        elif self.path.rstrip("/") in ("", "/status"):
            st = self.state
            self._reply(
                {
                    "receiver": "ccmetrics",
                    "version": __version__,
                    "protocol": "otlp/json",
                    "uptime_s": round(time.time() - st.started, 1) if st else None,
                    "batches": st.batches if st else 0,
                    "events_seen": st.events_seen if st else 0,
                    "events_new": st.events_new if st else 0,
                    "rejected": st.rejected if st else 0,
                }
            )
        else:
            self._reply({"error": "not found", "path": self.path}, 404)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path not in ("/v1/logs", "/v1/metrics", "/v1/traces"):
                self._reply({"error": "not found", "path": path}, 404)
                return
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/json" not in ctype:
                if self.state:
                    self.state.rejected += 1
                self._reply(
                    {
                        "error": "unsupported media type",
                        "got": ctype or "(none)",
                        "hint": "ccmetrics speaks OTLP/JSON only — set "
                                "OTEL_EXPORTER_OTLP_PROTOCOL=http/json "
                                "(protobuf and grpc are not parsed)",
                    },
                    415,
                )
                self._drain()
                return
            body = self._read_body()
            if body is None:
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                if self.state:
                    self.state.rejected += 1
                self._reply(
                    {"error": "malformed OTLP/JSON body", "detail": str(exc)[:120]}, 400
                )
                return
            if not isinstance(payload, dict):
                if self.state:
                    self.state.rejected += 1
                self._reply({"error": "OTLP/JSON body must be an object"}, 400)
                return
            if path == "/v1/logs":
                stats = self.state.ingest(payload) if self.state else {}
                if self.verbose and stats.get("new"):
                    print(
                        f"otel: +{stats['new']} exact-cost events "
                        f"({stats.get('duplicates', 0)} dup)",
                        flush=True,
                    )
            else:
                # Metrics/traces are accepted so the exporter never sees an
                # error, but nothing is stored: claude_code.cost.usage is a
                # cumulative counter and double-counting it would inflate spend.
                if self.state:
                    self.state.metric_batches += 1
            self._reply({"partialSuccess": {}})
        except BrokenPipeError:
            return
        except Exception as exc:  # a bad post never takes the receiver down
            try:
                self._reply(
                    {"error": exc.__class__.__name__, "detail": str(exc)[:200]}, 500
                )
            except Exception:
                pass

    def _drain(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < length <= MAX_BODY:
            try:
                self.rfile.read(length)
            except Exception:
                pass


def receiver_live(port: int = DEFAULT_PORT, timeout: float = PROBE_TIMEOUT) -> bool:
    """Is a receiver answering on loopback? A plain TCP connect, done by the
    server process — the dash page never talks to another port itself (that
    would be a cross-origin request the page's own CSP forbids)."""
    try:
        with socket.create_connection((HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


def serve(port: int = DEFAULT_PORT, conn: sqlite3.Connection | None = None,
          quiet: bool = False) -> int:
    """Run the receiver in the foreground until ctrl-c.

    The connection is opened here with check_same_thread=False and every write
    goes through _State's lock: threaded handlers, still one writer.
    """
    own = conn is None
    conn = conn or store.connect(check_same_thread=False)
    state = _State(conn)
    handler = type("BoundHandler", (Handler,), {"state": state, "verbose": not quiet})
    httpd = ThreadingHTTPServer((HOST, port), handler)
    httpd.daemon_threads = True
    if not quiet:
        print(
            f"ccmetrics otel: OTLP/JSON receiver on http://{HOST}:{port}"
            f"  (POST /v1/logs · ctrl-c to stop)",
            flush=True,
        )
        print(
            "  waiting for Claude Code telemetry — run `ccmetrics otel --setup` "
            "for the env block",
            flush=True,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if not quiet:
            print("", flush=True)
    finally:
        httpd.server_close()
        if not quiet:
            print(
                f"ccmetrics otel: {state.events_new} new exact-cost events from "
                f"{state.batches} batches ({state.events_seen - state.events_new} "
                f"duplicates ignored, {state.rejected} rejected)",
                flush=True,
            )
        if own:
            conn.close()
    return 0
