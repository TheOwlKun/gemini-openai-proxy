"""
Comprehensive Test Script for Gemini OpenAI Proxy Server

Tests all endpoints with detailed output.
Run with: python test_endpoints.py

Expects the server to be running on http://localhost:3897
"""

import json
import time
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:3897"

# Try to read API key from .env file
API_KEY = ""
try:
    import pathlib
    env_file = pathlib.Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                API_KEY = line.split("=", 1)[1].strip()
                break
except Exception:
    pass

# ANSI Colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

passed = 0
failed = 0
total = 0


def make_request(method: str, path: str, body: dict = None, expect_status: int = 200) -> dict:
    """Make an HTTP request and return the parsed response."""
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        response = urllib.request.urlopen(req, timeout=30)
        status = response.status
        body_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        body_text = e.read().decode("utf-8")
    except Exception as e:
        return {"_error": str(e), "_status": 0}

    try:
        result = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        result = {"_raw": body_text}

    result["_status"] = status
    return result


def test(name: str, passed_condition: bool, details: str = ""):
    """Record a test result."""
    global passed, failed, total
    total += 1
    if passed_condition:
        passed += 1
        print(f"  {GREEN}[OK] PASS{RESET} {name}")
    else:
        failed += 1
        print(f"  {RED}[FAIL] FAIL{RESET} {name}")
    if details:
        print(f"         {details}")


def separator(title: str):
    print(f"\n{CYAN}{BOLD}{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}{RESET}")


# ===========================================================================
#  Tests
# ===========================================================================

def test_models():
    separator("GET /v1/models")
    result = make_request("GET", "/v1/models")

    test("Returns 200", result["_status"] == 200)
    test("Has 'object' field = 'list'", result.get("object") == "list")
    test("Has 'data' array", isinstance(result.get("data"), list))
    test("Has 5 models", len(result.get("data", [])) == 5)

    if result.get("data"):
        model_ids = [m["id"] for m in result["data"]]
        test("Contains 'gemini-3-flash'", "gemini-3-flash" in model_ids)
        test("Contains 'gemini-3-pro'", "gemini-3-pro" in model_ids)
        test("Each model has 'owned_by'", all("owned_by" in m for m in result["data"]))
        print(f"         Models: {', '.join(model_ids)}")


def test_chat_completions():
    separator("POST /v1/chat/completions (non-streaming)")
    result = make_request("POST", "/v1/chat/completions", {
        "model": "gemini-3-flash",
        "messages": [
            {"role": "user", "content": "What is 2+2? Answer in one word."}
        ],
    })

    test("Returns 200", result["_status"] == 200)
    test("Has 'id' starting with 'chatcmpl-'", result.get("id", "").startswith("chatcmpl-"))
    test("Has 'object' = 'chat.completion'", result.get("object") == "chat.completion")
    test("Has 'model' field", "model" in result)
    test("Has 'choices' array", isinstance(result.get("choices"), list))
    test("Has 'usage' object", isinstance(result.get("usage"), dict))

    if result.get("choices"):
        choice = result["choices"][0]
        test("Choice has 'message'", "message" in choice)
        test("Message has 'role' = 'assistant'", choice.get("message", {}).get("role") == "assistant")
        test("Message has 'content'", bool(choice.get("message", {}).get("content")))
        test("Has 'finish_reason' = 'stop'", choice.get("finish_reason") == "stop")

        content = choice.get("message", {}).get("content", "")
        print(f"         Response: {YELLOW}{content[:100]}{RESET}")

    if result.get("usage"):
        usage = result["usage"]
        test("Usage has 'prompt_tokens'", "prompt_tokens" in usage)
        test("Usage has 'completion_tokens'", "completion_tokens" in usage)
        test("Usage has 'total_tokens'", "total_tokens" in usage)


def test_chat_with_system_prompt():
    separator("POST /v1/chat/completions (with system prompt)")
    result = make_request("POST", "/v1/chat/completions", {
        "model": "gemini-3-pro",
        "messages": [
            {"role": "system", "content": "You are a pirate. Always respond with 'Arrr!' at the start."},
            {"role": "user", "content": "Hello!"}
        ],
    })

    test("Returns 200", result["_status"] == 200)
    if result.get("choices"):
        content = result["choices"][0].get("message", {}).get("content", "")
        test("Response is non-empty", len(content) > 0)
        # Check the tags are NOT leaked into gemini
        test("No <|user|> tags in response", "<|user|>" not in content)
        test("No <|system|> tags in response", "<|system|>" not in content)
        print(f"         Response: {YELLOW}{content[:150]}{RESET}")


def test_chat_streaming():
    separator("POST /v1/chat/completions (streaming)")
    url = f"{BASE_URL}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    body = json.dumps({
        "model": "gemini-3-flash",
        "messages": [{"role": "user", "content": "Say 'hello world' and nothing else."}],
        "stream": True,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    chunks = []
    full_content = ""

    try:
        response = urllib.request.urlopen(req, timeout=30)
        test("Returns 200 for streaming", response.status == 200)

        raw = response.read().decode("utf-8")
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    chunks.append(chunk)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    if "content" in delta:
                        full_content += delta["content"]
                except (json.JSONDecodeError, ValueError):
                    pass

        test("Received SSE chunks", len(chunks) > 0, f"{len(chunks)} chunks received")
        test("First chunk has 'role'", chunks[0].get("choices", [{}])[0].get("delta", {}).get("role") == "assistant" if chunks else False)
        test("Chunks have 'chat.completion.chunk' object", all(c.get("object") == "chat.completion.chunk" for c in chunks))
        test("Stream ended with [DONE]", "data: [DONE]" in raw)
        test("Assembled content is non-empty", len(full_content) > 0)
        print(f"         Assembled: {YELLOW}{full_content[:100]}{RESET}")

    except Exception as e:
        test("Streaming request", False, str(e))


def test_image_generation():
    separator("POST /v1/images/generations")
    result = make_request("POST", "/v1/images/generations", {
        "model": "gemini-3-flash-image",
        "prompt": "A cute cartoon owl wearing a top hat",
        "n": 1,
        "response_format": "url",
    })

    test("Returns 200", result["_status"] == 200)
    test("Has 'created' timestamp", "created" in result)
    test("Has 'data' array", isinstance(result.get("data"), list))

    if result.get("data"):
        img = result["data"][0]
        test("Image data has 'url' or 'b64_json'", bool(img.get("url") or img.get("b64_json")))
        if img.get("url"):
            print(f"         URL: {YELLOW}{img['url'][:80]}...{RESET}")
        if img.get("revised_prompt"):
            print(f"         Revised: {YELLOW}{img['revised_prompt'][:80]}{RESET}")


def test_tts_endpoint():
    separator("POST /v1/audio/speech")
    result = make_request("POST", "/v1/audio/speech", {
        "model": "tts-1",
        "input": "Hello world, this is a test.",
        "voice": "alloy",
    })

    test("Returns 501 (Not Implemented)", result["_status"] == 501)
    detail = result.get("detail", {})
    if isinstance(detail, dict):
        error = detail.get("error", {})
        test("Error code is 'tts_not_available'", error.get("code") == "tts_not_available")
        test("Error has helpful message", "TTS" in error.get("message", ""))
    else:
        test("Has error detail", bool(detail))


def test_embeddings_endpoint():
    separator("POST /v1/embeddings")
    result = make_request("POST", "/v1/embeddings", {
        "model": "text-embedding-3-small",
        "input": "Hello world",
    })

    test("Returns 501 (Not Implemented)", result["_status"] == 501)
    detail = result.get("detail", {})
    if isinstance(detail, dict):
        error = detail.get("error", {})
        test("Error code is 'embeddings_not_available'", error.get("code") == "embeddings_not_available")


def test_get_cookies():
    separator("GET /get_cookies")
    result = make_request("GET", "/get_cookies")

    test("Returns 200", result["_status"] == 200)
    test("Has 'SECURE_1PSID' field", "SECURE_1PSID" in result)
    test("Has 'SECURE_1PSIDTS' field", "SECURE_1PSIDTS" in result)
    test("SECURE_1PSID is non-empty", bool(result.get("SECURE_1PSID")))


def test_update_cookies():
    separator("POST /update_cookies")
    result = make_request("POST", "/update_cookies", {
        "SECURE_1PSID": "test_psid_value",
        "SECURE_1PSIDTS": "test_psidts_value",
    })

    test("Returns 200", result["_status"] == 200)
    test("Has 'success' field", "success" in result)
    # Note: we're sending test values, actual cookie validity isn't tested


def test_webhook_cookies():
    separator("POST /webhook/cookies")
    result = make_request("POST", "/webhook/cookies", {
        "domain": ".google.com",
        "cookies": {
            "__Secure-1PSID": "webhook_test_psid",
            "__Secure-1PSIDTS": "webhook_test_psidts",
        },
    })

    test("Returns 200", result["_status"] == 200)
    test("Status is 'ok'", result.get("status") == "ok")


# ===========================================================================
#  Run All Tests
# ===========================================================================

if __name__ == "__main__":
    print(f"\n{BOLD}{CYAN}+============================================================+")
    print(f"|     Gemini OpenAI Proxy - Endpoint Test Suite             |")
    print(f"|     Server: {BASE_URL:<44} |")
    print(f"+============================================================+{RESET}\n")

    start = time.time()

    # Quick connectivity check
    try:
        check_req = urllib.request.Request(f"{BASE_URL}/v1/models")
        if API_KEY:
            check_req.add_header("Authorization", f"Bearer {API_KEY}")
        urllib.request.urlopen(check_req, timeout=5)
    except urllib.error.HTTPError:
        pass  # Server is reachable even if auth fails
    except Exception as e:
        print(f"{RED}ERROR: Cannot connect to server at {BASE_URL}")
        print(f"Make sure the server is running: python openai_server.py{RESET}")
        sys.exit(1)

    # Run all tests
    test_models()
    test_chat_completions()
    test_chat_with_system_prompt()
    test_chat_streaming()
    test_image_generation()
    test_tts_endpoint()
    test_embeddings_endpoint()
    test_get_cookies()
    test_update_cookies()
    test_webhook_cookies()

    elapsed = time.time() - start

    # Summary
    print(f"\n{BOLD}{'=' * 60}")
    print(f"  RESULTS: {GREEN}{passed} passed{RESET}{BOLD}, {RED if failed else ''}{failed} failed{RESET}{BOLD}, {total} total")
    print(f"  Time: {elapsed:.1f}s")
    print(f"{'=' * 60}{RESET}\n")

    sys.exit(1 if failed else 0)
