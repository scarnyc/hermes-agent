"""Tests for llm.py — Kimi K2.6 composer (post-P169/MOL-560).

P169 review fix-pass: this test file replaces the pre-P169 surface which
asserted against `claude-haiku-4-5-20251001` + `anthropic_adapter` — both
retired since at least P151. The new tests cover the four critical-class
scenarios surfaced by silent-failure-hunter + pr-test-analyzer on PR #200:

  1. Happy path — OpenRouter mock returns content; assert call_args has
     reasoning.effort = "high" (regression guard for I3).
  2. OPENROUTER_API_KEY not set — raise ComposerKeyMissing inside
     _call_composer; llm_compose catches and returns None; ERROR log +
     tripwire written. Most likely production failure mode (envchain drift).
  3. Auth failure (401/403) — raise ComposerAuthFailure inside
     _call_composer; ERROR log + tripwire. Distinct from transient errors.
  4. Transient API error — generic exception → WARNING log + None return.
  5. HERMES_MOCK_LLM_URL routing — env var honored; non-canonical URLs log
     at WARNING (guards against stale-env-var silent redirect).
  6. MAX_PROMPT_CHARS truncation — input > 300k chars truncated with marker.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _reload_llm():
    """Reload llm.py so module-level constants (BASE_URL, _call_count,
    _base_url_announced) reset to fresh state per test. Required because
    P169's per-process counter + announce-once gate are intentionally
    module-level singletons."""
    mod_name = "plugins.memory.tiered.llm"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    return importlib.import_module(mod_name)


class TestLlmComposeSuccess:
    def test_llm_compose_success_with_reasoning_payload(self):
        """Happy path — mock OpenAI client, assert content returned AND
        reasoning.effort=high is in the call's extra_body. P169 review
        fix-pass C4: source-pattern verifier can't catch a runtime payload
        regression; this test locks the call-shape behaviorally."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Composed memory output"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False):
            llm = _reload_llm()
            with patch("openai.OpenAI", return_value=mock_client):
                result = llm.llm_compose("Compose memory", "Context data")

        assert result == "Composed memory output"
        mock_client.chat.completions.create.assert_called_once()
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "moonshotai/kimi-k2.6"
        assert kwargs["max_tokens"] == 4096
        assert kwargs["temperature"] == 0.3
        # P169 C4 lock-in: reasoning_effort regression guard.
        assert kwargs["extra_body"]["reasoning"]["enabled"] is True
        assert kwargs["extra_body"]["reasoning"]["effort"] == "high"
        # Single user message containing the joined prompt+context.
        assert len(kwargs["messages"]) == 1
        assert kwargs["messages"][0]["role"] == "user"
        assert "Compose memory" in kwargs["messages"][0]["content"]
        assert "Context data" in kwargs["messages"][0]["content"]


class TestLlmComposeNoApiKey:
    def test_llm_compose_missing_api_key_returns_none_writes_tripwire(self, tmp_path, monkeypatch, caplog):
        """P169 review fix-pass C2 + C3: OPENROUTER_API_KEY unset is the
        most likely production failure (envchain drift). Must return None,
        log at ERROR, and write a tripwire."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        # Redirect tripwire dir to tmp_path so the test doesn't pollute real state.
        llm = _reload_llm()
        monkeypatch.setattr(llm, "_TRIPWIRE_DIR", str(tmp_path))

        with caplog.at_level("ERROR", logger="plugins.memory.tiered.llm"):
            result = llm.llm_compose("Compose", "Context")

        assert result is None
        assert "OPENROUTER_API_KEY not set" in caplog.text
        tripwires = list(tmp_path.glob("composer-key-missing-*.json"))
        assert len(tripwires) == 1, f"expected 1 key-missing tripwire, got {len(tripwires)}"


class TestLlmComposeAuthError:
    def test_llm_compose_auth_failure_returns_none_writes_tripwire(self, tmp_path, monkeypatch, caplog):
        """P169 review fix-pass I2: expired/revoked OpenRouter key returns
        AuthenticationError. Must surface at ERROR + tripwire (distinct from
        transient errors that log at WARNING)."""
        from openai import AuthenticationError

        monkeypatch.setenv("OPENROUTER_API_KEY", "expired-key")
        llm = _reload_llm()
        monkeypatch.setattr(llm, "_TRIPWIRE_DIR", str(tmp_path))

        mock_client = MagicMock()
        # openai.AuthenticationError requires positional args (message, response, body).
        auth_err = AuthenticationError(
            message="invalid_api_key",
            response=MagicMock(status_code=401, request=MagicMock()),
            body=None,
        )
        mock_client.chat.completions.create.side_effect = auth_err

        with patch("openai.OpenAI", return_value=mock_client):
            with caplog.at_level("ERROR", logger="plugins.memory.tiered.llm"):
                result = llm.llm_compose("Compose", "Context")

        assert result is None
        assert "AUTH FAILURE" in caplog.text
        tripwires = list(tmp_path.glob("composer-auth-failure-*.json"))
        assert len(tripwires) == 1, f"expected 1 auth-failure tripwire, got {len(tripwires)}"


class TestLlmComposeTransientError:
    def test_llm_compose_transient_api_error_returns_none_logs_warning(self, monkeypatch, caplog):
        """P169 review fix-pass I4: rate limit / 5xx / network blip should
        log at WARNING (not ERROR — these self-resolve on next tick) and
        return None without writing a permanent-failure tripwire."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        llm = _reload_llm()

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("OpenRouter 503 backend overloaded")

        with patch("openai.OpenAI", return_value=mock_client):
            with caplog.at_level("WARNING", logger="plugins.memory.tiered.llm"):
                result = llm.llm_compose("Compose", "Context")

        assert result is None
        assert "transient" in caplog.text
        assert "503 backend overloaded" in caplog.text
        # No ERROR-class log; no tripwire dir spam for transient errors.
        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert not error_records, f"transient error should not log at ERROR; got: {error_records}"


class TestLlmComposeMockUrlRouting:
    def test_hermes_mock_llm_url_redirects_base_url(self, monkeypatch):
        """P169 review fix-pass I5: HERMES_MOCK_LLM_URL routes COMPOSER_BASE_URL
        to the local mock for mock-first dev loops. Empty-string value must
        not mask via .strip() (project memory: stale-env-var trap)."""
        monkeypatch.setenv("HERMES_MOCK_LLM_URL", "http://localhost:4010")
        llm = _reload_llm()
        assert llm.COMPOSER_BASE_URL == "http://localhost:4010"

        # Empty-string env var must NOT redirect (defense against masking).
        monkeypatch.setenv("HERMES_MOCK_LLM_URL", "")
        llm = _reload_llm()
        assert llm.COMPOSER_BASE_URL == "https://openrouter.ai/api/v1"

    def test_non_canonical_base_url_logs_warning(self, monkeypatch, caplog):
        """P169 I1: non-canonical BASE_URL announces at WARNING so a stale
        HERMES_MOCK_LLM_URL doesn't silently redirect production to mock."""
        monkeypatch.setenv("HERMES_MOCK_LLM_URL", "http://localhost:4010")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        llm = _reload_llm()

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_client):
            with caplog.at_level("WARNING", logger="plugins.memory.tiered.llm"):
                llm.llm_compose("p", "c")

        # The warning should fire EXACTLY once (announce-once gate).
        warnings = [r for r in caplog.records if "non-canonical" in r.message]
        assert len(warnings) == 1, f"expected exactly one non-canonical warning, got {len(warnings)}"


class TestLlmComposeTruncation:
    def test_oversize_input_is_truncated_with_marker(self, monkeypatch):
        """P169 review fix-pass I5: prompts > MAX_PROMPT_CHARS (300k) are
        truncated with a visible marker. Loop guard — without this, a
        runaway upstream input could pass the gateway-side request size
        guards but fail at OpenRouter with an unhelpful 400."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        llm = _reload_llm()

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        huge_context = "x" * 400_000
        with patch("openai.OpenAI", return_value=mock_client):
            llm.llm_compose("p", huge_context)

        sent = mock_client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert len(sent) <= llm.MAX_PROMPT_CHARS + 200  # marker adds < 200 chars
        assert "[...truncated for size cap — see MOL-168...]" in sent


class TestLlmComposeBudgetCap:
    def test_budget_cap_returns_none_after_max_calls(self, monkeypatch, tmp_path, caplog):
        """P169 review fix-pass I3: per-process call counter caps composer
        runaway. Once MAX_COMPOSER_CALLS_PER_RUN is reached, further calls
        return None + log ERROR + write tripwire without contacting OpenRouter."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setenv("MEMORY_COMPOSER_MAX_CALLS_PER_RUN", "2")
        llm = _reload_llm()
        monkeypatch.setattr(llm, "_TRIPWIRE_DIR", str(tmp_path))

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_client):
            assert llm.llm_compose("p", "c") == "ok"
            assert llm.llm_compose("p", "c") == "ok"
            with caplog.at_level("ERROR", logger="plugins.memory.tiered.llm"):
                third = llm.llm_compose("p", "c")
        assert third is None
        assert "budget cap hit" in caplog.text
        tripwires = list(tmp_path.glob("composer-budget-exceeded-*.json"))
        assert len(tripwires) == 1
        # Third call must not have reached OpenRouter.
        assert mock_client.chat.completions.create.call_count == 2
