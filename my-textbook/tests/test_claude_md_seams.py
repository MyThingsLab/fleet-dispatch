from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The exact placeholder strings CLAUDE.template.md ships with. Legitimate prose
# may contain other <...> (URLs, pytest node ids), so only these count as seams.
TEMPLATE_SEAMS = (
    "my-<x>",
    "<one line — what this tool does>",
    "<the one judgment step delegated to a model",
    "<what must always hold; what this tool may never do>",
    "<the GitHub issue label it picks up>",
)


def _package_name() -> str:
    packages = [p for p in (ROOT / "src").iterdir() if (p / "__init__.py").exists()]
    assert len(packages) == 1, f"expected exactly one package under src/, found {packages}"
    return packages[0].name


def test_claude_md_seams_are_filled_after_rename() -> None:
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    remaining = [seam for seam in TEMPLATE_SEAMS if seam in text]
    if _package_name() == "mytemplate":
        # The pristine template must keep every seam open for the next copy.
        assert remaining == list(TEMPLATE_SEAMS)
    else:
        assert not remaining, (
            f"CLAUDE.md still has unfilled template seams: {remaining} — "
            "fill the four per-tool seams (purpose, Engine call, invariants, backlog label)"
        )
