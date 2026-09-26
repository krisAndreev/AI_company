"""
Phase 2 test: OllamaClient works and fails safely.

Run:  python test_phase2.py
"""

import sys

from clients import ChatMessage, ModelClient, ModelClientError, ModelResponse, OllamaClient

MODEL = "gemma3:4b"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append(True)
        print(f"[pass] {name}" + (f" -> {detail}" if detail else ""))
    except Exception as e:
        results.append(False)
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


client = OllamaClient()


def t_interface():
    assert isinstance(client, ModelClient)
    try:
        ModelClient()  # abstract, must not be instantiable
    except TypeError:
        return "OllamaClient is a ModelClient; base is abstract"
    raise AssertionError("ModelClient should be abstract")


def t_available():
    assert client.is_available(), "Ollama not reachable"
    return f"version {client.version()}"


def t_models():
    models = client.list_models()
    assert MODEL in models, f"{MODEL} missing from {models}"
    return ", ".join(models)


def t_generate():
    r = client.generate("What is 2 + 2? Answer with just the number.", model=MODEL, temperature=0)
    assert isinstance(r, ModelResponse)
    assert "4" in r.text, f"unexpected answer: {r.text!r}"
    assert r.provider == "ollama" and r.completion_tokens > 0
    return f"{r.text!r} in {r.duration_seconds:.1f}s, {r.prompt_tokens}+{r.completion_tokens} tokens"


def t_system_prompt():
    r = client.generate("Hello!", model=MODEL, temperature=0,
                        system="You are a test bot. Whatever the user says, reply with exactly the word PONG.")
    assert "PONG" in r.text.upper(), f"system prompt ignored: {r.text!r}"
    return repr(r.text)


def t_conversation():
    r = client.chat([
        ChatMessage("user", "My favourite colour is blue. Just say OK."),
        ChatMessage("assistant", "OK."),
        ChatMessage("user", "What is my favourite colour? One word."),
    ], model=MODEL, temperature=0)
    assert "blue" in r.text.lower(), f"history not used: {r.text!r}"
    return repr(r.text)


def t_loaded():
    loaded = client.loaded_models()
    m = next(m for m in loaded if m["name"] == MODEL)
    return f"{MODEL}: {m['size'] / 1e9:.2f} GB total, {m['size_vram'] / 1e9:.2f} GB in VRAM"


def t_missing_model():
    try:
        client.generate("hi", model="does-not-exist:1b")
    except ModelClientError as e:
        return f"ModelClientError: {e}"
    raise AssertionError("expected ModelClientError")


def t_server_down():
    dead = OllamaClient(base_url="http://localhost:1", timeout=3)
    assert dead.is_available() is False
    try:
        dead.generate("hi", model=MODEL)
    except ModelClientError:
        return "is_available() False, generate() raises ModelClientError"
    raise AssertionError("expected ModelClientError")


check("interface", t_interface)
check("server available", t_available)
check("model installed", t_models)
check("generate", t_generate)
check("system prompt", t_system_prompt)
check("conversation history", t_conversation)
check("loaded model info", t_loaded)
check("missing model -> clean error", t_missing_model)
check("server down -> clean error", t_server_down)

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
