from __future__ import annotations

import importlib
import asyncio
import os
import sys
from types import SimpleNamespace


def _reload_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in (
        "MODEL_ID",
        "API_KEY",
        "API_BASE",
        "TEMPERATURE",
        "EDGEBOT_MAX_CONCURRENT_SUBAGENTS",
    ):
        os.environ.pop(name, None)
    sys.modules.pop("edgebot.config", None)
    return importlib.import_module("edgebot.config")


def test_edgebot_config_env_overrides_workspace_env(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text(
        "MODEL_ID=deepseek/deepseek-chat\n"
        "API_KEY=root-key\n"
        "API_BASE=https://root.example/v1\n"
        "TEMPERATURE=0.7\n",
        encoding="utf-8",
    )
    runtime_dir = tmp_path / ".edgebot"
    runtime_dir.mkdir()
    (runtime_dir / "config.env").write_text(
        "MODEL_ID=moonshot/kimi-k2.7-code\n"
        "API_KEY=runtime-key\n"
        "API_BASE=https://api.moonshot.cn/v1\n"
        "TEMPERATURE=1.0\n",
        encoding="utf-8",
    )

    config = _reload_config(monkeypatch, tmp_path)

    assert config.MODEL == "moonshot/kimi-k2.7-code"
    assert config.API_KEY == "runtime-key"
    assert config.API_BASE == "https://api.moonshot.cn/v1"
    assert config.GENERATION_TEMPERATURE == 1.0


def test_seed_workspace_templates_creates_runtime_config(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text(
        "MODEL_ID=deepseek/deepseek-chat\n"
        "API_KEY=root-key\n",
        encoding="utf-8",
    )
    _reload_config(monkeypatch, tmp_path)
    sys.modules.pop("edgebot.agent.context", None)
    context = importlib.import_module("edgebot.agent.context")

    context.seed_workspace_templates()

    config_file = tmp_path / ".edgebot" / "config.env"
    content = config_file.read_text(encoding="utf-8")
    assert "MODEL_ID=deepseek/deepseek-chat" in content
    assert "TEMPERATURE=0.7" in content
    assert "moonshot/kimi-k2.7-code" in content


def test_subagent_concurrency_limit_config(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text(
        "MODEL_ID=deepseek/deepseek-chat\n"
        "API_KEY=root-key\n",
        encoding="utf-8",
    )

    config = _reload_config(monkeypatch, tmp_path)
    assert config.MAX_CONCURRENT_SUBAGENTS == 1

    sys.modules.pop("edgebot.config", None)
    monkeypatch.setenv("EDGEBOT_MAX_CONCURRENT_SUBAGENTS", "3")
    config = importlib.import_module("edgebot.config")
    assert config.MAX_CONCURRENT_SUBAGENTS == 3


def test_invalid_subagent_concurrency_limit_config_errors(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text(
        "MODEL_ID=deepseek/deepseek-chat\n"
        "API_KEY=root-key\n",
        encoding="utf-8",
    )
    _reload_config(monkeypatch, tmp_path)

    sys.modules.pop("edgebot.config", None)
    monkeypatch.setenv("EDGEBOT_MAX_CONCURRENT_SUBAGENTS", "0")
    try:
        importlib.import_module("edgebot.config")
    except RuntimeError as exc:
        assert "EDGEBOT_MAX_CONCURRENT_SUBAGENTS" in str(exc)
    else:
        raise AssertionError("expected invalid subagent concurrency config to fail")


def test_kimi_k27_code_forces_temperature_one(monkeypatch) -> None:
    from edgebot.providers import litellm_provider
    from edgebot.providers.litellm_provider import LiteLLMProvider

    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="ok")
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(litellm_provider.litellm, "acompletion", fake_acompletion)

    provider = LiteLLMProvider(
        api_key="key",
        model="moonshot/kimi-k2.7-code",
        api_base="https://api.moonshot.cn/v1",
    )
    response = asyncio.run(
        provider.chat(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
        )
    )

    assert response.content == "ok"
    assert captured["temperature"] == 1.0
