"""The hand-rolled agent loop that drives the LLM through iterative tool calls
against the code graph, dispatching and validating each tool call, tracking token
usage, and producing a final structured AnswerWithCitations."""
