"""Deterministic, offline, OpenAI-compatible fake chat client.

Purpose
=======

Every test of the Text2SQL tools needs an LLM, and every such test is currently
either skipped or costs money.  ``StubLLM`` is the offline substrate: it speaks
the small slice of the OpenAI chat-completions contract the repo actually uses
(``await client.create(model=..., messages=[...])`` returning an object with
``.choices[0].message.content`` and ``.usage``) and it is fully deterministic.

Properties
----------

* **Deterministic** — the same ``messages`` always produce the same answer,
  because canned answers can be keyed by a stable prompt hash
  (:func:`prompt_hash`, a SHA-256 over the canonicalised message list).
* **Scriptable** — answers may be a mapping ``{prompt_hash: content}``, a list
  (served in call order), a single string (constant answer) or a callable
  ``(messages, call_index) -> str``.
* **Usage counters** — ``calls``, ``prompt_tokens``, ``completion_tokens``,
  ``total_tokens`` and per-key call counts, mirroring
  ``benchmarks/token_tracking.py::get_usage`` so the stub can be dropped in
  where a real client is expected.
* **Failure injection** — ``fail_on_calls={2, 5}`` (or ``.fail_on_calls(...)``)
  raises a ``StubLLMError`` on exactly those call numbers, which is how the
  retry / error paths of the tools are tested without a network.
* **No network, no key** — nothing is imported besides the standard library.

Example
-------

>>> import asyncio
>>> stub = StubLLM(responses=["SELECT 1", "SELECT 2"])
>>> answer = asyncio.run(stub.create(messages=[{"role": "user", "content": "hi"}]))
>>> answer.choices[0].message.content
'SELECT 1'
>>> stub.get_usage()["calls"]
1
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

__all__ = [
    "StubChoice",
    "StubChatCompletion",
    "StubLLM",
    "StubLLMError",
    "StubMessage",
    "StubUsage",
    "prompt_hash",
]

DEFAULT_MODEL = "stub-model"


class StubLLMError(RuntimeError):
    """Raised when the stub is scripted to fail on a given call number."""


def _canonical_messages(messages: Iterable[Mapping[str, Any]]) -> str:
    """Stable JSON encoding of a chat message list (role + content only)."""

    normalised: List[Dict[str, str]] = []
    for message in messages:
        if isinstance(message, Mapping):
            normalised.append(
                {
                    "role": str(message.get("role", "")),
                    "content": str(message.get("content", "")),
                }
            )
        else:  # tolerate objects with .role/.content
            normalised.append(
                {
                    "role": str(getattr(message, "role", "")),
                    "content": str(getattr(message, "content", "")),
                }
            )
    return json.dumps(normalised, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prompt_hash(messages: Iterable[Mapping[str, Any]]) -> str:
    """SHA-256 (first 16 hex chars) of the canonicalised message list.

    Stable across processes and Python versions, unlike ``hash()``.
    """

    return hashlib.sha256(_canonical_messages(messages).encode("utf-8")).hexdigest()[:16]


def _estimate_tokens(text: str) -> int:
    """Cheap deterministic token estimate (not a tokenizer)."""

    return max(1, len(str(text).split()))


@dataclass
class StubUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def model_dump(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    # autogen-style accessor
    def dict(self) -> Dict[str, int]:  # pragma: no cover - alias
        return self.model_dump()

    def __getitem__(self, key: str) -> int:  # tolerate dict-style reads
        return self.model_dump()[key]


@dataclass
class StubMessage:
    role: str = "assistant"
    content: str = ""

    def model_dump(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class StubChoice:
    index: int = 0
    message: StubMessage = field(default_factory=StubMessage)
    finish_reason: str = "stop"

    def model_dump(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "message": self.message.model_dump(),
            "finish_reason": self.finish_reason,
        }


@dataclass
class StubChatCompletion:
    id: str = "stub-0"
    model: str = DEFAULT_MODEL
    created: int = 0
    choices: List[StubChoice] = field(default_factory=list)
    usage: StubUsage = field(default_factory=StubUsage)

    @property
    def content(self) -> str:
        return self.choices[0].message.content if self.choices else ""

    def model_dump(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "created": self.created,
            "choices": [choice.model_dump() for choice in self.choices],
            "usage": self.usage.model_dump(),
        }


Responses = Union[
    None,
    str,
    Sequence[str],
    Mapping[str, str],
    Callable[[List[Dict[str, str]], int], str],
]


class StubLLM:
    """Deterministic OpenAI-compatible chat client for tests.

    Parameters
    ----------
    responses:
        Canned answers.  ``dict`` — keyed by :func:`prompt_hash`; ``list`` —
        served in call order (the last entry repeats); ``str`` — constant;
        ``callable`` — ``(messages, call_index) -> str``.
    default:
        Answer used when the mapping has no entry for the prompt (and the
        fallback when ``responses`` is ``None``).  Defaults to ``"SELECT 1"``.
    fail_on_calls:
        Iterable of 1-based call numbers that must raise
        :class:`StubLLMError` instead of answering.
    """

    def __init__(
        self,
        responses: Responses = None,
        default: str = "SELECT 1",
        *,
        model: str = DEFAULT_MODEL,
        fail_on_calls: Iterable[int] = (),
        fail_with: Optional[BaseException] = None,
        prompt_tokens_per_call: Optional[int] = None,
        completion_tokens_per_call: Optional[int] = None,
    ) -> None:
        self.responses = responses
        self.default = default
        self.model = model
        self._fail_on_calls = set(int(n) for n in fail_on_calls)
        self._fail_with = fail_with
        self._prompt_tokens_per_call = prompt_tokens_per_call
        self._completion_tokens_per_call = completion_tokens_per_call

        self.calls: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0
        self.calls_by_prompt: Dict[str, int] = {}
        self.history: List[Dict[str, Any]] = []

    # -- scripting ---------------------------------------------------------
    def fail_on_calls(self, *call_numbers: int, error: Optional[BaseException] = None) -> "StubLLM":
        """Raise on the given 1-based call numbers (chainable)."""

        self._fail_on_calls.update(int(n) for n in call_numbers)
        if error is not None:
            self._fail_with = error
        return self

    def script(self, responses: Responses, *, replace: bool = True) -> "StubLLM":
        """Replace (or, with ``replace=False``, merge into) the canned answers."""

        if replace or self.responses is None:
            self.responses = responses
        elif isinstance(self.responses, dict) and isinstance(responses, dict):
            merged = dict(self.responses)
            merged.update(responses)
            self.responses = merged
        else:  # pragma: no cover - ambiguous combination
            self.responses = responses
        return self

    # -- counters ----------------------------------------------------------
    def get_usage(self) -> Dict[str, int]:
        """Same shape as ``benchmarks/token_tracking.py::get_usage`` plus ``calls``."""

        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    @property
    def usage(self) -> Dict[str, int]:
        return self.get_usage()

    def reset(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.calls_by_prompt.clear()
        self.history.clear()

    # -- answer selection --------------------------------------------------
    def _pick(self, messages: List[Dict[str, str]], key: str, call_index: int) -> str:
        responses = self.responses
        if responses is None:
            return self.default
        if callable(responses):
            return str(responses(messages, call_index))
        if isinstance(responses, str):
            return responses
        if isinstance(responses, Mapping):
            return str(responses.get(key, self.default))
        # sequence: serve in call order, repeat the last entry
        seq = list(responses)
        if not seq:
            return self.default
        return str(seq[min(call_index, len(seq) - 1)])

    def answer_for(
        self, messages: Sequence[Mapping[str, Any]], call_index: Optional[int] = None
    ) -> str:
        """Peek at the answer without counting a call (useful in assertions)."""

        normalised = json.loads(_canonical_messages(messages))
        key = prompt_hash(normalised)
        index = (self.calls + 1) if call_index is None else call_index
        return self._pick(normalised, key, index)

    # -- the OpenAI-compatible surface ------------------------------------
    async def create(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> StubChatCompletion:
        self.calls += 1
        call_number = self.calls
        normalised = json.loads(_canonical_messages(messages))
        key = prompt_hash(normalised)
        self.calls_by_prompt[key] = self.calls_by_prompt.get(key, 0) + 1
        self.history.append(
            {
                "call": call_number,
                "prompt_hash": key,
                "messages": normalised,
                "kwargs": dict(kwargs),
            }
        )

        if call_number in self._fail_on_calls:
            error = self._fail_with or StubLLMError(
                f"stub scripted failure on call {call_number}"
            )
            raise error

        content = self._pick(normalised, key, call_number - 1)

        prompt_tokens = (
            self._prompt_tokens_per_call
            if self._prompt_tokens_per_call is not None
            else sum(_estimate_tokens(message["content"]) for message in normalised)
        )
        completion_tokens = (
            self._completion_tokens_per_call
            if self._completion_tokens_per_call is not None
            else _estimate_tokens(content)
        )
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += prompt_tokens + completion_tokens

        return StubChatCompletion(
            id=f"stub-{call_number - 1}",
            model=str(kwargs.get("model", self.model)),
            created=int(time.time()),
            choices=[StubChoice(index=0, message=StubMessage(role="assistant", content=content))],
            usage=StubUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
