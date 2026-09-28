// Sources as readers count them: one website is one source, however many of its pages and passages a story
// quotes. Kept apart from lib/articles.ts so client components can use it without the Astra client.
import type { Article, Source } from "@/lib/articles";

// A fixed order, so each group keeps its color on every article. Three colored groups is the most that stay
// distinguishable side by side, including for colorblind readers; everything else is "Other".
export const SOURCE_GROUPS = [
  { key: "record", label: "Official records", one: "Official records", kinds: ["record", "data"] },
  { key: "news", label: "News outlets", one: "News outlet", kinds: ["news"] },
  { key: "company", label: "Companies & institutions", one: "Company or institution", kinds: ["company"] },
  { key: "other", label: "Other", one: "Other", kinds: [] as string[] },
] as const;

export type SourceGroup = (typeof SOURCE_GROUPS)[number]["key"];

export function sourceGroup(kind: string): SourceGroup {
  return SOURCE_GROUPS.find((g) => (g.kinds as readonly string[]).includes(kind))?.key ?? "other";
}

export type Passage = { id: string; quote: string };
export type Page = { title: string; url: string; accessed: string; passages: Passage[] };
// One website: numbered in order of first citation, with the pages and passages the story quotes from it.
export type Site = { n: number; name: string; group: SourceGroup; pages: Page[]; passages: number };

export function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

const STATES = new Set(("al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny " +
  "nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc").split(" "));

// The website a host belongs to: its domain ("controller.gatech.edu" -> "gatech.edu", "banks.data.fdic.gov" ->
// "fdic.gov"), except under a shared parent where each subdomain is its own organization: a state's agencies
// ("psc.ga.gov") or a country's ("ons.gov.uk").
function domainOf(h: string): string {
  const parts = h.split(".");
  if (parts.length <= 2) return h;
  const [second, top] = parts.slice(-2);
  const shared = (top === "gov" && STATES.has(second)) || (top.length === 2 && ["co", "com", "gov", "ac", "org", "net", "edu"].includes(second));
  return parts.slice(shared ? -3 : -2).join(".");
}

const cleanTitle = (title: string) => title.replace(/^\[(?:PDF|DOCX?|XLSX?)\]\s*/i, "").trim();
const compact = (name: string) => name.toLowerCase().replace(/[^a-z0-9]/g, "");

// A Google News link hides the outlet, but its title usually ends with it: "… - WCTV (WCTV)", "… - CBS News".
function outletOf(title: string): string | null {
  const named = title.match(/\(([^()]{2,40})\)\s*$/)?.[1] ?? title.match(/\s[-–—]\s([^-–—]{2,40})$/)?.[1];
  return named && !/[.!?]$/.test(named.trim()) ? named.trim() : null;
}

// Which website a passage comes from, as [key, name]. The newsroom's own list of what raised the question
// ("D") is a source of its own.
function website(id: string, s: Source, domains: Set<string>): [string, string] {
  const title = cleanTitle(s.title);
  if (id === "D") return ["D", title];
  const h = host(s.url);
  if (h === "news.google.com") {
    // A named outlet is that outlet, merged with its own site when the story also cites it directly. Links
    // that don't name one are all from the one website they came through.
    const outlet = outletOf(title);
    if (!outlet) return [h, h];
    const own = [...domains].find((d) => compact(d.split(".")[0]) === compact(outlet));
    return own ? [own, own] : [`outlet:${compact(outlet)}`, outlet];
  }
  return h ? [domainOf(h), domainOf(h)] : [s.url || title, title];
}

// The websites a story cites, numbered in order of first citation, and which one each passage is from
// (passage id -> index into sites). A website's kind is the kind most of its passages are.
export function citedSites(article: Pick<Article, "paragraphs" | "sources">, date: (iso: string) => string = (d) => d) {
  const cited: string[] = [];
  for (const paragraph of article.paragraphs)
    for (const sentence of paragraph)
      for (const id of sentence.cite) if (article.sources[id] && !cited.includes(id)) cited.push(id);
  const domains = new Set(cited.map((id) => host(article.sources[id].url)).filter((h) => h && h !== "news.google.com").map(domainOf));

  const sites: Site[] = [], keys: string[] = [], kinds: SourceGroup[][] = [];
  const siteOf: Record<string, number> = {};
  for (const id of cited) {
    const s = article.sources[id];
    const [key, name] = website(id, s, domains);
    let i = keys.indexOf(key);
    if (i < 0) {
      i = sites.push({ n: sites.length + 1, name, group: "other", pages: [], passages: 0 }) - 1;
      keys.push(key);
      kinds.push([]);
    }
    const site = sites[i];
    let page = site.pages.find((p) => p.url === s.url);
    if (!page) {
      page = { title: cleanTitle(s.title) || name, url: s.url, accessed: s.accessed ? date(s.accessed) : "", passages: [] };
      site.pages.push(page);
    }
    page.passages.push({ id, quote: s.quote ?? "" });
    site.passages += 1;
    kinds[i].push(sourceGroup(s.kind));
    siteOf[id] = i;
  }
  sites.forEach((site, i) => {
    const count = (g: SourceGroup) => kinds[i].filter((k) => k === g).length;
    site.group = kinds[i].reduce((best, g) => (count(g) > count(best) ? g : best), kinds[i][0]);
  });
  return { sites, siteOf };
}
