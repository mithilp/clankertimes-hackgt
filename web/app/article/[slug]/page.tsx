import Link from "next/link";
import { notFound } from "next/navigation";
import Cite from "@/components/Cite";
import Sample from "@/components/Sample";
import Share from "@/components/Share";
import Timeline from "@/components/Timeline";
import { AUTHOR, OG_BASE, SITE_NAME, absolute } from "@/lib/site";
import { BEAT_NAMES, SECTIONS, formatDate, getArticle, readingMinutes, sourceOrder } from "@/lib/articles";
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

  const order = sourceOrder(article);
  const number = (id: string) => order.indexOf(id) + 1;
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
    citation: order.map((id) => article.sources[id].url),
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
              {formatDate(article.published_at, true)} · {readingMinutes(article)} min read · {order.length} sources
              {article.corrections?.length ? ` · Corrected` : ""}
            </span>
          </div>
          <Share url={url} title={article.headline} />
        </header>

        <div className="body">
          {article.paragraphs.map((paragraph, p) => (
            <p key={p}>
              {paragraph.map((sentence, s) => (
                <span key={s} className="sentence">
                  {smart(sentence.text)}
                  {sentence.cite.filter((id) => article.sources[id]).map((id, i) => (
                    <span key={id}>
                      {i > 0 && <span className="cite-sep">,</span>}
                      <Cite n={number(id)} id={id} source={article.sources[id]} />
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
        {article.timeline?.length ? <Timeline steps={article.timeline} /> : null}
        <section aria-labelledby="sources">
          <h2 id="sources">Sources</h2>
          <ol className="sources">
            {order.map((id, i) => {
              const s = article.sources[id];
              return (
                <li key={id} id={`source-${id}`}>
                  <span className="n">{i + 1}</span>
                  <div>
                    <span className="t">{s.title}</span>
                    <div className="kind">{[s.publisher, s.kind, s.accessed && `retrieved ${formatDate(s.accessed)}`].filter(Boolean).join(" · ")}</div>
                    {s.quote && <q>{smart(s.quote)}</q>}
                    <a href={s.url} target="_blank" rel="noopener noreferrer">{s.url}</a>
                  </div>
                </li>
              );
            })}
          </ol>
        </section>

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
