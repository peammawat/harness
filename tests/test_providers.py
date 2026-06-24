"""Provider message conversion: image (vision) blocks + raw round-trip."""
from __future__ import annotations

from app.config import Settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import ImagePart, Message
from app.llm.openai_provider import OpenAIProvider


def _settings() -> Settings:
    return Settings(_env_file=None)


# --- Anthropic --------------------------------------------------------------

def test_anthropic_user_image_becomes_block_list():
    _, out = AnthropicProvider._to_native(
        [Message(role="user", content="hi", images=[ImagePart("image/png", "abc")])]
    )
    blocks = out[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[0] == {"type": "text", "text": "hi"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "abc",
    }


def test_anthropic_text_only_stays_string():
    _, out = AnthropicProvider._to_native([Message(role="user", content="hi")])
    assert out[0]["content"] == "hi"


def test_anthropic_assistant_raw_round_trips():
    raw = [{"type": "text", "text": "x"}]
    _, out = AnthropicProvider._to_native(
        [Message(role="assistant", content="x", raw=raw)]
    )
    assert out[0]["content"] == raw


# --- OpenAI -----------------------------------------------------------------

def test_openai_user_image_becomes_image_url():
    p = OpenAIProvider(_settings())
    out = p._to_native(
        [Message(role="user", content="hi", images=[ImagePart("image/png", "abc")])]
    )
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "hi"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,abc"


def test_openai_non_vision_drops_image_with_note():
    p = OpenAIProvider(_settings(), supports_vision=False)
    out = p._to_native(
        [Message(role="user", content="hi", images=[ImagePart("image/png", "abc")])]
    )
    assert isinstance(out[0]["content"], str)
    assert "ไม่รองรับการดูภาพ" in out[0]["content"]


def test_openai_assistant_raw_round_trips():
    p = OpenAIProvider(_settings())
    raw = {"role": "assistant", "content": "x"}
    out = p._to_native([Message(role="assistant", raw=raw)])
    assert out[0] == raw
