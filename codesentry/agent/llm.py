"""The sole home of the OpenAI SDK dependency: an LLMClient abstraction exposing
chat_with_tools and parse_structured so that swapping providers later is a
one-file change and nothing else in the codebase imports openai directly."""
