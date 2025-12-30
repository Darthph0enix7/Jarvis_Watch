"""
Cerebras LLM Provider - Ultra-fast inference API.
"""

from typing import AsyncIterator
from cerebras.cloud.sdk import Cerebras

from .base import BaseLLMClient


class CerebrasLLMClient(BaseLLMClient):
    """Cerebras LLM client with streaming support - optimized for speed."""
    
    def __init__(self, api_key: str, model: str = "llama-3.3-70b"):
        super().__init__()
        self.client = Cerebras(api_key=api_key)
        self.model = model
        self.system_instruction = (
            "You are Jarvis, a voice assistant. "
            "Be concise and natural. Prioritize brevity."
        )
        
        # Cerebras-specific config
        self.temperature = 0.7
        self.max_tokens = 512
        self.top_p = 0.95
    
    @property
    def provider_name(self) -> str:
        return "Cerebras"
    
    async def _stream_raw_tokens(self, prompt: str) -> AsyncIterator[str]:
        """Stream raw tokens from Cerebras API."""
        try:
            stream = self.client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": self.system_instruction,
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                model=self.model,
                stream=True,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                top_p=self.top_p,
            )
            
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                    
        except Exception as e:
            print(f"\n✗ Cerebras API Error: {e}")
            raise
