"use client";

import { useLayoutEffect } from "react";

// The site follows the system's light or dark setting until a reader picks one here. The inline script in
// app/layout.tsx applies a saved choice before the first paint; which label shows is decided in CSS, so the
// server and the browser render the same button.
const KEY = "theme";

function systemTheme(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function ThemeToggle() {
  // React's development remount resets <html> attributes; put the saved choice back. A no-op in production.
  useLayoutEffect(() => {
    try {
      const saved = localStorage.getItem(KEY);
      if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
    } catch {}
  }, []);

  function toggle() {
    const root = document.documentElement;
    const now = root.getAttribute("data-theme") ?? systemTheme();
    const next = now === "dark" ? "light" : "dark";
    try {
      // Back to what the system says: forget the choice, so the site follows the system again.
      if (next === systemTheme()) localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, next);
    } catch {}
    if (next === systemTheme()) root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", next);
  }

  return (
    <button type="button" className="theme-toggle" onClick={toggle} aria-label="Switch between light and dark mode">
      <span className="to-dark">Dark mode</span>
      <span className="to-light">Light mode</span>
    </button>
  );
}
