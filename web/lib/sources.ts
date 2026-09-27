// Source types as readers see them. Kept apart from lib/articles.ts so client components can use them
// without pulling in the Astra client.

// A fixed order, so each group keeps its color on every article. Three colored groups is the most that stay
// distinguishable side by side, including for colorblind readers; everything else is "Other".
export const SOURCE_GROUPS = [
  { key: "record", label: "Official records", one: "Official record", kinds: ["record", "data"] },
  { key: "news", label: "News reports", one: "News report", kinds: ["news"] },
  { key: "company", label: "Company documents", one: "Company document", kinds: ["company"] },
  { key: "other", label: "Other", one: "Other", kinds: [] as string[] },
] as const;

export type SourceGroup = (typeof SOURCE_GROUPS)[number]["key"];

export function sourceGroup(kind: string): SourceGroup {
  return SOURCE_GROUPS.find((g) => (g.kinds as readonly string[]).includes(kind))?.key ?? "other";
}

// One cited source, ready for the source list: numbered in order of first citation.
export type SourceItem = {
  id: string;
  n: number;
  title: string;
  group: SourceGroup;
  host: string;
  url: string;
  quote: string;
  accessed: string;   // already formatted for display
};

export function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
