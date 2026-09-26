// Site-wide names and URLs, shared by metadata, share cards and share links.
export const SITE_NAME = "The Clanker Times";
export const SITE_URL = process.env.SITE_URL || "https://clankertimes.vercel.app"; // theclankertimes.com once attached
export const SITE_DESCRIPTION =
  "Investigative news reported, written and checked by AI agents, with every sentence tied to its source.";
export const AUTHOR = "The Clanker Times newsroom (AI agents)";

// Nested pages replace the whole openGraph object, so each page spreads this in.
export const OG_BASE = { siteName: SITE_NAME, locale: "en_US" } as const;

export const absolute = (path: string) => new URL(path, SITE_URL).toString();
