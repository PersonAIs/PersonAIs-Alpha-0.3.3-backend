"""Friends, shared discussions and twin-to-twin deliberation (Alpha 0.4.4).

Three things live here:

1.  **Friends.** You are found by a short friend code, never by email address —
    an alpha with no verification step should not let anybody confirm that an
    address is registered just by typing it into a search box.
2.  **Shared conversations.** Two friends, one topic, one transcript. Either of
    them can type into it directly (the traditional way) or hand the turn to
    their twin (the semi-automated way). Both kinds of message sit in the same
    transcript, and the twins read the typed ones as instructions.
3.  **Deliberation.** A round is one turn from each twin, costing its owner one
    credit. After each round the closing twin leaves a proposal, and both
    humans vote on it. Agreement ends the discussion; a disagreement sends the
    twins round again — and keeps sending them until the credits are gone.

The module has no globals of its own: the Supabase client, the model call and
the identity check are all handed in by `main.py`, which keeps this file
importable (and testable) without a network, and keeps a stubbed client in a
test visible to both the chat path and this one.
"""

import logging
import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import deliberation

logger = logging.getLogger("personais.social")

# The 0.4.4 tables are new, so a deploy that has not had the migration run
# against it is the single most likely failure. It is named rather than left as
# a 500, in the same spirit as the engine remedies on /api/health.
MIGRATION_REMEDY = (
    "The Alpha 0.4.4 social tables are missing from this Supabase project. Run "
    "migrations/0001_social_0.4.4.sql from the backend repo (Supabase dashboard → "
    "SQL Editor → New query → paste → Run), then try again."
)

# Ambiguous characters are left out: a friend code gets read aloud and typed in
# by somebody else, so 0/O and 1/I/L have no place in it.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
CODE_PREFIX = "PA-"

VERDICTS = ("agree", "disagree")


def new_friend_code():
    return CODE_PREFIX + "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def rows_of(result):
    """The rows from a supabase response, whatever it returned."""
    return getattr(result, "data", None) or []


def looks_like_missing_schema(error):
    """Is this the 'you have not run the migration' error?"""
    text = str(error).lower()
    return any(
        needle in text
        for needle in (
            "does not exist",
            "could not find the table",
            "could not find the 'verdict",
            "schema cache",
            "pgrst205",
            "pgrst204",
            "undefined_table",
            "undefined_column",
        )
    )


# --- request bodies ------------------------------------------------------
class Identified(BaseModel):
    # Only consulted when SOCIAL_REQUIRE_AUTH is off; with auth on, the id
    # comes from the bearer token and anything sent here is ignored.
    user_id: Optional[str] = None


class ProfileUpdate(Identified):
    display_name: Optional[str] = None
    twin_brief: Optional[str] = None


class FriendSearch(Identified):
    query: str


class FriendRequest(Identified):
    friend_code: Optional[str] = None
    friend_id: Optional[str] = None


class FriendResponse(Identified):
    request_id: str
    action: str  # accept | decline


class ConversationCreate(Identified):
    friend_id: str
    topic: str
    mode: str = "manual"


class ModeChange(Identified):
    mode: str


class MessagePost(Identified):
    content: str


class DeliberateRequest(Identified):
    rounds: Optional[int] = None
    # Optimistic concurrency: both browsers watching a conversation can ask for
    # a round, and without this the two requests would each run one and charge
    # for both. The loser is told to refresh instead.
    expected_round: Optional[int] = None


class VerdictRequest(Identified):
    verdict: str
    note: Optional[str] = None


def build_social_router(
    *,
    db,
    generate,
    identify,
    round_cap=50,
    rounds_per_request=2,
    twin_max_tokens=512,
):
    """Build the /api/social router.

    Args:
        db: callable returning the Supabase client. Called per query rather
            than captured, so a test that swaps the client out is seen here.
        generate: `generate(system=..., content=..., max_tokens=...) -> str`,
            already carrying the chat path's provider error mapping.
        identify: `identify(authorization_header, claimed_user_id) -> user id`,
            raising 401 when a token is required and missing or bad.
        round_cap: runaway guard on rounds in one conversation. Credits are the
            intended stop; this is the backstop for a pair of twins that will
            never converge and a balance large enough to prove it.
        rounds_per_request: rounds a single HTTP call may run. Deliberation
            continues by calling again, so a request stays short enough not to
            time out and the UI can show each round as it lands.
        twin_max_tokens: budget for one twin turn.
    """
    router = APIRouter(prefix="/social", tags=["social"])

    # --- storage helpers -------------------------------------------------
    def run(query, what):
        """Execute a Supabase query, turning its failures into HTTP errors."""
        try:
            return rows_of(query.execute())
        except HTTPException:
            raise
        except Exception as e:
            if looks_like_missing_schema(e):
                logger.error("Social schema missing while %s: %s", what, e)
                raise HTTPException(status_code=503, detail=MIGRATION_REMEDY)
            logger.exception("Database error while %s", what)
            raise HTTPException(
                status_code=500,
                detail=f"The database could not {what}. This has been logged.",
            )

    def profiles_by_id(ids):
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        rows = run(
            db().table("profiles")
            .select("id, display_name, friend_code, twin_brief, subscription_tier, credits_balance")
            .in_("id", ids),
            "read profiles",
        )
        return {row["id"]: row for row in rows}

    def ensure_profile(user_id):
        """The caller's profile row, created if signup never made one.

        /api/chat can treat a missing row as a free account with 20 credits
        because it only ever reads it. Friendships point at it, so here the row
        has to actually exist.
        """
        found = profiles_by_id([user_id]).get(user_id)
        if not found:
            created = run(
                db().table("profiles").insert(
                    {"id": user_id, "subscription_tier": "free", "credits_balance": 20}
                ),
                "create your profile",
            )
            found = created[0] if created else {
                "id": user_id, "subscription_tier": "free", "credits_balance": 20
            }

        if not found.get("friend_code"):
            found = assign_friend_code(found)
        return found

    def assign_friend_code(profile):
        """Give a profile a friend code, retrying past the odd collision."""
        for _ in range(5):
            code = new_friend_code()
            try:
                updated = rows_of(
                    db().table("profiles").update({"friend_code": code})
                    .eq("id", profile["id"]).execute()
                )
            except Exception as e:
                if looks_like_missing_schema(e):
                    raise HTTPException(status_code=503, detail=MIGRATION_REMEDY)
                # A unique-index collision on the code: try another one.
                logger.warning("Friend code %s rejected (%s) — retrying", code, type(e).__name__)
                continue
            profile = updated[0] if updated else {**profile, "friend_code": code}
            return profile
        raise HTTPException(
            status_code=500,
            detail="Could not allocate a friend code. Please try again.",
        )

    def public_profile(profile, include_private=False):
        """What a profile looks like to somebody else — or to its owner."""
        if not profile:
            return None
        card = {
            "id": profile.get("id"),
            "display_name": deliberation.display_name(profile, fallback="Unnamed twin"),
            "friend_code": profile.get("friend_code"),
        }
        if include_private:
            card["twin_brief"] = profile.get("twin_brief") or ""
            card["tier"] = profile.get("subscription_tier") or "free"
            card["credits"] = int(profile.get("credits_balance") or 0)
        return card

    def friendship_rows(user_id):
        """Every friendship this user is on either side of."""
        sent = run(
            db().table("friendships").select("*").eq("requester_id", user_id),
            "read your friend requests",
        )
        received = run(
            db().table("friendships").select("*").eq("addressee_id", user_id),
            "read your friend requests",
        )
        return sent + received

    def friendship_between(a, b):
        for row in friendship_rows(a):
            other = row["addressee_id"] if row["requester_id"] == a else row["requester_id"]
            if other == b:
                return row
        return None

    def require_friend(user_id, friend_id):
        link = friendship_between(user_id, friend_id)
        if not link or link.get("status") != "accepted":
            raise HTTPException(
                status_code=403,
                detail="You can only start a discussion with an accepted friend.",
            )
        return link

    def load_conversation(conversation_id, user_id):
        """A conversation, its members and its transcript — or 403/404.

        Membership is checked here and nowhere else, so no endpoint can forget
        to do it.
        """
        conv_rows = run(
            db().table("conversations").select("*").eq("id", conversation_id),
            "read that discussion",
        )
        if not conv_rows:
            raise HTTPException(status_code=404, detail="That discussion does not exist.")
        conversation = conv_rows[0]

        members = run(
            db().table("conversation_members").select("*").eq("conversation_id", conversation_id),
            "read the members of that discussion",
        )
        if not any(m.get("user_id") == user_id for m in members):
            raise HTTPException(
                status_code=403, detail="That discussion is not one of yours."
            )

        messages = run(
            db().table("conversation_messages").select("*")
            .eq("conversation_id", conversation_id).order("created_at"),
            "read that transcript",
        )
        profiles = profiles_by_id([m.get("user_id") for m in members])
        return conversation, members, messages, profiles

    def add_message(conversation_id, user_id, author, content, round_number):
        row = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "author": author,
            "round": round_number,
            "content": content,
        }
        written = run(
            db().table("conversation_messages").insert(row), "save that message"
        )
        return written[0] if written else {**row, "created_at": now_iso()}

    def touch_conversation(conversation_id, fields):
        fields = {**fields, "updated_at": now_iso()}
        updated = run(
            db().table("conversations").update(fields).eq("id", conversation_id),
            "update that discussion",
        )
        return updated[0] if updated else fields

    def set_verdict(conversation_id, user_id, fields):
        run(
            db().table("conversation_members").update(fields)
            .eq("conversation_id", conversation_id).eq("user_id", user_id),
            "record your verdict",
        )

    def spend_credit(profile):
        """Charge one credit for one twin turn, and return the new balance."""
        balance = int(profile.get("credits_balance") or 0) - 1
        run(
            db().table("profiles").update({"credits_balance": balance}).eq("id", profile["id"]),
            "update your credit balance",
        )
        profile["credits_balance"] = balance
        return balance

    # --- view models -----------------------------------------------------
    def message_view(msg, profiles):
        owner = profiles.get(msg.get("user_id"))
        return {
            "id": msg.get("id"),
            "author": msg.get("author"),
            "user_id": msg.get("user_id"),
            "speaker": deliberation.display_name(owner, fallback="PersonAIs"),
            "round": msg.get("round", 0),
            "content": msg.get("content"),
            "created_at": msg.get("created_at"),
        }

    def conversation_view(conversation, members, profiles, user_id):
        me = next((m for m in members if m.get("user_id") == user_id), {})
        partner_member = next((m for m in members if m.get("user_id") != user_id), {})
        partner = profiles.get(partner_member.get("user_id"))
        mine = profiles.get(user_id, {})
        round_number = int(conversation.get("round") or 0)
        balances = [
            int(profiles.get(m.get("user_id"), {}).get("credits_balance") or 0)
            for m in members
        ]
        return {
            "id": conversation.get("id"),
            "topic": conversation.get("topic"),
            "mode": conversation.get("mode") or "manual",
            "status": conversation.get("status") or "open",
            "round": round_number,
            "round_cap": round_cap,
            "proposal": conversation.get("current_proposal"),
            "stop_reason": conversation.get("stop_reason"),
            "stop_detail": deliberation.stop_message(conversation.get("stop_reason")),
            "updated_at": conversation.get("updated_at"),
            "partner": public_profile(partner),
            "my_verdict": me.get("verdict") or "pending",
            "partner_verdict": partner_member.get("verdict") or "pending",
            "my_credits": int(mine.get("credits_balance") or 0),
            "partner_credits": int(
                profiles.get(partner_member.get("user_id"), {}).get("credits_balance") or 0
            ),
            "can_deliberate": deliberation.can_continue(
                balances, round_cap - round_number, conversation.get("status") or "open"
            ),
        }

    # --- profile ---------------------------------------------------------
    @router.get("/me")
    async def read_me(user_id: Optional[str] = None,
                      authorization: Optional[str] = Header(None)):
        uid = identify(authorization, user_id)
        return {"profile": public_profile(ensure_profile(uid), include_private=True)}

    @router.post("/me")
    async def update_me(req: ProfileUpdate,
                        authorization: Optional[str] = Header(None)):
        uid = identify(authorization, req.user_id)
        profile = ensure_profile(uid)

        fields = {}
        if req.display_name is not None:
            name = req.display_name.strip()[:60]
            if not name:
                raise HTTPException(status_code=400, detail="A display name cannot be blank.")
            fields["display_name"] = name
        if req.twin_brief is not None:
            fields["twin_brief"] = req.twin_brief.strip()[:600]

        if fields:
            updated = run(
                db().table("profiles").update(fields).eq("id", uid), "save your profile"
            )
            profile = updated[0] if updated else {**profile, **fields}
        return {"profile": public_profile(profile, include_private=True)}

    # --- friends ---------------------------------------------------------
    @router.post("/friends/search")
    async def search_friends(req: FriendSearch,
                             authorization: Optional[str] = Header(None)):
        """Find somebody by friend code, or by the name they chose.

        Email addresses are deliberately not searchable: with confirmation
        turned off for this release, a hit would be a free confirmation that an
        address has an account behind it.
        """
        uid = identify(authorization, req.user_id)
        ensure_profile(uid)

        query = (req.query or "").strip()
        if len(query) < 2:
            raise HTTPException(
                status_code=400, detail="Type at least two characters, or a full friend code."
            )

        found = []
        code = query.upper()
        if not code.startswith(CODE_PREFIX):
            code = CODE_PREFIX + code
        found += run(
            db().table("profiles")
            .select("id, display_name, friend_code, subscription_tier, credits_balance")
            .eq("friend_code", code).limit(5),
            "search for that friend code",
        )
        found += run(
            db().table("profiles")
            .select("id, display_name, friend_code, subscription_tier, credits_balance")
            .ilike("display_name", f"{query}%").limit(10),
            "search for that name",
        )

        links = {}
        for row in friendship_rows(uid):
            other = row["addressee_id"] if row["requester_id"] == uid else row["requester_id"]
            links[other] = row

        results, seen = [], set()
        for row in found:
            rid = row.get("id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            link = links.get(rid)
            if rid == uid:
                relationship = "self"
            elif not link or link.get("status") == "declined":
                relationship = "none"
            elif link.get("status") == "accepted":
                relationship = "friend"
            elif link.get("requester_id") == uid:
                relationship = "requested"
            else:
                relationship = "awaiting_you"
            results.append({**public_profile(row), "relationship": relationship})

        return {"results": results}

    @router.post("/friends/request")
    async def request_friend(req: FriendRequest,
                             authorization: Optional[str] = Header(None)):
        uid = identify(authorization, req.user_id)
        ensure_profile(uid)

        target = None
        if req.friend_id:
            target = profiles_by_id([req.friend_id]).get(req.friend_id)
        elif req.friend_code:
            code = req.friend_code.strip().upper()
            if not code.startswith(CODE_PREFIX):
                code = CODE_PREFIX + code
            matches = run(
                db().table("profiles").select("*").eq("friend_code", code).limit(1),
                "look up that friend code",
            )
            target = matches[0] if matches else None

        if not target:
            raise HTTPException(
                status_code=404, detail="No twin with that friend code. Check the code and try again."
            )
        if target["id"] == uid:
            raise HTTPException(status_code=400, detail="That is your own friend code.")

        existing = friendship_between(uid, target["id"])
        if existing:
            status = existing.get("status")
            if status == "accepted":
                return {"status": "already_friends", "friend": public_profile(target)}
            if status == "pending":
                if existing.get("requester_id") == uid:
                    return {"status": "already_requested", "friend": public_profile(target)}
                # They asked first. Answering with a request of your own is an
                # acceptance by any reasonable reading, so treat it as one.
                run(
                    db().table("friendships")
                    .update({"status": "accepted", "responded_at": now_iso()})
                    .eq("id", existing["id"]),
                    "accept that friend request",
                )
                return {"status": "accepted", "friend": public_profile(target)}
            # Previously declined, by either side: let it be asked again.
            run(
                db().table("friendships").update({
                    "requester_id": uid,
                    "addressee_id": target["id"],
                    "status": "pending",
                    "responded_at": None,
                }).eq("id", existing["id"]),
                "send that friend request",
            )
            return {"status": "pending", "friend": public_profile(target)}

        created = run(
            db().table("friendships").insert({
                "requester_id": uid,
                "addressee_id": target["id"],
                "status": "pending",
            }),
            "send that friend request",
        )
        return {
            "status": "pending",
            "request_id": created[0].get("id") if created else None,
            "friend": public_profile(target),
        }

    @router.post("/friends/respond")
    async def respond_to_friend(req: FriendResponse,
                                authorization: Optional[str] = Header(None)):
        uid = identify(authorization, req.user_id)
        if req.action not in ("accept", "decline"):
            raise HTTPException(status_code=400, detail="Answer with 'accept' or 'decline'.")

        rows = run(
            db().table("friendships").select("*").eq("id", req.request_id),
            "read that friend request",
        )
        if not rows:
            raise HTTPException(status_code=404, detail="That friend request no longer exists.")
        link = rows[0]

        # Only the person who was asked can answer, and only once.
        if link.get("addressee_id") != uid:
            raise HTTPException(status_code=403, detail="That request was not sent to you.")
        if link.get("status") != "pending":
            return {"status": link.get("status")}

        status = "accepted" if req.action == "accept" else "declined"
        run(
            db().table("friendships").update({"status": status, "responded_at": now_iso()})
            .eq("id", req.request_id),
            "answer that friend request",
        )
        return {"status": status}

    @router.get("/friends")
    async def list_friends(user_id: Optional[str] = None,
                           authorization: Optional[str] = Header(None)):
        uid = identify(authorization, user_id)
        me = ensure_profile(uid)

        links = friendship_rows(uid)
        others = [
            row["addressee_id"] if row["requester_id"] == uid else row["requester_id"]
            for row in links
        ]
        profiles = profiles_by_id(others)

        friends, incoming, outgoing = [], [], []
        for row in links:
            other_id = row["addressee_id"] if row["requester_id"] == uid else row["requester_id"]
            card = public_profile(profiles.get(other_id)) or {"id": other_id}
            if row.get("status") == "accepted":
                friends.append({**card, "since": row.get("responded_at")})
            elif row.get("status") == "pending":
                entry = {**card, "request_id": row.get("id"), "sent_at": row.get("created_at")}
                (incoming if row.get("addressee_id") == uid else outgoing).append(entry)

        friends.sort(key=lambda f: (f.get("display_name") or "").lower())
        return {
            "me": public_profile(me, include_private=True),
            "friends": friends,
            "incoming": incoming,
            "outgoing": outgoing,
        }

    # --- conversations ---------------------------------------------------
    @router.post("/conversations")
    async def create_conversation(req: ConversationCreate,
                                  authorization: Optional[str] = Header(None)):
        uid = identify(authorization, req.user_id)
        ensure_profile(uid)
        require_friend(uid, req.friend_id)

        topic = (req.topic or "").strip()
        if len(topic) < 3:
            raise HTTPException(
                status_code=400, detail="Give the discussion a topic — the twins plan around it."
            )
        mode = req.mode if req.mode in ("manual", "auto") else "manual"

        created = run(
            db().table("conversations").insert({
                "topic": topic[:200],
                "created_by": uid,
                "mode": mode,
                "status": "open",
                "round": 0,
            }),
            "start that discussion",
        )
        if not created:
            raise HTTPException(status_code=500, detail="The discussion could not be created.")
        conversation = created[0]

        run(
            db().table("conversation_members").insert([
                {"conversation_id": conversation["id"], "user_id": uid, "verdict": "pending"},
                {"conversation_id": conversation["id"], "user_id": req.friend_id, "verdict": "pending"},
            ]),
            "add both of you to that discussion",
        )

        conv, members, _messages, profiles = load_conversation(conversation["id"], uid)
        return {"conversation": conversation_view(conv, members, profiles, uid)}

    @router.get("/conversations")
    async def list_conversations(user_id: Optional[str] = None,
                                 authorization: Optional[str] = Header(None)):
        uid = identify(authorization, user_id)
        ensure_profile(uid)

        mine = run(
            db().table("conversation_members").select("*").eq("user_id", uid),
            "read your discussions",
        )
        ids = [row["conversation_id"] for row in mine]
        if not ids:
            return {"conversations": []}

        conversations = run(
            db().table("conversations").select("*").in_("id", ids),
            "read your discussions",
        )
        all_members = run(
            db().table("conversation_members").select("*").in_("conversation_id", ids),
            "read your discussions",
        )
        profiles = profiles_by_id([m.get("user_id") for m in all_members])

        views = []
        for conversation in conversations:
            members = [m for m in all_members if m.get("conversation_id") == conversation.get("id")]
            views.append(conversation_view(conversation, members, profiles, uid))
        views.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
        return {"conversations": views}

    @router.get("/conversations/{conversation_id}")
    async def read_conversation(conversation_id: str,
                                user_id: Optional[str] = None,
                                authorization: Optional[str] = Header(None)):
        uid = identify(authorization, user_id)
        conversation, members, messages, profiles = load_conversation(conversation_id, uid)
        return {
            "conversation": conversation_view(conversation, members, profiles, uid),
            "messages": [message_view(m, profiles) for m in messages],
        }

    @router.post("/conversations/{conversation_id}/mode")
    async def set_mode(conversation_id: str, req: ModeChange,
                       authorization: Optional[str] = Header(None)):
        uid = identify(authorization, req.user_id)
        conversation, members, _messages, profiles = load_conversation(conversation_id, uid)
        if req.mode not in ("manual", "auto"):
            raise HTTPException(status_code=400, detail="Mode is 'manual' or 'auto'.")
        updated = touch_conversation(conversation_id, {"mode": req.mode})
        return {
            "conversation": conversation_view(
                {**conversation, **updated}, members, profiles, uid
            )
        }

    @router.post("/conversations/{conversation_id}/messages")
    async def post_message(conversation_id: str, req: MessagePost,
                           authorization: Optional[str] = Header(None)):
        """A message the human typed themselves.

        Free — no model call, no credit. The twins read these as their owner's
        instructions on the next round, which is what makes the two modes one
        conversation rather than two.
        """
        uid = identify(authorization, req.user_id)
        conversation, members, _messages, profiles = load_conversation(conversation_id, uid)

        content = (req.content or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="Write something first.")

        message = add_message(
            conversation_id, uid, "human", content[:4000], int(conversation.get("round") or 0)
        )
        updated = touch_conversation(conversation_id, {})
        return {
            "message": message_view(message, profiles),
            "conversation": conversation_view(
                {**conversation, **updated}, members, profiles, uid
            ),
        }

    @router.post("/conversations/{conversation_id}/deliberate")
    async def deliberate(conversation_id: str, req: DeliberateRequest,
                         authorization: Optional[str] = Header(None)):
        """Run twin rounds: one turn each, a proposal at the end of every round.

        Called once per round by the browser rather than looping to exhaustion
        here, so each round appears as it happens, the humans can stop it, and
        no single HTTP request sits open while a balance drains.
        """
        uid = identify(authorization, req.user_id)
        conversation, members, messages, profiles = load_conversation(conversation_id, uid)

        if len(members) != 2:
            raise HTTPException(
                status_code=409,
                detail="A twin discussion needs exactly two people in it.",
            )
        if (conversation.get("status") or "open") == "resolved":
            raise HTTPException(
                status_code=409,
                detail="You both agreed on this one — the twins have stopped.",
            )

        round_number = int(conversation.get("round") or 0)
        if req.expected_round is not None and req.expected_round != round_number:
            # Somebody else's browser already ran the round this one asked for.
            raise HTTPException(
                status_code=409,
                detail="This discussion has moved on — reload to see the latest round.",
            )

        member_ids = [m["user_id"] for m in members]
        balances = [int(profiles.get(mid, {}).get("credits_balance") or 0) for mid in member_ids]
        requested = max(1, min(int(req.rounds or 1), rounds_per_request))
        allowed, stop_reason = deliberation.affordable_rounds(
            balances, requested, round_cap - round_number
        )

        if allowed == 0:
            # Nothing ran. Say so in the transcript once, so the other person
            # sees why it stopped without having to be told.
            status = "exhausted" if stop_reason else conversation.get("status") or "open"
            new_messages = []
            if stop_reason and conversation.get("stop_reason") != stop_reason:
                new_messages.append(
                    add_message(conversation_id, None, "system",
                                deliberation.stop_message(stop_reason), round_number)
                )
            updated = touch_conversation(
                conversation_id, {"status": status, "stop_reason": stop_reason}
            )
            return {
                "messages": [message_view(m, profiles) for m in new_messages],
                "conversation": conversation_view(
                    {**conversation, **updated}, members, profiles, uid
                ),
                "rounds_run": 0,
                "can_continue": False,
                "stop_reason": stop_reason,
            }

        names = {mid: deliberation.display_name(profiles.get(mid)) for mid in member_ids}
        transcript_source = list(messages)
        new_messages = []
        proposal = conversation.get("current_proposal")
        verdict_notes = {
            m["user_id"]: (m.get("verdict_note") if m.get("verdict") == "disagree" else None)
            for m in members
        }

        for _ in range(allowed):
            round_number += 1
            order = deliberation.speaking_order(member_ids, round_number)

            for position, speaker_id in enumerate(order):
                partner_id = next(mid for mid in member_ids if mid != speaker_id)
                speaker = profiles.get(speaker_id, {"id": speaker_id})
                closing = position == len(order) - 1

                notes = [
                    m.get("content")
                    for m in transcript_source
                    if m.get("author") == "human" and m.get("user_id") == speaker_id
                ][-5:]

                reply = generate(
                    system=deliberation.twin_system_prompt(
                        owner_name=names[speaker_id],
                        partner_name=names[partner_id],
                        tier=speaker.get("subscription_tier") or "free",
                        twin_brief=speaker.get("twin_brief"),
                        closing=closing,
                    ),
                    content=deliberation.turn_prompt(
                        topic=conversation.get("topic") or "",
                        owner_name=names[speaker_id],
                        partner_name=names[partner_id],
                        transcript=deliberation.render_transcript(transcript_source, names),
                        notes=notes,
                        objection=verdict_notes.get(speaker_id),
                    ),
                    max_tokens=twin_max_tokens,
                )

                # Charged only once the model has actually answered: a provider
                # failure raises out of generate() and must not cost a credit.
                spend_credit(speaker)
                verdict_notes[speaker_id] = None

                message = add_message(conversation_id, speaker_id, "twin", reply, round_number)
                transcript_source.append(message)
                new_messages.append(message)

                if closing:
                    proposal = deliberation.extract_proposal(reply)

            # A fresh proposal is a fresh vote: whatever either side said about
            # the last one does not carry over to this one.
            for member in members:
                member["verdict"] = "pending"
                member["verdict_note"] = None
                set_verdict(conversation_id, member["user_id"],
                            {"verdict": "pending", "verdict_round": round_number,
                             "verdict_note": None})

        balances = [int(profiles.get(mid, {}).get("credits_balance") or 0) for mid in member_ids]
        keep_going = deliberation.can_continue(balances, round_cap - round_number, "deliberating")
        _, next_stop = deliberation.affordable_rounds(balances, 1, round_cap - round_number)

        if not keep_going and next_stop:
            new_messages.append(
                add_message(conversation_id, None, "system",
                            deliberation.stop_message(next_stop), round_number)
            )

        updated = touch_conversation(conversation_id, {
            "status": "deliberating" if keep_going else ("exhausted" if next_stop else "deliberating"),
            "round": round_number,
            "current_proposal": proposal,
            "stop_reason": next_stop,
        })

        return {
            "messages": [message_view(m, profiles) for m in new_messages],
            "conversation": conversation_view(
                {**conversation, **updated}, members, profiles, uid
            ),
            "rounds_run": allowed,
            "can_continue": keep_going,
            "stop_reason": next_stop,
        }

    @router.post("/conversations/{conversation_id}/verdict")
    async def record_verdict(conversation_id: str, req: VerdictRequest,
                             authorization: Optional[str] = Header(None)):
        """Agree or disagree with the proposal on the table.

        Both agree and it is settled. One disagrees and the twins go again —
        the objection is handed to that person's twin as its brief for the next
        round, and the round after that, until somebody agrees or the credits
        run out.
        """
        uid = identify(authorization, req.user_id)
        conversation, members, _messages, profiles = load_conversation(conversation_id, uid)

        if req.verdict not in VERDICTS:
            raise HTTPException(status_code=400, detail="A verdict is 'agree' or 'disagree'.")
        round_number = int(conversation.get("round") or 0)
        if round_number < 1 or not conversation.get("current_proposal"):
            raise HTTPException(
                status_code=409,
                detail="There is no proposal to vote on yet — let the twins talk first.",
            )

        note = (req.note or "").strip()[:1000]
        fields = {"verdict": req.verdict, "verdict_round": round_number,
                  "verdict_note": note if req.verdict == "disagree" else None}
        set_verdict(conversation_id, uid, fields)
        for member in members:
            if member.get("user_id") == uid:
                member.update(fields)

        new_messages = []
        if req.verdict == "disagree":
            # Written into the transcript as well as onto the member row: the
            # other person should be able to read why their twin is still
            # arguing, not just see that it is.
            new_messages.append(add_message(
                conversation_id, uid, "human",
                f"Rejected the proposal. {note}" if note else "Rejected the proposal.",
                round_number,
            ))

        outcome = deliberation.verdict_outcome([m.get("verdict") or "pending" for m in members])
        if outcome == "resolved":
            fields_conv = {"status": "resolved", "stop_reason": "resolved"}
        elif outcome == "continue":
            # Back to 'open': there is a live objection and the next round is
            # the answer to it.
            fields_conv = {"status": "open", "stop_reason": None}
        else:
            fields_conv = {"status": "deliberating"}
        updated = touch_conversation(conversation_id, fields_conv)

        return {
            "outcome": outcome,
            "messages": [message_view(m, profiles) for m in new_messages],
            "conversation": conversation_view(
                {**conversation, **updated}, members, profiles, uid
            ),
        }

    return router
