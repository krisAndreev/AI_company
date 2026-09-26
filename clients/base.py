"""
Provider-independent model client interface.

Every model provider (Ollama, OpenAI, ...) implements ModelClient.
The rest of the system only depends on this file, never on a specific provider.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ModelClientError(Exception):
    """Any failure talking to a model: server down, model missing, timeout, bad reply."""


@dataclass
class ChatMessage:
    role: str  # "system", "user" or "assistant"
    content: str


@dataclass
class ModelResponse:
    text: str
    model: str
    provider: str
    duration_seconds: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking: str = ""  # reasoning text, for models that produce it separately
    raw: dict = field(default_factory=dict, repr=False)  # provider's original reply


class ModelClient(ABC):
    provider: str = "unknown"

    @abstractmethod
    def is_available(self) -> bool:
        """True if the provider can be reached right now."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Names of the models this provider can run."""

    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float | None = None,
        json_schema: dict | None = None,
        think: bool | None = None,
    ) -> ModelResponse:
        """Send a conversation and return the model's reply.

        json_schema: if given, ask the provider to constrain output to this JSON schema.
        Providers that cannot do this may ignore it; callers must still validate the reply.
        think: turn a reasoning model's hidden "thinking" on/off (None = model default).
        """

    def generate(
        self,
        prompt: str,
        model: str,
        system: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        think: bool | None = None,
    ) -> ModelResponse:
        """Convenience wrapper: one prompt (plus optional system prompt) in, one reply out."""
        messages = []
        if system:
            messages.append(ChatMessage("system", system))
        messages.append(ChatMessage("user", prompt))
        return self.chat(messages, model=model, temperature=temperature,
                         json_schema=json_schema, think=think)
