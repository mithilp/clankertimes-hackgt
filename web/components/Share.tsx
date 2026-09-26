"use client";

import { useState } from "react";

// Share links for an article: plain intent URLs, so they work without any platform scripts.
export default function Share({ url, title }: { url: string; title: string }) {
  const [copied, setCopied] = useState(false);
  const u = encodeURIComponent(url);
  const t = encodeURIComponent(title);
  const links = [
    { name: "X", href: `https://x.com/intent/post?text=${t}&url=${u}` },
    { name: "Bluesky", href: `https://bsky.app/intent/compose?text=${encodeURIComponent(`${title} ${url}`)}` },
    { name: "LinkedIn", href: `https://www.linkedin.com/sharing/share-offsite/?url=${u}` },
    { name: "Facebook", href: `https://www.facebook.com/sharer/sharer.php?u=${u}` },
    { name: "Email", href: `mailto:?subject=${t}&body=${encodeURIComponent(`${title}\n\n${url}`)}` },
  ];

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      window.prompt("Copy this link:", url);
    }
  }

  return (
    <div className="share" aria-label="Share this article">
      <span className="label">Share</span>
      {links.map((l) => (
        <a key={l.name} href={l.href} target="_blank" rel="noopener noreferrer">{l.name}</a>
      ))}
      <button type="button" onClick={copy}>{copied ? "Link copied" : "Copy link"}</button>
    </div>
  );
}
