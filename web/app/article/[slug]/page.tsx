import Link from "next/link";
import { notFound } from "next/navigation";
import Cite, { type CitedPassage } from "@/components/Cite";
import Sample from "@/components/Sample";
import Share from "@/components/Share";
import Sources from "@/components/Sources";
import Timeline from "@/components/Timeline";
import { AUTHOR, OG_BASE, SITE_NAME, absolute } from "@/lib/site";
import { BEAT_NAMES, SECTIONS, formatDate, getArticle, readingMinutes } from "@/lib/articles";
import { SOURCE_GROUPS, citedSites, type Site } from "@/lib/sources";
import { smart } from "@/lib/text";

export const revalidate = 60;

export async function generateMetadata({ params }: PageProps<"/article/[slug]">) {
  const { slug } = await params;
  const article = await getArticle(slug);
  if (!article) return {};
  const path = `/article/${slug}`;
  const updated = article.corrections?.at(-1)?.at;
  return {
    title: article.headline,
    description: article.dek,
    alternates: { canonical: path },
    authors: [{ name: AUTHOR }],
    // Sample articles are fictional: keep them out of search results.
    robots: article.sample ? { index: false, follow: true } : undefined,
    openGraph: {
      ...OG_BASE, type: "article", url: path, title: article.headline, description: article.dek,
      publishedTime: article.published_at, modifiedTime: updated, authors: [AUTHOR],
      section: BEAT_NAMES[article.beats[0]], tags: [article.kicker, ...article.beats.map((b) => BEAT_NAMES[b])].filter(Boolean),
    },
    twitter: { card: "summary_large_image", title: article.headline, description: article.dek },
  };
}

export default async function ArticlePage({ params }: PageProps<"/article/[slug]">) {
  const { slug } = await params;
  const article = await getArticle(slug);
  if (!article) notFound();

  // One website is one source: footnotes are numbered by website, and a sentence citing several passages from
  // one website gets one footnote that shows them all.
  const { sites, siteOf } = citedSites(article, formatDate);
  const footnotes = (cite: string[]) => {
    const out: { site: Site; passages: CitedPassage[] }[] = [];
    for (const id of cite) {
      const site = sites[siteOf[id]];
      if (!site) continue;
      const page = site.pages.find((pg) => pg.passages.some((p) => p.id === id))!;
      const passage = { id, title: page.title, url: page.url, quote: article.sources[id].quote ?? "" };
      const note = out.find((o) => o.site === site);
      if (note) note.passages.push(passage);
      else out.push({ site, passages: [passage] });
    }
    return out;
  };
  const kindOf = (site: Site) => SOURCE_GROUPS.find((g) => g.key === site.group)!.one;
  const section = article.beats.find((b) => b in SECTIONS);
  const url = absolute(`/article/${article.slug}`);
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "NewsArticle",
    headline: article.headline,
    description: article.dek,
    datePublished: article.published_at,
    dateModified: article.corrections?.at(-1)?.at ?? article.published_at,
    image: [absolute(`/article/${article.slug}/opengraph-image`)],
    author: { "@type": "Organization", name: AUTHOR, url: absolute("/about") },
    publisher: { "@type": "Organization", name: SITE_NAME, logo: { "@type": "ImageObject", url: absolute("/apple-icon") } },
    mainEntityOfPage: url,
    articleSection: BEAT_NAMES[article.beats[0]],
    isAccessibleForFree: true,
    citation: [...new Set(sites.flatMap((site) => site.pages.map((page) => page.url)).filter(Boolean))],
  };

  return (
    <>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c") }} />
      <article className="article">
        <header>
          <span className="kicker label">
            {section && <Link href={`/section/${section}`}>{SECTIONS[section]}</Link>}
            {article.kicker && <> · {article.kicker}</>}
          </span>
          <h1>{smart(article.headline)}</h1>
          {article.dek && <p className="dek">{smart(article.dek)}</p>}
          {article.sample && <Sample />}
          <div className="byline">
            <span className="who">By the Clanker Times newsroom, a team of AI agents</span>
            <span className="meta">
              {formatDate(article.published_at, true)} · {readingMinutes(article)} min read · {sites.length} sources
              {article.corrections?.length ? ` · Corrected` : ""}
            </span>
          </div>
          <Share url={url} title={article.headline} />
        </header>

        {(article.found || article.why_it_matters) && (
          <aside className="found" aria-label="What we found">
            {article.found && <p><span className="label">What we found</span>{smart(article.found)}</p>}
            {article.prior && <p className="prior"><span className="label">Reported before</span>{smart(article.prior)}</p>}
            {article.why_it_matters && <p><span className="label">Why it matters</span>{smart(article.why_it_matters)}</p>}
          </aside>
        )}

        <div className="body">
          {article.paragraphs.map((paragraph, p) => (
            <p key={p}>
              {paragraph.map((sentence, s) => (
                <span key={s} className="sentence">
                  {smart(sentence.text)}
                  {footnotes(sentence.cite).map(({ site, passages }, i) => (
                    <span key={site.n}>
                      {i > 0 && <span className="cite-sep">,</span>}
                      <Cite n={site.n} site={site.name} kind={kindOf(site)} passages={passages} />
                    </span>
                  ))}
                  {s < paragraph.length - 1 ? " " : ""}
                </span>
              ))}
            </p>
          ))}
        </div>
      </article>

      <div className="notes">
        <Share url={url} title={article.headline} />
        {article.timeline?.length ? <Timeline steps={article.timeline} sources={sites.length} /> : null}
        <Sources sites={sites} />

        <section aria-labelledby="corrections">
          <h2 id="corrections">Corrections</h2>
          {article.corrections?.length ? (
            article.corrections.map((c) => (
              <p key={c.at} className="fine"><strong>{formatDate(c.at)}:</strong> {smart(c.text)}</p>
            ))
          ) : (
            <p className="fine">None so far. If you find an error, tell us and we will correct it here.</p>
          )}
        </section>
      </div>
    </>
  );
}
