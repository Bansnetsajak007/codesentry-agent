"""Pydantic models for agent tool inputs and outputs plus the structured result
types (Citation, AnswerWithCitations, ReviewComment, ReviewResult) that the agent
and reviewer return. Field descriptions double as the LLM-facing schema docs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "error"]


# --- Tool inputs (one per tool) ------------------------------------------------


class ListFilesInput(BaseModel):
    pattern: str | None = Field(
        default=None, description="Optional glob to filter file paths, e.g. 'src/*.py'."
    )
    language: str | None = Field(
        default=None, description="Optional language filter, e.g. 'python' or 'go'."
    )


class ReadFileInput(BaseModel):
    path: str = Field(description="Repo-relative path of the file to read.")
    start_line: int | None = Field(
        default=None, description="1-based first line to read (inclusive)."
    )
    end_line: int | None = Field(
        default=None, description="1-based last line to read (inclusive)."
    )


class FindSymbolInput(BaseModel):
    name: str = Field(description="Simple or qualified symbol name to look up.")
    language: str | None = Field(
        default=None, description="Optional language to restrict the search to."
    )


class GetDefinitionInput(BaseModel):
    node_id: str = Field(description="Graph node id, e.g. 'src/auth.py::LoginHandler.login'.")


class GetCallersInput(BaseModel):
    node_id: str = Field(description="Graph node id whose callers to list.")


class GetCalleesInput(BaseModel):
    node_id: str = Field(description="Graph node id whose callees to list.")


class GetNeighborsInput(BaseModel):
    node_id: str = Field(description="Graph node id to center the neighborhood on.")
    hops: int = Field(default=1, description="Neighborhood radius (1 or 2).")


class GrepInput(BaseModel):
    pattern: str = Field(description="Python regular expression to search for.")
    path_glob: str | None = Field(
        default=None, description="Optional glob to restrict which files are searched."
    )


class ListLanguagesInput(BaseModel):
    pass


# --- Structured results --------------------------------------------------------


class Citation(BaseModel):
    file: str = Field(description="Repo-relative file path being cited.")
    start_line: int = Field(description="1-based first line of the cited range.")
    end_line: int = Field(description="1-based last line of the cited range.")


class AnswerWithCitations(BaseModel):
    answer: str = Field(description="The answer, grounded in the cited locations.")
    citations: list[Citation] = Field(
        default_factory=list,
        description="Every file:line range that supports the answer.",
    )


class ReviewComment(BaseModel):
    file: str = Field(description="Repo-relative file path the comment applies to.")
    line: int = Field(description="1-based line number the comment applies to.")
    severity: Severity = Field(description="One of info, warning, or error.")
    message: str = Field(description="What is wrong and why it matters.")
    suggestion: str | None = Field(
        default=None, description="Optional concrete fix suggestion."
    )


class ReviewResult(BaseModel):
    comments: list[ReviewComment] = Field(
        default_factory=list, description="Line-level review comments."
    )
