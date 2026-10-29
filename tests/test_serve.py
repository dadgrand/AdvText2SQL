"""Contract tests for ``dev_tools/serve_openai.py``.

No GPU, no network, no database, no 7B model: the HTTP surface is exercised with
``FakeBackend`` and with a **tiny randomly-initialised** transformers model built
in memory (a 14-word WordLevel tokenizer + a 2-layer GPT-2), so the real
tokenizer/token-count code path is covered without downloading anything.

What is pinned:

* ``GET /v1/models`` returns the served name (and reports the adapter parent);
* ``POST /v1/chat/completions`` returns OpenAI's shape with a real ``usage``
  object (prompt tokens from the tokenizer, completion tokens from the generated
  ids, ``total = prompt + completion``), one choice, and a ``finish_reason``;
* greedy is the default and ``temperature`` cannot make it non-deterministic
  unless the server was started with ``--allow-sampling`` (and even then a fixed
  seed makes identical requests identical);
* ``--max-new-tokens`` / ``max_tokens`` are honoured, ``stop`` truncates;
* malformed requests are 400 with OpenAI's error envelope, ``stream: true`` is
  rejected explicitly, an unknown ``model`` is 404 unless relaxed, and unknown
  request fields are ignored;
* the server refuses to start when ``--adapter`` does not exist;
* the module docstring states why the server exists and what it does not do.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dev_tools import serve_openai as serve

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "dev_tools" / "requirements-serve.txt"

MESSAGES = [
    {"role": "system", "content": "You are a Text2SQL assistant."},
    {"role": "user", "content": "How many users are there?"},
]


# ---------------------------------------------------------------------------
# Backends and fixtures
# ---------------------------------------------------------------------------


def _fake_app(**kwargs) -> TestClient:
    return TestClient(serve.create_app(serve.FakeBackend(), **kwargs))


@pytest.fixture(scope="module")
def tiny_backend():
    """A real transformers backend built entirely in memory (no download)."""

    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import GPT2Config, PreTrainedTokenizerFast
    from transformers import AutoModelForCausalLM

    vocab = {
        "<pad>": 0,
        "<unk>": 1,
        "<s>": 2,
        "</s>": 3,
        "SELECT": 4,
        "COUNT": 5,
        "FROM": 6,
        "users": 7,
        "*": 8,
        "(": 9,
        ")": 10,
        "1": 11,
        "system:": 12,
        "user:": 13,
        "assistant:": 14,
        "How": 15,
        "many": 16,
        "are": 17,
        "there?": 18,
        "You": 19,
    }
    tokenizer_object = Tokenizer(models.WordLevel(vocab, unk_token="<unk>"))
    tokenizer_object.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=tokenizer_object,
        pad_token="<pad>",
        unk_token="<unk>",
        bos_token="<s>",
        eos_token="</s>",
    )
    config = GPT2Config(
        vocab_size=len(vocab),
        n_positions=128,
        n_embd=32,
        n_layer=2,
        n_head=2,
        bos_token_id=2,
        eos_token_id=3,
        pad_token_id=0,
    )
    model = AutoModelForCausalLM.from_config(config)
    model.eval()
    return serve.TransformersBackend.from_objects(
        model, tokenizer, served_name="tiny-test-model", base_model="tiny-base"
    )


@pytest.fixture(scope="module")
def tiny_client(tiny_backend) -> TestClient:
    return TestClient(serve.create_app(tiny_backend, max_new_tokens=4))


# ---------------------------------------------------------------------------
# /v1/models, /health
# ---------------------------------------------------------------------------


def test_models_endpoint_lists_the_served_name():
    client = _fake_app()
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [model["id"] for model in body["data"]] == ["fake-local-model"]
    assert body["data"][0]["object"] == "model"
    assert body["data"][0]["owned_by"] == "local"


def test_models_endpoint_reports_the_adapter_name():
    backend = serve.FakeBackend(served_name="Qwen/Qwen2.5-Coder-1.5B-Instruct+adapter:final")
    backend.adapter = "data_cache/training_out/adapter"
    body = TestClient(serve.create_app(backend)).get("/v1/models").json()
    entry = body["data"][0]
    assert entry["id"] == "Qwen/Qwen2.5-Coder-1.5B-Instruct+adapter:final"
    assert entry["parent"] == "fake"  # base model, i.e. the adapter is on top of something


def test_health_endpoint_reports_the_backend():
    body = _fake_app().get("/health").json()
    assert body["status"] == "ok"
    assert body["served_name"] == "fake-local-model"


def test_health_reports_the_provenance_a_run_json_needs(tiny_backend):
    """run_benchmark.py copies /health into run.json, so the keys must always exist."""

    body = TestClient(serve.create_app(tiny_backend, max_new_tokens=7)).get("/health").json()
    assert body["status"] == "ok"
    assert body["server"] == "dev_tools.serve_openai"
    # the generation cap that actually binds: the harness never sends max_tokens
    assert body["max_new_tokens"] == 7
    for key in (
        "served_name",
        "base_model",
        "adapter",
        "adapter_sha256",
        "adapter_config_sha256",
        "load_in_4bit",
        "device",
        "cuda_max_memory_allocated_bytes",
        "requests_served",
        "max_completion_tokens_observed",
    ):
        assert key in body, key


def test_health_counts_requests_and_the_largest_completion(tiny_backend):
    # a fresh backend object over the shared tiny model: the counters are per server
    fresh = serve.TransformersBackend.from_objects(
        tiny_backend.model, tiny_backend.tokenizer, served_name="tiny-test-model"
    )
    client = TestClient(serve.create_app(fresh, max_new_tokens=6))
    assert client.get("/health").json()["requests_served"] == 0
    client.post("/v1/chat/completions", json={"model": "tiny-test-model", "messages": MESSAGES, "max_tokens": 3})
    body = client.get("/health").json()
    assert body["requests_served"] == 1
    assert 0 < body["max_completion_tokens_observed"] <= 3


# ---------------------------------------------------------------------------
# Chat completions: shape, usage, decoding
# ---------------------------------------------------------------------------


def test_chat_completion_has_openai_shape_and_usage():
    response = _fake_app().post("/v1/chat/completions", json={"model": "fake-local-model", "messages": MESSAGES})
    assert response.status_code == 200
    body = response.json()

    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "fake-local-model"
    assert len(body["choices"]) == 1
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"]["role"] == "assistant"
    assert isinstance(choice["message"]["content"], str) and choice["message"]["content"]
    assert choice["finish_reason"] in {"stop", "length"}

    usage = body["usage"]
    assert set(usage) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert all(isinstance(value, int) and value >= 0 for value in usage.values())
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_usage_counts_come_from_the_tokenizer(tiny_client, tiny_backend):
    response = tiny_client.post(
        "/v1/chat/completions", json={"model": "tiny-test-model", "messages": MESSAGES, "max_tokens": 3}
    )
    assert response.status_code == 200
    usage = response.json()["usage"]

    rendered = tiny_backend._render_prompt(serve.parse_chat_request(
        {"model": "tiny-test-model", "messages": MESSAGES}, {"tiny-test-model"}, relax_model_name=False
    )["messages"])
    expected_prompt = len(tiny_backend.tokenizer(rendered, return_tensors="pt")["input_ids"][0])
    assert usage["prompt_tokens"] == expected_prompt
    assert 0 <= usage["completion_tokens"] <= 3
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_greedy_is_the_default_and_temperature_is_ignored(tiny_client):
    payload = {"model": "tiny-test-model", "messages": MESSAGES, "max_tokens": 4}
    greedy = tiny_client.post("/v1/chat/completions", json=payload).json()
    hot = tiny_client.post("/v1/chat/completions", json={**payload, "temperature": 0.9}).json()
    assert greedy["choices"][0]["message"]["content"] == hot["choices"][0]["message"]["content"]


def test_allow_sampling_is_deterministic_with_a_seed(tiny_backend):
    sampling = serve.TransformersBackend.from_objects(
        tiny_backend.model,
        tiny_backend.tokenizer,
        served_name="tiny-test-model",
        allow_sampling=True,
        seed=7,
    )
    client = TestClient(serve.create_app(sampling, max_new_tokens=4))
    payload = {"model": "tiny-test-model", "messages": MESSAGES, "temperature": 0.8, "max_tokens": 4}
    first = client.post("/v1/chat/completions", json=payload).json()["choices"][0]["message"]["content"]
    second = client.post("/v1/chat/completions", json=payload).json()["choices"][0]["message"]["content"]
    assert first == second


def test_max_tokens_is_honoured_and_reports_length():
    backend = serve.FakeBackend(answer="one two three four five six seven eight")
    client = TestClient(serve.create_app(backend, max_new_tokens=8))
    body = client.post(
        "/v1/chat/completions", json={"messages": MESSAGES, "max_tokens": 3}
    ).json()
    assert body["usage"]["completion_tokens"] == 3
    assert body["choices"][0]["finish_reason"] == "length"
    assert len(body["choices"][0]["message"]["content"].split()) == 3


def test_server_default_max_new_tokens_applies_when_request_omits_it():
    backend = serve.FakeBackend(answer="one two three four five six")
    client = TestClient(serve.create_app(backend, max_new_tokens=2))
    body = client.post("/v1/chat/completions", json={"messages": MESSAGES}).json()
    assert body["usage"]["completion_tokens"] == 2
    assert body["choices"][0]["finish_reason"] == "length"


def test_stop_sequence_truncates_the_answer():
    backend = serve.FakeBackend(answer="SELECT 1 STOP here is noise")
    client = TestClient(serve.create_app(backend, max_new_tokens=64))
    body = client.post(
        "/v1/chat/completions", json={"messages": MESSAGES, "stop": ["STOP"]}
    ).json()
    assert body["choices"][0]["message"]["content"] == "SELECT 1 "
    assert body["choices"][0]["finish_reason"] == "stop"


def test_chat_template_is_used_when_the_tokenizer_has_one(tiny_backend):
    class TemplateTokenizer:
        chat_template = "{{ messages }}"

        @staticmethod
        def apply_chat_template(messages, tokenize=False, add_generation_prompt=True):
            return "TEMPLATED::" + messages[-1]["content"]

    backend = serve.TransformersBackend.from_objects(
        tiny_backend.model, TemplateTokenizer(), served_name="templated"
    )
    assert backend._render_prompt(MESSAGES) == "TEMPLATED::How many users are there?"


# ---------------------------------------------------------------------------
# Errors: 400, 404, streaming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": []},
        {"messages": "not a list"},
        {"messages": [{"content": "no role"}]},
        {"messages": [{"role": "user"}]},
        {"messages": [{"role": "user", "content": 42}]},
        {"messages": MESSAGES, "temperature": "hot"},
        {"messages": MESSAGES, "temperature": 3.5},
        {"messages": MESSAGES, "max_tokens": 0},
        {"messages": MESSAGES, "max_tokens": "many"},
        {"messages": MESSAGES, "stop": 7},
        {"messages": MESSAGES, "n": 0},
    ],
)
def test_malformed_requests_are_400(payload):
    response = _fake_app().post("/v1/chat/completions", json=payload)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert isinstance(error["message"], str) and error["message"]


def test_non_json_body_is_400():
    response = _fake_app().post(
        "/v1/chat/completions", content=b"not json at all", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_streaming_is_rejected_explicitly():
    response = _fake_app().post(
        "/v1/chat/completions", json={"messages": MESSAGES, "stream": True}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == serve.STREAMING_ERROR_CODE
    assert "stream" in error["message"].lower()
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_unknown_model_is_404_unless_relaxed():
    strict = _fake_app()
    response = strict.post("/v1/chat/completions", json={"model": "not-a-model", "messages": MESSAGES})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"

    relaxed = _fake_app(relax_model_name=True)
    response = relaxed.post("/v1/chat/completions", json={"model": "not-a-model", "messages": MESSAGES})
    assert response.status_code == 200
    assert response.json()["model"] == "fake-local-model"  # the served name is always reported


def test_unknown_fields_are_ignored_gracefully():
    payload = {
        "messages": MESSAGES,
        "model": "fake-local-model",
        "top_p": 0.9,
        "presence_penalty": 0.1,
        "frequency_penalty": 0.1,
        "seed": 42,
        "user": "benchmark",
        "logprobs": True,
        "response_format": {"type": "text"},
        "tools": [{"type": "function", "function": {"name": "noop"}}],
        "tool_choice": "auto",
        "n": 1,
        "stream": False,
    }
    response = _fake_app().post("/v1/chat/completions", json=payload)
    assert response.status_code == 200
    assert len(response.json()["choices"]) == 1


def test_content_parts_are_accepted():
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "How many "}, {"type": "text", "text": "users?"}]}
        ]
    }
    body = _fake_app().post("/v1/chat/completions", json=payload).json()
    assert "How many users?" in body["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Startup guards, CLI defaults, documentation and dependencies
# ---------------------------------------------------------------------------


def test_adapter_path_must_exist(tmp_path, capsys):
    missing = tmp_path / "no-such-adapter"
    with pytest.raises(FileNotFoundError):
        serve.TransformersBackend.load("Qwen/Qwen2.5-Coder-1.5B-Instruct", adapter=str(missing))

    exit_code = serve.main(["--adapter", str(missing), "--fake", "--port", "0"])
    assert exit_code == 2
    assert "adapter path does not exist" in capsys.readouterr().err


def test_missing_optional_dependencies_fail_before_any_download(tmp_path):
    """An existing adapter without peft must fail fast, not after 3 GB."""

    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    try:
        import peft  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="peft"):
            serve.TransformersBackend.load("Qwen/Qwen2.5-Coder-1.5B-Instruct", adapter=str(adapter_dir))
    else:  # pragma: no cover - this machine has no peft
        pytest.skip("peft is installed; the fast-fail path cannot be exercised")

    try:
        import bitsandbytes  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="bitsandbytes"):
            serve.TransformersBackend.load("Qwen/Qwen2.5-Coder-1.5B-Instruct", load_in_4bit=True)
    else:  # pragma: no cover
        pytest.skip("bitsandbytes is installed; the 4-bit guard cannot be exercised")


def test_max_new_tokens_must_be_positive(capsys):
    assert serve.main(["--fake", "--max-new-tokens", "0", "--port", "0"]) == 2
    assert "max-new-tokens" in capsys.readouterr().err


def test_cli_defaults_match_the_documented_contract():
    args = serve.build_parser().parse_args([])
    assert args.model == "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    assert args.max_new_tokens == serve.DEFAULT_MAX_NEW_TOKENS
    assert (args.host, args.port) == ("127.0.0.1", 8000)
    assert args.device == "auto"
    assert args.fake is False
    assert args.allow_sampling is False
    assert args.served_name is None


def test_module_docstring_states_why_and_what_it_does_not_do():
    docstring = " ".join(serve.__doc__.lower().split())
    assert "vllm is linux-only" in docstring
    assert "no docker" in docstring
    assert "no batching" in docstring
    assert "no continuous batching" in docstring
    assert "one request is decoded at a time" in docstring
    assert "llm_base_url" in docstring


def test_requirements_serve_pins_the_stack():
    text = REQUIREMENTS.read_text(encoding="utf-8").lower()
    for package in ("fastapi", "uvicorn", "transformers", "torch"):
        assert package in text, package
    for optional in ("peft", "accelerate", "bitsandbytes"):
        assert optional in text, optional
    assert "requirements-serve.txt" in text


def test_test_suite_never_needs_a_gpu_or_the_network():
    """The fake backend must serve requests without importing torch at all."""

    import subprocess

    code = (
        "import sys;"
        "from fastapi.testclient import TestClient;"
        "from dev_tools import serve_openai as serve;"
        "client = TestClient(serve.create_app(serve.FakeBackend()));"
        "r = client.post('/v1/chat/completions', json={'messages': [{'role': 'user', 'content': 'hi'}]});"
        "assert r.status_code == 200, r.text;"
        "assert 'torch' not in sys.modules, 'torch must not be imported for the fake backend';"
        "print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Thinking mode (Qwen3-class arms): the switch, the post-filter and /health
# ---------------------------------------------------------------------------


class _Qwen3TemplateTokenizer:
    """A tokenizer stub whose template advertises ``enable_thinking`` (Qwen3-like)."""

    chat_template = (
        "{% for m in messages %}{{ m['content'] }}{% endfor %}"
        "{% if enable_thinking is defined and not enable_thinking %}<think></think>{% endif %}"
    )

    def __init__(self) -> None:
        self.calls = []

    def apply_chat_template(self, messages, **kwargs):
        self.calls.append(dict(kwargs))
        flag = kwargs.get("enable_thinking")
        return "QWEN3::" + ("off::" if flag is False else "on::") + messages[-1]["content"]


class _Qwen25TemplateTokenizer:
    """A tokenizer stub without the hook: passing the flag would be an invented mode."""

    chat_template = "{% for m in messages %}{{ m['content'] }}{% endfor %}"

    def __init__(self) -> None:
        self.calls = []

    def apply_chat_template(self, messages, **kwargs):
        self.calls.append(dict(kwargs))
        return "QWEN25::" + messages[-1]["content"]


class _RejectingTemplateTokenizer:
    """Template mentions the hook but the method rejects unknown keywords (TypeError)."""

    chat_template = "{% if enable_thinking is defined %}{% endif %}"

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        return "PLAIN::" + messages[-1]["content"]


def test_strip_thinking_removes_a_complete_block():
    text, fired = serve.strip_thinking("<think>count the rows</think>\nSELECT COUNT(*) FROM users")
    assert fired is True
    assert text == "SELECT COUNT(*) FROM users"


def test_strip_thinking_removes_an_unterminated_block():
    """The shape produced when the generation cap is burned inside the reasoning."""

    text, fired = serve.strip_thinking("<think>Let me think about the join, and the filter, and</think")
    assert (text, fired) == ("", True)

    # A blank line is the only boundary we trust: the answer starts after it.
    text, fired = serve.strip_thinking("<think>Let me think about the join and the filter\n\nSELECT 1")
    assert (text, fired) == ("SELECT 1", True)

    # No boundary at all means the generation died inside reasoning: no answer,
    # rather than handing the tool a paragraph of prose.
    text, fired = serve.strip_thinking("<think>reasoning with no boundary\nSELECT 1")
    assert (text, fired) == ("", True)


def test_strip_thinking_removes_a_thinking_prefix():
    text, fired = serve.strip_thinking("Thinking: I should count rows.\n\nSELECT COUNT(*) FROM users")
    assert (text, fired) == ("SELECT COUNT(*) FROM users", True)

    # No blank line: only the lead-in line is dropped, and what follows survives.
    text, fired = serve.strip_thinking("Thinking: I should count rows.\nSELECT COUNT(*) FROM users")
    assert (text, fired) == ("SELECT COUNT(*) FROM users", True)

    # A lone lead-in with no answer is empty rather than prose.
    assert serve.strip_thinking("Thinking: only prose here") == ("", True)


def test_strip_thinking_leaves_a_clean_answer_untouched():
    for answer in (
        "SELECT COUNT(*) FROM users",
        "  SELECT 1  ",
        "SELECT 'Thinking: ok' AS note",
        "SELECT 1\n\n-- second statement is fine too",
    ):
        assert serve.strip_thinking(answer) == (answer, False)


def test_thinking_disabled_passes_the_flag_to_a_qwen3_template(tiny_backend):
    tokenizer = _Qwen3TemplateTokenizer()
    backend = serve.TransformersBackend.from_objects(
        tiny_backend.model, tokenizer, served_name="qwen3-stub", enable_thinking=False
    )
    prompt = backend._render_prompt(MESSAGES)
    assert prompt.startswith("QWEN3::off::")
    assert tokenizer.calls[0]["enable_thinking"] is False
    assert tokenizer.calls[0]["add_generation_prompt"] is True

    body = TestClient(serve.create_app(backend)).get("/health").json()
    assert body["thinking_mode"] == "off"
    assert body["thinking_template_supported"] is True
    assert body["thinking_flag_passed"] is True
    assert body["thinking_stripped_count"] == 0


def test_thinking_flag_is_a_graceful_noop_on_an_old_template(tiny_backend):
    """Qwen2.5 has no ``enable_thinking`` hook: the switch must not invent one."""

    tokenizer = _Qwen25TemplateTokenizer()
    backend = serve.TransformersBackend.from_objects(
        tiny_backend.model, tokenizer, served_name="qwen25-stub", enable_thinking=False
    )
    assert backend._render_prompt(MESSAGES).startswith("QWEN25::")
    assert "enable_thinking" not in tokenizer.calls[0]

    body = TestClient(serve.create_app(backend)).get("/health").json()
    assert body["thinking_mode"] == "off-unsupported"
    assert body["thinking_template_supported"] is False
    assert body["thinking_flag_passed"] is False


def test_thinking_default_mode_leaves_the_template_alone(tiny_backend):
    tokenizer = _Qwen3TemplateTokenizer()
    backend = serve.TransformersBackend.from_objects(
        tiny_backend.model, tokenizer, served_name="qwen3-default"
    )
    assert backend._render_prompt(MESSAGES).startswith("QWEN3::on::")  # template default
    assert "enable_thinking" not in tokenizer.calls[0]
    body = TestClient(serve.create_app(backend)).get("/health").json()
    assert body["thinking_mode"] == "default"
    assert body["thinking_flag_passed"] is False


def test_a_tokenizer_that_rejects_the_keyword_is_reported_as_unsupported(tiny_backend):
    tokenizer = _RejectingTemplateTokenizer()
    backend = serve.TransformersBackend.from_objects(
        tiny_backend.model, tokenizer, served_name="rejecting", enable_thinking=False
    )
    assert backend._render_prompt(MESSAGES).startswith("PLAIN::")
    assert backend.thinking_mode == "off-unsupported"
    assert backend.thinking_flag_passed is False
    assert TestClient(serve.create_app(backend)).get("/health").json()["thinking_flag_passed"] is False


def test_fake_backend_serves_without_a_template_and_says_so():
    body = TestClient(
        serve.create_app(serve.FakeBackend(enable_thinking=False))
    ).get("/health").json()
    assert body["thinking_mode"] == "not-applicable"
    assert body["thinking_flag_passed"] is False
    assert body["thinking_stripped_count"] == 0


def test_server_strips_a_residual_reasoning_block_and_counts_it():
    backend = serve.FakeBackend(
        answer="<think>count the rows of the users table</think>\nSELECT COUNT(*) FROM users"
    )
    client = TestClient(serve.create_app(backend))

    first = client.post("/v1/chat/completions", json={"messages": MESSAGES}).json()
    assert first["choices"][0]["message"]["content"] == "SELECT COUNT(*) FROM users"
    assert client.get("/health").json()["thinking_stripped_count"] == 1

    client.post("/v1/chat/completions", json={"messages": MESSAGES})
    assert client.get("/health").json()["thinking_stripped_count"] == 2


def test_server_returns_a_clean_answer_untouched():
    backend = serve.FakeBackend(answer="SELECT COUNT(*) FROM users")
    client = TestClient(serve.create_app(backend))
    body = client.post("/v1/chat/completions", json={"messages": MESSAGES}).json()
    assert body["choices"][0]["message"]["content"] == "SELECT COUNT(*) FROM users"
    assert client.get("/health").json()["thinking_stripped_count"] == 0


def test_cli_thinking_flags_are_explicit_and_mutually_exclusive():
    parser = serve.build_parser()
    assert parser.parse_args([]).enable_thinking is None
    assert parser.parse_args(["--no-thinking"]).enable_thinking is False
    assert parser.parse_args(["--thinking"]).enable_thinking is True
    with pytest.raises(SystemExit):
        parser.parse_args(["--thinking", "--no-thinking"])


def test_every_construction_path_accepts_the_thinking_switch():
    """``load`` is what the A100/Windows serving command calls, so wire it everywhere."""

    import inspect

    assert "enable_thinking" in inspect.signature(serve.TransformersBackend.load).parameters
    assert "enable_thinking" in inspect.signature(serve.TransformersBackend.from_objects).parameters
    assert "enable_thinking" in inspect.signature(serve.TransformersBackend.__init__).parameters
    assert "enable_thinking" in inspect.signature(serve.FakeBackend.__init__).parameters


def test_cli_starts_and_prints_the_thinking_mode(monkeypatch, capsys):
    """The operator sees the mode at startup; the flag reaches the backend object."""

    uvicorn = pytest.importorskip("uvicorn")
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.update(app=app, kwargs=kwargs))
    assert serve.main(["--fake", "--no-thinking", "--port", "0"]) == 0
    out = capsys.readouterr().out
    assert "thinking_mode=not-applicable" in out
    assert "thinking_stripped_count" in out
    assert "app" in started


def test_thinking_mode_vocabulary_is_closed():
    """``/health.thinking_mode`` is a closed set: a consumer can branch on it."""

    assert set(serve.THINKING_MODES) == {
        "default",
        "off",
        "on",
        "off-unsupported",
        "on-unsupported",
        "not-applicable",
    }
    assert serve.thinking_mode_label(None, supported=True) == "default"
    assert serve.thinking_mode_label(False, supported=True) == "off"
    assert serve.thinking_mode_label(True, supported=True) == "on"
    assert serve.thinking_mode_label(False, supported=False) == "off-unsupported"
    assert serve.thinking_mode_label(True, supported=False) == "on-unsupported"


def test_thinking_provenance_reaches_run_json():
    """``run.json`` must record which thinking mode produced a number."""

    from dev_tools import run_benchmark

    health = TestClient(serve.create_app(serve.FakeBackend())).get("/health").json()
    metrics = run_benchmark._server_metrics(health)
    assert metrics["thinking_mode"] == "not-applicable"
    assert metrics["thinking_template_supported"] is False
    assert metrics["thinking_flag_passed"] is False
    assert metrics["thinking_stripped_count"] == 0
