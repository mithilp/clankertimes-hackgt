"use client";

import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { SOURCE_GROUPS, type Site, type SourceGroup } from "@/lib/sources";
import { smart } from "@/lib/text";

const SHOWN = 8;   // websites listed before "Show all"

// Open the website a passage is from and bring the passage into view. Every row is always in the page,
// maybe folded away under "Show all".
function open(passage: HTMLElement, more: HTMLDetailsElement | null) {
  const row = passage.closest(".src");
  if (more?.contains(passage)) more.open = true;
  row?.querySelector("details")?.setAttribute("open", "");
  (passage.offsetHeight || !row ? passage : row).scrollIntoView({ block: "center" });   // a passage with no quote shows nothing
}

const counted = (site: Site) =>
  site.pages.length > 1
    ? `${site.pages.length} pages, ${site.passages} passages`
    : `${site.passages} passage${site.passages === 1 ? "" : "s"}`;

// The sources a story cites, one per website: a bar of what kind of evidence it rests on, a legend that
// filters the list by kind, and one line per website that opens to the pages and passages quoted from it.
// The list is the bar's table view: every number the bar shows is also written out.
export default function Sources({ sites }: { sites: Site[] }) {
  const [only, setOnly] = useState<SourceGroup | null>(null);   // the kind the reader filtered to
  const [hover, setHover] = useState<SourceGroup | null>(null);
  const more = useRef<HTMLDetailsElement>(null);
  const moreWasOpen = useRef(false);

  const total = sites.length;
  const groups = SOURCE_GROUPS.map((g) => ({ ...g, count: sites.filter((s) => s.group === g.key).length })).filter((g) => g.count);
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

  // A footnote's "Source N below" link, or a shared #source-… URL, opens that passage's website even when it is
  // folded away or filtered out.
  useEffect(() => {
    const reveal = (id: string) => {
      const passage = document.getElementById(id);
      if (!passage?.closest(".src")) return;
      flushSync(() => setOnly(null));
      open(passage, more.current);
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
    const passage = first.startsWith("source-") ? document.getElementById(first) : null;
    if (passage?.closest(".src")) open(passage, more.current);
    window.addEventListener("hashchange", fromHash);
    document.addEventListener("click", fromClick);
    return () => {
      window.removeEventListener("hashchange", fromHash);
      document.removeEventListener("click", fromClick);
    };
  }, []);

  const share = (count: number) => `${Math.round((count / total) * 100)}%`;
  const row = (site: Site) => {
    const group = SOURCE_GROUPS.find((g) => g.key === site.group)!;
    return (
      <li key={site.n} className={`src g-${site.group}`} hidden={only !== null && site.group !== only}>
        <details>
          <summary>
            <span className="n">{site.n}</span>
            <span>
              <span className="t">{site.name}</span>
              <span className="pg">{site.pages[0].title}</span>
              <span className="m">
                <span className="sw" aria-hidden="true" />
                <span>{group.one}</span>
                <span aria-hidden="true">·</span>
                <span>{counted(site)}</span>
              </span>
            </span>
            <span className="chev" aria-hidden="true" />
          </summary>
          <div className="d">
            {site.pages.map((page) => (
              <div key={page.url || page.title} className="page">
                <a className="pt" href={page.url} target="_blank" rel="noopener noreferrer">{page.title}</a>
                {page.passages.map((p) => (
                  <p key={p.id} id={`source-${p.id}`} className="psg">{p.quote && <q>{smart(p.quote)}</q>}</p>
                ))}
                <p className="u">{page.url}{page.accessed && <> · retrieved {page.accessed}</>}</p>
              </div>
            ))}
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
          ? <><strong>{total}</strong> {total === 1 ? "source" : "sources"} cited, all {groups[0].label.toLowerCase()} · each website counts once</>
          : read
            ? <>{read.label}: <strong>{read.count}</strong> of {total} sources ({share(read.count)})</>
            : <><strong>{total}</strong> sources cited · each website counts once</>}
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
        <ol className="srcs">{sites.slice(0, SHOWN).map(row)}</ol>
        {total > SHOWN && (
          <details className="more-srcs" ref={more}>
            <summary>
              <span className="closed">Show all {total} sources</span>
              <span className="opened">Show fewer sources</span>
            </summary>
            <ol className="srcs">{sites.slice(SHOWN).map(row)}</ol>
          </details>
        )}
      </div>
    </section>
  );
}
