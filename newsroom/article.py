"""Step 8: check that every sentence is cited and every quote is real, then publish as Markdown."""

import re
from datetime import date
from pathlib import Path

from .text import contains_quote

QUOTED = re.compile(r'["“]([^"”]{10,})["”]')


def check(article: dict, sources: dict[str, dict]) -> list[str]:
    """Problems with an article; empty means it can be published.

    article: {"headline", "paragraphs": [[{"text", "cite": [source ids]}]]}
    sources: source id -> {"title", "url", "text"}, where text is everything that may be quoted from it.
    """
    problems = []
    if not str(article.get("headline", "")).strip():
        problems.append("missing headline")
    paragraphs = article.get("paragraphs") or []
    if not paragraphs:
        problems.append("no paragraphs")
    for p, paragraph in enumerate(paragraphs, 1):
        for s, sentence in enumerate(paragraph, 1):
            where = f"paragraph {p}, sentence {s}"
            text = str(sentence.get("text", "")).strip()
            cites = sentence.get("cite") or []
            if not text:
                problems.append(f"{where}: empty sentence")
            elif not cites:
                problems.append(f"{where}: cites no source")
            elif unknown := [c for c in cites if c not in sources]:
                problems.append(f"{where}: cites unknown source {', '.join(map(str, unknown))}")
            else:
                for quote in QUOTED.findall(text):
                    if not any(contains_quote(sources[c]["text"], quote) for c in cites):
                        problems.append(f'{where}: quote not found in its sources: "{quote}"')
    return problems


def render(article: dict, sources: dict[str, dict], published: date) -> str:
    """Markdown with numbered sources, numbered in order of first citation."""
    numbers: dict[str, int] = {}
    lines = [f"# {article['headline'].strip()}", "",
             f"*Published {published:%B} {published.day}, {published.year}. "
             "Every sentence links to its sources, listed below.*", ""]
    for paragraph in article["paragraphs"]:
        parts = []
        for sentence in paragraph:
            refs = "".join(f"[{numbers.setdefault(c, len(numbers) + 1)}]" for c in sentence["cite"])
            parts.append(f"{sentence['text'].strip()} {refs}")
        lines += [" ".join(parts), ""]
    lines += ["## Sources", ""]
    for source_id, n in sorted(numbers.items(), key=lambda item: item[1]):
        source = sources[source_id]
        line = f"{n}. [{source['title'] or source['url']}]({source['url']})"
        if source.get("quote"):
            line += f': "{source["quote"]}"'
        lines.append(line)
    return "\n".join(lines) + "\n"


def publish(story_id: int, article: dict, sources: dict[str, dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", article["headline"].lower()).strip("-")[:60]
    path = out_dir / f"{story_id:04d}-{slug}.md"
    path.write_text(render(article, sources, date.today()), encoding="utf-8")
    return path
