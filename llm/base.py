"""
LLM Base Class - Abstract interface for all LLM providers.
"""

import time
from abc import ABC, abstractmethod
from typing import AsyncIterator


class BaseLLMClient(ABC):
    """Abstract base class for LLM providers."""
    
    def __init__(self):
        # Chunking config - optimized for low latency TTS
        self.chunk_buffer = ""
        self.min_first_chunk_words = 2  # Tiny first chunk for fast response
        self.min_chunk_words = 6  # Smaller chunks = more parallel TTS
        self.first_chunk_sent = False
        
        self.strong_breaks = {'.', '!', '?', '\n'}
        self.weak_breaks = {',', ';', ':', '-', '—'}
        
        # Statistics tracking
        self.stats = {
            'request_sent_time': None,
            'first_token_time': None,
            'first_chunk_time': None,
            'response_complete_time': None,
            'total_chunks': 0,
            'total_response_chars': 0,
            'total_response_words': 0,
            'chunk_times': [],
        }
    
    def reset_stats(self) -> None:
        """Reset statistics for a new request."""
        self.stats = {
            'request_sent_time': None,
            'first_token_time': None,
            'first_chunk_time': None,
            'response_complete_time': None,
            'total_chunks': 0,
            'total_response_chars': 0,
            'total_response_words': 0,
            'chunk_times': [],
        }
        self.chunk_buffer = ""
        self.first_chunk_sent = False
    
    @abstractmethod
    async def _stream_raw_tokens(self, prompt: str) -> AsyncIterator[str]:
        """
        Stream raw tokens from the LLM provider.
        
        This method must be implemented by each provider.
        It should yield individual tokens/text chunks as they arrive.
        
        Args:
            prompt: The user's input prompt
            
        Yields:
            Raw text tokens from the LLM
        """
        pass
    
    async def generate_streaming_response(self, prompt: str) -> AsyncIterator[str]:
        """
        Stream LLM response, yielding TTS-optimized chunks.
        
        This method handles chunking logic - providers only need to
        implement _stream_raw_tokens().
        """
        self.reset_stats()
        self.stats['request_sent_time'] = time.time()
        
        try:
            first_token_received = False
            
            async for token in self._stream_raw_tokens(prompt):
                # Track first token time
                if not first_token_received:
                    self.stats['first_token_time'] = time.time()
                    first_token_received = True
                
                self.chunk_buffer += token
                
                # Try to yield complete phrases/chunks
                async for output_chunk in self._process_buffer():
                    self.stats['chunk_times'].append(time.time())
                    if not self.stats['first_chunk_time']:
                        self.stats['first_chunk_time'] = time.time()
                    yield output_chunk
            
            # Flush any remaining text
            if self.chunk_buffer.strip():
                self.stats['chunk_times'].append(time.time())
                yield self.chunk_buffer.strip()
                self.chunk_buffer = ""
                
        except Exception as e:
            print(f"\n✗ LLM API Error: {e}")
            yield "I apologize, but I encountered an error processing your request."
    
    async def _process_buffer(self) -> AsyncIterator[str]:
        """Yield complete chunks from buffer."""
        while True:
            chunk = self._extract_chunk()
            if chunk:
                yield chunk
            else:
                break
    
    def _extract_chunk(self) -> str | None:
        """Extract a chunk if ready, else None."""
        if not self.chunk_buffer:
            return None
        
        words = self.chunk_buffer.split()
        word_count = len(words)
        
        # Strategy for FIRST chunk: get something out FAST
        if not self.first_chunk_sent:
            if word_count >= self.min_first_chunk_words:
                # Look for ANY break point (weak or strong)
                for i, char in enumerate(self.chunk_buffer):
                    if char in self.strong_breaks or char in self.weak_breaks:
                        # Found a break! Extract up to and including it
                        chunk = self.chunk_buffer[:i+1].strip()
                        if len(chunk.split()) >= self.min_first_chunk_words:
                            self.chunk_buffer = self.chunk_buffer[i+1:]
                            self.first_chunk_sent = True
                            return chunk
                
                # No punctuation yet but have enough words - force first chunk
                if word_count >= self.min_first_chunk_words * 2:
                    chunk = ' '.join(words[:self.min_first_chunk_words])
                    self.chunk_buffer = ' '.join(words[self.min_first_chunk_words:])
                    self.first_chunk_sent = True
                    return chunk
        
        # Strategy for SUBSEQUENT chunks: prefer natural sentence boundaries
        else:
            if word_count >= self.min_chunk_words:
                # Prefer strong breaks (end of sentence)
                for i, char in enumerate(self.chunk_buffer):
                    if char in self.strong_breaks:
                        chunk = self.chunk_buffer[:i+1].strip()
                        if len(chunk.split()) >= self.min_chunk_words:
                            self.chunk_buffer = self.chunk_buffer[i+1:]
                            return chunk
                
                # Fall back to weak breaks (commas, etc.)
                for i, char in enumerate(self.chunk_buffer):
                    if char in self.weak_breaks:
                        chunk = self.chunk_buffer[:i+1].strip()
                        if len(chunk.split()) >= self.min_chunk_words:
                            self.chunk_buffer = self.chunk_buffer[i+1:]
                            return chunk
                
                # Buffer is long but no punctuation - force a chunk
                if word_count >= self.min_chunk_words * 2:
                    chunk = ' '.join(words[:self.min_chunk_words])
                    self.chunk_buffer = ' '.join(words[self.min_chunk_words:])
                    return chunk
        
        return None
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the name of the LLM provider."""
        pass
