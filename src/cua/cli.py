"""Composition root: wires modules together and exposes the demo commands."""

import typer

app = typer.Typer(no_args_is_help=True)


@app.command()
def discover(goal: str, target: str) -> None:
    """Run LLM discovery on a goal and save a draft capability artifact."""
    raise NotImplementedError


@app.command()
def replay(capability: str, params: str = "{}") -> None:
    """Replay a saved capability deterministically with JSON params."""
    raise NotImplementedError


if __name__ == "__main__":
    app()
