"use client";

import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { SOURCE_GROUPS, type SourceGroup, type SourceItem } from "@/lib/sources";
import { smart } from "@/lib/text";

const SHOWN = 8;   // sources listed before "Show all"

// Open one source and bring it into view: its row is always in the page, maybe folded away under "Show all".
function open(row: HTMLElement, more: HTMLDetailsElement | null) {
  if (more?.contains(row)) more.open = true;
  row.querySelector("details")?.setAttribute("open", "");
  row.scrollIntoView({ block: "center" });
}

// The sources a story cites: a bar of what kind of evidence it rests on, a legend that filters the list by
// kind, and one line per source that opens to the quoted passage. The list is the bar's table view: every
// number the bar shows is also written out.
export default function Sources({ items }: { items: SourceItem[] }) {
  const [only, setOnly] = useState<SourceGroup | null>(null);   // the kind the reader filtered to
  const [hover, setHover] = useState<SourceGroup | null>(null);
  const more = useRef<HTMLDetailsElement>(null);
  const moreWasOpen = useRef(false);

  const total = items.length;
  const groups = SOURCE_GROUPS.map((g) => ({ ...g, count: items.filter((s) => s.group === g.key).length })).filter((g) => g.count);
  const focus = hover ?? only;
  const read = groups.find((g) => g.key === focus);

  // Filtering hides the other kinds' rows rather than removing them, and unfolds "Show all" so every match shows;
  // clearing the filter folds it back the way the reader left it.
  function filter(next: SourceGroup | null) {
    const fold = more.current;
    if (fold && next && !only) {
      moreWasOpen.current = fold.open;
      fold.open = true;
    }
    if (fold && !next && only) fold.open = moreWasOpen.current;
    setOnly(next);
  }

  // A footnote's "Source N below" link, or a shared #source-… URL, opens that source even when it is folded
  // away or filtered out.
  useEffect(() => {
    const reveal = (id: string) => {
      const row = document.getElementById(id);
      if (!row?.classList.contains("src")) return;
      flushSync(() => setOnly(null));
      open(row, more.current);
    };
    const fromHash = () => {
      const id = decodeURIComponent(window.location.hash.slice(1));
      if (id.startsWith("source-")) reveal(id);
    };
    const fromClick = (e: MouseEvent) => {
      const link = (e.target as Element | null)?.closest?.('a[href^="#source-"]:not(.cite)');
      if (link) reveal(link.getAttribute("href")!.slice(1));
    };
    // A page opened at #source-…: nothing is filtered yet, so just unfold it.
    const first = decodeURIComponent(window.location.hash.slice(1));
    const row = first.startsWith("source-") ? document.getElementById(first) : null;
    if (row) open(row, more.current);
    window.addEventListener("hashchange", fromHash);
    document.addEventListener("click", fromClick);
    return () => {
      window.removeEventListener("hashchange", fromHash);
      document.removeEventListener("click", fromClick);
    };
  }, []);

  const share = (count: number) => `${Math.round((count / total) * 100)}%`;
  const noun = (g: (typeof groups)[number]) =>
    g.key === "other" ? (g.count === 1 ? "other source" : "other sources") : (g.count === 1 ? g.one : g.label).toLowerCase();
  const row = (s: SourceItem) => {
    const group = SOURCE_GROUPS.find((g) => g.key === s.group)!;
    return (
      <li key={s.id} id={`source-${s.id}`} className={`src g-${s.group}`} hidden={only !== null && s.group !== only}>
        <details>
          <summary>
            <span className="n">{s.n}</span>
            <span>
              <span className="t">{s.title}</span>
              {s.quote && <span className="q">{smart(s.quote)}</span>}
              <span className="m">
                <span className="sw" aria-hidden="true" />
                <span>{group.one}</span>
                <span aria-hidden="true">·</span>
                <span>{s.host}</span>
              </span>
            </span>
            <span className="chev" aria-hidden="true" />
          </summary>
          <div className="d">
            {s.quote && <q>{smart(s.quote)}</q>}
            <p>
              <a href={s.url} target="_blank" rel="noopener noreferrer">Open the record</a>
              {s.accessed && <> · retrieved {s.accessed}</>}
              <br />
              {s.url}
            </p>
          </div>
        </details>
      </li>
    );
  };

  return (
    <section aria-labelledby="sources" className={only ? "filtered" : undefined}>
      <h2 id="sources">Sources</h2>

      <p className="mix-read" aria-live="polite">
        {groups.length === 1
          ? <><strong>{total}</strong> sources cited, all {noun(groups[0])}</>
          : read
            ? <><strong>{read.count}</strong> {noun(read)} · {share(read.count)} of the {total} sources</>
            : <><strong>{total}</strong> sources cited, by kind</>}
      </p>
      {groups.length > 1 && (
        <div className="mix" aria-hidden="true">
          {groups.map((g) => (
            <span
              key={g.key}
              className={`seg g-${g.key}${focus && focus !== g.key ? " dim" : ""}`}
              style={{ flexGrow: g.count }}
              onMouseEnter={() => setHover(g.key)}
              onMouseLeave={() => setHover(null)}
              onClick={() => filter(only === g.key ? null : g.key)}
            />
          ))}
        </div>
      )}
      {groups.length > 1 && (
        <ul className="legend" aria-label="Show only one kind of source">
          {groups.map((g) => (
            <li key={g.key}>
              <button
                type="button"
                className={`g-${g.key}${hover === g.key ? " hot" : ""}`}
                aria-pressed={only === g.key}
                onClick={() => filter(only === g.key ? null : g.key)}
                onMouseEnter={() => setHover(g.key)}
                onMouseLeave={() => setHover(null)}
              >
                <span className="sw" aria-hidden="true" />
                {g.label}
                <span className="count">{g.count}</span>
              </button>
            </li>
          ))}
          {only && (
            <li><button type="button" className="all" onClick={() => filter(null)}>Show every kind</button></li>
          )}
        </ul>
      )}

      <div>
        <ol className="srcs">{items.slice(0, SHOWN).map(row)}</ol>
        {total > SHOWN && (
          <details className="more-srcs" ref={more}>
            <summary>
              <span className="closed">Show all {total} sources</span>
              <span className="opened">Show fewer sources</span>
            </summary>
            <ol className="srcs">{items.slice(SHOWN).map(row)}</ol>
          </details>
        )}
      </div>
    </section>
  );
}
