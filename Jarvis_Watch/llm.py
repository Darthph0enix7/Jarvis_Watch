"""LLM - Placeholder with streaming simulation."""

import asyncio
from config import get_config

class LLM:
    def __init__(self):
        cfg = get_config()
        self.model = cfg.llm.model
        self.stream_delay = cfg.llm.stream_delay
        self.system_prompt = cfg.llm.system_prompt
    
    async def stream_response(self, user_input: str):
        """Yields text chunks simulating LLM streaming output."""
        # TODO: Replace with actual LLM API call (OpenAI, Anthropic, etc.)
        response = self._generate_mock_response(user_input)
        
        words = response.split()
        for i, word in enumerate(words):
            chunk = word + (" " if i < len(words) - 1 else "")
            yield chunk
            await asyncio.sleep(self.stream_delay)
    
    def _generate_mock_response(self, user_input: str) -> str:
        """Generate mock response based on input."""
        user_lower = user_input.lower()
        
        if any(w in user_lower for w in ["hello", "hi", "hey"]):
            return "Hello! I'm Jarvis, your voice assistant. How can I help you today?"
        
        if "time" in user_lower:
            return "The current time is displayed on your watch face. Would you like me to set a reminder?"
        
        if "weather" in user_lower:
            return "It's currently sunny with a temperature of seventy two degrees. Perfect weather for outdoor activities."
        
        if any(w in user_lower for w in ["thank", "thanks"]):
            return "You're welcome! Let me know if you need anything else."
        
        if "name" in user_lower:
            return "I'm Jarvis, your personal voice assistant running on your watch."
        
        if any(w in user_lower for w in ["help", "can you"]):
            return "I can help you with setting reminders, checking the weather, answering questions, and much more. Just ask!"
        
        # Default response
        return f"I heard you say: {user_input}. This is a demo response. In production, I would process this with a real language model."


# Convenience function for direct use
async def get_llm_response(user_input: str):
    """Yields streaming response chunks."""
    llm = LLM()
    async for chunk in llm.stream_response(user_input):
        yield chunk
