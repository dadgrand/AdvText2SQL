"""Grammar-constrained decoding in front of a vLLM OpenAI endpoint.

Why this file exists
====================
The course harness speaks OpenAI at ``LLM_BASE_URL`` and asks the *tool* for SQL;
the tool does not send ``response_format`` or any guided-decoding field, and the
AutoGen client it uses cannot be asked to.  vLLM, however, constrains generation
per request (``structured_outputs``/``guided_*``).  Round 1 therefore had no way
to make "constrained decoding" a *decoding* claim: the closest thing was the
tool's own sqlglot gate plus one repair call, which is post-hoc filtering, not
decoding (see ``docs/DECODER.md``).

This module is the missing adapter: a small, dependency-free HTTP proxy that

* exposes ``GET /health`` with exactly the provenance ``run_benchmark.py`` copies
  into ``run.json`` (served name, base model, ``max_new_tokens``, request count,
  peak memory) plus a ``guided_decoding`` block, and
* forwards ``POST /v1/chat/completions`` to vLLM after injecting
  ``structured_outputs`` **only for the SQL-generation request**.

What it deliberately does NOT do
--------------------------------
* It never edits the model's text.  No fence stripping, no regex cleanup, no
  "repair": the whole point of the grammar arm is that a *decoder* change, not a
  post-processor, produced the difference.  The only transform is adding the
  grammar field to the request body.
* It does not constrain the ambiguity gate (its answer must be ``OK`` /
  ``AMBIGUOUS: <clause>``).  Requests are classified by the tool's own system
  prompt; anything unrecognised is forwarded unconstrained and counted as such,
  so a silent "everything was guided" claim cannot happen.
* It does not stream: the harness never streams, and a streamed response cannot
  be timed or token-counted the way ``/health`` promises.

Usage::

    python -m dev_tools.guided_proxy --port 8100 --upstream http://127.0.0.1:8000 \
        --served-name qwen2.5-coder-7b-awq-guided \
        --base-model Qwen/Qwen2.5-Coder-7B-Instruct-AWQ \
        --max-new-tokens 512 --quantization awq --load-in-4bit \
        --log data_cache/runs/<run-id>/llm_requests.jsonl

    export LLM_BASE_URL=http://127.0.0.1:8100/v1
    export LLM_API_KEY=EMPTY
    export LLM_MODEL_NAME=qwen2.5-coder-7b-awq-guided

``/health`` must answer for ``run_benchmark.py`` to accept a run at all; that is
why the proxy, not vLLM, is the endpoint the benchmark points at.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("guided_proxy")

GRAMMAR_PATH = Path(__file__).with_name("grammars") / "postgres_select.gbnf"

SQL_MARKER = "You are an expert PostgreSQL analyst"
"""First line of ``prompts.SYSTEM_PROMPT_TEMPLATE``: the SQL-generation call.

A repair call uses the same system prompt and a different user prompt, so the
grammar also applies there - which is correct, both want one SELECT statement.
"""

AMBIGUITY_MARKER = "You audit a natural-language question"
"""First line of ``prompts.AMBIGUITY_PROMPT_TEMPLATE``: must stay unconstrained."""


# ---------------------------------------------------------------------------
# Request classification
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "".join(_text_of(part) for part in content)
    if isinstance(content, dict):
        return _text_of(content.get("content") or content.get("text") or "")
    return "" if content is None else str(content)


def classify_messages(
    messages: Sequence[Any], *, sql_marker: str = SQL_MARKER, ambiguity_marker: str = AMBIGUITY_MARKER
) -> str:
    """Return ``"sql"``, ``"ambiguity"`` or ``"other"`` for one chat request.

    ``sql`` is the only class that gets the grammar.  Classification is by the
    *system* message because that is the only string the tool controls; the user
    message differs per question (and, on BIRD, may carry evidence).
    """

    systems: List[str] = []
    for message in messages:
        role = None
        content: Any = message
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content")
        if role == "system" or (role is None and isinstance(message, str)):
            systems.append(_text_of(content))
    haystack = "\n".join(systems)
    if sql_marker in haystack:
        return "sql"
    if ambiguity_marker in haystack:
        return "ambiguity"
    return "other"


def inject_structured_outputs(payload: Dict[str, Any], grammar: str) -> Dict[str, Any]:
    """Return a copy of ``payload`` with the grammar attached to the request.

    vLLM >= 0.11 spells per-request guided decoding ``structured_outputs`` with a
    ``grammar`` key (GBNF/EBNF, compiled by xgrammar).  The field is rejected
    loudly by the server if this build spells it differently, which is the
    behaviour we want: a silently ignored grammar would make the arm a lie.
    """

    body = dict(payload)
    body["structured_outputs"] = {"grammar": grammar}
    return body


# ---------------------------------------------------------------------------
# GPU sampling (the run.json VRAM gap, closed from the proxy side)
# ---------------------------------------------------------------------------


class GpuSampler(threading.Thread):
    """Sample whole-device memory/utilisation so a run records its own VRAM use.

    ``run.json`` historically could not carry ``torch.cuda.max_memory_allocated``
    because the serving process owns that counter (REPORT 5.1).  The A100 is
    *shared* with the training job, so the device-wide figure is the honest one
    here: it says what the box was doing during the run, including contention.
    """

    def __init__(self, interval_s: float = 2.0) -> None:
        super().__init__(daemon=True, name="gpu-sampler")
        self.interval_s = interval_s
        self._stop = threading.Event()
        self.samples: List[Dict[str, Optional[int]]] = []

    def run(self) -> None:  # pragma: no cover - needs nvidia-smi
        while not self._stop.is_set():
            sample = self._sample()
            if sample is not None:
                self.samples.append(sample)
            self._stop.wait(self.interval_s)

    def _sample(self) -> Optional[Dict[str, Optional[int]]]:
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None
        line = out.stdout.strip().splitlines()[0]
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            return None
        try:
            return {"memory_used_mib": int(float(parts[0])), "utilization_pct": int(float(parts[1]))}
        except ValueError:
            return None

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> Dict[str, Any]:
        if not self.samples:
            return {"n_samples": 0}
        memory = [s["memory_used_mib"] for s in self.samples if s["memory_used_mib"] is not None]
        util = [s["utilization_pct"] for s in self.samples if s["utilization_pct"] is not None]
        return {
            "n_samples": len(self.samples),
            "memory_used_mib_peak": max(memory) if memory else None,
            "memory_used_mib_min": min(memory) if memory else None,
            "utilization_pct_peak": max(util) if util else None,
            "utilization_pct_mean": round(sum(util) / len(util), 1) if util else None,
        }


# ---------------------------------------------------------------------------
# State the server keeps for /health and the request log
# ---------------------------------------------------------------------------


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


class RunState:
    """Counters and latency samples for one proxy lifetime (= one benchmark arm)."""

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.guided = 0
        self.unguided = 0
        self.by_kind: Dict[str, int] = {}
        self.failures = 0
        self.max_completion_tokens = 0
        self.latencies_guided: List[float] = []
        self.latencies_all: List[float] = []
        self.log_path = log_path
        self.started_at = time.time()
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")

    def record(self, entry: Dict[str, Any]) -> None:
        with self.lock:
            self.requests += 1
            kind = entry.get("kind", "other")
            self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
            if entry.get("guided"):
                self.guided += 1
                if entry.get("latency_s") is not None:
                    self.latencies_guided.append(float(entry["latency_s"]))
            else:
                self.unguided += 1
            if entry.get("latency_s") is not None:
                self.latencies_all.append(float(entry["latency_s"]))
            if not entry.get("ok", True):
                self.failures += 1
            completion = entry.get("completion_tokens") or 0
            self.max_completion_tokens = max(self.max_completion_tokens, int(completion))
            if self.log_path is not None:
                with open(self.log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def latency_block(self) -> Dict[str, Any]:
        def block(values: Sequence[float]) -> Dict[str, Any]:
            if not values:
                return {"n": 0}
            return {
                "n": len(values),
                "mean_s": round(sum(values) / len(values), 3),
                "p50_s": round(_percentile(values, 0.50) or 0.0, 3),
                "p95_s": round(_percentile(values, 0.95) or 0.0, 3),
                "max_s": round(max(values), 3),
            }

        return {"guided": block(self.latencies_guided), "all": block(self.latencies_all)}


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _openai_error(message: str, status: int) -> Tuple[int, Dict[str, Any]]:
    return status, {
        "error": {"message": message, "type": "proxy_error", "param": None, "code": status}
    }


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "guided-proxy/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------
    @property
    def config(self) -> Dict[str, Any]:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def state(self) -> RunState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # keep stderr readable
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _upstream(self, path: str, body: Optional[bytes] = None, method: str = "GET") -> Tuple[int, bytes]:
        url = self.config["upstream"].rstrip("/") + path
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Content-Type", "application/json")
        api_key = self.config.get("upstream_api_key")
        if api_key:
            request.add_header("Authorization", f"Bearer {api_key}")
        timeout = float(self.config.get("upstream_timeout_s") or 600.0)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:  # pass the server's own error through
            return error.code, error.read()
        except Exception as error:  # pragma: no cover - transport failure
            return 502, json.dumps(
                _openai_error(f"upstream unreachable: {type(error).__name__}: {error}", 502)[1]
            ).encode("utf-8")

    # -- endpoints -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        if path == "/health":
            self._send_json(200, self.describe_health())
            return
        if path in ("/v1/models", "/models"):
            status, body = self._upstream("/v1/models")
            self._send_json(status, json.loads(body.decode("utf-8")) if body else {})
            return
        if path in ("/", "/docs", "/openapi.json", "/metrics"):
            status, body = self._upstream(path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(*_openai_error(f"unknown path {self.path!r}", 404))

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?")[0]
        if path not in ("/v1/chat/completions", "/v1/completions"):
            self._send_json(*_openai_error(f"unknown path {self.path!r}", 404))
            return

        raw = self._read_body()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json(*_openai_error("request body must be valid JSON", 400))
            return
        if not isinstance(payload, dict):
            self._send_json(*_openai_error("request body must be a JSON object", 400))
            return
        if payload.get("stream"):
            # Refusing is better than pretending: a streamed answer cannot be
            # timed, token-counted or grammar-checked the way /health claims.
            self._send_json(*_openai_error("this proxy does not stream; send stream=false", 400))
            return

        messages = payload.get("messages") or []
        kind = classify_messages(messages) if path.endswith("chat/completions") else "other"
        # ``--no-grammar`` is the surrogate arm: the classification still runs (so the
        # log shows how many requests *would* have been guided) but nothing is injected.
        guided = kind == "sql" and bool(self.config["grammar"])
        if guided:
            payload = inject_structured_outputs(payload, self.config["grammar"])
        payload.setdefault("temperature", 0)
        # The harness never sends a generation cap (round-1 note: the *server* cap is
        # the one that binds).  vLLM would otherwise default to ``max_model_len``
        # minus the prompt, so the cap has to be imposed here - and then /health's
        # ``max_new_tokens`` is again the number that actually bound, which is what
        # run.json records and what ``finish_reason == "length"`` can be read against.
        if not payload.get("max_tokens") and not payload.get("max_completion_tokens"):
            payload["max_tokens"] = self.config["max_new_tokens"]
        if self.config.get("upstream_model"):
            # The proxy answers to its own served name (that is what /health and
            # run.json report); the upstream only knows the name it was started
            # with, so the alias has to be resolved here rather than in the caller.
            payload["model"] = self.config["upstream_model"]

        started = time.time()
        status, body = self._upstream(
            "/v1/chat/completions" if path.endswith("chat/completions") else "/v1/completions",
            json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        latency = time.time() - started

        completion_text = None
        usage: Dict[str, Any] = {}
        finish_reason = None
        if status == 200:
            try:
                response = json.loads(body.decode("utf-8"))
                choices = response.get("choices") or []
                if choices:
                    message = choices[0].get("message") or {}
                    completion_text = _text_of(message.get("content"))
                    finish_reason = choices[0].get("finish_reason")
                usage = response.get("usage") or {}
            except Exception:  # pragma: no cover - malformed upstream body
                logger.warning("upstream returned a body that is not JSON")
        else:
            completion_text = body.decode("utf-8", "replace")[:2000]

        self.state.record(
            {
                "ts": round(time.time(), 3),
                "kind": kind,
                "guided": guided,
                "latency_s": round(latency, 4),
                "status": status,
                "ok": status == 200,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "finish_reason": finish_reason,
                "completion": completion_text,
            }
        )
        self._send_json(status, json.loads(body.decode("utf-8")) if body else {})

    # -- /health ---------------------------------------------------------
    def describe_health(self) -> Dict[str, Any]:
        config = self.config
        state = self.state
        grammar = config["grammar"]
        payload: Dict[str, Any] = {
            "status": "ok",
            "server": "dev_tools.guided_proxy",
            "upstream": config["upstream"],
            "served_name": config["served_name"],
            "base_model": config["base_model"],
            "adapter": None,
            "adapter_sha256": None,
            "adapter_config_sha256": None,
            "load_in_4bit": config.get("load_in_4bit"),
            "quantization": config.get("quantization"),
            "device": config.get("device"),
            "max_new_tokens": config["max_new_tokens"],
            "requests_served": state.requests,
            "max_completion_tokens_observed": state.max_completion_tokens,
            # vLLM owns the CUDA context, so a torch high-water mark is not
            # reachable from here; the device-wide sample below is what the run
            # can honestly claim (see docs/REPORT.md 5.1 for the gap).
            "cuda_max_memory_allocated_bytes": None,
            "device_memory": config["sampler"].summary(),
            "guided_decoding": {
                "backend": config.get("guided_backend"),
                "grammar_file": str(config["grammar_path"]),
                "grammar_sha256": hashlib.sha256(grammar.encode("utf-8")).hexdigest(),
                "grammar_chars": len(grammar),
                "applies_to": "requests whose system prompt contains the SQL marker",
                "sql_marker": config["sql_marker"],
                "requests_guided": state.guided,
                "requests_unguided": state.unguided,
                "requests_by_kind": dict(state.by_kind),
                "requests_failed": state.failures,
            },
            "latency": state.latency_block(),
            "measurement_note": config.get("note"),
            "uptime_s": round(time.time() - state.started_at, 1),
        }
        return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.guided_proxy",
        description=(
            "OpenAI-compatible proxy that adds vLLM guided decoding (a SELECT grammar) to "
            "SQL-generation requests only, and exposes /health for dev_tools/run_benchmark.py."
        ),
    )
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--upstream", default="http://127.0.0.1:8000", help="vLLM OpenAI endpoint root")
    parser.add_argument("--upstream-api-key", default=None, help="bearer token for the upstream, if any")
    parser.add_argument("--upstream-timeout-s", type=float, default=600.0)
    parser.add_argument("--grammar", default=str(GRAMMAR_PATH), help="GBNF file to constrain SQL generation")
    parser.add_argument("--no-grammar", action="store_true", help="forward everything unconstrained (surrogate arm)")
    parser.add_argument("--guided-backend", default="xgrammar")
    parser.add_argument("--served-name", default="qwen2.5-coder-7b-awq")
    parser.add_argument(
        "--upstream-model",
        default=None,
        help="model name the upstream answers to; defaults to --served-name",
    )
    parser.add_argument("--base-model", default="Qwen/Qwen2.5-Coder-7B-Instruct-AWQ")
    parser.add_argument("--quantization", default="awq")
    parser.add_argument("--load-in-4bit", action="store_true", default=False)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--sql-marker", default=SQL_MARKER)
    parser.add_argument("--log", default=None, help="JSONL of every forwarded request (latency, tokens, text)")
    parser.add_argument("--sample-gpu", action="store_true", default=True)
    parser.add_argument("--no-sample-gpu", dest="sample_gpu", action="store_false")
    parser.add_argument("--note", default=None, help="free text stored in /health (e.g. shared-GPU caveat)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    grammar_path = Path(args.grammar)
    grammar = "" if args.no_grammar else grammar_path.read_text(encoding="utf-8")
    if not args.no_grammar and not grammar.strip():
        print(f"refusing to start: grammar file {grammar_path} is empty", file=sys.stderr)
        return 2

    sampler = GpuSampler()
    if args.sample_gpu:
        sampler.start()

    state = RunState(Path(args.log) if args.log else None)
    httpd = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    httpd.config = {  # type: ignore[attr-defined]
        "upstream": args.upstream,
        "upstream_api_key": args.upstream_api_key,
        "upstream_timeout_s": args.upstream_timeout_s,
        "grammar": grammar,
        "grammar_path": grammar_path,
        "guided_backend": None if args.no_grammar else args.guided_backend,
        "served_name": args.served_name,
        "upstream_model": args.upstream_model or args.served_name,
        "base_model": args.base_model,
        "quantization": args.quantization,
        "load_in_4bit": bool(args.load_in_4bit),
        "device": args.device,
        "max_new_tokens": args.max_new_tokens,
        "sql_marker": args.sql_marker,
        "sampler": sampler,
        "note": args.note,
    }
    httpd.state = state  # type: ignore[attr-defined]
    logger.info(
        "guided proxy on http://%s:%d -> %s (grammar %s, %d chars)",
        args.host,
        args.port,
        args.upstream,
        "disabled" if args.no_grammar else grammar_path.name,
        0 if args.no_grammar else len(grammar),
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        sampler.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
