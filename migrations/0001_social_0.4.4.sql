-- PersonAIs Alpha 0.4.4 — friends, discussions and twin deliberation.
--
-- HOW TO RUN IT
--   Supabase dashboard → your project → SQL Editor → New query → paste this
--   whole file → Run. It takes a second and prints a table of what it made.
--
-- It is safe to run more than once: every statement is "if not exists", and
-- nothing here drops, alters or reads existing data. The only change to a
-- table you already have is three new nullable columns on `profiles`.
--
-- Afterwards, GET /api/health should report "social_schema": "ready".

-- 1. Profiles gain a public identity for the twin network.
--    display_name  what other people see
--    friend_code   how they find you (email is deliberately not searchable)
--    twin_brief    standing instructions your twin carries into every room
alter table public.profiles add column if not exists display_name text;
alter table public.profiles add column if not exists friend_code  text;
alter table public.profiles add column if not exists twin_brief   text;

-- Two people must never end up with the same code. Rows with no code yet are
-- unaffected: Postgres does not treat nulls as duplicates of each other.
create unique index if not exists profiles_friend_code_key
  on public.profiles (friend_code);

-- 2. Friendships. One row per pair, in whichever direction it was asked.
create table if not exists public.friendships (
  id           uuid primary key default gen_random_uuid(),
  requester_id uuid not null references public.profiles(id) on delete cascade,
  addressee_id uuid not null references public.profiles(id) on delete cascade,
  status       text not null default 'pending'
               check (status in ('pending', 'accepted', 'declined')),
  created_at   timestamptz not null default now(),
  responded_at timestamptz,
  constraint friendships_not_self check (requester_id <> addressee_id),
  constraint friendships_pair_key unique (requester_id, addressee_id)
);

create index if not exists friendships_requester_idx
  on public.friendships (requester_id);
create index if not exists friendships_addressee_idx
  on public.friendships (addressee_id);

-- 3. A discussion: one topic, two people, and a running proposal to vote on.
--    status  open          somebody may run a round
--            deliberating  a proposal is on the table, waiting on verdicts
--            resolved      both agreed; the twins have stopped
--            exhausted     the credits ran out before anybody agreed
create table if not exists public.conversations (
  id               uuid primary key default gen_random_uuid(),
  topic            text not null,
  created_by       uuid not null references public.profiles(id) on delete cascade,
  mode             text not null default 'manual'
                   check (mode in ('manual', 'auto')),
  status           text not null default 'open'
                   check (status in ('open', 'deliberating', 'resolved', 'exhausted')),
  round            integer not null default 0,
  current_proposal text,
  stop_reason      text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

-- 4. Who is in a discussion, and how they voted on the current proposal.
--    A new proposal clears these: every round is voted on for itself.
create table if not exists public.conversation_members (
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  user_id         uuid not null references public.profiles(id) on delete cascade,
  verdict         text not null default 'pending'
                  check (verdict in ('pending', 'agree', 'disagree')),
  verdict_round   integer,
  verdict_note    text,
  primary key (conversation_id, user_id)
);

create index if not exists conversation_members_user_idx
  on public.conversation_members (user_id);

-- 5. The transcript. Typed messages and twin turns live in the same table —
--    that is what makes typing and sending your twin one conversation rather
--    than two.
--      author  human   somebody typed it
--              twin    a digital twin said it, on its owner's behalf
--              system  the service explaining itself (e.g. credits ran out)
create table if not exists public.conversation_messages (
  id              uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  user_id         uuid references public.profiles(id) on delete set null,
  author          text not null check (author in ('human', 'twin', 'system')),
  round           integer not null default 0,
  content         text not null,
  created_at      timestamptz not null default now()
);

create index if not exists conversation_messages_thread_idx
  on public.conversation_messages (conversation_id, created_at);

-- 6. Show what is now in place, so the SQL Editor prints proof it worked.
--    Expect four rows, and three columns listed against profiles.
select table_name,
       (select count(*) from information_schema.columns c
         where c.table_schema = 'public' and c.table_name = t.table_name) as columns
  from information_schema.tables t
 where table_schema = 'public'
   and table_name in ('friendships', 'conversations',
                      'conversation_members', 'conversation_messages')
 order by table_name;
