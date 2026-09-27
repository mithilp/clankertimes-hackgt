"use client";

// A script that runs while the browser parses the server's HTML, before the first paint. On the client
// React renders it inert (text/plain), so it doesn't run twice or warn; suppressHydrationWarning accepts
// the difference. The pattern from Next's "Preventing flash before hydration" guide.
export default function InlineScript({ html }: { html: string }) {
  return (
    <script
      type={typeof window === "undefined" ? "text/javascript" : "text/plain"}
      suppressHydrationWarning
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
