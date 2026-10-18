
"""llm.py: provider-agnostic JSON LLM helper (stdlib only). LLM_PROVIDER = none | ollama | groq | gemini."""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional


class LLM:
    def __init__(self, provider: Optional[str] = None, timeout: float = 8.0):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "none")).lower()
        self.timeout = timeout
        self.simulate_outage = False

    def _post(self, url: str, body: dict, headers: Optional[dict] = None) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "safeguard/1.0",
                **(headers or {}),
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 700,
    ) -> Optional[str]:
        """Returns the model's text, or None on any failure (callers have deterministic fallbacks)."""

        if self.simulate_outage:
            return None

        try:
            if self.provider == "ollama":
                out = self._post(
                    os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/chat",
                    {
                        "model": os.getenv("OLLAMA_MODEL", "llama3.2"),
                        "stream": False,
                        "format": "json",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "options": {
                            "temperature": 0,
                            "num_predict": max_tokens,
                        },
                    },
                )
                return out["message"]["content"]

            if self.provider == "groq":
                out = self._post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    {
                        "model": os.getenv(
                            "GROQ_MODEL", "llama-3.1-8b-instant"
                        ),
                        "temperature": 0,
                        "max_tokens": max_tokens,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    },
                    {
                        "Authorization": "Bearer "
                        + os.environ["GROQ_API_KEY"]
                    },
                )
                return out["choices"][0]["message"]["content"]

            if self.provider == "gemini":
                model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

                out = self._post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    f"?key={os.environ['GEMINI_API_KEY']}",
                    {
                        "systemInstruction": {
                            "parts": [{"text": system}]
                        },
                        "contents": [
                            {
                                "role": "user",
                                "parts": [{"text": user}],
                            }
                        ],
                        "generationConfig": {
                            "temperature": 0,
                            "maxOutputTokens": max_tokens,
                            "responseMimeType": "application/json",
                        },
                    },
                )
                return out["candidates"][0]["content"]["parts"][0]["text"]

        except Exception:
            return None

        return None  # provider "none"
