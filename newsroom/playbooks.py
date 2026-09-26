"""Load an agent's job spec from agents/<name>/ so it can be used as that agent's instructions.

Improving an agent means editing its playbook or examples and rerunning it; nothing here changes.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "agents"
MAX_EXAMPLE_CHARS = 12_000   # keep instructions from swamping the actual input


def path(agent: str) -> Path:
    return ROOT / agent


def load(agent: str, *, examples: bool = True) -> str:
    """The playbook, followed by the agent's good and bad examples.

    agent: a folder under agents/, e.g. "bossman" or "council/skeptic".
    """
    folder = path(agent)
    playbook = folder / "playbook.md"
    if not playbook.exists():
        raise FileNotFoundError(f"no playbook at {playbook}")
    parts = [playbook.read_text(encoding="utf-8")]
    if examples:
        budget = MAX_EXAMPLE_CHARS
        for kind, label in (("good", "Examples of doing this job well"), ("bad", "Examples of doing it badly")):
            files = sorted((folder / "examples" / kind).glob("*.md"))
            texts = []
            for f in files:
                text = f.read_text(encoding="utf-8").strip()
                if len(text) > budget:
                    break
                budget -= len(text)
                texts.append(text)
            if texts:
                parts.append(f"# {label}\n\n" + "\n\n---\n\n".join(texts))
    return "\n\n".join(parts)
