"""A small local OpenAI-compatible model server for the course harness.

Why this exists
---------------
The harness talks to an OpenAI-compatible endpoint (``LLM_BASE_URL`` /
``LLM_API_KEY`` / ``LLM_MODEL_NAME``).  The usual answer is vLLM, but **vLLM is
Linux-only** (it needs CUDA kernels and a POSIX toolchain) and this machine has
**no Docker**, so neither is available on the Windows workstation where the LoRA
track runs.  This module is the smallest thing that satisfies the same contract:
``transformers`` for the model, FastAPI for the HTTP surface, optional PEFT for
the adapter.

Two backends
------------
``TransformersBackend``
    ``AutoModelForCausalLM`` + ``AutoTokenizer``, device auto (``cuda`` when
    available), optional ``--adapter`` (``PeftModel.from_pretrained``), optional
    ``--load-in-4bit``.  ``--model`` defaults to ``Qwen/Qwen2.5-Coder-1.5B-Instruct``.
``FakeBackend`` (``--fake``)
    No model at all: a deterministic canned answer with a whitespace tokenizer.
    It exists so the HTTP contract, the harness wiring and the tests are
    exercisable without a GPU, a download or an API key.

Endpoints
---------
``GET /v1/models``
    ``{"object": "list", "data": [{"id": <served name>, ...}]}``.
``POST /v1/chat/completions``
    One choice out, ``finish_reason`` in ``{"stop", "length"}`` and a real
    ``usage`` object (prompt/completion/total tokens counted by the tokenizer).
``GET /health``
    Liveness probe the harness can use before a run.

What it does NOT do
-------------------
* **No batching** — one request is decoded at a time; a second in-flight request
  waits for the first to finish (the model is not thread-safe and a benchmark
  must not interleave generations).
* **No continuous batching** (that is exactly what vLLM buys you).
* **No streaming** — ``"stream": true`` is rejected with HTTP 400 rather than
  silently ignored, so a caller cannot believe it is getting SSE.
* **No tool calling, no logprobs, no embeddings, no vision, no ``n > 1``.**
  Unknown request fields are ignored (OpenAI clients send many), but anything
  that would change the meaning of the answer is rejected.

Usage (identical to vLLM from the harness's point of view)::

    python -m dev_tools.serve_openai --model Qwen/Qwen2.5-Coder-1.5B-Instruct \
        --adapter ../data_cache/training_out/adapter --port 8000

    export LLM_BASE_URL=http://127.0.0.1:8000/v1
    export LLM_API_KEY=EMPTY
    export LLM_MODEL_NAME=Qwen/Qwen2.5-Coder-1.5B-Instruct+adapter:adapter

Add ``--fake`` to serve the deterministic backend instead of a model (tests and
smoke runs), and ``--served-name`` when the harness expects a specific name.

Dependencies are pinned in ``dev_tools/requirements-serve.txt`` (``fastapi``,
``uvicorn``, ``transformers``, ``torch``, and ``peft``/``accelerate``/
``bitsandbytes`` only for the adapter and 4-bit paths).
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("dev_tools.serve_openai")

DEFAULT_MODEL = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
DEFAULT_MAX_NEW_TOKENS = 256
DEFAULT_SERVED_SUFFIX = "EMPTY"  # the harness sends LLM_API_KEY=EMPTY; anything is accepted
STREAMING_ERROR_CODE = "stream_not_supported"
MAX_TEMPERATURE = 2.0


# ---------------------------------------------------------------------------
# Result and backend protocol
# ---------------------------------------------------------------------------


@dataclass
class GenerationResult:
    """One completion, already decoded."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"


class Backend:
    """What the HTTP layer needs from a model."""

    served_name: str = "unknown"
    base_model: str = "unknown"
    adapter: Optional[str] = None

    def generate(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float = 0.0,
        stop: Sequence[str] = (),
    ) -> GenerationResult:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"served_name": self.served_name, "base_model": self.base_model, "adapter": self.adapter}


# ---------------------------------------------------------------------------
# Deterministic fake backend (tests, smoke runs, no GPU)
# ---------------------------------------------------------------------------


class FakeBackend(Backend):
    """A canned, deterministic answer produced without any model.

    It is deliberately dumb: the point is to exercise the HTTP contract and the
    harness wiring.  The answer echoes the last user message so a caller can see
    the server received it, and the token counts are whitespace counts (documented
    as an approximation — the real backends use the model's tokenizer).
    """

    def __init__(self, served_name: str = "fake-local-model", answer: Optional[str] = None) -> None:
        self.served_name = served_name
        self.base_model = "fake"
        self.adapter = None
        self._answer = answer

    @staticmethod
    def _count(text: str) -> int:
        return len(text.split())

    def generate(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float = 0.0,
        stop: Sequence[str] = (),
    ) -> GenerationResult:
        prompt_text = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        answer = self._answer if self._answer is not None else f"SELECT 1  -- fake answer to: {last_user}"
        finish_reason = "stop"
        truncated = answer
        for marker in stop:
            if marker and marker in truncated:
                truncated = truncated.split(marker, 1)[0]
        completion_tokens = min(self._count(answer), max_new_tokens)
        if completion_tokens >= max_new_tokens:
            words = answer.split()[:max_new_tokens]
            truncated = " ".join(words)
            finish_reason = "length"
        return GenerationResult(
            text=truncated,
            prompt_tokens=self._count(prompt_text),
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
        )


# ---------------------------------------------------------------------------
# transformers backend
# ---------------------------------------------------------------------------


def _fallback_prompt(messages: Sequence[Dict[str, str]]) -> str:
    """Deterministic plain-text rendering when the tokenizer has no chat template."""

    parts = [f"<|{message['role']}|>\n{message['content']}" for message in messages]
    parts.append("<|assistant|>\n")
    return "\n".join(parts)


class TransformersBackend(Backend):
    """``transformers`` generation behind the OpenAI surface (greedy by default)."""

    def __init__(
        self,
        model: Any,
        tokenizer: Any,
        *,
        served_name: str,
        base_model: str,
        adapter: Optional[str] = None,
        device: Optional[str] = None,
        allow_sampling: bool = False,
        seed: int = 0,
    ) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.served_name = served_name
        self.base_model = base_model
        self.adapter = adapter
        self.device = device or self._infer_device(model)
        self.allow_sampling = allow_sampling
        self.seed = seed
        self._lock = None  # created lazily: single request at a time, see module docstring

    @staticmethod
    def _infer_device(model: Any) -> str:
        try:
            return str(next(model.parameters()).device)
        except Exception:
            return "cpu"

    # -- construction -----------------------------------------------------
    @classmethod
    def load(
        cls,
        model_id: str = DEFAULT_MODEL,
        *,
        adapter: Optional[str] = None,
        load_in_4bit: bool = False,
        device: str = "auto",
        served_name: Optional[str] = None,
        allow_sampling: bool = False,
        seed: int = 0,
    ) -> "TransformersBackend":
        """Load the base model, then the adapter (if any).  Raises on bad input.

        Every cheap check (adapter path, peft/bitsandbytes availability) happens
        *before* the model download, so a misconfigured start fails in
        milliseconds instead of after several gigabytes.
        """

        if adapter is not None:
            if not Path(adapter).exists():
                raise FileNotFoundError(
                    f"adapter path does not exist: {adapter} (refusing to start with a "
                    f"half-loaded model; check --adapter)"
                )
            try:
                import peft  # noqa: F401, PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "--adapter needs peft; install dev_tools/requirements-serve.txt"
                ) from exc

        if load_in_4bit:
            try:
                import bitsandbytes  # noqa: F401, PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "--load-in-4bit needs bitsandbytes (and accelerate); install "
                    "dev_tools/requirements-serve.txt"
                ) from exc

        import torch  # noqa: PLC0415  (imported lazily: tests must run without touching torch)
        from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: PLC0415

        target_device = (
            "cuda" if (device == "auto" and torch.cuda.is_available()) else ("cpu" if device == "auto" else device)
        )

        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=False)
        model_kwargs: Dict[str, Any] = {"trust_remote_code": False}
        if load_in_4bit:
            try:
                from transformers import BitsAndBytesConfig  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise RuntimeError(
                    "--load-in-4bit needs bitsandbytes (and accelerate); install "
                    "dev_tools/requirements-serve.txt"
                ) from exc
            model_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_quant_type="nf4",
            )
            model_kwargs["device_map"] = "auto"
        model = AutoModelForCausalLM.from_pretrained(model_id, **model_kwargs)
        if not load_in_4bit:
            model = model.to(target_device)
        model.eval()

        if adapter is not None:
            try:
                from peft import PeftModel  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "--adapter needs peft; install dev_tools/requirements-serve.txt"
                ) from exc
            model = PeftModel.from_pretrained(model, adapter)
            model.eval()

        resolved_name = served_name or (
            f"{model_id}+adapter:{Path(adapter).name}" if adapter else model_id
        )
        logger.info(
            "loaded %s on %s%s", resolved_name, target_device, " (4-bit)" if load_in_4bit else ""
        )
        return cls(
            model,
            tokenizer,
            served_name=resolved_name,
            base_model=model_id,
            adapter=adapter,
            device=target_device,
            allow_sampling=allow_sampling,
            seed=seed,
        )

    @classmethod
    def from_objects(
        cls,
        model: Any,
        tokenizer: Any,
        *,
        served_name: str = "tiny-local-model",
        base_model: Optional[str] = None,
        adapter: Optional[str] = None,
        allow_sampling: bool = False,
        seed: int = 0,
    ) -> "TransformersBackend":
        """Wrap already-built objects (tests build a tiny random model offline)."""

        return cls(
            model,
            tokenizer,
            served_name=served_name,
            base_model=base_model or served_name,
            adapter=adapter,
            device=cls._infer_device(model),
            allow_sampling=allow_sampling,
            seed=seed,
        )

    # -- generation -------------------------------------------------------
    def _render_prompt(self, messages: Sequence[Dict[str, str]]) -> str:
        template = getattr(self.tokenizer, "chat_template", None)
        if template:
            try:
                return self.tokenizer.apply_chat_template(
                    list(messages), tokenize=False, add_generation_prompt=True
                )
            except Exception:  # pragma: no cover - exotic tokenizers
                logger.warning("chat template failed; falling back to plain rendering")
        return _fallback_prompt(messages)

    def generate(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float = 0.0,
        stop: Sequence[str] = (),
    ) -> GenerationResult:
        import torch  # noqa: PLC0415

        prompt = self._render_prompt(messages)
        encoded = self.tokenizer(prompt, return_tensors="pt")
        input_ids = encoded["input_ids"].to(self.device)
        prompt_tokens = int(input_ids.shape[-1])

        sampling = self.allow_sampling and temperature > 0.0
        generation_kwargs: Dict[str, Any] = {
            "max_new_tokens": int(max_new_tokens),
            "do_sample": sampling,
            "pad_token_id": getattr(self.tokenizer, "pad_token_id", None),
            "eos_token_id": getattr(self.tokenizer, "eos_token_id", None),
        }
        if sampling:
            torch.manual_seed(self.seed)
            generation_kwargs["temperature"] = float(temperature)

        if self._lock is None:  # one request at a time (see module docstring)
            import threading  # noqa: PLC0415

            self._lock = threading.Lock()
        with self._lock:
            with torch.inference_mode():
                output = self.model.generate(input_ids, **generation_kwargs)

        new_ids = output[0][prompt_tokens:]
        completion_tokens = int(new_ids.shape[-1])
        text = self.tokenizer.decode(new_ids, skip_special_tokens=True)

        finish_reason = "length" if completion_tokens >= int(max_new_tokens) else "stop"
        for marker in stop:
            if marker and marker in text:
                text = text.split(marker, 1)[0]
                finish_reason = "stop"
                break
        return GenerationResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
        )


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def _error(status: int, message: str, *, code: Optional[str] = None, param: Optional[str] = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}},
    )


def _content_text(content: Any) -> Optional[str]:
    """Accept a string or an OpenAI content-parts list; return the concatenated text."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
            elif isinstance(part, str):
                chunks.append(part)
        return "".join(chunks) if chunks else None
    return None


def parse_chat_request(payload: Any, served_names: Iterable[str], *, relax_model_name: bool) -> Dict[str, Any]:
    """Validate an OpenAI chat-completions body; raise ``HTTPException`` on error."""

    if not isinstance(payload, dict):
        raise _error(400, "request body must be a JSON object")
    if payload.get("stream"):
        raise _error(
            400,
            "streaming is not supported by this server (dev_tools/serve_openai.py decodes one "
            "request at a time and returns a single JSON body); omit \"stream\" or set it to false",
            code=STREAMING_ERROR_CODE,
            param="stream",
        )

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _error(400, "\"messages\" must be a non-empty list", param="messages")
    normalised: List[Dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise _error(400, f"messages[{index}] must be an object", param=f"messages[{index}]")
        role = message.get("role")
        if not isinstance(role, str) or not role:
            raise _error(400, f"messages[{index}].role must be a non-empty string", param=f"messages[{index}].role")
        text = _content_text(message.get("content"))
        if text is None:
            raise _error(
                400,
                f"messages[{index}].content must be a string or a list of text parts",
                param=f"messages[{index}].content",
            )
        normalised.append({"role": role, "content": text})

    temperature = payload.get("temperature", 0.0)
    if temperature is None:
        temperature = 0.0
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise _error(400, "\"temperature\" must be a number", param="temperature")
    if temperature < 0 or temperature > MAX_TEMPERATURE:
        raise _error(400, f"\"temperature\" must be within [0, {MAX_TEMPERATURE}]", param="temperature")

    max_tokens = payload.get("max_tokens", payload.get("max_completion_tokens"))
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise _error(400, "\"max_tokens\" must be an integer", param="max_tokens")
        if max_tokens < 1:
            raise _error(400, "\"max_tokens\" must be >= 1", param="max_tokens")

    n = payload.get("n", 1)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise _error(400, "\"n\" must be an integer >= 1", param="n")
    # n > 1 is accepted but only one choice is produced (documented limitation).

    stop = payload.get("stop")
    if isinstance(stop, str):
        stop_sequences: List[str] = [stop]
    elif isinstance(stop, list) and all(isinstance(item, str) for item in stop):
        stop_sequences = list(stop)
    elif stop is None:
        stop_sequences = []
    else:
        raise _error(400, "\"stop\" must be a string or a list of strings", param="stop")

    declared_model = payload.get("model")
    known = set(served_names)
    if declared_model is not None:
        if not isinstance(declared_model, str):
            raise _error(400, "\"model\" must be a string", param="model")
        if declared_model not in known and not relax_model_name:
            raise _error(
                404,
                f"model {declared_model!r} is not served here; served: {sorted(known)} "
                f"(restart with --relax-model-name to accept any name)",
                code="model_not_found",
                param="model",
            )

    return {
        "messages": normalised,
        "temperature": float(temperature),
        "max_tokens": max_tokens,
        "stop": stop_sequences,
        "model": declared_model,
    }


# ---------------------------------------------------------------------------
# The FastAPI application
# ---------------------------------------------------------------------------


def create_app(
    backend: Backend,
    *,
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
    relax_model_name: bool = False,
) -> FastAPI:
    """Build the app around ``backend`` (tests inject a fake or a tiny model)."""

    app = FastAPI(title="dev_tools.serve_openai", version="1.0")
    served_names = {backend.served_name, backend.base_model}
    created = int(time.time())

    @app.exception_handler(HTTPException)
    async def openai_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        """Return OpenAI's error envelope (``{"error": {...}}``), not FastAPI's ``{"detail": ...}``."""

        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = {"error": detail["error"]}
        else:
            body = {"error": {"message": str(detail), "type": "invalid_request_error", "param": None, "code": None}}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.get("/v1/models")
    def list_models() -> Dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": backend.served_name,
                    "object": "model",
                    "created": created,
                    "owned_by": "local",
                    "root": backend.base_model,
                    "parent": backend.base_model if backend.adapter else None,
                }
            ],
        }

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", **backend.describe()}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            raise _error(400, "request body must be valid JSON")

        parsed = parse_chat_request(payload, served_names, relax_model_name=relax_model_name)
        requested_max = parsed["max_tokens"] or max_new_tokens

        try:
            result = backend.generate(
                parsed["messages"],
                max_new_tokens=int(requested_max),
                temperature=parsed["temperature"],
                stop=parsed["stop"],
            )
        except HTTPException:
            raise
        except Exception as exc:  # a generation failure is a server error, not a client error
            logger.exception("generation failed")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": {
                        "message": f"generation failed: {type(exc).__name__}: {exc}",
                        "type": "server_error",
                        "param": None,
                        "code": "generation_failed",
                    }
                },
            )

        completion = {
            "id": f"chatcmpl-{secrets.token_hex(12)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": backend.served_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.text},
                    "logprobs": None,
                    "finish_reason": result.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": int(result.prompt_tokens),
                "completion_tokens": int(result.completion_tokens),
                "total_tokens": int(result.prompt_tokens + result.completion_tokens),
            },
            "system_fingerprint": None,
        }
        return JSONResponse(content=completion)

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.serve_openai",
        description=(
            "Local OpenAI-compatible chat server (transformers + optional PEFT adapter). "
            "Speaks the same three env vars as vLLM: LLM_BASE_URL/LLM_API_KEY/LLM_MODEL_NAME."
        ),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"HF model id (default: {DEFAULT_MODEL})")
    parser.add_argument("--adapter", default=None, help="PEFT adapter directory (must exist)")
    parser.add_argument("--load-in-4bit", action="store_true", help="4-bit quantization (needs bitsandbytes)")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--served-name", default=None, help="name reported by /v1/models (default: model[+adapter])")
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=DEFAULT_MAX_NEW_TOKENS,
        help=f"generation cap when the request has no max_tokens (default: {DEFAULT_MAX_NEW_TOKENS})",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allow-sampling", action="store_true", help="honour temperature > 0 (seeded); default greedy")
    parser.add_argument("--seed", type=int, default=0, help="seed for --allow-sampling (default: 0)")
    parser.add_argument(
        "--relax-model-name",
        action="store_true",
        help="accept any \"model\" value instead of 404 on an unknown one",
    )
    parser.add_argument("--fake", action="store_true", help="serve the deterministic FakeBackend (no model at all)")
    parser.add_argument("--log-level", default="info")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    if args.adapter and not Path(args.adapter).exists():
        print(f"refusing to start: adapter path does not exist: {args.adapter}", file=sys.stderr)
        return 2
    if args.max_new_tokens < 1:
        print("refusing to start: --max-new-tokens must be >= 1", file=sys.stderr)
        return 2

    if args.fake:
        backend: Backend = FakeBackend(served_name=args.served_name or "fake-local-model")
    else:
        try:
            backend = TransformersBackend.load(
                args.model,
                adapter=args.adapter,
                load_in_4bit=args.load_in_4bit,
                device=args.device,
                served_name=args.served_name,
                allow_sampling=args.allow_sampling,
                seed=args.seed,
            )
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"refusing to start: {exc}", file=sys.stderr)
            return 2

    app = create_app(backend, max_new_tokens=args.max_new_tokens, relax_model_name=args.relax_model_name)

    print(f"serving {backend.served_name} on http://{args.host}:{args.port}/v1")
    print(f"  export LLM_BASE_URL=http://{args.host}:{args.port}/v1")
    print("  export LLM_API_KEY=EMPTY")
    print(f"  export LLM_MODEL_NAME={backend.served_name}")
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        print("uvicorn is missing: pip install -r dev_tools/requirements-serve.txt", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
