# PersonAIs — Backend Engine (Alpha 0.4.3)

FastAPI service behind the PersonAIs digital-twin alpha. It exposes two
endpoints: `POST /api/chat` (the twin) and `GET /api/health` (a full
configuration read-out you can open in a browser).

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

1. **`GET /api/health` — `"version"` must read `0.4.2`.** If it still says
   `0.4.1`, the new code is not live and nothing below means anything.
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

## Known alpha limitations

- `routes/billing.py` and `routes/credits.py` are **not registered** on the app
  (no `include_router` call) and read `profiles.compute_credits`, while the
  live chat path writes `profiles.credits_balance`. Only one of those column
  names matches the real table. Settle that before wiring payments up.
- There is no pro-tier model. Every paying tier is served by the same model as
  the free tier. Whether this key can serve `claude-fable-5` is still unknown —
  the boot check that would answer it was itself blocked by the workspace 400.
  Once `/api/health` lists a populated `served_models`, that list settles it.
