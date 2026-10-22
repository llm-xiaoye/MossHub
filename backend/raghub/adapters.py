"""Network adapter boundaries; cloud and inference calls are later-stage work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings


@dataclass(frozen=True)
class LocalModelAdapter:
    base_url: str = "http://127.0.0.1:8001"

    async def readiness(self) -> dict:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(self.base_url + "/health/ready")
            response.raise_for_status()
            return response.json()


@dataclass(frozen=True)
class FlashAdapter:
    """Reads the API key only for an explicitly requested cloud call."""

    env_file: Path
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"

    @classmethod
    def from_settings(cls, settings: Settings) -> "FlashAdapter":
        private_file = settings.home / "private/deepseek.env"
        existing_file = (Path.home() / ".config/vector-rag/deepseek.env")
        return cls(env_file=private_file if private_file.is_file() else existing_file)

    def _api_key(self) -> str:
        if not self.env_file.is_file():
            raise RuntimeError("DeepSeek credential file is missing")
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                value = line.partition("=")[2].strip().strip("'\"")
                if value:
                    return value
        raise RuntimeError("DeepSeek credential is unavailable")

    async def complete(self, messages: list[dict], *, timeout_seconds: float) -> dict:
        key = self._api_key()
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout_seconds) as client:
            response = await client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": self.model, "messages": messages, "stream": False},
            )
            response.raise_for_status()
            return response.json()

