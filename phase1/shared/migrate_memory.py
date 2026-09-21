"""
One-time cleanup of first-version memories into the new shape.

The first version stored everything as undated "facts", including moods
("he is sleepy now"), her own role-play details, and plans whose
"tomorrow" is long gone. The new code converts those mechanically on first
load (keeping the original as memory.v1.json); this goes further and has
the model decide where each old note really belongs -- about him, about the
two of you, a still-upcoming plan, or nothing -- rewriting "tomorrow" into
real dates.

It runs in two steps so a person approves what changes. The preview saves
the model's decisions to memory.cleanup.json (edit it if you disagree), and
--apply writes exactly those saved decisions -- the model is not asked
again, since it would not answer the same way twice. Anything learned since
the mechanical conversion (new facts, plans, calls) is kept.

    python -m shared.migrate_memory [path/to/memory.json]           # preview
    python -m shared.migrate_memory [path/to/memory.json] --apply   # write
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from shared import llm
from shared import memory as m

PROMPT = """\
These are old memory notes a girlfriend character kept about her boyfriend. \
They were stored carelessly: some are moods that were only true at the time, \
some are her own role-play details, some mention "tomorrow" or "tonight" \
relative to the day they were written. Sort each one.

NOW: {now}

Notes (id, the date it was written, text):
{notes}

For EVERY note return one decision in a JSON object {{"decisions": [...]}}:
{{"id": ..., "action": "him" | "us" | "plan" | "drop",
  "kind": ..., "text": ..., "importance": 1-5, "when": ...,
  "merged_into": ..., "reason": ...}}

- "him": a lasting fact about HIM. kind: identity (who he is, work, studies, \
placement, family), preference, habit, or other.
- "us": something that belongs to the two of them. kind: milestone (firsts, \
big moments, that they're together, that he loves her), ritual, joke, \
promise, or moment. Statements about the relationship itself go here, not \
in "him".
- A plan whose date has already PASSED (like "a date tomorrow", written days \
ago) is a shared event that happened: keep it as "us" with its real date \
("Went on a date at a traditional Indian restaurant on 20 Sep 2026"). The \
same goes for things they did together ("met for pizza and binge-watched a \
show").
- Merging: when several notes describe the same thing, KEEP ONE of them \
(its "text" carries the merged details) and drop the others with \
"merged_into" set to the kept note's id. Never drop every note of a group.
- "plan": ONLY if it's a dated event still AFTER NOW; give "when" as \
YYYY-MM-DD or YYYY-MM-DDTHH:MM.
- "drop": moods and passing states ("sleepy now", "sad right now", "drinking \
water today", "watching videos"), things that were only true that day, her \
own role-play details, or anything too trivial to remember.
- Rewrite "text" so it's still true when read later: replace "tonight", \
"tomorrow", "two weeks ago" with real dates (e.g. "Got placed around 7 Sep \
2026"). Write dates like "7 Sep 2026". Use "he" for him and "you" for her. \
Add NOTHING that isn't in the notes.
- "reason": a few words explaining the decision.
"""

LABELS = {"him": "ABOUT HIM", "us": "ABOUT US", "plan": "PLAN", "drop": "DROP"}


def _old_facts(v1: dict) -> list[dict]:
    return [f if isinstance(f, dict) else {"text": f} for f in v1.get("facts", [])]


def propose(client, v1: dict) -> list[dict]:
    old = _old_facts(v1)
    notes = [f'n{i} | written {(f.get("created_at") or "")[:10] or "unknown"} | {f.get("text", "")}'
             for i, f in enumerate(old)]
    prompt = PROMPT.format(now=f"{m.now_local():%A %d %B %Y}", notes="\n".join(notes))
    data = m._ask_json(client, llm.MEMORY_MODEL, prompt) or {}
    by_id = {d.get("id"): d for d in data.get("decisions", []) if isinstance(d, dict)}
    out = []
    for i, f in enumerate(old):
        d = by_id.get(f"n{i}") or {"action": "him", "kind": "other", "text": f.get("text"),
                                   "reason": "no decision returned -- kept as-is"}
        if d.get("action") not in LABELS:
            d.update(action="him", kind="other", reason=f"unknown action {d.get('action')!r} -- kept as-is")
        d.update(id=f"n{i}", original=f.get("text", ""), created_at=f.get("created_at"))
        out.append(d)
    return out


def problems(decisions: list[dict]) -> list[str]:
    """Merges that point at a note that isn't kept would silently lose it."""
    kept = {d["id"] for d in decisions if d.get("action") != "drop"}
    return [f'{d["id"]} "{d["original"]}" is merged into {d["merged_into"]}, which is not kept'
            for d in decisions if d.get("action") == "drop" and d.get("merged_into")
            and d["merged_into"] not in kept]


def show(decisions: list[dict]) -> None:
    for d in decisions:
        action = d.get("action")
        kind = f"/{d['kind']}" if d.get("kind") and action in ("him", "us") else ""
        print(f"{d['id']:>4}  {LABELS[action] + kind:22} {d['original']}")
        if action != "drop" and d.get("text") and d["text"] != d["original"]:
            print(f"{'':28}-> {d['text']}" + (f"  (when {d['when']})" if action == "plan" else ""))
        why = d.get("reason", "")
        if d.get("merged_into"):
            why = f"merged into {d['merged_into']}; {why}"
        print(f"{'':28}({why})")
    counts: dict = {}
    for d in decisions:
        counts[d["action"]] = counts.get(d["action"], 0) + 1
    print(f"\n{len(decisions)} notes -> " + ", ".join(f"{v} {LABELS[k].lower()}" for k, v in counts.items()))


def build(base: dict, decisions: list[dict], converted_at: datetime | None) -> dict:
    """The cleaned memory: `base` with its converted old facts replaced by
    the decisions. Facts learned after the conversion, plans, calls and
    everything else in `base` are kept as they are."""
    now = m.now_local()
    mem = json.loads(json.dumps(base))
    if converted_at is not None:
        mem["facts"] = [f for f in mem["facts"] if m._parse(f["created_at"]) >= converted_at]
    else:
        mem["facts"] = []
    for d in decisions:
        action, learned = d.get("action"), d.get("created_at") or m._iso(now)
        if action in ("him", "us"):
            before = len(mem["facts"])
            m.apply_ops(mem, [{"op": "add", "section": action, "kind": d.get("kind"),
                               "text": d.get("text") or d["original"], "importance": d.get("importance", 3)}], now)
            if len(mem["facts"]) > before:
                # Age it from when it was first learned, not from today.
                mem["facts"][-1]["created_at"] = mem["facts"][-1]["last_mentioned_at"] = learned
        elif action == "plan":
            m.apply_ops(mem, [{"op": "add_plan", "text": d.get("text"), "when": d.get("when")}], now)
    m.enforce(mem, now)
    return mem


def main() -> None:
    path = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), m.MEMORY_PATH))
    backup = path.with_name("memory.v1.json")
    proposal = path.with_name("memory.cleanup.json")
    current = json.loads(path.read_text(encoding="utf-8"))

    if current.get("version") == m.VERSION:
        if not backup.exists():
            print(f"{path} is already in the new format and there's no memory.v1.json to clean up from.")
            return
        v1 = json.loads(backup.read_text(encoding="utf-8"))
        converted_at, base = datetime.fromtimestamp(backup.stat().st_mtime, m.now_local().tzinfo), current
    else:
        v1, converted_at, base = current, None, m._empty()
        base["last_seen"] = current.get("last_seen")

    if "--apply" not in sys.argv:
        decisions = propose(llm.sync_client(), v1)
        proposal.write_text(json.dumps(decisions, indent=2, ensure_ascii=False), encoding="utf-8")
        show(decisions)
        for p in problems(decisions):
            print(f"  ! {p}")
        print(f"\nPreview only -- saved to {proposal}. Edit it if you disagree, then re-run with --apply.")
        return

    if not proposal.exists():
        print("No saved proposal -- run the preview first.")
        return
    decisions = json.loads(proposal.read_text(encoding="utf-8"))
    bad = problems(decisions)
    if bad:
        print("Not applied -- fix these in the proposal first:\n  " + "\n  ".join(bad))
        return
    if not backup.exists():
        shutil.copy2(path, backup)
    mem = build(base, decisions, converted_at)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    proposal.replace(proposal.with_name("memory.cleanup.applied.json"))
    print(f"Written: {sum(f['section'] == 'him' for f in mem['facts'])} about him, "
          f"{sum(f['section'] == 'us' for f in mem['facts'])} about you two, {len(mem['plans'])} plans. "
          f"The original is kept as {backup.name}.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
