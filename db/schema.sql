-- AI newsroom schema. See ARCHITECTURE.md §6.
-- Beat notes, source reliability, and editorial precedent live in mem0 (its own
-- pgvector table in this same database), not here.

create extension if not exists pgcrypto;   -- gen_random_uuid()
create extension if not exists vector;     -- pgvector: near-duplicate hypotheses

create type lead_status   as enum ('new', 'below_threshold', 'promoted', 'duplicate');
create type story_status  as enum ('assigned', 'reporting', 'in_review', 'approved', 'published', 'killed', 'dormant');
create type resolution    as enum ('confirmed', 'killed', 'unresolved');
create type plan_status   as enum ('pending', 'checked_no_change', 'checked_moved', 'unreachable');
create type reviewer_role as enum ('verifier', 'skeptic', 'fairness');
create type verdict       as enum ('approve', 'block');

-- Every URL any agent touched. Dedupe across scouts + archive of what we relied on.
create table sources (
  id            uuid primary key default gen_random_uuid(),
  url           text not null,
  url_hash      text not null unique,          -- sha256 of normalized url
  first_seen_by text not null,                 -- 'scout:civic', 'reporter:<worker>'
  first_seen_at timestamptz not null default now(),
  fetched_at    timestamptz,
  archive_path  text,                          -- extracted text on the archive volume; quoted spans are checked against it
  content_hash  text                           -- detect page changes (wake conditions)
);

-- A falsifiable hypothesis, not a topic.
create table leads (
  id               uuid primary key default gen_random_uuid(),
  scout            text not null,
  beat             text not null,
  hypothesis       text not null,
  why_now          text not null,
  why_now_at       timestamptz,                -- date of the triggering item (timeliness)
  who_would_know   text not null,
  would_settle_it  text not null,
  score            numeric(3,2) not null check (score between 0 and 1),
  -- {"gates": {...}, "impact": {"value": 0.8, "reason": "..."}, "settleability": {...},
  --  "novelty": {...}, "tip_strength": {...}, "timeliness": {...}}
  score_components jsonb not null,
  score_reason     text not null,
  fingerprint      text not null unique,       -- exact dedupe; killed ones never return
  embedding        vector(384),                -- fuzzy dedupe (local bge-small, 384 dims)
  status           lead_status not null default 'new',
  duplicate_of     uuid references leads(id),
  created_at       timestamptz not null default now()
);
create index on leads using hnsw (embedding vector_cosine_ops);
create index on leads (status, score desc);

create table lead_sources (
  lead_id   uuid references leads(id) on delete cascade,
  source_id uuid references sources(id),
  primary key (lead_id, source_id)
);

-- One story per lead (unique) = two reporters can't work the same hypothesis.
-- lease_* lets any number of reporter/reviewer processes share the work: a worker
-- claims a row with FOR UPDATE SKIP LOCKED and heartbeats the lease; if it dies,
-- the lease expires and another worker resumes from the rows below.
create table stories (
  id               uuid primary key default gen_random_uuid(),
  lead_id          uuid not null unique references leads(id),
  reporter         text,
  status           story_status not null default 'assigned',
  resolution       resolution,
  headline         text,
  body             text,
  kill_memo        text,                       -- checked / found / what would change our mind
  wake_condition   text,                       -- 'agenda posted for 10/3 commission mtg'
  wake_source_id   uuid references sources(id),-- page to re-hash
  wake_at          timestamptz,
  review_round     int not null default 0,
  timeline         text,                       -- written by the managing editor at publish
  lease_owner      text,
  lease_expires_at timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  published_at     timestamptz
);
create index on stories (status);
create index on stories (wake_at) where status = 'dormant';

-- Stopping rule (a): plan coverage.
create table evidence_plan (
  id          uuid primary key default gen_random_uuid(),
  story_id    uuid not null references stories(id) on delete cascade,
  rank        int not null,
  description text not null,                   -- 'bid tabulation for RFP 24-117'
  why         text not null,
  status      plan_status not null default 'pending',
  checked_by  bigint,                          -- tool_calls.id
  unique (story_id, rank)
);

-- The trail. Every tool call with the agent's stated reason.
create table tool_calls (
  id             bigserial primary key,
  agent          text not null,
  story_id       uuid references stories(id),
  lead_id        uuid references leads(id),
  tool           text not null,                -- 'web_search', 'fetch_url', 'browse', 'memory_search', ...
  reason         text not null,
  input          jsonb not null,
  result_summary text,
  source_id      uuid references sources(id),
  billable       boolean not null default true, -- research calls count toward budget and yield; bookkeeping doesn't
  started_at     timestamptz not null default now(),
  duration_ms    int
);
create index on tool_calls (story_id, id);

-- Stopping rule (b): marginal yield. New facts tied to the call that produced them.
create table facts (
  id           uuid primary key default gen_random_uuid(),
  story_id     uuid not null references stories(id) on delete cascade,
  tool_call_id bigint references tool_calls(id),
  fact         text not null,
  source_id    uuid references sources(id),
  bearing      text check (bearing in ('supports', 'contradicts', 'context')),
  created_at   timestamptz not null default now()
);

-- Every published sentence and where it came from.
create table claims (
  id          uuid primary key default gen_random_uuid(),
  story_id    uuid not null references stories(id) on delete cascade,
  position    int not null,
  paragraph   int not null default 0,
  text        text not null,
  source_id   uuid not null references sources(id),
  quoted_span text not null,
  verified    boolean,                         -- set by verifier reviewer
  unique (story_id, position)
);

-- Stopping rule (c): budget with appeal. Initial size comes from the lead's score.
create table budgets (
  story_id           uuid primary key references stories(id) on delete cascade,
  tool_calls_granted int not null,
  tool_calls_used    int not null default 0,
  wall_clock_granted interval not null,
  started_at         timestamptz not null default now()
);

create table budget_appeals (
  id               uuid primary key default gen_random_uuid(),
  story_id         uuid not null references stories(id),
  extra_calls      int not null,
  extra_time       interval,
  next_checks      text not null,              -- what specifically
  why_could_change text not null,              -- why it could move the outcome
  decision         text check (decision in ('granted', 'denied')),
  decision_reason  text,
  created_at       timestamptz not null default now(),
  decided_at       timestamptz
);

-- Review panel. Any block sends it back once; a second block kills it.
create table reviews (
  id         uuid primary key default gen_random_uuid(),
  story_id   uuid not null references stories(id) on delete cascade,
  round      int not null,
  role       reviewer_role not null,
  model      text not null,
  verdict    verdict not null,
  reason     text not null,
  created_at timestamptz not null default now(),
  unique (story_id, round, role)
);

-- Dashboard feed. Poll with: where id > $last_seen order by id.
create table agent_events (
  id         bigserial primary key,
  agent      text not null,
  action     text not null,                    -- 'lead_raised', 'handoff', 'appeal_denied', 'blocked', 'published'
  reason     text not null,
  story_id   uuid references stories(id),
  lead_id    uuid references leads(id),
  detail     jsonb,
  created_at timestamptz not null default now()
);

-- Every model call, for the global spend cap.
create table model_usage (
  id            bigserial primary key,
  agent         text not null,
  model         text not null,
  input_tokens  int not null,
  output_tokens int not null,
  cache_read    int not null default 0,
  cache_write   int not null default 0,
  cost_usd      numeric(10,5) not null,
  created_at    timestamptz not null default now()
);

-- Wake-ups. Workers LISTEN on these and also poll every few seconds as a fallback,
-- so a missed notification costs seconds, not a stuck pipeline.
--   new_lead       scouts -> managing editor
--   appeal         reporter -> managing editor
--   story_ready    managing editor/reviewers -> reporters (status 'assigned')
--   story_review   reporter -> reviewers (status 'in_review')
--   story_approved reviewers -> managing editor (status 'approved')
create function notify_new_lead() returns trigger language plpgsql as $$
begin
  perform pg_notify('new_lead', new.id::text);
  return new;
end $$;
create trigger lead_inserted after insert on leads
  for each row execute function notify_new_lead();

create function notify_appeal() returns trigger language plpgsql as $$
begin
  perform pg_notify('appeal', new.id::text);
  return new;
end $$;
create trigger appeal_inserted after insert on budget_appeals
  for each row execute function notify_appeal();

create function notify_story_status() returns trigger language plpgsql as $$
begin
  if tg_op = 'INSERT' or new.status is distinct from old.status then
    if new.status = 'assigned' then perform pg_notify('story_ready', new.id::text);
    elsif new.status = 'in_review' then perform pg_notify('story_review', new.id::text);
    elsif new.status = 'approved' then perform pg_notify('story_approved', new.id::text);
    end if;
  end if;
  new.updated_at := now();
  return new;
end $$;
create trigger story_status_changed before insert or update on stories
  for each row execute function notify_story_status();

-- Budget accounting happens automatically as tool calls are logged.
create function bump_budget() returns trigger language plpgsql as $$
begin
  if new.story_id is not null and new.billable then
    update budgets set tool_calls_used = tool_calls_used + 1 where story_id = new.story_id;
  end if;
  return new;
end $$;
create trigger tool_call_logged after insert on tool_calls
  for each row execute function bump_budget();

-- Dashboard counters.
create view counters as select
  (select count(*) from sources)                                  as sources_checked,
  (select count(*) from leads)                                    as leads_raised,
  (select count(*) from leads where status = 'promoted')          as leads_promoted,
  (select count(*) from stories where status = 'published')       as published,
  (select count(*) from stories where status = 'killed')          as killed,
  (select count(*) from stories where status = 'dormant')         as parked,
  (select count(*) from reviews where verdict = 'block')          as reviewer_blocks,
  (select count(*) from budget_appeals where decision = 'denied') as appeals_denied,
  (select coalesce(sum(cost_usd), 0) from model_usage)            as spend_usd;
