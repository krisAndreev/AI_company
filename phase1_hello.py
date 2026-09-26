"""
Phase 1: verify Python can talk to Ollama / Gemma over the local HTTP API.

Uses only the Python standard library (no pip installs needed).
Run:  python phase1_hello.py
"""

import json
import sys
import time
import urllib.error
import urllib.request

OLLAMA_URL = "http://localhost:11434"
MODEL = "gemma3:4b"
PROMPT = "In one short sentence, say hello and tell me what you are."


def main() -> int:
    # 1. Is the Ollama API reachable?
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/version", timeout=5) as resp:
            version = json.loads(resp.read())["version"]
        print(f"[ok] Ollama API reachable, version {version}")
    except (urllib.error.URLError, OSError) as e:
        print(f"[fail] Cannot reach Ollama at {OLLAMA_URL}: {e}")
        print("       Start the Ollama app and try again.")
        return 1

    # 2. Is the model installed?
    with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as resp:
        installed = [m["name"] for m in json.loads(resp.read())["models"]]
    if MODEL not in installed:
        print(f"[fail] Model '{MODEL}' not installed. Installed: {installed}")
        print(f"       Run: ollama pull {MODEL}")
        return 1
    print(f"[ok] Model '{MODEL}' is installed")

    # 3. Send a prompt and print the response.
    payload = json.dumps({"model": MODEL, "prompt": PROMPT, "stream": False}).encode()
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"[..] Sending prompt: {PROMPT!r}")
    start = time.perf_counter()
    try:
        # First call can be slow because the model is loaded into VRAM.
        with urllib.request.urlopen(request, timeout=300) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        print(f"[fail] Generation request failed: {e}")
        return 1
    elapsed = time.perf_counter() - start

    answer = data["response"].strip()
    print(f"[ok] Response received in {elapsed:.1f}s")
    print("-" * 60)
    print(answer)
    print("-" * 60)
    print(f"Tokens generated: {data.get('eval_count', '?')}")
    return 0 if answer else 1


if __name__ == "__main__":
    sys.exit(main())
