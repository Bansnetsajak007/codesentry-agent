"""Typer command-line interface for CodeSentry, wiring the index, ask, review,
stats, and languages commands to the graph, agent, and review subsystems with
rich-formatted output and spinners for long-running operations.

This is the Phase 1 step-4 slice: only ``index`` and ``stats`` are implemented;
``ask``, ``review``, and ``languages`` are added in their build-order steps."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from codesentry.agent.llm import LLMClient
from codesentry.agent.loop import run_agent
from codesentry.config import get_settings
from codesentry.graph.builder import build_graph
from codesentry.graph.store import (
    load_graph,
    load_metadata,
    per_language_file_counts,
    save_graph,
)

app = typer.Typer(help="CodeSentry: language-agnostic code understanding.")
console = Console()

_GRAPH_RELATIVE_PATH = Path(".codesentry") / "graph.pkl"


def _graph_path(repo_path: Path) -> Path:
    return repo_path / _GRAPH_RELATIVE_PATH


@app.command()
def index(repo_path: Path = typer.Argument(..., help="Path to the repository.")) -> None:
    """Index REPO_PATH into a graph saved under .codesentry/graph.pkl."""

    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        console.print(f"[red]Not a directory:[/red] {repo_path}")
        raise typer.Exit(code=1)

    with console.status(f"Indexing {repo_path}..."):
        graph = build_graph(repo_path)
        graph_path = _graph_path(repo_path)
        save_graph(graph, graph_path, repo_path=repo_path)

    console.print(
        f"[green]Indexed[/green] {graph.number_of_nodes()} nodes, "
        f"{graph.number_of_edges()} edges -> {graph_path}"
    )
    _print_language_table(per_language_file_counts(graph))


@app.command()
def stats(repo_path: Path = typer.Argument(..., help="Path to the repository.")) -> None:
    """Print graph statistics for a previously indexed REPO_PATH."""

    repo_path = repo_path.resolve()
    graph_path = _graph_path(repo_path)
    if not graph_path.is_file():
        console.print(
            f"[red]No graph found[/red] at {graph_path}. Run `codesentry index` first."
        )
        raise typer.Exit(code=1)

    graph = load_graph(graph_path)
    meta = load_metadata(graph_path)
    console.print(f"[bold]Repository:[/bold] {meta.get('repo_path')}")
    console.print(f"[bold]Indexed at:[/bold] {meta.get('indexed_at')}")
    console.print(f"[bold]Git commit:[/bold] {meta.get('git_commit')}")
    console.print(
        f"[bold]Nodes:[/bold] {graph.number_of_nodes()}  "
        f"[bold]Edges:[/bold] {graph.number_of_edges()}"
    )
    resolution = meta.get("resolution")
    if isinstance(resolution, dict):
        console.print(
            f"[bold]Unresolved calls:[/bold] {resolution.get('unresolved_calls')}  "
            f"[bold]Skipped files:[/bold] {resolution.get('files_skipped')}  "
            f"[bold]Parse errors:[/bold] {resolution.get('files_with_parse_errors')}"
        )
    _print_language_table(per_language_file_counts(graph))


@app.command()
def ask(
    repo_path: Path = typer.Argument(..., help="Path to the indexed repository."),
    question: str = typer.Argument(..., help="Question to ask about the repository."),
    max_iterations: int = typer.Option(15, help="Max agent tool-loop iterations."),
    model: str | None = typer.Option(None, help="Override the configured model."),
) -> None:
    """Answer a QUESTION about REPO_PATH with citations to real file:line locations."""

    repo_path = repo_path.resolve()
    graph_path = _graph_path(repo_path)
    if not graph_path.is_file():
        console.print(
            f"[red]No graph found[/red] at {graph_path}. Run `codesentry index` first."
        )
        raise typer.Exit(code=1)

    settings = get_settings()
    if not settings.openai_api_key:
        console.print("[red]OPENAI_API_KEY is not set.[/red] Add it to your .env.")
        raise typer.Exit(code=1)

    graph = load_graph(graph_path)
    llm = LLMClient(
        api_key=settings.openai_api_key,
        model=model or settings.model,
        base_url=settings.openai_base_url,
        max_tokens=settings.max_tokens,
    )
    with console.status("Thinking..."):
        answer = run_agent(
            question, graph, llm, repo_root=repo_path, max_iterations=max_iterations
        )

    console.print(answer.answer)
    if answer.citations:
        table = Table(title="Citations")
        table.add_column("File")
        table.add_column("Lines", justify="right")
        for citation in answer.citations:
            table.add_row(citation.file, f"{citation.start_line}-{citation.end_line}")
        console.print(table)


def _print_language_table(counts: dict[str, int]) -> None:
    table = Table(title="Files per language")
    table.add_column("Language")
    table.add_column("Files", justify="right")
    for language, count in counts.items():
        table.add_row(language, str(count))
    if not counts:
        table.add_row("(none)", "0")
    console.print(table)
