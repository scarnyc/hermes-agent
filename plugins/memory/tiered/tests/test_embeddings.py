"""Tests for embeddings.py — in-process fastembed client (P168/MOL-546).

Pre-MOL-546 this module wrapped an httpx call to Ollama. The contract surface
is unchanged (`generate_embedding(text)` and `generate_embeddings(texts)` still
return `Optional[list[float]]` / `list[Optional[list[float]]]`), so these tests
re-verify the SAME contracts against the new in-process implementation.

Mocking shape: tests patch the module-level `_get_model()` singleton accessor
to return a MagicMock with an `.embed(iterable)` method, mirroring the
fastembed.TextEmbedding API surface used by the call sites.
"""

import unittest
from typing import Iterable
from unittest.mock import MagicMock, patch


def _fake_vector(dim: int, fill: float = 0.1):
    """Build a numpy-array-like that exposes .tolist() and len() — what fastembed yields."""
    v = MagicMock()
    v.tolist.return_value = [fill] * dim
    v.__len__.return_value = dim
    return v


def _fake_embed_iter(vectors):
    """Build a callable returning an iterator over `vectors`, mirroring fastembed's .embed()."""
    def _embed(texts: Iterable[str]):
        for v in vectors:
            yield v
    return _embed


class TestGenerateEmbedding(unittest.TestCase):
    """Tests for generate_embedding (single text)."""

    @patch("plugins.memory.tiered.embeddings._get_model")
    def test_generate_embedding_success(self, mock_get_model: MagicMock) -> None:
        """Successful 1024-dim fastembed call returns a list of 1024 floats."""
        model = MagicMock()
        model.embed = _fake_embed_iter([_fake_vector(1024)])
        mock_get_model.return_value = model

        from plugins.memory.tiered.embeddings import generate_embedding

        result = generate_embedding("hello world")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1024)
        self.assertIsInstance(result[0], float)

    def test_generate_embedding_empty_text(self) -> None:
        """Empty string returns None without invoking the model."""
        from plugins.memory.tiered.embeddings import generate_embedding

        result = generate_embedding("")
        self.assertIsNone(result)

    @patch("plugins.memory.tiered.embeddings._get_model")
    def test_generate_embedding_model_raises(self, mock_get_model: MagicMock) -> None:
        """Any exception from the model layer (HF download, ONNX session, etc) returns None."""
        model = MagicMock()
        model.embed = MagicMock(side_effect=RuntimeError("simulated model failure"))
        mock_get_model.return_value = model

        from plugins.memory.tiered.embeddings import generate_embedding

        result = generate_embedding("hello world")
        self.assertIsNone(result)

    @patch("plugins.memory.tiered.embeddings._get_model")
    def test_generate_embedding_wrong_dims(self, mock_get_model: MagicMock) -> None:
        """Vector with wrong dimensions returns None (defensive check, shouldn't trigger in prod)."""
        model = MagicMock()
        model.embed = _fake_embed_iter([_fake_vector(128)])  # wrong dim
        mock_get_model.return_value = model

        from plugins.memory.tiered.embeddings import generate_embedding

        result = generate_embedding("hello world")
        self.assertIsNone(result)

    @patch("plugins.memory.tiered.embeddings._get_model")
    def test_generate_embedding_truncation(self, mock_get_model: MagicMock) -> None:
        """Text longer than MAX_CHARS gets truncated before the embed call."""
        captured = {}

        def _capturing_embed(texts: Iterable[str]):
            captured["texts"] = list(texts)
            yield _fake_vector(1024)

        model = MagicMock()
        model.embed = _capturing_embed
        mock_get_model.return_value = model

        from plugins.memory.tiered.embeddings import MAX_CHARS, generate_embedding

        long_text = "x" * (MAX_CHARS + 5000)
        generate_embedding(long_text)
        self.assertEqual(len(captured["texts"][0]), MAX_CHARS)


class TestGenerateEmbeddings(unittest.TestCase):
    """Tests for generate_embeddings (batch)."""

    @patch("plugins.memory.tiered.embeddings._get_model")
    def test_generate_embeddings_batch(self, mock_get_model: MagicMock) -> None:
        """Batch of 2 texts returns 2 embeddings of correct dim."""
        model = MagicMock()
        model.embed = _fake_embed_iter([_fake_vector(1024, 0.1), _fake_vector(1024, 0.2)])
        mock_get_model.return_value = model

        from plugins.memory.tiered.embeddings import generate_embeddings

        result = generate_embeddings(["hello", "world"])
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 1024)
        self.assertEqual(len(result[1]), 1024)

    def test_generate_embeddings_empty_list(self) -> None:
        """Empty input returns empty list without invoking the model."""
        from plugins.memory.tiered.embeddings import generate_embeddings

        result = generate_embeddings([])
        self.assertEqual(result, [])

    @patch("plugins.memory.tiered.embeddings._get_model")
    def test_generate_embeddings_mixed_empty(self, mock_get_model: MagicMock) -> None:
        """Empty strings in batch get a None slot — model is only called for non-empty inputs."""
        model = MagicMock()
        # Two non-empty texts in the input → two vectors back
        model.embed = _fake_embed_iter([_fake_vector(1024), _fake_vector(1024)])
        mock_get_model.return_value = model

        from plugins.memory.tiered.embeddings import generate_embeddings

        result = generate_embeddings(["hello", "", "world"])
        self.assertEqual(len(result), 3)
        self.assertEqual(len(result[0]), 1024)
        self.assertIsNone(result[1])
        self.assertEqual(len(result[2]), 1024)


if __name__ == "__main__":
    unittest.main()
