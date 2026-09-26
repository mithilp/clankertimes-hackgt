import type { MetadataRoute } from "next";
import { SECTIONS, listArticles } from "@/lib/articles";
import { absolute } from "@/lib/site";

export const revalidate = 300;

// Real articles only: the fictional samples stay out of search engines.
export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const articles = (await listArticles()).filter((a) => !a.sample);
  return [
    { url: absolute("/"), changeFrequency: "hourly", priority: 1 },
    ...Object.keys(SECTIONS).map((beat) => ({ url: absolute(`/section/${beat}`), changeFrequency: "hourly" as const, priority: 0.7 })),
    { url: absolute("/about"), changeFrequency: "monthly", priority: 0.4 },
    ...articles.map((a) => ({ url: absolute(`/article/${a.slug}`), lastModified: a.corrections?.at(-1)?.at ?? a.published_at, priority: 0.8 })),
  ];
}
