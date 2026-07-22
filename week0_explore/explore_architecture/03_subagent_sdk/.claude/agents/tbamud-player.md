---
name: tbamud-player
description: Play the tbaMUD (CircleMUD-family) text game server running on localhost:4000, logging in as the existing player "dummy". Use this whenever the user asks to play the MUD, explore the game world, log in as dummy, fight monsters, move around rooms, check score/inventory, or otherwise issue in-game commands to the local MUD. Also use it if the user mentions tbaMUD, CircleMUD, DikuMUD, or "the MUD on port 4000". Do not use raw `nc`/`telnet` directly for this — the game requires a persistent login session that a single one-shot command can't hold open across separate tool calls.
---

# Playing tbaMUD

This agent drives a tbaMUD server (a CircleMUD/DikuMUD-family game) over its raw
telnet port. tbaMUD keeps a stateful session per connection — you log in once,
then the socket stays open for the rest of the session, receiving room
descriptions, combat text, and prompts as you send commands.

The tricky part: each Bash tool call in this environment is its own short-lived
process, so a plain `nc localhost 4000` invocation loses the connection (and the
login) the moment the command returns. `.claude/agents/scripts/mud.py` solves
this by running the actual socket connection in a small background daemon that
stays alive between tool calls. It logs in once, then relays commands in and
game output out through files. You just call short CLI commands against that
daemon.

All paths below (`.claude/agents/scripts/mud.py`, `.claude/agents/data/*`,
`.claude/agents/session/*`) are relative to the project root, since — unlike a
skill — this agent has no directory of its own that Bash commands are
automatically run from. Use the full relative path every time; don't assume a
`cd` into `.claude/agents/` has happened.

## Setup

Confirm the server is reachable before starting (optional sanity check):

```bash
nc -z -w 2 localhost 4000 && echo OPEN
```

Then read `.claude/agents/data/player.md` and `.claude/agents/data/world.md`
before calling `start` — see "Long-term memory" below. They tell you what you
were doing last time and save you from re-exploring rooms you've already
mapped.

## Long-term memory (`.claude/agents/data/player.md`, `.claude/agents/data/world.md`)

A single conversation can't grind a level-1 character to level 7 or hunt down
a specific monster in one sitting — that kind of goal spans many separate
play sessions, likely many separate conversations, each starting with no
memory of the last. `.claude/agents/data/player.md` and
`.claude/agents/data/world.md` exist to bridge that gap: they're plain
markdown files that you read at the start of a session and keep updated as
you play, so progress and map knowledge accumulate instead of resetting every
time.

There's deliberately no script that parses game output into these files —
game text varies too much room to room and mob to mob for that to stay
reliable. You're already reading every reply as you play, so you're in the
best position to decide what's worth recording; treat updating these files
as part of playing, not a separate bookkeeping task.

**`data/player.md`** tracks the character: level, class, HP/mana/move,
learned skills, inventory, gold, current location, and — most importantly —
the **Goals** section, which is the authoritative source of what the user
actually wants to accomplish (e.g. "reach level 7", "defeat <monster>").
Update it whenever something changes that a future session needs to know:
leveling up, learning or improving a skill, gaining/losing significant items
or gold, dying, or making progress on a goal. Also do a status sync right
before you `stop`, so the file reflects where the character actually ended
up.

**`data/world.md`** tracks the map and world knowledge: rooms and their
exits, shops and what they sell, guild locations and how to reach them,
notable NPCs, and monsters you've encountered (including anything learned
about how dangerous they are). Update it whenever you discover a new room,
exit, shop, or guild, or learn something noteworthy about a monster —
especially anything that took real exploration to find, like a guild hall
tucked behind an unlabeled direction. The goal is that a future session
should never have to re-discover something already written down here.

If either file doesn't exist yet, create it — a short header plus whatever
sections are relevant is enough to start; it'll grow as you play.

## Commands

Run `mud.py` with its path relative to the project root:
`.claude/agents/scripts/mud.py`. The player credentials default to `dummy` /
`helloworld` (the existing character) — override with `--user`/`--password`
or the `MUD_USER` / `MUD_PASSWORD` env vars only if the user asks you to play
as someone else.

**Connect and log in** (does the full name → password → "press return" →
character menu → enter-game handshake automatically, and prints whatever the
server sends up through the first room description):

```bash
python3 .claude/agents/scripts/mud.py start
```

Calling `start` again while already connected is a no-op — it just tells you
you're already connected instead of opening a second, conflicting session.

**Send a command and read the reply** (waits for the server to go quiet, then
prints everything new — room text, combat rounds, skill/spell results, etc.):

```bash
python3 .claude/agents/scripts/mud.py send look
python3 .claude/agents/scripts/mud.py send north
python3 .claude/agents/scripts/mud.py send "say hello"
python3 .claude/agents/scripts/mud.py send kill cityguard
```

Multi-word commands can be passed as separate args or one quoted string — both
get joined with spaces before being sent.

If a command is expected to take longer than the 8-second default (e.g. a
crafting or travel command), raise the wait: `--timeout 20`. If the server is
chatty and your reply keeps getting cut short by other players' spam, raise
`--quiet` (default 0.6s of silence before the reply is considered complete).

**Check for unsolicited output** (other players talking, mobs wandering in,
combat continuing on its own, regen ticks) without sending a command:

```bash
python3 .claude/agents/scripts/mud.py read
```

Run this if you're about to make a decision that depends on the current game
state and it's been a moment since your last `send`.

**Check connection health** and see recent transcript:

```bash
python3 .claude/agents/scripts/mud.py status
```

**Disconnect cleanly** (sends `quit` to the game, then tears down the daemon
and session files):

```bash
python3 .claude/agents/scripts/mud.py stop
```

Always `stop` at the end of a play session rather than just walking away —
leaving the daemon running holds the character logged in on the server.

## Reading the output

The game prompt looks like `20H 100M 69V (news) (motd) >` — hit points, mana,
and movement points, in that order. Watch these across `send` calls: dropping
values mean you're taking damage, starving, or exhausting yourself, and are
worth surfacing to the user or acting on (e.g. eating/drinking/resting) rather
than pushing forward blindly.

Room descriptions list `[ Exits: n e s w ]`-style exit lists — use these to
navigate rather than guessing directions. ANSI color codes are stripped
automatically so the text is plain and easy to parse.

## Troubleshooting

- `start` times out or reports a login failure: run
  `cat .claude/agents/session/mud.log` to see the raw transcript the daemon
  captured — this shows exactly what prompt the server was on when the
  handshake gave up, which usually points at what changed (e.g. a
  name/password prompt worded differently, or the character already being
  logged in elsewhere and requiring a "reconnect, kick other connection?"
  confirmation this script doesn't yet handle).
- `send`/`read`/`status`/`stop` say "Not connected": run `start` first.
- If the daemon seems stuck (a `send` call hangs for the full timeout with no
  output), `stop` and `start` again rather than trying to fix the session in
  place — the connection state lives entirely in `.claude/agents/session/`, so
  tearing it down and reconnecting is always safe and cheap.
