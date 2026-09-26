"""
Get validated, structured output from any ModelClient.

Flow per attempt:
  1. ask the model (with the JSON schema, so capable providers constrain output)
  2. extract JSON from the reply
  3. validate it with Pydantic
  4. on failure, tell the model exactly what was wrong and retry
After max_attempts failures, raise StructuredOutputError. Nothing invalid is returned.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from clients import ChatMessage, ModelClient, ModelResponse

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    def __init__(self, message: str, errors: list[str]):
        super().__init__(message)
        self.errors = errors  # one entry per failed attempt


@dataclass
class StructuredResult(Generic[T]):
    value: T
    attempts: int
    responses: list[ModelResponse] = field(default_factory=list, repr=False)

    @property
    def total_tokens(self) -> int:
        return sum(r.prompt_tokens + r.completion_tokens for r in self.responses)

    @property
    def total_seconds(self) -> float:
        return sum(r.duration_seconds for r in self.responses)


def extract_json(text: str) -> dict:
    """Parse a JSON object from model text, tolerating ```json fences and surrounding chatter."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in reply")
        data = json.loads(text[start:end + 1])  # may raise JSONDecodeError (a ValueError)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _format_validation_error(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in e.errors()
    )


def generate_structured(
    client: ModelClient,
    model: str,
    prompt: str,
    schema: type[T],
    system: str | None = None,
    max_attempts: int = 3,
    temperature: float = 0.0,
    think: bool | None = None,
    constrain_to: type[BaseModel] | None = None,
) -> StructuredResult[T]:
    """constrain_to: optional stricter schema sent to the model (e.g. with enums of valid
    names); the reply is still validated with `schema`, so code can reject item by item."""
    json_schema = (constrain_to or schema).model_json_schema()
    system_text = (
        (system + "\n\n" if system else "")
        + "Respond ONLY with a single JSON object matching this JSON schema. "
          "No explanations, no markdown.\n"
        + json.dumps(json_schema)
    )
    messages = [ChatMessage("system", system_text), ChatMessage("user", prompt)]
    errors: list[str] = []
    responses: list[ModelResponse] = []

    for attempt in range(1, max_attempts + 1):
        response = client.chat(messages, model=model, temperature=temperature,
                               json_schema=json_schema, think=think)
        responses.append(response)
        try:
            value = schema.model_validate(extract_json(response.text))
            return StructuredResult(value=value, attempts=attempt, responses=responses)
        except ValidationError as e:
            problem = f"schema validation failed: {_format_validation_error(e)}"
        except ValueError as e:  # includes json.JSONDecodeError
            problem = f"invalid JSON: {e}"

        errors.append(f"attempt {attempt}: {problem}")
        # Show the model its own reply and what was wrong, then retry.
        messages += [
            ChatMessage("assistant", response.text),
            ChatMessage("user", f"Your reply was rejected ({problem}). "
                                "Reply again with ONLY a corrected JSON object."),
        ]

    raise StructuredOutputError(
        f"{schema.__name__}: no valid output after {max_attempts} attempts", errors
    )
