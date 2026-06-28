"""Pydantic request/response models for the public API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- Search ---------------------------------------------------------------

class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    score: float | None = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    backend: str | None = Field(
        None, description="Search backend; defaults to the server's configured one."
    )
    num_results: int = Field(10, ge=1, le=50)
    aggregate: bool = Field(
        False, description="Fan out across multiple backends and merge results."
    )
    backends: list[str] | None = Field(
        None, description="Backends to use when aggregate=true (default: all configured)."
    )


class SearchResponse(BaseModel):
    query: str
    backend: str
    results: list[SearchResult]


# --- Auth -----------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


Role = Literal["admin", "user"]


class LoginResponse(BaseModel):
    token: str
    username: str
    role: Role = "user"
    expires_at: int


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=6, max_length=256)


class RegisterResponse(BaseModel):
    status: str = "pending"
    message: str


class AppSettings(BaseModel):
    # Optional so PUT /v1/admin/settings can patch one field at a time; GET
    # always returns concrete values.
    registration_enabled: bool | None = None
    model_provider: str | None = None  # global LLM provider for non-admins


# --- Users (admin management) --------------------------------------------

class UserOut(BaseModel):
    username: str
    role: Role
    disabled: bool
    created_at: float


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    role: Role = "user"


class PasswordReset(BaseModel):
    password: str = Field(..., min_length=1)


class RoleUpdate(BaseModel):
    role: Role


class DisabledUpdate(BaseModel):
    disabled: bool


# --- Token usage ----------------------------------------------------------

class UsageTotals(BaseModel):
    user: str
    input_tokens: int = 0
    output_tokens: int = 0
    events: int = 0
    last_used: float | None = None


class UsageEventOut(BaseModel):
    user: str
    conversation_id: str | None = None
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    created_at: float


class UsageSeriesPoint(BaseModel):
    day: float
    input_tokens: int
    output_tokens: int


# --- Chat -----------------------------------------------------------------

class ChatImage(BaseModel):
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data: str = Field(..., description="base64-encoded image bytes, no data: prefix")


class ChatDocument(BaseModel):
    filename: str = ""
    media_type: str = Field("", description="e.g. application/pdf, text/plain")
    data: str = Field(..., description="base64-encoded file bytes")


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = ""
    images: list[ChatImage] = Field(default_factory=list)
    documents: list[ChatDocument] = Field(default_factory=list)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    conversation_id: str | None = Field(
        None,
        description="Continue an existing conversation; omit to start a new one.",
    )
    provider: str | None = Field(None, description="anthropic | openai | local")
    model: str | None = None
    search_backend: str | None = Field(
        None, description="Backend the web_search tool should use."
    )
    enable_search: bool = Field(True, description="Expose the web_search tool to the model.")
    deep_research: bool = Field(
        False, description="Deep-research mode: deeper iteration + fetch_url tool."
    )
    stream: bool = Field(True, description="Stream the answer as SSE (false = single JSON).")


class ChatResponse(BaseModel):
    provider: str
    model: str
    content: str
    tool_calls: int = 0
    conversation_id: str | None = None


# --- Stored chat history --------------------------------------------------

class StoredMessage(BaseModel):
    role: str
    content: str
    created_at: float


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float
    message_count: int


class ConversationDetail(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float
    messages: list[StoredMessage]


class ShareResponse(BaseModel):
    token: str
