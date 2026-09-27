"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { smart } from "@/lib/text";

const MARGIN = 12;   // the closest a popover gets to the edge of the screen

// One passage a sentence rests on: the quote, and the page it is on.
export type CitedPassage = { id: string; title: string; url: string; quote: string };

// A footnote number after a sentence: the number of the website it cites. Hovering or focusing it shows the
// exact passages from that website the sentence rests on; clicking pins that open (tap on phones). Without
// JavaScript it is a plain link to the source list below the article.
export default function Cite({ n, site, kind, passages }: { n: number; site: string; kind: string; passages: CitedPassage[] }) {
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const pop = useRef<HTMLSpanElement>(null);
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

  // Keep the popover on screen: centered on its footnote, but slid sideways where that would cross an edge,
  // and opened upward when there is no room below. Placed before paint, so it never visibly jumps.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const el = pop.current, note = ref.current;
      if (!el || !note) return;
      el.classList.remove("above");
      el.style.maxHeight = "";
      const at = note.getBoundingClientRect();
      const width = el.offsetWidth;
      const screen = document.documentElement.clientWidth;
      const left = Math.min(Math.max(at.left + at.width / 2 - width / 2, MARGIN), screen - MARGIN - width);
      el.style.left = `${Math.round(left - at.left)}px`;
      // Too tall for the room below: open on whichever side has more room, and scroll inside if it still doesn't fit.
      const box = el.getBoundingClientRect();
      const below = window.innerHeight - MARGIN - box.top, above = at.bottom - 16 - MARGIN;
      if (box.height > below) {
        if (above > below) el.classList.add("above");
        el.style.maxHeight = `${Math.max(120, Math.floor(Math.max(above, below)))}px`;
      }
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open]);

  const first = passages[0];
  return (
    <span ref={ref} className="cite-wrap" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <a
        href={`#source-${first.id}`}
        className="cite"
        aria-expanded={open}
        aria-label={`Source ${n}: ${site}`}
        onClick={(e) => { e.preventDefault(); setPinned((p) => !p); }}
        onFocus={() => setHover(true)}
        onBlur={(e) => { if (!ref.current?.contains(e.relatedTarget as Node)) setHover(false); }}
      >
        {n}
      </a>
      {open && (
        <span ref={pop} className="pop" role="tooltip">
          <span className="src">{site}</span>
          <span className="meta">{kind}{passages.length > 1 ? ` · ${passages.length} passages` : ""}</span>
          {passages.map((p) => (
            <span key={p.id} className="psg">
              {p.quote && <q>{smart(p.quote)}</q>}
              <a href={p.url} target="_blank" rel="noopener noreferrer">{p.title}</a>
            </span>
          ))}
          <a href={`#source-${first.id}`} onClick={() => setPinned(false)}>Source {n} below</a>
        </span>
      )}
    </span>
  );
}
