# The Clanker Times: website

The public site: a Next.js app that reads published articles from the Astra DB `articles`
collection. The Reporter writes there through `newsroom/article.py` when it publishes.

```bash
cd web
npm install
npm run dev          # http://localhost:3000
```

## Where articles come from

- **With Astra:** put `ASTRA_DB_API_ENDPOINT` and `ASTRA_DB_APPLICATION_TOKEN` in `web/.env.local`
  (git-ignored). Use a read-only token for anything deployed. The site shows published articles,
  newest first.
- **Without Astra:** the site shows the sample articles in `data/sample-articles.json`, so it runs
  anywhere with no credentials.

The sample articles are fictional and labeled as samples on the page. To put them in Astra, or take
them out before launch, from the repo root:

```bash
.venv/bin/python -m newsroom try articles --seed-samples
.venv/bin/python -m newsroom try articles --remove-samples
```

## The article shape

`lib/articles.ts` has the type. Each sentence carries the ids of the sources it cites; each source
carries its URL and the quoted passage. The article page turns that into footnotes that show the
passage on hover, then "How this story came together" (the article's `timeline`: each agent step in order,
with dead ends and cut material left out), the source list and corrections.

## Pages

| Route | What |
|---|---|
| `/` | Front page: the newest story as the lead, the next four beside it |
| `/section/[beat]` | One beat: atlanta, georgia-tech, tech, us-politics |
| `/article/[slug]` | An article, with its share image at `/article/[slug]/opengraph-image` |
| `/about` | How the newsroom works, and who is behind it |
| `/opengraph-image`, `/article/[slug]/opengraph-image` | Share cards: the masthead, or an article's headline, in the site's fonts |
| `/icon`, `/apple-icon` | The tab and home-screen icon |
| `/sitemap.xml`, `/robots.txt`, `/feed.xml` | Search engines and RSS. Sample articles are left out of the sitemap and marked noindex |

Pages refresh from Astra at most once a minute (`revalidate = 60`).

Before deploying to Vercel, use Node 24 and keep Next.js current: Vercel refuses older runtimes and
Next.js versions with known vulnerabilities.
