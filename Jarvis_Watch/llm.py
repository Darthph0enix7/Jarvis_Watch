"""Gemini LLM with streaming support."""

import asyncio
from google import genai
import config

# Initialize Gemini client
client = genai.Client(api_key=config.GEMINI_API_KEY)

async def generate_response(user_input: str):
    """Generate streaming LLM response using Gemini."""
    try:
        # Prepare messages
        messages = [
            {"role": "user", "parts": [{"text": config.LLM_SYSTEM_PROMPT}]},
            {"role": "model", "parts": [{"text": "Understood. I'll be concise and natural."}]},
            {"role": "user", "parts": [{"text": user_input}]}
        ]
        
        # Stream response
        response = client.models.generate_content_stream(
            model=config.GEMINI_MODEL,
            contents=messages
        )
        
        for chunk in response:
            if chunk.text:
                # Yield text as it arrives
                yield chunk.text
                await asyncio.sleep(0)  # Allow other tasks to run
                
    except Exception as e:
        print(f"Gemini API error: {e}")
        yield f"I encountered an error. Please try again."
