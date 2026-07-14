"""Tests for diff parsing and the diff reviewer. The reviewer's LLM is a scripted
fake returning ReviewResults; no test touches the network."""

from pathlib import Path

from codesentry.agent.schemas import ReviewComment, ReviewResult
from codesentry.graph.builder import build_graph
from codesentry.review.diff import parse_diff
from codesentry.review.reviewer import review_diff

PY_FIXTURE = Path(__file__).parent / "fixtures" / "sample_python"

REPO_DIFF = """\
diff --git a/repository.py b/repository.py
--- a/repository.py
+++ b/repository.py
@@ -18,3 +18,3 @@ class UserRepository:
     def count(self) -> int:
-        # BUG: off-by-one; should return len(self._users).
-        return len(self._users) + 1
+        # Fixed off-by-one.
+        return len(self._users)
"""

UTILS_DIFF = """\
diff --git a/utils.py b/utils.py
--- a/utils.py
+++ b/utils.py
@@ -5,2 +5,2 @@ def slugify(value: str) -> str:
     \"\"\"Turn a display string into a URL-friendly slug.\"\"\"
-    return _normalize(value).replace(" ", "-")
+    return _normalize(value).replace(" ", "_")
"""


class FakeReviewLLM:
    def __init__(self, results: list[ReviewResult]) -> None:
        self._results = list(results)
        self.calls: list[list[dict]] = []

    def parse_structured(self, messages, schema):  # type: ignore[no-untyped-def]
        self.calls.append([dict(m) for m in messages])
        return self._results.pop(0)


def test_parse_diff_multiple_files() -> None:
    diffs = parse_diff(REPO_DIFF + UTILS_DIFF)
    assert [d.path for d in diffs] == ["repository.py", "utils.py"]
    repo = diffs[0]
    assert any("len(self._users)" in line.content for line in repo.added)
    assert any("+ 1" in line.content for line in repo.removed)
    assert repo.changed_line_numbers


def test_review_diff_returns_comments_and_context() -> None:
    graph = build_graph(PY_FIXTURE)
    result = ReviewResult(
        comments=[
            ReviewComment(
                file="repository.py",
                line=20,
                severity="error",
                message="off-by-one in count",
                suggestion="return len(self._users)",
            )
        ]
    )
    llm = FakeReviewLLM([result])
    comments = review_diff(REPO_DIFF, graph, llm)  # type: ignore[arg-type]

    assert len(comments) == 1
    assert comments[0].message == "off-by-one in count"
    assert len(llm.calls) == 1
    user_content = llm.calls[0][1]["content"]
    # Context should include the changed symbol and its caller (cross-file).
    assert "UserRepository.count" in user_content
    assert "headcount" in user_content


def test_review_diff_one_call_per_file() -> None:
    graph = build_graph(PY_FIXTURE)
    results = [
        ReviewResult(comments=[]),
        ReviewResult(
            comments=[
                ReviewComment(file="utils.py", line=6, severity="info", message="ok")
            ]
        ),
    ]
    llm = FakeReviewLLM(results)
    comments = review_diff(REPO_DIFF + UTILS_DIFF, graph, llm)  # type: ignore[arg-type]
    assert len(llm.calls) == 2
    assert [c.file for c in comments] == ["utils.py"]


def test_review_diff_lenient_fallback_recovers_nonconforming_comments() -> None:
    from codesentry.agent.llm import StructuredOutputError

    class NonStrictLLM:
        """Mimics a provider that ignores the strict schema: parse_structured
        rejects the reply, and complete() returns comments missing severity/message
        with the text under 'suggestion' (exactly the observed failure)."""

        def __init__(self) -> None:
            self.completed = False

        def parse_structured(self, messages, schema):  # type: ignore[no-untyped-def]
            raise StructuredOutputError("provider does not enforce strict schema")

        def complete(self, messages):  # type: ignore[no-untyped-def]
            self.completed = True
            return (
                '```json\n'
                '{"comments": [{"file": "repository.py", "line": 20, '
                '"suggestion": "off-by-one: count returns len + 1"}]}\n'
                '```'
            )

    graph = build_graph(PY_FIXTURE)
    llm = NonStrictLLM()
    comments = review_diff(REPO_DIFF, graph, llm)  # type: ignore[arg-type]

    assert llm.completed  # the fallback path ran
    assert len(comments) == 1
    assert comments[0].file == "repository.py"
    assert comments[0].line == 20
    assert comments[0].severity == "warning"  # defaulted from missing severity
    assert "off-by-one" in comments[0].message  # recovered from 'suggestion'


def test_review_diff_no_comments() -> None:
    graph = build_graph(PY_FIXTURE)
    llm = FakeReviewLLM([ReviewResult(comments=[])])
    assert review_diff(REPO_DIFF, graph, llm) == []  # type: ignore[arg-type]
