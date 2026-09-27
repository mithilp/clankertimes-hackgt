// Articles come from the Astra DB `articles` collection that the Reporter publishes into
// (newsroom/articles_store.py). Without Astra settings, the site shows the sample articles in
// data/sample-articles.json, so it runs anywhere without credentials.
import { DataAPIClient } from "@datastax/astra-db-ts";
import sampleData from "@/data/sample-articles.json";

export type Sentence = { text: string; cite: string[] };

export type Source = {
  kind: string;
  title: string;
  publisher?: string;
  url: string;
  accessed?: string;
  quote?: string;
};

// One step in how the story came together, oldest first. Dead ends and material that was cut are
// not recorded here; the timeline shows the path that led to the story.
export type Step = {
  at: string;
  who: string;          // the agent: "Technology desk", "Scout 2", "Skeptic", "Published"
  text: string;
  result?: "" | "confirmed" | "unclear" | "revise" | "approved" | "published" | "corrected";
};

export type Article = {
  slug: string;
  sample?: boolean;
  status: string;
  beats: string[];
  kicker?: string;
  headline: string;
  dek?: string;
  found?: string;          // what this reporting established that no outlet had published
  prior?: string;          // who reported what before
  why_it_matters?: string;
  published_at: string;
  paragraphs: Sentence[][];
  sources: Record<string, Source>;
  timeline?: Step[];
  corrections?: { at: string; text: string }[];
};

const samples = sampleData as unknown as Article[];

export const SECTIONS: Record<string, string> = {
  atlanta: "Atlanta",
  "georgia-tech": "Georgia Tech",
  "us-politics": "Politics",
  markets: "Markets",
  banking: "Banking",
  "public-money": "Public Money",
  tech: "Technology",
};

// Every beat's name, including beats without their own section, for kickers.
export const BEAT_NAMES: Record<string, string> = {
  ...SECTIONS,
  "ai-industry": "Artificial Intelligence",
  "product-safety": "Product Safety",
  "higher-ed": "Higher Education",
};

const FIELDS = [
  "slug", "sample", "status", "beats", "kicker", "headline", "dek", "found", "prior", "why_it_matters", "published_at",
  "paragraphs", "sources", "timeline", "corrections",
] as const;

function collection() {
  const endpoint = process.env.ASTRA_DB_API_ENDPOINT;
  const token = process.env.ASTRA_DB_APPLICATION_TOKEN;
  if (!endpoint || !token) return null;
  const db = new DataAPIClient(token).db(endpoint, { keyspace: process.env.ASTRA_DB_KEYSPACE || undefined });
  return db.collection(process.env.NEWSROOM_ARTICLES_COLLECTION || "articles");
}

// Only plain fields cross into pages: Astra's own fields ($vector, dates) stay behind.
function clean(doc: Record<string, unknown>): Article {
  const out: Record<string, unknown> = {};
  for (const key of FIELDS) if (key in doc) out[key] = doc[key];
  return out as Article;
}

function newestFirst(a: Article, b: Article) {
  return b.published_at.localeCompare(a.published_at);
}

export async function listArticles(beat?: string): Promise<Article[]> {
  const coll = collection();
  if (coll) {
    try {
      const filter: Record<string, unknown> = { status: "published" };
      if (beat) filter.beats = beat;
      const docs = await coll.find(filter, { sort: { published_ts: -1 }, limit: 60 }).toArray();
      return docs.map((d) => clean(d as Record<string, unknown>));
    } catch (error) {
      console.error("Astra read failed; showing sample articles", error);
    }
  }
  const all = samples.filter((a) => a.status === "published");
  return (beat ? all.filter((a) => a.beats.includes(beat)) : all).sort(newestFirst);
}

export async function getArticle(slug: string): Promise<Article | null> {
  const coll = collection();
  if (coll) {
    try {
      const doc = await coll.findOne({ _id: slug, status: "published" });
      return doc ? clean(doc as Record<string, unknown>) : null;
    } catch (error) {
      console.error("Astra read failed; showing sample articles", error);
    }
  }
  return samples.find((a) => a.slug === slug) ?? null;
}

// Numbered in order of first citation, the way a reader meets them.
export function sourceOrder(article: Article): string[] {
  const seen: string[] = [];
  for (const paragraph of article.paragraphs)
    for (const sentence of paragraph)
      for (const id of sentence.cite) if (!seen.includes(id) && article.sources[id]) seen.push(id);
  return seen;
}

export function formatDate(iso: string, withTime = false): string {
  const d = new Date(iso);
  // A bare date ("2026-09-25") is a calendar day, not midnight UTC; don't shift it into the day before.
  const zone = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? "UTC" : "America/New_York";
  const date = d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: zone });
  if (!withTime) return date;
  const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York", timeZoneName: "short" });
  return `${date}, ${time}`;
}

export function readingMinutes(article: Article): number {
  const words = article.paragraphs.flat().map((s) => s.text).join(" ").split(/\s+/).length;
  return Math.max(1, Math.round(words / 230));
}

