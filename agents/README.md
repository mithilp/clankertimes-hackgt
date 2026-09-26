# Agents

Every agent in the newsroom has a folder here that defines what doing its job well means. The code that runs an agent should load these files; improving an agent means editing them and rerunning its evals, not editing Python.

```
Bossman ──> vector DB ──> McLovin ──> Reporter ──> Council ──> publish
(what's worth         (what's the     (owns the      │  skeptic
 knowing right now)    story here?)    story)        │  virality
                                          │           │  novelty
                                       Scouts <───────┘  (fails go back to the Reporter)
```

| Agent | Job |
|---|---|
| [bossman](bossman/) | Watches the live web and decides what is worth adding to the DB |
| [mclovin](mclovin/) | Reads the DB, spots patterns, turns them into falsifiable hypotheses |
| [reporter](reporter/) | Owns one story end to end: hypotheses, scouts, verdict, draft |
| [scout](scout/) | Settles one hypothesis with browser and DB access; brings back quotes |
| [council/skeptic](council/skeptic/) | Is it true, fair, and defensible? |
| [council/virality](council/virality/) | Will people care and share it? |
| [council/novelty](council/novelty/) | Is this actually new, or did someone already report it? |

## Running agents individually

Every agent runs on its own with `python -m newsroom try <agent>`, and each folder has a `RUN.md` with the details:

| Agent | Command | How-to |
|---|---|---|
| Bossman | `try bossman [--beat NAME] [--sources ...] [--replay FILE] [--loop MIN]` | [bossman/RUN.md](bossman/RUN.md) |
| McLovin | `try mclovin [--hours N] [--input FILE]` | [mclovin/RUN.md](mclovin/RUN.md) |
| Reporter | `try reporter --from-mclovin FILE\|latest [--pick N\|--all]` or `--hypothesis "..."` | [reporter/RUN.md](reporter/RUN.md) |
| Scout | `try scout --hypothesis "..." --context "..."` | [scout/RUN.md](scout/RUN.md) |
| Council | `try council [--draft FILE] [--judges ...] [--seeded]` | [council/RUN.md](council/RUN.md) |

They chain through files and the signals store, so this runs the whole path one step at a time:

```bash
.venv/bin/python -m newsroom try bossman --sources google_trends,reddit,gov
.venv/bin/python -m newsroom try mclovin
.venv/bin/python -m newsroom try reporter --from-mclovin latest --pick 1     # scouts, verdict, draft, council
.venv/bin/python -m newsroom try council --seeded
```

## What's in each folder

| File | Purpose |
|---|---|
| `RUN.md` | How to run this agent on its own |
| `playbook.md` | How to do the job: the method, the rules, what to avoid. Seeded from research into how investigative journalism actually works and how it fails |
| `rubric.md` | How to tell good output from bad, as checkable criteria. Without this, "is it better now?" is a vibe |
| `examples/good/` | Worked examples of doing the job well. Traces (the path taken), not just polished output |
| `examples/bad/` | Worked examples of doing it badly, each with what went wrong. Many come from real published failures |
| `evals/` | Saved inputs plus human labels, rerunnable. See below |

## How agents get better

1. **Record every run.** Inputs, outputs, tool calls with their stated reasons. For Bossman, also a snapshot of what it saw, since the live web won't be there tomorrow.
2. **Label a small set by hand.** 10–20 cases per agent, marked good or bad by a person, with a one-line why. This is the ground truth and the step people skip.
3. **Score with the rubric.** Code checks where possible; an LLM judge for the rest, checked against the human labels before anyone trusts it.
4. **Change one thing, rerun the same set, compare.** The SQLite LLM cache replays every unchanged call for free, so only the part you changed costs anything.
5. **Every live failure becomes a new eval case**, and usually a new bad example too. The eval set should grow from real mistakes, not imagined ones.
6. **Hold some cases back.** Keep a few per agent that nobody tunes against, and check them last. Tuning 30 times against the same 10 cases makes those 10 look great and teaches nothing.

### Two feedback signals worth wiring up

- **Seeded errors for the council.** Plant known mistakes in otherwise good drafts (an office as the defendant, a fabricated quote, stale data stated as current, a complaint stated as fact) and measure what fraction each judge catches. See [council/skeptic/examples/bad](council/skeptic/examples/bad/).
- **Downstream use as upstream feedback.** A Bossman item McLovin never touches was probably noise; a McLovin hypothesis the Reporter kills in three calls was probably weak. This measures what the pipeline liked, not what's true, so human labels stay the anchor.

## Eval case format

One folder per case under `evals/`:

```
evals/<case-id>/
  input.json    exactly what the agent was given
  output.json   what it produced on the run being judged (optional; the harness regenerates it)
  label.json    {"verdict": "good" | "bad", "why": "...", "held_out": false}
```

## Editing rules

- Change a playbook in a PR, and say which eval cases you expect it to move.
- Examples must be real or clearly marked as constructed. Don't invent a news event and present it as one that happened.
- Keep sources. Every rule in a playbook that comes from somewhere should link to where.
