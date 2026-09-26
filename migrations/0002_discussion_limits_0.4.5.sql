-- PersonAIs Alpha 0.4.5 — a daily limit on twin discussions.
--
-- HOW TO RUN IT
--   Run migrations/0001_social_0.4.4.sql first if this project has never had
--   it: the table below points at its conversations. Then: Supabase dashboard →
--   your project → SQL Editor → New query → paste this whole file → Run. It
--   prints one row naming the table it made.
--
-- It is safe to run more than once: every statement is "if not exists", and
-- nothing here drops, alters or reads existing data. No existing table changes.
--
-- Afterwards, GET /api/health should report "limits_schema": "ready". Until
-- then twin rounds answer 503 — an allowance that cannot be counted is not
-- enforced by guessing — while friends, transcripts and typing keep working.

-- 1. One row per credit a twin turn spent in a discussion.
--    Today's usage is the sum of a person's rows since midnight UTC. Nothing is
--    ever reset or deleted at midnight; yesterday's rows simply stop counting.
create table if not exists public.discussion_usage (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references public.profiles(id) on delete cascade,
  -- Set null rather than cascade: deleting a discussion must not hand back
  -- the credits that were spent in it today.
  conversation_id uuid references public.conversations(id) on delete set null,
  round           integer,
  credits         integer not null default 1 check (credits > 0),
  created_at      timestamptz not null default now()
);

-- Every read is "this person's rows since midnight", so that is the index.
create index if not exists discussion_usage_user_day_idx
  on public.discussion_usage (user_id, created_at);

-- 2. Show what is now in place, so the SQL Editor prints proof it worked.
--    Expect one row: discussion_usage, with 6 columns.
select table_name,
       (select count(*) from information_schema.columns c
         where c.table_schema = 'public' and c.table_name = t.table_name) as columns
  from information_schema.tables t
 where table_schema = 'public'
   and table_name = 'discussion_usage';
