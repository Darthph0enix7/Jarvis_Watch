"""LLM placeholder - Replace with real API."""

import asyncio
import config

async def generate_response(user_input: str):
    """Generate streaming LLM response."""
    # Mock responses
    responses = {
        "hello": "Hello! I'm Jarvis. How can I help you today?",
        "hi": "Hi there! What can I do for you?",
        "time": "I don't have access to the current time, but your watch should display it.",
        "weather": "It looks like a beautiful day outside. Perfect for a walk!",
        "name": "I'm Jarvis, your voice assistant.",
        "help": "I can answer questions, set reminders, and help with various tasks.",
    }
    
    lower = user_input.lower()
    response = None
    for key, val in responses.items():
        if key in lower:
            response = val
            break
    
    if not response:
        response = f"I heard: {user_input}. This is a demo response."
    
    # Stream word by word
    for word in response.split():
        yield word + " "
        await asyncio.sleep(config.LLM_STREAM_DELAY)
