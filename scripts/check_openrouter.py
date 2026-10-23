"""Check OpenRouter credentials and the same AutoGen client used by benchmarks."""

import argparse
import asyncio
from pathlib import Path

from autogen_core.models import UserMessage
from autogen_ext.models.openai import OpenAIChatCompletionClient
from dotenv import dotenv_values


async def check(env_path: Path) -> int:
    if not env_path.is_file():
        print(f"Configuration file not found: {env_path}")
        return 2

    config = dotenv_values(env_path)
    key = config.get("LLM_API_KEY") or ""
    if not key:
        print(f"Set LLM_API_KEY in {env_path} to your OpenRouter API key.")
        print("Do not paste the key into chat or commit it to Git.")
        return 2
    if not key.startswith("sk-or-"):
        print("LLM_API_KEY does not look like an OpenRouter key; no request sent.")
        return 2
    if config.get("LLM_BASE_URL", "").rstrip("/") != "https://openrouter.ai/api/v1":
        print("Expected LLM_BASE_URL=https://openrouter.ai/api/v1; no request sent.")
        return 2

    model = config.get("LLM_MODEL_NAME") or "openai/gpt-5.4-mini"
    parameters = {
        "model": model,
        "base_url": config["LLM_BASE_URL"],
        "api_key": key,
        "temperature": 0,
        "max_tokens": 16,
        "max_retries": 0,
        "timeout": 30,
        "model_info": {
            "json_output": False,
            "function_calling": True,
            "vision": False,
            "family": "unknown",
            "structured_output": False,
        },
    }
    effort = config.get("LLM_REASONING_EFFORT")
    if effort:
        parameters["reasoning_effort"] = effort

    client = OpenAIChatCompletionClient(**parameters)
    try:
        response = await client.create(
            [UserMessage(content="Reply with exactly OK.", source="user")]
        )
        print(f"Connected to OpenRouter: {model}")
        print("Response:", str(response.content).replace(key, "[REDACTED]"))
        print("Usage:", response.usage)
        return 0
    except Exception as error:
        message = str(error).replace(key, "[REDACTED]")
        print(f"Connection failed: {type(error).__name__}: {message}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.openrouter"))
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(check(arguments.env_file)))
