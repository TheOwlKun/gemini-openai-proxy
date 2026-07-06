"""
Gemini → OpenAI Compatible API Proxy Server

Translates OpenAI-format requests into Google Gemini internal API calls,
providing a drop-in replacement for the OpenAI API.

Features:
    - OpenAI-compatible /v1/chat/completions and /v1/images/generations
    - Automatic cookie refresh via webhook subscription
    - Image watermark removal with alpha-blending
    - Image proxying for authenticated Google URLs
    - Tool/function calling translation (XML ↔ JSON)
    - Streaming and non-streaming response modes
    - Cross-platform (no OS-specific code)

Author: TheOwlKun
"""

import os
import sys
import re
import time
import uuid
import json
import html
import base64
import asyncio
import hashlib
import logging
import tempfile
import mimetypes
import urllib.parse
from io import BytesIO
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from contextlib import asynccontextmanager
from xml.etree import ElementTree as ET

import dotenv
import httpx
import numpy as np
from PIL import Image as PILImage
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict
from dotenv import load_dotenv

from src.client import GeminiClient, ChatSession
from src.constants import Model

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s:%(name)s:%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("openai_server")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv(override=True)


# ===========================================================================
#  Session Manager — UUID-based, thread-safe, with LRU eviction
# ===========================================================================

class SessionManager:
    """
    Thread-safe chat session storage with UUID-based keys and LRU eviction.

    Unlike the original hash-based approach (which could collide when two users
    sent the same first message), each session is keyed by a unique UUID that
    is returned to the caller via the ``chatcmpl-`` ID.
    """

    def __init__(self, max_sessions: int = 500, ttl_seconds: int = 86400 * 30):
        self._sessions: Dict[str, tuple[ChatSession, float]] = {}
        self._prompt_index: Dict[str, str] = {}  # prompt_hash → session_key
        self._max = max_sessions
        self._ttl = ttl_seconds

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [k for k, (_, ts) in self._sessions.items() if now - ts > self._ttl]
        for k in expired:
            del self._sessions[k]

    def _evict_lru(self) -> None:
        if len(self._sessions) < self._max:
            return
        sorted_items = sorted(self._sessions.items(), key=lambda x: x[1][1])
        to_remove = len(self._sessions) - self._max + self._max // 3
        for k, _ in sorted_items[:to_remove]:
            del self._sessions[k]

    def get(self, key: str) -> Optional[ChatSession]:
        """Retrieve a session by key, updating its last-access timestamp."""
        if key in self._sessions:
            session, _ = self._sessions[key]
            self._sessions[key] = (session, time.time())
            return session
        return None

    def get_by_prompt_hash(self, prompt_hash: str) -> Optional[tuple[str, ChatSession]]:
        """Look up a session by the hash of the first user message."""
        key = self._prompt_index.get(prompt_hash)
        if key:
            session = self.get(key)
            if session:
                return key, session
            # Stale index entry
            del self._prompt_index[prompt_hash]
        return None

    def create(self, model: Model, prompt_hash: str = "") -> tuple[str, ChatSession]:
        """Create a new session with a unique UUID key."""
        self._evict_expired()
        self._evict_lru()

        key = uuid.uuid4().hex[:16]
        session = ChatSession(geminiclient=gemini_client, model=model)
        self._sessions[key] = (session, time.time())

        if prompt_hash:
            self._prompt_index[prompt_hash] = key

        return key, session

    def store(self, key: str, session: ChatSession, prompt_hash: str = "") -> None:
        """Store/update an existing session under a given key."""
        self._sessions[key] = (session, time.time())
        if prompt_hash:
            self._prompt_index[prompt_hash] = key

    @property
    def count(self) -> int:
        return len(self._sessions)


sessions = SessionManager()


# ===========================================================================
#  Tool Call Parser — XML-first with regex fallback
# ===========================================================================

class ToolCallParser:
    """
    Parses ``<tool_use>`` XML blocks from Gemini responses and converts them
    into OpenAI-compatible tool_call objects.

    Uses ``xml.etree.ElementTree`` for reliable parsing, with a regex fallback
    for malformed/HTML-escaped output.
    """

    # Regex fallback for HTML-escaped or malformed XML
    _REGEX_PATTERN = re.compile(
        r"(?:&lt;|<)tool_use(?:&gt;|>)\s*"
        r"(?:&lt;|<)name(?:&gt;|>)\s*(.*?)\s*(?:&lt;|<)/name(?:&gt;|>)\s*"
        r"(?:&lt;|<)arguments(?:&gt;|>)\s*(.*?)\s*(?:&lt;|<)/arguments(?:&gt;|>)\s*"
        r"(?:&lt;|<)/tool_use(?:&gt;|>)",
        re.DOTALL,
    )

    @classmethod
    def parse(cls, content: str) -> tuple[str, list, list]:
        """
        Parse tool_use blocks from content.

        Returns:
            (remaining_text, tool_calls_list, content_items_list)
        """
        # Try XML parsing first
        tool_calls, content_items, spans = cls._parse_xml(content)

        # Fallback to regex if XML parsing found nothing
        if not tool_calls:
            tool_calls, content_items, spans = cls._parse_regex(content)

        if not tool_calls:
            return content, [], []

        # Remove matched spans from content (reverse order to preserve indices)
        remaining = content
        for start, end in sorted(spans, reverse=True):
            remaining = remaining[:start] + remaining[end:]

        remaining = re.sub(r"\n{3,}", "\n\n", remaining).strip()
        return remaining, tool_calls, content_items

    @classmethod
    def _parse_xml(cls, content: str) -> tuple[list, list, list[tuple[int, int]]]:
        """Try to parse well-formed XML tool_use blocks."""
        tool_calls = []
        content_items = []
        spans = []

        # Find all <tool_use>...</tool_use> blocks
        pattern = re.compile(r"<tool_use>(.*?)</tool_use>", re.DOTALL)
        for match in pattern.finditer(content):
            xml_str = f"<root>{match.group(1)}</root>"
            try:
                root = ET.fromstring(xml_str)
                name_el = root.find("name")
                args_el = root.find("arguments")

                if name_el is None or args_el is None:
                    continue

                name = (name_el.text or "").strip()
                arguments = (args_el.text or "").strip()

                # Clean markdown links from JSON arguments
                arguments = re.sub(r"\[([^\]]*)\]\(([^)]+)\)", r"\2", arguments)

                try:
                    args_dict = json.loads(arguments)
                except (json.JSONDecodeError, ValueError):
                    continue

                call_id = f"call_{uuid.uuid4().hex[:12]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                })
                content_items.append({
                    "type": "tool-call",
                    "toolCallId": call_id,
                    "toolName": name,
                    "input": args_dict,
                })
                spans.append((match.start(), match.end()))

            except ET.ParseError:
                continue

        return tool_calls, content_items, spans

    @classmethod
    def _parse_regex(cls, content: str) -> tuple[list, list, list[tuple[int, int]]]:
        """Regex fallback for HTML-escaped or malformed XML."""
        tool_calls = []
        content_items = []
        spans = []

        for match in cls._REGEX_PATTERN.finditer(content):
            name = match.group(1).strip()
            arguments = html.unescape(match.group(2).strip())
            arguments = re.sub(r"\[([^\]]*)\]\(([^)]+)\)", r"\2", arguments)

            try:
                args_dict = json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                continue

            call_id = f"call_{uuid.uuid4().hex[:12]}"
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            })
            content_items.append({
                "type": "tool-call",
                "toolCallId": call_id,
                "toolName": name,
                "input": args_dict,
            })
            spans.append((match.start(), match.end()))

        return tool_calls, content_items, spans


# ===========================================================================
#  Image Processor — watermark removal & proxying
# ===========================================================================

class ImageProcessor:
    """Handles watermark removal and image URL proxying."""

    ALPHA_THRESHOLD = 0.002

    @staticmethod
    def remove_watermark(image_bytes: bytes) -> bytes:
        """
        Remove the Gemini watermark using fixed-position reverse alpha blending.

        Uses pre-computed alpha maps (assets/bg_48.png, bg_96.png) to identify
        the watermark position and mathematically reverse the blend operation.
        """
        img = PILImage.open(BytesIO(image_bytes))

        if img.mode == "RGBA":
            alpha_channel = img.split()[3]
            img = img.convert("RGB")
            has_alpha = True
        else:
            img = img.convert("RGB")
            has_alpha = False
            alpha_channel = None

        width, height = img.size

        # Select watermark size: both dimensions must exceed 1024 for large watermark
        if width > 1024 and height > 1024:
            wm_size, margin = 96, 64
            alpha_map_path = "assets/bg_96.png"
        else:
            wm_size, margin = 48, 32
            alpha_map_path = "assets/bg_48.png"

        # Fixed bottom-right position
        wm_x = width - margin - wm_size
        wm_y = height - margin - wm_size

        # Boundary check — return original if image is too small
        if wm_x < 0 or wm_y < 0 or wm_x + wm_size > width or wm_y + wm_size > height:
            logger.warning(f"Image too small for watermark removal: {width}x{height}")
            return _save_image(img, has_alpha, alpha_channel)

        # Load alpha map
        try:
            alpha_img = PILImage.open(alpha_map_path).convert("RGB")
            alpha_arr = np.array(alpha_img, dtype=np.float32)
            alpha_map = np.max(alpha_arr, axis=2) / 255.0
        except Exception as e:
            logger.warning(f"Failed to load alpha map: {e}")
            return _save_image(img, has_alpha, alpha_channel)

        img_array = np.array(img, dtype=np.float32)
        logger.info(f"Watermark at ({wm_x},{wm_y}) size {wm_size}x{wm_size}")

        # Apply reverse alpha blending
        wm_region = img_array[wm_y : wm_y + wm_size, wm_x : wm_x + wm_size].copy()

        for row in range(wm_size):
            for col in range(wm_size):
                alpha = alpha_map[row, col]
                if alpha > ImageProcessor.ALPHA_THRESHOLD:
                    alpha = min(alpha, 0.99)  # Prevent division by zero
                    for c in range(3):
                        watermarked = wm_region[row, col, c]
                        original = (watermarked - alpha * 255.0) / (1.0 - alpha)
                        wm_region[row, col, c] = np.clip(original, 0, 255)

        img_array[wm_y : wm_y + wm_size, wm_x : wm_x + wm_size] = wm_region
        result_img = PILImage.fromarray(img_array.astype(np.uint8), "RGB")

        if has_alpha and alpha_channel is not None:
            result_img.putalpha(alpha_channel)

        output = BytesIO()
        result_img.save(output, format="PNG")
        return output.getvalue()

    @staticmethod
    def get_proxy_url(request: Request, image_url: str, model: str = "") -> str:
        """Generate a proxy URL for serving Google-authenticated images."""
        base_url = str(request.base_url).rstrip("/")
        encoded_url = urllib.parse.quote(image_url)
        proxy_url = f"{base_url}/v1/images/proxy?url={encoded_url}"
        if model:
            proxy_url += f"&model={urllib.parse.quote(model)}"
        proxy_url += "&force_original=true"
        return proxy_url


def _save_image(img: PILImage.Image, has_alpha: bool, alpha_channel) -> bytes:
    """Helper to save a PIL image to PNG bytes."""
    if has_alpha and alpha_channel is not None:
        img.putalpha(alpha_channel)
    output = BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


# ===========================================================================
#  OpenAI API Pydantic Models
# ===========================================================================

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = int(time.time())
    owned_by: str = "google"


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


class FunctionCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: FunctionCall


class Message(BaseModel):
    role: str
    content: Optional[Any] = None
    tool_calls: Optional[List[ToolCall]] = None


class Tool(BaseModel):
    type: str = "function"
    function: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    description: Optional[str] = None
    inputSchema: Optional[Dict[str, Any]] = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    max_tokens: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: Message
    finish_reason: str


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Usage


class ChatCompletionStreamChoice(BaseModel):
    index: int
    delta: Dict[str, Any]
    finish_reason: Optional[str]


class ChatCompletionStreamResponse(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionStreamChoice]


class ImageRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-3-pro"
    n: Optional[int] = 1
    size: Optional[str] = "1024x1024"
    quality: Optional[str] = "standard"
    response_format: Optional[str] = "url"
    user: Optional[str] = None


class ImageData(BaseModel):
    url: Optional[str] = None
    b64_json: Optional[str] = None
    revised_prompt: Optional[str] = None


class ImageResponse(BaseModel):
    created: int
    data: List[ImageData]


class TTSRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "alloy"
    response_format: Optional[str] = "mp3"
    speed: Optional[float] = 1.0


# ===========================================================================
#  Global State
# ===========================================================================

gemini_client: Optional[GeminiClient] = None


# ===========================================================================
#  Helper Functions
# ===========================================================================

def map_model(openai_model: str) -> Model:
    """Map an OpenAI model string to a Gemini Model enum."""
    model_str = openai_model.lower()
    if "ultra" in model_str or "pro" in model_str:
        return Model.BASIC_PRO
    return Model.BASIC_FLASH


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return len(text) // 4


def extract_text_from_content(content: Any) -> str:
    """
    Extract text from OpenAI-format message content.

    Handles both plain string and multimodal array formats.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
        return "\n".join(texts)
    return str(content) if content else ""


async def extract_files_from_content(content: Any) -> list[str]:
    """
    Extract image files from OpenAI-format multimodal content.

    Supports base64-encoded data URIs and HTTP URLs.
    Returns a list of temporary file paths.
    """
    if not isinstance(content, list):
        return []

    files = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "image_url":
            continue

        image_url_data = item.get("image_url", {})
        url = image_url_data.get("url", "")

        if url.startswith("data:"):
            # Base64-encoded image
            try:
                header, data = url.split(",", 1)
                img_data = base64.b64decode(data)

                ext = ".png"
                if "jpeg" in header or "jpg" in header:
                    ext = ".jpg"
                elif "gif" in header:
                    ext = ".gif"
                elif "webp" in header:
                    ext = ".webp"

                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
                    f.write(img_data)
                    files.append(f.name)
            except Exception as e:
                logger.warning(f"Failed to decode base64 image: {e}")

        elif url.startswith(("http://", "https://")):
            # Download from URL (async)
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    response = await client.get(url)
                    if response.status_code == 200:
                        content_type = response.headers.get("content-type", "")
                        ext = ".png"
                        if "jpeg" in content_type or "jpg" in content_type:
                            ext = ".jpg"
                        elif "gif" in content_type:
                            ext = ".gif"
                        elif "webp" in content_type:
                            ext = ".webp"
                        elif url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                            ext = Path(url).suffix

                        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
                            f.write(response.content)
                            files.append(f.name)
            except Exception as e:
                logger.warning(f"Failed to download image from {url}: {e}")

        else:
            # Local file path
            if Path(url).exists():
                files.append(url)

    return files


def cleanup_temp_files(file_paths: list[str]) -> None:
    """Safely remove temporary files."""
    for fp in file_paths:
        try:
            if fp.startswith(tempfile.gettempdir()):
                os.unlink(fp)
        except OSError:
            pass


def build_tools_description(tools: Optional[List[Any]]) -> str:
    """Generate XML-formatted tool description for Gemini's system prompt."""
    if not tools:
        return ""

    tool_xmls = []
    for tool in tools:
        if isinstance(tool, str):
            try:
                tool = json.loads(tool)
            except (json.JSONDecodeError, ValueError):
                continue

        if isinstance(tool, dict):
            # Support both OpenAI formats:
            #   1. {"type": "function", "function": {...}}
            #   2. {"type": "function", "name": "...", "inputSchema": {...}}
            if "function" in tool:
                name = tool["function"].get("name", "")
                desc = tool["function"].get("description", "")
                schema = tool["function"].get("parameters", {})
            else:
                name = tool.get("name", "")
                desc = tool.get("description", "")
                schema = tool.get("inputSchema", {})

            tool_xml = (
                f"<tool>\n"
                f"  <name>{name}</name>\n"
                f"  <description>{desc}</description>\n"
                f"  <arguments>\n"
                f'    {{"jsonSchema":{json.dumps(schema, ensure_ascii=False)}}}\n'
                f"  </arguments>\n"
                f"</tool>"
            )
            tool_xmls.append(tool_xml)

    if not tool_xmls:
        return ""

    return (
        "In this environment you have access to a set of tools you can use "
        "to answer the user's question.\n\n"
        "## Tool Use Formatting\n\n"
        "Tool use is formatted using XML-style tags:\n\n"
        "<tool_use>\n"
        "  <name>{tool_name}</name>\n"
        "  <arguments>{json_arguments}</arguments>\n"
        "</tool_use>\n\n"
        "## Available Tools\n\n"
        "<tools>\n\n"
        + "\n\n".join(tool_xmls)
        + "\n\n</tools>\n\n"
        "## Rules\n\n"
        "1. Always use the right arguments for the tools\n"
        "2. Call a tool only when needed\n"
        "3. Use XML tag format as shown above\n"
        "4. Never re-do a tool call with the exact same parameters"
    )


def fix_gemini_content(content: str) -> str:
    """Clean up common Gemini response artifacts."""
    # Fix escaped XML tags
    content = content.replace("\\<", "<").replace("\\>", ">")
    content = content.replace("\\_", "_")

    # Fix Gemini converting URLs into Markdown links inside tool call blocks
    def fix_block(match: re.Match) -> str:
        block = match.group(1)
        fixed = re.sub(r"\[([^\]]*)\]\(([^)]+)\)", r"\2", block)
        return f"<arguments>{fixed}</arguments>"

    content = re.sub(
        r"<arguments>(.*?)</arguments>", fix_block, content, flags=re.DOTALL
    )
    return content


def get_prompt_hash(text: str) -> str:
    """Generate a deterministic hash of a prompt for session matching."""
    normalized = " ".join(text.split())
    return hashlib.md5(normalized.encode("utf-8", errors="ignore")).hexdigest()[:16]


# ===========================================================================
#  Cookie Service Subscription
# ===========================================================================

async def subscribe_to_cookie_service(
    port: int, max_retries: int = 10, retry_delay: int = 10
) -> bool:
    """Subscribe to the Cookie Service for automatic cookie refresh."""
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "http://localhost:3898/subscribe",
                    json={
                        "domain": ".google.com",
                        "webhook_url": f"http://localhost:{port}/webhook/cookies",
                        "cookie_names": ["__Secure-1PSID", "__Secure-1PSIDTS"],
                        "app_name": "Gemini OpenAI Server",
                    },
                    timeout=5.0,
                )
                logger.info(f"Subscribed to Cookie Service: {response.json()}")
                return True
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(
                    f"Cookie Service not ready (attempt {attempt + 1}/{max_retries}): {e}"
                )
                logger.info(f"Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
            else:
                logger.warning(f"Failed to subscribe after {max_retries} attempts: {e}")
                logger.warning("Will use cookies from .env file")
                return False


# ===========================================================================
#  Application Lifecycle
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global gemini_client

    secure_1psid = os.getenv("SECURE_1PSID")
    secure_1psidts = os.getenv("SECURE_1PSIDTS")
    proxy = os.getenv("PROXY")
    port = int(os.getenv("PORT", 3897))

    # Subscribe to Cookie Service in background so it doesn't block startup
    asyncio.create_task(subscribe_to_cookie_service(port))

    if not secure_1psid or not secure_1psidts:
        logger.error("SECURE_1PSID or SECURE_1PSIDTS not found in .env")
        logger.error("Please ensure browser extension is running and has updated cookies")

    gemini_client = GeminiClient(
        secure_1psid=secure_1psid,
        secure_1psidts=secure_1psidts,
        proxy=proxy,
    )
    try:
        logger.info("Initializing Gemini client...")
        await gemini_client.init(auto_refresh=False)
        logger.info("Gemini client initialized successfully")
    except Exception as e:
        logger.warning(f"Failed to initialize Gemini Client: {e}")
        logger.warning("Server will start anyway. Waiting for extension to update cookies...")

    yield

    if gemini_client:
        await gemini_client.close()


# ===========================================================================
#  FastAPI Application
# ===========================================================================

app = FastAPI(
    title="Gemini OpenAI Proxy",
    description="OpenAI-compatible API powered by Google Gemini",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
#  Authentication helper
# ---------------------------------------------------------------------------

def verify_api_key(request: Request) -> None:
    """Verify the Bearer token if GEMINI_API_KEY is configured."""
    expected_key = os.getenv("GEMINI_API_KEY", "")
    if not expected_key:
        return  # No key configured, allow all

    auth_header = request.headers.get("Authorization", "")
    provided_key = ""
    if auth_header.startswith("Bearer "):
        provided_key = auth_header[7:].strip()

    if provided_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
#  Cookie Endpoints
# ---------------------------------------------------------------------------

@app.get("/get_cookies")
async def get_cookies(request: Request):
    """Return the current cookies from environment."""
    verify_api_key(request)
    return {
        "SECURE_1PSID": os.getenv("SECURE_1PSID", ""),
        "SECURE_1PSIDTS": os.getenv("SECURE_1PSIDTS", ""),
    }


@app.post("/update_cookies")
async def update_cookies_legacy(request: Request):
    """Receive cookies from the browser extension and update in-memory + .env."""
    global gemini_client
    try:
        data = await request.json()
        new_psid = data.get("SECURE_1PSID")
        new_psidts = data.get("SECURE_1PSIDTS")

        if not new_psid or not new_psidts:
            return {"success": False, "message": "Missing required cookies"}

        if gemini_client:
            gemini_client.cookies["__Secure-1PSID"] = new_psid
            gemini_client.cookies["__Secure-1PSIDTS"] = new_psidts
            if gemini_client.client:
                gemini_client.client.cookies.set("__Secure-1PSID", new_psid)
                gemini_client.client.cookies.set("__Secure-1PSIDTS", new_psidts)
            logger.info("Cookies updated in memory")

        dotenv.set_key(".env", "SECURE_1PSID", new_psid)
        dotenv.set_key(".env", "SECURE_1PSIDTS", new_psidts)

        return {"success": True, "message": "Cookies updated"}
    except Exception as e:
        logger.error(f"Failed to update cookies: {e}")
        return {"success": False, "message": str(e)}


@app.post("/webhook/cookies")
async def webhook_cookies(request: Request):
    """Receive cookie updates from the Cookie Service webhook."""
    global gemini_client
    try:
        data = await request.json()
        domain = data.get("domain")
        cookies = data.get("cookies", {})

        if domain != ".google.com":
            return {"status": "ignored", "reason": "not google domain"}

        new_psid = cookies.get("__Secure-1PSID")
        new_psidts = cookies.get("__Secure-1PSIDTS")

        if not new_psid or not new_psidts:
            return {"status": "ignored", "reason": "missing required cookies"}

        if gemini_client:
            gemini_client.cookies["__Secure-1PSID"] = new_psid
            gemini_client.cookies["__Secure-1PSIDTS"] = new_psidts
            if gemini_client.client:
                gemini_client.client.cookies.set("__Secure-1PSID", new_psid)
                gemini_client.client.cookies.set("__Secure-1PSIDTS", new_psidts)
            logger.info("Cookies updated from webhook")

        dotenv.set_key(".env", "SECURE_1PSID", new_psid)
        dotenv.set_key(".env", "SECURE_1PSIDTS", new_psidts)

        return {"status": "ok", "message": "Cookies updated"}
    except Exception as e:
        logger.error(f"Failed to process webhook: {e}")
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
#  Model Listing
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def list_models(request: Request):
    """List available models (OpenAI-compatible)."""
    verify_api_key(request)
    models = [
        ModelInfo(id="gemini-3-flash", owned_by="google"),
        ModelInfo(id="gemini-3-pro", owned_by="google"),
        ModelInfo(id="gemini-3-pro-high", owned_by="google"),
        ModelInfo(id="gemini-3-flash-image", owned_by="google"),
        ModelInfo(id="gemini-3-pro-image", owned_by="google"),
    ]
    return ModelList(data=models)


# ---------------------------------------------------------------------------
#  Image Proxy
# ---------------------------------------------------------------------------

@app.get("/v1/images/proxy")
async def proxy_image(
    url: str,
    model: str = "",
    remove_watermark: Optional[bool] = None,
    force_original: bool = False,
):
    """Proxy image requests to Google with authentication cookies."""
    if not gemini_client:
        raise HTTPException(status_code=503, detail="Gemini client not initialized")

    # Auto-detect watermark removal based on model name
    if remove_watermark is None:
        remove_watermark = "image" in model.lower() if model else False

    logger.info(
        f"Proxy request: model={model}, remove_watermark={remove_watermark}, "
        f"force_original={force_original}"
    )

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            cookies=gemini_client.cookies.get_dict(),
            proxy=gemini_client.proxy,
        ) as client:
            # Handle Google image URL size parameters
            if "googleusercontent.com" in url:
                if force_original:
                    if "=s" in url:
                        url = url.split("=s")[0]
                    url += "=s0"
                elif "=s" not in url:
                    url += "=s512"

            response = await client.get(url, timeout=30.0)
            if response.status_code == 200:
                content = response.content

                if remove_watermark and "googleusercontent.com" in url:
                    try:
                        content = ImageProcessor.remove_watermark(content)
                        logger.info("Watermark removed from image")
                    except Exception as e:
                        logger.warning(f"Failed to remove watermark: {e}")

                return Response(
                    content=content,
                    media_type=response.headers.get("content-type", "image/png"),
                )
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to fetch image from Google",
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image proxy error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
#  Chat Completions
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, fast_request: Request):
    """OpenAI-compatible chat completions endpoint."""
    global gemini_client

    verify_api_key(fast_request)

    if not gemini_client:
        raise HTTPException(
            status_code=503,
            detail="Gemini client not initialized. Waiting for cookie update.",
        )

    model = map_model(request.model)

    # Determine if this is a continuation of an existing conversation
    has_history = any(msg.role == "assistant" for msg in request.messages)

    # Try to find an existing session by hashing the first user message
    session_key = None
    session = None

    if has_history:
        for msg in request.messages:
            if msg.role == "user":
                first_user = extract_text_from_content(msg.content)
                if first_user:
                    prompt_hash = get_prompt_hash(first_user)
                    result = sessions.get_by_prompt_hash(prompt_hash)
                    if result:
                        session_key, session = result
                        logger.info(f"Resumed session: {session_key}")
                    break

    if session is None:
        session_key, session = sessions.create(model)

    logger.info(f"Has history: {has_history}, Active sessions: {sessions.count}")

    # Extract the last user message or tool result
    last_user_content = ""
    last_user_files: list[str] = []
    has_tool_result = False

    for msg in reversed(request.messages):
        if msg.role == "user":
            last_user_content = extract_text_from_content(msg.content)
            last_user_files = await extract_files_from_content(msg.content)
            break
        elif msg.role == "tool":
            has_tool_result = True
            content_to_parse = msg.content

            # Parse JSON string content
            if isinstance(content_to_parse, str):
                try:
                    content_to_parse = json.loads(content_to_parse)
                except (json.JSONDecodeError, ValueError):
                    last_user_content = f"<tool_result>\n{content_to_parse}\n</tool_result>"
                    break

            # Handle {"content": [...]} format
            if isinstance(content_to_parse, dict) and "content" in content_to_parse:
                content_to_parse = content_to_parse["content"]

            if isinstance(content_to_parse, list):
                parts = []
                for item in content_to_parse:
                    if isinstance(item, dict) and item.get("type") == "text":
                        result_text = item.get("text", "")
                        parts.append(f"<tool_result>\n{result_text}\n</tool_result>")
                if parts:
                    last_user_content = "\n\n".join(parts)
            break

    # Build the prompt
    parts = []

    # Only add system prompt and tool descriptions for new conversations
    if not has_history and not has_tool_result:
        system_parts = [
            extract_text_from_content(m.content)
            for m in request.messages
            if m.role == "system"
        ]
        if system_parts:
            parts.append(chr(10).join(system_parts))

        if request.tools:
            tools_desc = build_tools_description(request.tools)
            if tools_desc:
                parts.append(tools_desc)

    # Add message content
    if has_tool_result:
        parts.append(
            last_user_content
            or "<tool_result>\nTool execution completed.\n</tool_result>"
        )
    else:
        parts.append(last_user_content)

    prompt = "\n\n".join(parts)

    # Streaming response
    if request.stream:
        return StreamingResponse(
            stream_chat_completion(
                request, session, session_key, prompt, model,
                fast_request, not has_history, last_user_files,
            ),
            media_type="text/event-stream",
        )

    # Non-streaming response with retry
    MAX_RETRY = 2
    RETRY_DELAY = 3
    content = ""
    response = None

    for attempt in range(MAX_RETRY):
        try:
            response = await session.send_message(
                prompt, files=last_user_files or None
            )
            content = response.text or ""
            break
        except Exception as e:
            error_msg = str(e).lower()
            is_auth_error = any(
                kw in error_msg
                for kw in ("expired", "invalid", "failed initialization", "401")
            )

            if is_auth_error and attempt < MAX_RETRY - 1:
                logger.warning(f"Auth error (attempt {attempt + 1}/{MAX_RETRY}): {e}")
                logger.info(f"Waiting {RETRY_DELAY}s for Cookie Service to update...")
                await asyncio.sleep(RETRY_DELAY)
            else:
                cleanup_temp_files(last_user_files)
                raise

    cleanup_temp_files(last_user_files)

    # Post-process the response
    content = fix_gemini_content(content)

    # Append generated images
    if response and response.images:
        for img in response.images:
            if hasattr(img, "url") and img.url:
                proxy_url = ImageProcessor.get_proxy_url(
                    fast_request, img.url, request.model
                )
                content += f"\n\n![Generated Image]({proxy_url})"

    # Cache the session for future continuations
    if not has_history and content:
        for msg in request.messages:
            if msg.role == "user":
                first_user = extract_text_from_content(msg.content)
                if first_user:
                    prompt_hash = get_prompt_hash(first_user)
                    sessions.store(session_key, session, prompt_hash)
                    logger.info(f"Cached session: {session_key}")
                    break

    # Parse tool calls if tools were provided
    tool_calls_list = None
    final_content: Any = content
    finish_reason = "stop"

    if request.tools:
        remaining_text, parsed_tool_calls, parsed_content_items = (
            ToolCallParser.parse(content)
        )
        if parsed_tool_calls:
            tool_calls_list = [
                ToolCall(
                    id=tc["id"],
                    type="function",
                    function=FunctionCall(
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    ),
                )
                for tc in parsed_tool_calls
            ]
            finish_reason = "tool_calls"

            content_array: list[dict] = []
            if remaining_text:
                content_array.append({"type": "text", "text": remaining_text})
            content_array.extend(parsed_content_items)
            final_content = content_array

    return ChatCompletionResponse(
        id=f"chatcmpl-{session_key}",
        created=int(time.time()),
        model=request.model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=Message(
                    role="assistant",
                    content=final_content,
                    tool_calls=tool_calls_list,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=Usage(
            prompt_tokens=estimate_tokens(prompt),
            completion_tokens=estimate_tokens(str(final_content)),
            total_tokens=estimate_tokens(prompt) + estimate_tokens(str(final_content)),
        ),
    )


# ---------------------------------------------------------------------------
#  Streaming Chat Completions
# ---------------------------------------------------------------------------

async def stream_chat_completion(
    request: ChatCompletionRequest,
    session: ChatSession,
    session_key: str,
    prompt: str,
    model: Model,
    fast_request: Request,
    is_new_session: bool = True,
    files: Optional[list[str]] = None,
) -> AsyncGenerator[str, None]:
    """Generate SSE streaming chunks for chat completions."""

    completion_id = f"chatcmpl-{session_key}"
    created = int(time.time())

    # Initial chunk with role
    initial_chunk = ChatCompletionStreamResponse(
        id=completion_id,
        created=created,
        model=request.model,
        choices=[
            ChatCompletionStreamChoice(
                index=0,
                delta={"role": "assistant", "content": ""},
                finish_reason=None,
            )
        ],
    )
    yield f"data: {initial_chunk.model_dump_json()}\n\n"

    try:
        response = await session.send_message(prompt, files=files or None)
        content = response.text or ""
        content = fix_gemini_content(content)

        # Cache session on success
        if is_new_session and content:
            for msg in request.messages:
                if msg.role == "user":
                    first_user = extract_text_from_content(msg.content)
                    if first_user:
                        prompt_hash = get_prompt_hash(first_user)
                        sessions.store(session_key, session, prompt_hash)
                        logger.info(f"Cached session: {session_key}")
                        break

        # Handle tool calls in streaming mode
        if request.tools:
            remaining_text, parsed_tool_calls, _ = ToolCallParser.parse(content)

            if parsed_tool_calls:
                # Stream any remaining text first
                if remaining_text:
                    chunk_size = 20
                    for i in range(0, len(remaining_text), chunk_size):
                        delta_text = remaining_text[i : i + chunk_size]
                        text_chunk = ChatCompletionStreamResponse(
                            id=completion_id,
                            created=created,
                            model=request.model,
                            choices=[
                                ChatCompletionStreamChoice(
                                    index=0,
                                    delta={"content": delta_text},
                                    finish_reason=None,
                                )
                            ],
                        )
                        yield f"data: {text_chunk.model_dump_json()}\n\n"

                # Stream tool calls
                tool_chunk = ChatCompletionStreamResponse(
                    id=completion_id,
                    created=created,
                    model=request.model,
                    choices=[
                        ChatCompletionStreamChoice(
                            index=0,
                            delta={
                                "tool_calls": [
                                    {
                                        "index": i,
                                        "id": tc["id"],
                                        "type": "function",
                                        "function": {
                                            "name": tc["function"]["name"],
                                            "arguments": tc["function"]["arguments"],
                                        },
                                    }
                                    for i, tc in enumerate(parsed_tool_calls)
                                ]
                            },
                            finish_reason=None,
                        )
                    ],
                )
                yield f"data: {tool_chunk.model_dump_json()}\n\n"

                # Final chunk
                final_chunk = ChatCompletionStreamResponse(
                    id=completion_id,
                    created=created,
                    model=request.model,
                    choices=[
                        ChatCompletionStreamChoice(
                            index=0, delta={}, finish_reason="tool_calls"
                        )
                    ],
                )
                yield f"data: {final_chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"
                return

        # Stream text content in chunks
        chunk_size = 20
        for i in range(0, len(content), chunk_size):
            delta_text = content[i : i + chunk_size]
            text_chunk = ChatCompletionStreamResponse(
                id=completion_id,
                created=created,
                model=request.model,
                choices=[
                    ChatCompletionStreamChoice(
                        index=0,
                        delta={"content": delta_text},
                        finish_reason=None,
                    )
                ],
            )
            yield f"data: {text_chunk.model_dump_json()}\n\n"

        # Stream any generated images
        if response.images:
            for img in response.images:
                if hasattr(img, "url") and img.url:
                    proxy_url = ImageProcessor.get_proxy_url(
                        fast_request, img.url, request.model
                    )
                    image_chunk = ChatCompletionStreamResponse(
                        id=completion_id,
                        created=created,
                        model=request.model,
                        choices=[
                            ChatCompletionStreamChoice(
                                index=0,
                                delta={"content": f"\n\n![Image]({proxy_url})"},
                                finish_reason=None,
                            )
                        ],
                    )
                    yield f"data: {image_chunk.model_dump_json()}\n\n"

        # Final stop chunk
        final_chunk = ChatCompletionStreamResponse(
            id=completion_id,
            created=created,
            model=request.model,
            choices=[
                ChatCompletionStreamChoice(index=0, delta={}, finish_reason="stop")
            ],
        )
        yield f"data: {final_chunk.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"Stream error: {e}")
        error_data = {"error": {"message": str(e)}}
        yield f"data: {json.dumps(error_data)}\n\n"

    finally:
        if files:
            cleanup_temp_files(files)


# ---------------------------------------------------------------------------
#  Image Generation
# ---------------------------------------------------------------------------

@app.post("/v1/images/generations")
async def image_generations(request: ImageRequest, fast_request: Request):
    """OpenAI-compatible image generation endpoint."""
    verify_api_key(fast_request)

    if not gemini_client:
        raise HTTPException(status_code=503, detail="Gemini client not initialized")

    should_remove_watermark = "image" in request.model.lower()

    try:
        gen_prompt = f"Generate an image: {request.prompt}"
        response = await gemini_client.generate_content(
            gen_prompt, model=Model.BASIC_FLASH
        )

        if not response.images:
            raise HTTPException(
                status_code=400, detail="Gemini failed to generate images."
            )

        image_data = []
        for img in response.images:
            if not (hasattr(img, "url") and img.url):
                continue

            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    cookies=gemini_client.cookies,
                    proxy=gemini_client.proxy,
                ) as client:
                    img_url = img.url
                    # Force original resolution
                    if "googleusercontent.com" in img_url:
                        if "=s" in img_url:
                            img_url = img_url.split("=s")[0]
                        img_url += "=s0"

                    logger.info(f"Fetching original image: {img_url}")
                    img_response = await client.get(img_url, timeout=30.0)

                    if img_response.status_code == 200:
                        img_content = img_response.content

                        if should_remove_watermark:
                            try:
                                img_content = ImageProcessor.remove_watermark(img_content)
                                logger.info(
                                    f"Watermark removed (model: {request.model})"
                                )
                            except Exception as e:
                                logger.warning(f"Failed to remove watermark: {e}")

                        b64_data = base64.b64encode(img_content).decode("utf-8")

                        if request.response_format == "b64_json":
                            image_data.append(
                                ImageData(
                                    b64_json=b64_data,
                                    revised_prompt=response.text,
                                )
                            )
                        else:
                            proxy_url = (
                                ImageProcessor.get_proxy_url(
                                    fast_request, img.url, request.model
                                )
                                + "&force_original=true"
                            )
                            image_data.append(
                                ImageData(
                                    url=proxy_url,
                                    revised_prompt=response.text,
                                )
                            )
            except Exception as e:
                logger.error(f"Failed to process image: {e}")
                # Fallback to proxy URL
                proxy_url = ImageProcessor.get_proxy_url(
                    fast_request, img.url, request.model
                )
                image_data.append(
                    ImageData(url=proxy_url, revised_prompt=response.text)
                )

        return ImageResponse(created=int(time.time()), data=image_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in image_generations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
#  Text-to-Speech (TTS)
# ---------------------------------------------------------------------------

@app.post("/v1/audio/speech")
async def text_to_speech(request: TTSRequest, fast_request: Request):
    """OpenAI-compatible text-to-speech endpoint.

    Uses Gemini to generate spoken audio from text input.
    Returns audio data as a streaming response.
    """
    verify_api_key(fast_request)

    if not gemini_client:
        raise HTTPException(status_code=503, detail="Gemini client not initialized")

    try:
        # Ask Gemini to describe the text for audio generation
        # Since Gemini doesn't have native TTS, we use a workaround:
        # generate a spoken-word version and return it
        tts_prompt = (
            f"Please read the following text aloud in a natural, "
            f"{request.voice}-style voice. Return only the spoken text, "
            f"no commentary:\n\n{request.input}"
        )

        response = await gemini_client.generate_content(
            tts_prompt, model=Model.BASIC_FLASH
        )

        spoken_text = response.text or request.input

        # Since Gemini web doesn't produce actual audio,
        # return a placeholder response indicating the limitation
        raise HTTPException(
            status_code=501,
            detail={
                "error": {
                    "message": (
                        "TTS is not natively supported by the Gemini web interface. "
                        "The text was processed but audio generation requires a "
                        "dedicated TTS service (e.g., Google Cloud TTS, ElevenLabs)."
                    ),
                    "type": "not_implemented",
                    "code": "tts_not_available",
                    "processed_text": spoken_text[:200],
                }
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
#  Embeddings
# ---------------------------------------------------------------------------

class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "text-embedding-3-small"
    input: Any  # str or list[str]
    encoding_format: Optional[str] = "float"
    dimensions: Optional[int] = None
    user: Optional[str] = None


@app.post("/v1/embeddings")
async def create_embedding(request: EmbeddingRequest, fast_request: Request):
    """OpenAI-compatible embeddings endpoint.

    Since Gemini web doesn't have a native embedding API,
    this returns a proper error directing users to alternatives.
    """
    verify_api_key(fast_request)

    raise HTTPException(
        status_code=501,
        detail={
            "error": {
                "message": (
                    "Embeddings are not supported by the Gemini web interface. "
                    "Use Google's Vertex AI or the official Gemini API for embeddings."
                ),
                "type": "not_implemented",
                "code": "embeddings_not_available",
            }
        },
    )


# ===========================================================================
#  Entry Point
# ===========================================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 3897))
    host = os.getenv("HOST", "0.0.0.0")

    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run("openai_server:app", host=host, port=port, reload=False)
