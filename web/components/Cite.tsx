"use client";

import { useEffect, useRef, useState } from "react";
import type { Source } from "@/lib/articles";
import { smart } from "@/lib/text";

// A footnote number after a sentence. Hovering or focusing it shows the exact passage the sentence
// rests on; clicking pins that open (tap on phones). Without JavaScript it is a plain link to the
// source list below the article.
export default function Cite({ n, id, source }: { n: number; id: string; source: Source }) {
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const open = hover || pinned;

  useEffect(() => {
    if (!pinned) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setPinned(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [pinned]);

  return (
    <span ref={ref} className="cite-wrap" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <a
        href={`#source-${id}`}
        className="cite"
        aria-expanded={open}
        aria-label={`Source ${n}: ${source.title}`}
        onClick={(e) => { e.preventDefault(); setPinned((p) => !p); }}
        onFocus={() => setHover(true)}
        onBlur={(e) => { if (!ref.current?.contains(e.relatedTarget as Node)) setHover(false); }}
      >
        {n}
      </a>
      {open && (
        <span className="pop" role="tooltip">
          <span className="src">{source.title}</span>
          <span className="meta">{[source.publisher, source.kind].filter(Boolean).join(" · ")}</span>
          {source.quote && <q>{smart(source.quote)}</q>}
          <span>
            <a href={`#source-${id}`} onClick={() => setPinned(false)}>Source {n} below</a>
            {" · "}
            <a href={source.url} target="_blank" rel="noopener noreferrer">Open the record</a>
          </span>
        </span>
      )}
    </span>
  );
}
