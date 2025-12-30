"""
LLM Module - Pluggable LLM providers for Project Jarvis.

Available Providers:
- GeminiLLMClient: Google Gemini API
- CerebrasLLMClient: Cerebras (ultra-fast inference)

Usage:
    from llm import GeminiLLMClient, CerebrasLLMClient
    
    client = CerebrasLLMClient(api_key="your-key")
    async for chunk in client.generate_streaming_response("Hello"):
        print(chunk)
"""

from .base import BaseLLMClient
from .gemini import GeminiLLMClient
from .cerebras import CerebrasLLMClient

__all__ = [
    "BaseLLMClient",
    "GeminiLLMClient",
    "CerebrasLLMClient",
]
