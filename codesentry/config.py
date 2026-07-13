"""Application configuration loaded from the environment via python-dotenv,
exposing a Pydantic ``Settings`` model and a cached ``get_settings()`` singleton
that surfaces the OpenAI credentials, model choice, token budget, and log level."""
