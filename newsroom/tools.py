"""Research tools shared by scouts and reporters. No source whitelist: agents choose where to look."""

from . import memory, web
from .config import settings
from .llm import AgentContext, Tool, dumps


def research_tools(ctx: AgentContext) -> list[Tool]:
    async def web_search(inp: dict) -> str:
        results = await web.search(inp["query"], freshness=inp.get("freshness"))
        return dumps(results) if results else "No results."

    async def news_search(inp: dict) -> str:
        results = await web.search(inp["query"], news=True, freshness=inp.get("freshness"))
        return dumps(results) if results else "No news coverage found."

    async def _page(url: str, text: str, offset: int) -> tuple[str, object]:
        source_id, changed = await web.archive(url, text, ctx.agent)
        chunk = text[offset: offset + settings.fetch_chars]
        more = len(text) - (offset + len(chunk))
        tail = f"\n\n[{more} more chars; call again with offset={offset + len(chunk)}]" if more > 0 else ""
        return f"[{len(text)} chars total]\n{chunk}{tail}", source_id

    async def fetch_url(inp: dict):
        text = await web.fetch(inp["url"])
        return await _page(inp["url"], text, int(inp.get("offset", 0)))

    async def browse(inp: dict):
        text = await web.browser.render(inp["url"], inp.get("wait_for"))
        return await _page(inp["url"], text, int(inp.get("offset", 0)))

    async def memory_search(inp: dict) -> str:
        hits = await memory.recall(inp["query"])
        return "\n".join(f"- {h}" for h in hits) if hits else "Nothing in newsroom memory."

    async def memory_note(inp: dict) -> str:
        await memory.remember(inp["note"], agent_id=ctx.agent, kind=inp["kind"])
        return "Saved."

    freshness = {
        "type": "string", "enum": ["pd", "pw", "pm", "py"],
        "description": "Optional recency filter: past day / week / month / year.",
    }
    url_props = {
        "url": {"type": "string"},
        "offset": {"type": "integer", "description": "Character offset to continue a long document."},
    }
    return [
        Tool("web_search", "Search the web. Returns titles, URLs, snippets.",
             {"query": {"type": "string"}, "freshness": freshness}, ["query"], web_search),
        Tool("news_search", "Search news coverage. Use it to find what has already been reported and by whom.",
             {"query": {"type": "string"}, "freshness": freshness}, ["query"], news_search),
        Tool("fetch_url", "Fetch a URL (HTML or PDF) and return its text. The text is archived; quotes you cite "
             "later are checked against it verbatim.", url_props, ["url"], fetch_url),
        Tool("browse", "Render a page in a real headless browser, for pages that need JavaScript (portals, search "
             "UIs). Slower than fetch_url. Never tries to get past CAPTCHAs or bot checks.",
             {**url_props, "wait_for": {"type": "string", "description": "Optional CSS selector to wait for."}},
             ["url"], browse),
        Tool("memory_search", "Search shared newsroom memory: beat notes, past kill-memo lessons, which sources "
             "are reliable or hard to reach. Hints about where to look, never evidence.",
             {"query": {"type": "string"}}, ["query"], memory_search),
        Tool("memory_note", "Save a lesson for other agents: a source that was useful or useless, a portal quirk, "
             "why a line of inquiry went nowhere. Not for facts about the story.",
             {"note": {"type": "string"},
              "kind": {"type": "string", "enum": ["beat_note", "source_reliability", "kill_lesson"]}},
             ["note", "kind"], memory_note, billable=False),
    ]
