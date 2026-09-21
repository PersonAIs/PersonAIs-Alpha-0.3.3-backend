# PersonAIs — Backend Engine (Alpha 0.4.4)

FastAPI service behind the PersonAIs digital-twin alpha. It serves one-to-one
chat with your own twin (`POST /api/chat`), a full configuration read-out you
can open in a browser (`GET /api/health`), and — new in 0.4.4 — friends and
twin-to-twin discussions under `/api/social`.

## What's new in 0.4.4 — friends, and twins that argue for you

You can add a friend and open a discussion with them. Inside a discussion there
are two ways to talk, and they share one transcript:

- **Type it yourself.** An ordinary message. No model call, no credit.
- **Send your twin.** Each side's twin takes a turn, working from the topic,
  the transcript, and whatever its owner has typed — which it reads as
  instructions, not as suggestions.

One turn from each twin is a **round**. The twin that closes the round ends on
a line beginning `PROPOSAL:`, and that line is what the two humans vote on.

- **Both agree** → the discussion is `resolved` and the twins stop.
- **Either disagrees** → the objection is handed to that person's twin as its
  brief, both verdicts are cleared, and the twins go again. They keep going,
  round after round, **until the credits run out** (`exhausted`) or somebody
  agrees.

### What a round costs

A round is one turn from each twin, and each turn costs **one credit from its
own owner**. So a round needs both participants to have at least one credit —
the poorer of the two sets how long the argument can last. Nobody is ever
charged for a turn that failed: the credit is deducted only after the model has
answered.

Two limits sit above that, both settable from the dashboard:

- `DELIBERATION_ROUNDS_PER_REQUEST` (default 1) — how many rounds one HTTP call
  may run. The browser calls again for the next round, which is what lets each
  round appear as it lands, and lets the humans stop it.
- `DELIBERATION_ROUND_CAP` (default 50) — a runaway guard for a pair of twins
  that will never converge and a balance large enough to prove it. Credits are
  the intended stop; this is the backstop.

### Alpha 0.4.4 schema

**This migration has to be run before friends will work.** Supabase → SQL
Editor → paste → run. `/api/health` reports `social_schema` so you can check it
took.

```sql
-- PersonAIs Alpha 0.4.4 — friends, discussions and twin deliberation.
alter table public.profiles add column if not exists display_name text;
alter table public.profiles add column if not exists friend_code  text;
alter table public.profiles add column if not exists twin_brief   text;
create unique index if not exists profiles_friend_code_key
  on public.profiles (friend_code);

create table if not exists public.friendships (
  id           uuid primary key default gen_random_uuid(),
  requester_id uuid not null references public.profiles(id) on delete cascade,
  addressee_id uuid not null references public.profiles(id) on delete cascade,
  status       text not null default 'pending'
               check (status in ('pending', 'accepted', 'declined')),
  created_at   timestamptz not null default now(),
  responded_at timestamptz,
  check (requester_id <> addressee_id),
  unique (requester_id, addressee_id)
);

create table if not exists public.conversations (
  id               uuid primary key default gen_random_uuid(),
  topic            text not null,
  created_by       uuid not null references public.profiles(id) on delete cascade,
  mode             text not null default 'manual' check (mode in ('manual', 'auto')),
  status           text not null default 'open'
                   check (status in ('open', 'deliberating', 'resolved', 'exhausted')),
  round            integer not null default 0,
  current_proposal text,
  stop_reason      text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create table if not exists public.conversation_members (
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  user_id         uuid not null references public.profiles(id) on delete cascade,
  verdict         text not null default 'pending'
                  check (verdict in ('pending', 'agree', 'disagree')),
  verdict_round   integer,
  verdict_note    text,
  primary key (conversation_id, user_id)
);

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
```

Row-level security is not part of this migration. Nothing in the browser
touches these tables directly — every read and write goes through this service
with the service key — so if you turn RLS on, the service key still reaches
them and the anon key still cannot.

### Who the caller is

`/api/chat` has always trusted the `user_id` in its request body. That is
survivable when the only thing an id buys you is your own credit balance; it is
not survivable for a private discussion between two people. So the social
endpoints resolve the caller from their **Supabase access token** (an
`Authorization: Bearer …` header, verified against Supabase on every request)
and ignore any `user_id` in the body.

`SOCIAL_REQUIRE_AUTH=off` falls back to the body id, for local development
against a project with no auth set up. It makes every discussion readable by
anyone who can guess a uuid — do not set it in production.

### Endpoints

| Endpoint | Does |
| -------- | ---- |
| `GET  /api/social/me` | Your card, allocating a friend code on first use. |
| `POST /api/social/me` | Set your display name and your twin's standing instructions. |
| `POST /api/social/friends/search` | Find somebody by friend code or display name. |
| `POST /api/social/friends/request` | Ask to be friends (by code or id). |
| `POST /api/social/friends/respond` | Accept or decline a request sent to you. |
| `GET  /api/social/friends` | Friends, plus what is pending in each direction. |
| `POST /api/social/conversations` | Open a discussion with an accepted friend. |
| `GET  /api/social/conversations` | Your discussions, newest first. |
| `GET  /api/social/conversations/{id}` | One discussion: state and full transcript. |
| `POST /api/social/conversations/{id}/mode` | Switch the default between typing and twins. |
| `POST /api/social/conversations/{id}/messages` | Type a message yourself. Free. |
| `POST /api/social/conversations/{id}/deliberate` | Run a round of twin discussion. |
| `POST /api/social/conversations/{id}/verdict` | Agree or disagree with the proposal. |

Searching by email address is deliberately not possible. With email
confirmation switched off for this release, a hit would be a free confirmation
that an address has an account behind it — so you are found by a short friend
code (`PA-` and six unambiguous characters) or by the display name you chose.

`deliberate` takes an `expected_round`. Both participants can be watching the
same discussion, and without it two browsers asking for "the next round" would
each run one and charge for both. The second one gets a `409` telling it to
reload instead.

## What was actually broken (found by the 0.4.2 boot self test)

The 0.4.2 self test sent one real message at boot and got this back:

```
HTTP 400 calling claude-haiku-4-5-20251001: This API key is not scoped to a
workspace, so this request must include the anthropic-workspace-id header with
the ID of the workspace to use. Add the header, or use an API key that is
scoped to a workspace.
```

**`ANTHROPIC_API_KEY` is an organisation-scoped key.** A key like that has to
name a workspace on every request. Nothing was naming one, so the API rejected
every call with a 400 — which the engine mapped to "The AI engine is not set up
correctly". This hit **every tier, free and pro alike**, and it also blocked
`GET /v1/models`, which is why `served_models` came back empty and
`model_status` read `unverified`.

The model id was never the problem. `claude-haiku-4-5-20251001` is correct.

### Fixing it — pick one

**Either** set `ANTHROPIC_WORKSPACE_ID`:

1. Anthropic Console → **Settings → Workspaces**
2. Open the workspace the key belongs to; its id starts with `wrkspc_`
3. Render → Environment → add `ANTHROPIC_WORKSPACE_ID` = that id → save (Render
   restarts the service automatically)

**Or** replace the key with a workspace-scoped one:

1. Anthropic Console → **Settings → API keys → Create key**
2. Assign it to a **workspace**, not to the organisation
3. Render → Environment → replace `ANTHROPIC_API_KEY` → save

The second is the more durable fix — the scope travels with the key, so there
is no second variable to forget. Either way `/api/health` tells you within a
minute of the restart whether it took.

0.4.3 sends the workspace id as a **client-level header**, so every endpoint
carries it — including the `GET /v1/models` call the boot check uses. 0.4.2
sent it only on `messages.create`; 0.4.1 set a client header but the variable
was never populated, so nothing was sent at all.

## What was broken in 0.4.1, and what 0.4.2 changed

**Symptom.** Chat replied:

> Critical error: The AI engine is not set up correctly, so this message could
> not be answered. It has been logged for us to fix — retrying will not help.

**Cause.** 0.4.1 routed accounts by tier to two different models:

| Tier         | Model sent          | Result                                 |
| ------------ | ------------------- | -------------------------------------- |
| free / guest | `claude-haiku-4-5-20251001` | fine — a real, servable model id |
| pro / ultra  | `claude-fable-5`    | **404 from the API — not available on this key** |

The API answers a model it will not serve for your key with a `404
not_found_error`. The handler for that mapped it to the flat sentence above,
which names neither the model nor the tier — so it read like a key problem
even though `/api/health` was reporting `anthropic_key_status: ok`. Any
account whose `profiles.subscription_tier` is `pro` or `ultra` hit it on every
single message; the same account on `free` worked.

0.4.1 also sent `output_config={"effort": "medium"}` on that same pro path —
a second, independent way for the request to be rejected.

**Fix.** One model serves every tier, and it is the one that was already
working:

- `claude-fable-5` is gone. Do not add a second model id back until that model
  is actually available on this API key — the boot check below is what proves
  it.
- `output_config` is gone with it. Nothing sends an effort parameter now.
- The tier still chooses the persona (pro/ultra keep the "elite digital twin"
  system prompt) and still meters credits. It no longer chooses a model.
- The model id is settable from the environment (`ANTHROPIC_MODEL`), so a
  future change is a dashboard edit and a restart, not a code deploy.
- **At boot** the service asks the API which models this key can serve
  (`GET /v1/models`) and then **sends one real message**. Both verdicts, and
  the full list of servable ids, are on `/api/health`. A broken model id now
  shows up on the health page at deploy time instead of in a user's chat
  window.
- When a request is still rejected, the message names the model and quotes the
  provider's own explanation, e.g. `(claude-fable-5: model: claude-fable-5)`.
  Model ids are not secret; the key never appears in a response.
- `ANTHROPIC_WORKSPACE_ID` is now sent as the SDK's `workspace_id` request
  parameter. 0.4.1 sent it as an `anthropic-workspace-id` HTTP header, which
  the API does not read — so setting it had no effect at all. It is unset in
  production today and is not needed for a workspace-scoped key.

## Environment variables

| Variable                 | Required | Default                       | Notes |
| ------------------------ | -------- | ----------------------------- | ----- |
| `ANTHROPIC_API_KEY`      | yes      | —                             | Starts `sk-ant-`. Quotes and trailing newlines are stripped automatically. |
| `SUPABASE_URL`           | yes      | —                             | Project URL. |
| `SUPABASE_KEY`           | yes      | —                             | Service key used for the `profiles` table. |
| `ANTHROPIC_MODEL`        | no       | `claude-haiku-4-5-20251001`   | Overrides the model for every tier. Set this if the health page says the default is not served. |
| `CHAT_MAX_TOKENS`        | no       | `1024`                        | Raise if replies come back truncated. A non-numeric or negative value is ignored with a warning. |
| `ENGINE_SELFTEST`        | no       | `on`                          | `off` skips the one boot message (it costs a few tokens per restart). |
| `ANTHROPIC_WORKSPACE_ID` | **if the key is org-scoped** | unset     | Starts `wrkspc_`. Required unless `ANTHROPIC_API_KEY` is itself scoped to a workspace — without it an org-scoped key gets a 400 on **every** request. |
| `ANTHROPIC_AUTH_TOKEN`   | no       | unset                         | OAuth bearer alternative to the API key. |
| `SOCIAL_REQUIRE_AUTH`    | no       | `on`                          | `off` lets the social endpoints trust the `user_id` in the body. Local development only — see [Who the caller is](#who-the-caller-is). |
| `DELIBERATION_ROUND_CAP` | no       | `50`                          | Runaway guard on rounds in one discussion. Credits are the intended stop. |
| `DELIBERATION_ROUNDS_PER_REQUEST` | no | `1`                        | Rounds a single HTTP call may run. Raise it to trade live progress for fewer round trips. |
| `TWIN_MAX_TOKENS`        | no       | `512`                         | Budget for one twin turn. Twin turns are capped at ~110 words, so this is generous. |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Tests

No network and no real credentials needed — the Anthropic and Supabase clients
are both stubbed.

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 test_main.py
```

Exits non-zero on any failure, so it can be wired into CI as-is.

## Deploy (Render)

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Branch:** whichever branch the Render service is set to track. Confirm this
  — the live service has been running a branch commit, not `main`.

The boot sequence is: resolve the model (one short `GET /v1/models`), open the
port, then send the self-test message on a background thread. A slow or
unreachable API delays neither the port nor the platform's health check.

## After deploying: what to check

1. **`GET /api/health` — `"version"` must read `0.4.4`.** If it says anything
   older, the new code is not live and nothing below means anything.
2. **`"status": "ok"`.** It is only `ok` when the key is usable, the model id
   is one this key can serve, *and* the boot message got a real answer back.
3. **`"engine_selftest": "ok"`** with `engine_selftest_detail` reading
   `claude-haiku-4-5-20251001 answered`. This is the important one: the engine
   sent a real message through the same code path `/api/chat` uses and got a
   reply. Chat works.
4. **`"remedy"`** — `null` when everything is fine. When it is not null it
   names the exact variable to change and where to find the value. Read it
   before anything else on this list.
5. **`"model_status"`** — `served` is the good case. `corrected` means the id
   was auto-swapped for a sibling and `model_detail` says which. `not served`
   means chat will refuse; pick an id from `served_models` and set
   `ANTHROPIC_MODEL`. `unverified` only means the model list could not be
   read — harmless on its own if the self test passed.
6. **`"models"` must show the same id for `free` and `pro`.** If `pro` reads
   `claude-fable-5`, the old build is still live.
7. **Send one chat message as a pro/ultra account** — the case that was
   broken. The response body carries `model_used`, which should be the same id
   the health page shows.

8. **`"social_schema": "ready"`.** `missing` means the 0.4.4 migration above
   has not been run against this project — friends and discussions will answer
   `503` until it is, and `social_remedy` says so. `unknown` only means the
   check itself could not run (usually Supabase is unconfigured).

If the self test fails, `engine_selftest_detail` carries the HTTP status and
the provider's own sentence. The deploy log has the same line prefixed `🛑`.

## Reading a failure

| What you see                                              | What it means |
| --------------------------------------------------------- | ------------- |
| `HTTP 400 ... not scoped to a workspace` | Org-scoped key with no workspace named. Set `ANTHROPIC_WORKSPACE_ID`, or use a workspace-scoped key. |
| `HTTP 400 ... header must be a valid workspace ID` | `ANTHROPIC_WORKSPACE_ID` is set but wrong. Re-copy it from Console → Settings → Workspaces. |
| `engine_selftest_detail: HTTP 401 ... API key is invalid` | The key is wrong, rotated, or from another org. |
| `HTTP 404 ... model: <id>`                                | That model is not available on this key. Use an id from `served_models`. |
| `HTTP 400 ...`                                            | The request was rejected as malformed; the quoted text names the parameter. |
| `model_status: not served`                                | Chat refuses before making a call. Set `ANTHROPIC_MODEL`. |
| `anthropic_key_status` is not `ok`                        | The key is missing, blank, or not in `sk-ant-` form. |
| Chat says "…is not set up correctly (…: …)"               | Configuration; the parenthesis names the model and the reason. Retrying will not help. |
| Chat says "…try again in a moment"                        | Upstream 5xx, a rate limit, or a network blip. Retrying will help. |
| `social_schema: missing`, or a `503` naming "Alpha 0.4.4" | The migration has not been run on this project. Run it, then restart. |
| A discussion answers `409 … moved on`                     | The other participant's browser already ran that round. Reload. |
| The twins stop with `credits_exhausted`                   | Working as designed — a round costs both sides a credit. Top up to continue. |

## Known alpha limitations

- `routes/billing.py` and `routes/credits.py` are **not registered** on the app
  (no `include_router` call) and read `profiles.compute_credits`, while the
  live chat path writes `profiles.credits_balance`. Only one of those column
  names matches the real table. Settle that before wiring payments up.
- **A deliberation round is not transactional.** Two simultaneous requests are
  caught by `expected_round`, but the check and the charge are separate
  statements, so a genuinely concurrent pair could still slip past and charge
  two rounds. Credit balances are read-modify-write for the same reason, here
  and in `/api/chat`.
- **A twin discussion is two people only.** The schema would carry more, but
  the round structure (one turn each, alternating opener) assumes two.
- There is no pro-tier model. Every paying tier is served by the same model as
  the free tier. Whether this key can serve `claude-fable-5` is still unknown —
  the boot check that would answer it was itself blocked by the workspace 400.
  Once `/api/health` lists a populated `served_models`, that list settles it.
