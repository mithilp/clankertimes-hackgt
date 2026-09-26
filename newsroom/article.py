"""Step 8: check that every sentence is cited and every quote is real, then publish as Markdown.

The gates below are mechanical, not editorial: each one blocks a way real newsrooms have
published something false, and none of them needs a model call.
"""

import re
from datetime import date
from pathlib import Path

from .text import contains_quote

QUOTED = re.compile(r'["“]([^"”]{10,})["”]')

# Text that means the pipeline leaked into the copy. Gannett printed "[[WINNING_TEAM_MASCOT]]",
# and in 2026 the Telegraph, Bristol Live and Marie Claire each shipped a prompt or an AI note.
LEAKAGE = re.compile(
    r"\[\[[^\]]+\]\]|\{\{|\bTODO\b|\bundefined\b|\bnull\b|```"
    r"|as an AI|I cannot|I\'m sorry|here(?:\'s| is) the (?:JSON|article|revised)|^certainly[,!]",
    re.I | re.M,
)

# An office is the charging party, never the defendant, in its own records. Hoodline published
# "San Mateo County DA charged with murder" because a parser made the office the subject.
OFFICE = (r"(?:district attorney|attorney general|prosecutor\'s office|sheriff\'s office|police department"
          r"|office of [a-z ]+|department of [a-z ]+|the (?:FDA|NHTSA|OSHA|CPSC|EPA|DOJ)|commission|agency|administration)")
CRIMINAL_VERB = (r"(?:was |were |is |are |has been |have been )?(?:charged with|indicted|convicted of|arrested"
                 r"|pleaded guilty|sentenced to|found guilty)")
OFFICE_AS_DEFENDANT = re.compile(rf"\b{OFFICE}\b[^.]{{0,40}}?\b{CRIMINAL_VERB}\b", re.I)

# Words that impute a crime. They need a record, not complaint data, behind them.
CRIMINAL_LABEL = re.compile(
    r"\b(?:fraud(?:ulent|ulently)?|black market|kickback|launder(?:ing|ed)|embezzl\w+|bribe\w*|scheme to"
    r"|cover(?:ed)? up|illegal(?:ly)?|criminal(?:ly)?)\b", re.I)

# The data is current through a date. Present-tense absolutes outrun it.
TIMELESS = re.compile(r"\b(?:as of today|as of now|currently|right now|to date|so far this year)\b", re.I)

# Counts are known exactly, so vague quantifiers are a choice to be less accurate.
VAGUE_COUNT = re.compile(r"\b(?:many|numerous|several|countless|dozens of|scores of|a number of)\b", re.I)


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
            problems += _sentence_gates(where, text, cites)
    problems += _leakage(article)
    return problems


def _sentence_gates(where: str, text: str, cites: list) -> list[str]:
    """Checks that hold for any sentence, regardless of which sources it cites."""
    problems = []
    if match := OFFICE_AS_DEFENDANT.search(text):
        problems.append(f"{where}: makes an agency or office the subject of a criminal verb "
                        f"(\"{match.group(0).strip()}\"). An office charges; it is not the accused. "
                        "Name the person charged, or rewrite.")
    if TIMELESS.search(text):
        problems.append(f"{where}: says what is true now, but the data only runs through its cutoff date. "
                        "Date-scope the claim instead.")
    # Complaint data ("D") is the allegation itself; it cannot establish a crime or a count-free adjective.
    if cites and set(map(str, cites)) == {"D"}:
        if match := CRIMINAL_LABEL.search(text):
            problems.append(f"{where}: uses \"{match.group(0)}\" while citing only the complaint data. "
                            "A crime needs a government record, court record or news report.")
        if match := VAGUE_COUNT.search(text):
            problems.append(f"{where}: says \"{match.group(0)}\" when the exact count is known. Use the number.")
    return problems


def _leakage(article: dict) -> list[str]:
    """Template syntax, prompt echoes and refusal boilerplate anywhere in the draft."""
    blob = str(article.get("headline", "")) + "\n" + "\n".join(
        str(sentence.get("text", ""))
        for paragraph in article.get("paragraphs") or [] for sentence in paragraph
    )
    return [f'pipeline text left in the draft: "{m.group(0).strip()}"' for m in [LEAKAGE.search(blob)] if m]


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


def publish(story_id: int, article: dict, sources: dict[str, dict], out_dir: Path, site: dict | None = None) -> Path:
    """Write the Markdown file, and the website's copy. site: extra fields for the site's document
    (beats, timeline), passed to articles_store.to_document."""
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", article["headline"].lower()).strip("-")[:60]
    path = out_dir / f"{story_id:04d}-{slug}.md"
    path.write_text(render(article, sources, date.today()), encoding="utf-8")
    _to_site(path.stem, article, sources, site or {})
    return path


def _to_site(slug: str, article: dict, sources: dict[str, dict], site: dict | None = None) -> None:
    """Also publish to the articles collection the website reads, when Astra is configured. A failure
    here never loses the article: the Markdown file above is already written."""
    from . import articles_store
    if not articles_store.configured():
        return
    try:
        articles_store.AstraArticles().save(articles_store.to_document(slug, article, sources, **(site or {})))
    except Exception as e:  # noqa: BLE001 - the site copy is best effort; the file is the record
        print(f"could not publish {slug} to the site: {type(e).__name__}: {e}")
