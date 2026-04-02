"""
Gemini LLM Provider - Google's Gemini API implementation.
"""

from typing import AsyncIterator
from google import genai

from .base import BaseLLMClient


class GeminiLLMClient(BaseLLMClient):
    """Google Gemini LLM client with streaming support."""
    
    def __init__(self, api_key: str, model: str = "gemini-3.1-flash-lite-preview"):
        super().__init__()
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.system_instruction = (
            "You are Jarvis, a voice assistant. "
            "Be concise and natural. Prioritize brevity."
        )
        
        # Gemini-specific config
        self.temperature = 0.7
        self.max_output_tokens = 40000
        self.top_p = 0.95
    
    @property
    def provider_name(self) -> str:
        return "Gemini"
    
    async def _stream_raw_tokens(self, prompt: str) -> AsyncIterator[str]:
        """Stream raw tokens from Gemini API."""
        try:
            response = self.client.models.generate_content_stream(
                model=self.model,
                contents=prompt,
                config={
                    'system_instruction': self.system_instruction,
                    'temperature': self.temperature,
                    'max_output_tokens': self.max_output_tokens,
                    'top_p': self.top_p,
                }
            )
            
            for chunk in response:
                if hasattr(chunk, 'text') and chunk.text:
                    yield chunk.text
                    
        except Exception as e:
            print(f"\n✗ Gemini API Error: {e}")
            raise
