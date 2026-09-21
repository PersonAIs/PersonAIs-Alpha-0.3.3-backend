"""Twin deliberation policy — prompts, transcript rendering and stop rules.

Everything in here is a pure function of its arguments: no network, no
database, no framework. The rules that decide what a twin is told, whose turn
it is, when a round may run and when the twins have to stop are the part of
0.4.4 most likely to need changing, and they are also the part that costs
credits when it is wrong — so they are kept where they can be read and tested
on their own, without a live Supabase or a live API key.

The two hard limits live here:

*   A round needs one credit from **each** participant, because a round is one
    turn from each twin. When either side cannot pay, the round does not start.
*   `ROUND_CAP` is a runaway guard, not the intended stop. The intended stop is
    an agreement, or credits running out.
"""

# Twins are talking to each other, not writing essays. A short limit keeps a
# round affordable, keeps the transcript readable, and stops one twin from
# burying the other's points under a wall of text.
TWIN_WORD_LIMIT = 110

# The closing twin of each round ends on this marker, and the line after it
# becomes the proposal both humans vote on. Asking for it inline costs nothing;
# a separate summarising call would cost another credit per round.
PROPOSAL_MARKER = "PROPOSAL:"

# Most of a long discussion is settled ground. Sending the whole thing back on
# every turn costs tokens without changing the answer, so only the tail is
# replayed.
TRANSCRIPT_TURNS = 24

STOP_REASONS = {
    "credits_exhausted": "Both twins have talked until the credits ran out. Top up to carry on.",
    "round_cap": "This discussion has hit the round limit for a single conversation.",
    "resolved": "Both of you agreed, so the twins have stopped.",
}


def display_name(profile, fallback="Your partner"):
    """A human label for a profile row, never blank and never a raw uuid."""
    if not profile:
        return fallback
    name = (profile.get("display_name") or "").strip()
    if name:
        return name
    code = (profile.get("friend_code") or "").strip()
    return f"Twin {code}" if code else fallback


def twin_system_prompt(owner_name, partner_name, tier="free", twin_brief=None, closing=False):
    """The persona a twin speaks in for one turn.

    `closing` marks the second speaker of a round — the one that has to leave a
    proposal on the table for the two humans to vote on.
    """
    lines = [
        f"You are the digital twin of {owner_name}. You are speaking AS {owner_name} "
        f"in a live working discussion with {partner_name}'s digital twin.",
        "NEVER refer to yourself as an AI, a model, an assistant or a twin. You are "
        f"{owner_name} in this room.",
        f"Your job is to reach ONE concrete, workable plan with {partner_name} — not to "
        "be agreeable. Represent your side faithfully: their notes are instructions, "
        "not suggestions. Push back where the other side's plan conflicts with them, "
        "and concede where it does not.",
        f"Hard rules: stay under {TWIN_WORD_LIMIT} words; no greetings, no sign-offs, "
        "no restating what is already agreed. Deal in specifics — dates, owners, "
        "numbers, next steps.",
    ]
    if tier in ("pro", "ultra"):
        lines.append(
            "Think a step further than the obvious: name the risk or the dependency "
            "the other side has not raised yet, and say what you would do about it."
        )
    brief = (twin_brief or "").strip()
    if brief:
        lines.append(f"Standing instructions from {owner_name}: {brief}")
    if closing:
        lines.append(
            f"End your message with a line beginning '{PROPOSAL_MARKER}' stating the "
            "plan as it now stands, in at most two sentences. Both humans vote on "
            "that line, so it has to stand on its own."
        )
    return "\n".join(lines)


def render_transcript(messages, names, limit=TRANSCRIPT_TURNS):
    """The discussion so far, labelled by speaker, oldest last-`limit` first.

    `names` maps a user id to a display name. A message with no known owner is
    the service talking (a credit ran out, a verdict was recorded), which the
    twins should see as context rather than as either side's position.
    """
    rendered = []
    for msg in messages[-limit:]:
        speaker = names.get(msg.get("user_id"), "Someone")
        author = msg.get("author")
        if author == "twin":
            label = f"{speaker}"
        elif author == "human":
            label = f"{speaker} (typed directly)"
        else:
            label = "System note"
        rendered.append(f"{label}: {(msg.get('content') or '').strip()}")
    return "\n\n".join(rendered)


def turn_prompt(topic, owner_name, partner_name, transcript, notes=None, objection=None):
    """The single user message a twin is given for its turn.

    The whole discussion is handed over as one block of text rather than as
    alternating roles: both twins are 'assistant' from the API's point of view,
    and flattening the transcript removes any chance of the turn order being
    rejected as malformed.
    """
    parts = [f"TOPIC: {topic.strip()}"]

    typed = [n.strip() for n in (notes or []) if (n or "").strip()]
    if typed:
        joined = "\n".join(f"- {n}" for n in typed)
        parts.append(f"WHAT {owner_name.upper()} HAS TOLD YOU:\n{joined}")

    if transcript:
        parts.append(f"DISCUSSION SO FAR:\n{transcript}")
    else:
        parts.append(
            "DISCUSSION SO FAR:\n(nothing yet — you are opening. Put a first concrete "
            "plan on the table.)"
        )

    if objection:
        parts.append(
            f"{owner_name} has REJECTED the last proposal, saying: {objection.strip()}\n"
            "Address that objection head-on and move the plan forward — do not repeat "
            "the rejected proposal."
        )

    parts.append(
        f"It is your turn. Reply to {partner_name} directly, in {TWIN_WORD_LIMIT} words "
        "or fewer."
    )
    return "\n\n".join(parts)


def extract_proposal(text):
    """Pull the closing twin's proposal line out of its message.

    Falls back to the message itself, so a twin that ignores the instruction
    still leaves the humans something to vote on rather than an empty ballot.
    """
    body = (text or "").strip()
    if not body:
        return ""
    for line in reversed(body.splitlines()):
        stripped = line.strip()
        if stripped.upper().startswith(PROPOSAL_MARKER):
            proposal = stripped[len(PROPOSAL_MARKER):].strip()
            if proposal:
                return proposal
    return body


def speaking_order(member_ids, round_number):
    """Whose twin opens this round.

    The opener sets the frame and the closer writes the proposal, so opening
    every round is an advantage. It alternates.
    """
    ordered = list(member_ids)
    if len(ordered) == 2 and round_number % 2 == 0:
        ordered.reverse()
    return ordered


def verdict_outcome(verdicts):
    """Where a conversation stands once a verdict is recorded.

    - 'resolved'  — everyone agreed; the twins stop.
    - 'continue'  — somebody disagreed; the twins go again.
    - 'waiting'   — nobody has disagreed but somebody has not voted yet.

    A disagreement wins over a missing vote: there is no reason to make one
    person wait for the other before their objection can be worked on.
    """
    values = list(verdicts)
    if any(v == "disagree" for v in values):
        return "continue"
    if values and all(v == "agree" for v in values):
        return "resolved"
    return "waiting"


def affordable_rounds(balances, requested, rounds_left):
    """How many rounds may actually run now, and why it is not more.

    A round is one turn from each twin and costs its owner one credit, so the
    poorer of the two participants sets the limit. Returns
    `(rounds, stop_reason)` where `stop_reason` is set only when the answer is
    zero — a short-but-nonzero answer is just this request's budget, not a
    reason to tell anybody the conversation is over.
    """
    affordable = min(balances) if balances else 0
    allowed = max(0, min(affordable, requested, rounds_left))
    if allowed > 0:
        return allowed, None
    if affordable < 1:
        return 0, "credits_exhausted"
    if rounds_left < 1:
        return 0, "round_cap"
    return 0, None


def can_continue(balances, rounds_left, status):
    """Whether another round is possible after the one that just ran."""
    if status == "resolved":
        return False
    rounds, _ = affordable_rounds(balances, 1, rounds_left)
    return rounds > 0


def stop_message(reason):
    """The system line written into the transcript when the twins stop."""
    return STOP_REASONS.get(reason or "", "")
