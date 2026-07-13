"""Diff reviewer that, for each changed file or function, detects its language,
gathers cross-language graph context, and invokes the agent with the review prompt
to return structured line-level review comments."""
