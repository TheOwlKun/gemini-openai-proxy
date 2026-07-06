<div align="center">
  <img src="https://capsule-render.vercel.app/api?type=waving&height=180&color=0:0F0F0F,100:3A3A3A&text=Gemini%20OpenAI%20Proxy&fontColor=FFFFFF&fontAlignY=35&desc=Google%20Gemini%20to%20OpenAI%20API%20Gateway&descAlignY=58&animation=fadeIn" />

<h1 align="center">✨ Gemini OpenAI Proxy</h1>

<p align="center">
An elegant, production-ready OpenAI-compatible proxy for Google Gemini's Web Interface.
</p>

<p align="center">
Built for developers who value performance, stability, and clean architecture.
</p>

<p align="center">
<a href="https://github.com/TheOwlKun/gemini-openai-proxy/blob/main/LICENSE">
<img src="https://img.shields.io/badge/License-MIT-EAEAEA?style=for-the-badge&labelColor=111111"/>
</a>

<a href="https://www.python.org/">
<img src="https://img.shields.io/badge/Python-3.10+-EAEAEA?style=for-the-badge&logo=python&labelColor=111111"/>
</a>

<img src="https://img.shields.io/badge/OpenAI-Compatible-EAEAEA?style=for-the-badge&labelColor=111111"/>

<img src="https://img.shields.io/badge/Production-Ready-EAEAEA?style=for-the-badge&labelColor=111111"/>
</p>

<p align="center">
⚡ OpenAI Compatible • 🚀 Production Ready • 🛡 Secure by Default • 🤖 Gemini Advanced
</p>

</div>

---

# ✨ Overview

Gemini OpenAI Proxy bridges the gap between your applications and Google Gemini's powerful web models by exposing a fully **OpenAI-compatible API**.

Simply replace your existing OpenAI endpoint with Gemini Proxy and continue using your favorite SDKs (like AutoGen, LobeChat, Chatbox, or AnythingLLM) without changing your application logic.

Enjoy access to `gemini-3-pro`, `gemini-3-flash`, and their multimodal capabilities — all through standard OpenAI formats.

---

# 🚀 Why Gemini Proxy?

<table>
<tr>
<td width="33%" align="center">

### ⚡ Compatibility

Works with existing OpenAI SDKs.

No client-side rewrites.

Drop-in replacement.

</td>

<td width="33%" align="center">

### 🛡 Security

Docker / Non-root support

API Key Authentication

No Data Logging

Cookie-based Auth

</td>

<td width="33%" align="center">

### 🚀 Performance

Asynchronous Core (FastAPI)

Thread-safe Sessions

Automatic Cookie Refresh

Cross-platform Launcher

</td>

</tr>
</table>

---

# ✨ Features

* ✅ **Fully OpenAI-compatible API** (Chat Completions, Models, Image Generation)
* ⚡ **Multimodal Support** — Send images and files to Gemini seamlessly
* 🚀 **Tool Calling / Function Calling** support built-in
* 💧 **Watermark Removal** — Automatically removes Gemini watermarks from generated images
* 🛡 **Collision-Free Sessions** — UUID-based session management
* 🔄 **Auto-Refreshing Cookies** — Integrated webhook for browser extension sync
* 📦 **Cross-Platform Launcher** — Run in foreground, background, systemd, or Task Scheduler
* 🐳 **Docker Support** — Clean, secure containerized deployment

---

# 📦 Quick Start

## 1. Clone Repository

```bash
git clone https://github.com/TheOwlKun/gemini-openai-proxy.git
cd gemini-openai-proxy
```

---

## 2. Install

```bash
pip install -r requirements.txt
```

---

## 3. Configure

```bash
cp .env.example .env
```

<div align="center">
  <h3>🔍 Obtaining your Cookies</h3>
  <p>
    <i>Valid Google cookies are <b>strictly required</b> to interface with Gemini.</i><br><br>
    Visit <a href="https://gemini.google.com/">https://gemini.google.com/</a> ➔ Open Developer Tools (<code>F12</code>)<br>
    Navigate to the <b>Application / Storage</b> tab ➔ Select <b>Cookies</b> ➔ Copy the values for <code>__Secure-1PSID</code> and <code>__Secure-1PSIDTS</code> and add them to your <code>.env</code> file.
  </p>
</div>

---

## 4. Run

The proxy comes with a powerful cross-platform launcher that works on Windows, Linux, and macOS.

```bash
# Interactive Menu
python launch.py

# Or start directly in background
python launch.py background
```

### Alternative: Docker
```bash
docker-compose up -d
```

---

# 💻 Usage

## Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-your-secret-key", # Set in your .env as GEMINI_API_KEY
    base_url="http://localhost:3897/v1"
)

# 1. Chat Completion
response = client.chat.completions.create(
    model="gemini-3-pro",
    messages=[{"role": "user", "content": "Explain quantum computing in one sentence."}]
)
print(response.choices[0].message.content)

# 2. Image Generation
image = client.images.generate(
    model="gemini-3-flash-image",
    prompt="A futuristic cyberpunk city at night",
    n=1
)
print(image.data[0].url)
```

---

## cURL

```bash
curl http://localhost:3897/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-secret-key" \
  -d '{
    "model": "gemini-3-pro",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

# 🛡 Built For Production

Unlike simple scripts, Gemini Proxy includes production-grade architecture by default:

* **FastAPI & Uvicorn** asynchronous core
* **True Session Management** (no more collision bugs with identical prompts)
* **Robust XML Parser** for Gemini's tool calls
* **Background Service Wrappers** (systemd for Linux, hidden process/Task Scheduler for Windows)
* **Secure Docker Image** (runs as non-root user)

Perfect for personal AI agents, internal tools, and LobeChat/AnythingLLM backends.

---

# ❤️ Support Development

If this project saved you time or made your workflow easier, consider supporting future open-source development.

<p align="center">
  <a href="https://www.buymeacoffee.com/theowlkun">
    <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" height="55"/>
  </a>
</p>

---

<p align="center">
  Crafted with ❤️ by <b><a href="https://github.com/TheOwlKun">@TheOwlKun</a></b>
  <br><br>⭐ If you find this project useful, consider giving it a star!
</p>
