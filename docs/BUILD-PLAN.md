# Build plan — the classroom inside Academy

The classroom moves into WeBuddhist Academy: students reach it through Academy, signed in
with their Academy account, and the reader is ours.

`PLAN.md` is the original design and the reasoning behind each locked decision. This
document is what replaces its phase plan. Where the two disagree, this one is current —
and the section below says where, and why.

## What we're building

Exactly this, and deliberately nothing else:

1. **Display the text.**
2. **Live sync** — the teacher drives, every student's reader follows.
3. **Translations stacked** where they exist — Tibetan above, English below.
4. **Highlight** a line.
5. **Notes** on a line, belonging to the student's Academy account.

## What we are NOT building

This matters as much as the list above, because the WeBuddhist reader has all of it and
carrying it across was seriously considered:

| Not building | Why not |
|---|---|
| Paging as you scroll | The whole text loads on join. There is nothing left to page |
| Transliteration (Wylie) | ~1,070 lines of lookup tables. Nobody asked for script switching |
| Durchen / yigchung annotations | Not asked for |
| Side-by-side editions, scroll sync | Not asked for |
| Resources panel, search, AI chat, share | Not asked for |
| Focus mode, auto-scroll | Not asked for |

If one of these is genuinely wanted later, add it then. Adding a feature to a small
reader is easy; removing five from a large one is not.

## Three pieces

| Piece | Where it lives | State |
|---|---|---|
| **Sync server** | this repo — Python | Step 1 done |
| **Reader** | `frontend-app-extension`, styles in `brand-openedx` | not started |
| **Notes** | `sherab-custom-plugin` (Django) | not started |

They are independent. The protocol doesn't change, so each can proceed without waiting on
the others.

## Where this departs from PLAN.md

Three locked decisions have been revisited. The reasoning is recorded so nobody has to
re-derive it:

**Decision 3 — "texts are imported into Postgres; a live class never reads upstream."**
*Changed.* That decision was made when the sync server was the student's source of text.
It isn't any more — the reader fetches from the library directly. The server now stores
**segment ids only**, registered the first time a room is created against a text. The
import pipeline is gone.

*The trade:* if the library is down **and** a student joins **after** that, the latecomer
cannot load the text. Everyone already in the room is unaffected — their text is in their
browser, and sync carries only ids. Judged worth it against maintaining an import
pipeline and a copy of the corpus.

**Decision 5 — "offline-first client, full text cached on join."**
*Kept, and it is load-bearing.* It is the reason paging is not needed, which in turn is
the reason this reader is small.

**Decision 6 — "no student accounts."**
*Changed for notes.* Joining a room still needs no account. But a note has to belong to
someone and outlive the class, so notes require an Academy identity. Sync stays anonymous;
notes do not.

## Why we recreate the reader rather than port it

The WeBuddhist reader is ~20,000 lines. Three options were weighed:

- **An iframe** of the existing reader — ruled out by notes. Classroom notes would have
  had to be built inside the WeBuddhist codebase, which we cannot modify, and which is
  the wrong home for an Academy feature.
- **Porting `chapterV2`** — ruled out by scope. Most of those 20,000 lines implement
  features in the "not building" table. It also carries Tailwind into a webpack build
  that was not designed for it, which was the single largest unknown in the project.
- **Recreating it** — chosen. Writing ~600 lines of SCSS-styled UI for the five
  requirements above, rather than carrying a library reader to run a classroom.

Recreating removes the Tailwind question entirely: the new reader is SCSS with brand
tokens from its first line, like `.wishlist-page` already is.

## The one thing to copy, not rewrite

Stacked Tibetan/English is the only requirement with hidden depth. **The library does not
hand you translations attached to segments.** The client works it out:

1. Fetch alignment pairs between the two editions
   (`GET /v2/editions/{source}/alignments/{target}`)
2. Map each source segment to its target segment ids — **often not one-to-one**
3. Fetch the target content and attach it

That is `alignments.ts` (339 lines) plus the fetching parts of `textDetails.ts` — about
**700 lines**. Copy them from the WeBuddhist repo and **never edit them**. The
not-one-to-one mapping and its fallback path is exactly the kind of thing a rewrite gets
subtly wrong, and the comments record wrong turns already taken.

Record which commit they came from. `WeBuddhist/WeBuddhist` was at `80ed57cd` when this
was written. We cannot modify that repo, so this is a one-way copy; keeping it unedited is
what makes a later refresh a clean re-copy rather than a merge.

---

# Track A — the sync server

Node (Fastify + Socket.IO) becomes Python (FastAPI + python-socketio), in this repo.

## Why Python

Notes will be Django. Running two backend languages for one product is a cost paid
forever.

## Why not inside `sherab-custom-plugin`

Open edX runs under uWSGI, which cannot hold connections open — and live sync needs them
held open all lesson. Confirmed in our own Tutor config:

```
env/build/openedx/settings/uwsgi.ini
  wsgi-file = $(SERVICE_VARIANT)/wsgi.py
```

So this is a **separate service container**, like `notes` and `studio-tool-service`
already are. It just happens to be Python too.

## How big

698 lines of Node, including comments. `socket.ts` — the live messages, the part that
sounds hardest — is 109 of them.

## What it's built with

| Part | Was | Is |
|---|---|---|
| Web routes | Fastify | **FastAPI** |
| Live messages | Socket.IO | **python-socketio** — same wire protocol |
| Database | Postgres | **MySQL 8.4** (`aiomysql`) — Tutor's own instance |
| Running it | systemd | Tutor service container |

`python-socketio` speaks the same protocol, so **no client code changes because of this
port.**

## The five live messages

| Who sends it | Message | Carries |
|---|---|---|
| anyone → server | `room:join` | room code, nickname |
| server → that person | `room:state` | which text, which line, how many present |
| teacher → server | `teach:goto` | segment id, host token |
| server → everyone | `teach:goto` | segment id |
| server → everyone | `presence` | count |
| teacher → server | `room:close` | host token |
| server → everyone | `room:closed` | — |

That is the whole protocol. Everything else is ordinary web requests.

## Things that must not be lost in translation

These are the details that took real thought the first time. Each one exists because
something went wrong or could go wrong. **Read this list before writing any code.**

### 1. Room codes must be unguessable

Use `secrets.choice`, **not** `random.choice`. A guessable code means a stranger can sit
in on a class.

```python
import secrets
code = ''.join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(6))
```

The alphabet deliberately has no `O/0`, no `I/1/l`, no `S/5` — it gets read off a
whiteboard from the back of a room.

### 2. Compare tokens in constant time

Both the host token and the shared host password are compared with a timing-safe
comparison. In Python that's `hmac.compare_digest`, never `==`.

```python
import hashlib, hmac
presented = hashlib.sha256(token.encode()).hexdigest()
if hmac.compare_digest(presented, room.host_token_hash):
    ...
```

**This is the actual security boundary.** Hiding the teacher's buttons in the UI is
convenience; this is what stops a student driving the class.

### 3. Don't check for a duplicate code — let the database decide

Codes are only 6 characters, so collisions really happen. Don't look first and then
insert; that's a race. Insert, catch the unique-violation error, try again. Up to 10
times, then give up.

### 4. Check the segment belongs to the text

Before moving the room, confirm the segment id is actually part of this room's text.
Otherwise a bad id surfaces as a database error in the middle of a class.

### 5. Update the room and write the log in one transaction

Moving the room does two writes: update the room's current line, add a row to the
history log. Both or neither.

### 6. Broadcast to everyone, including the teacher

When the teacher moves the room, send the broadcast to **everyone, the teacher
included.** Don't update the teacher's screen locally and broadcast to the rest.

The reason: if the teacher's view goes through a different path from the students', the
two can silently drift apart, and the teacher won't know.

### 7. Room codes are matched uppercase

A student typing `abc123` must find room `ABC123`.

### 8. Update presence on disconnect too

When someone closes their browser, the remaining count must drop. Easy to forget,
because nothing fails loudly when you do.

### 9. Keep polling as a fallback

Prefer a real WebSocket, but allow long-polling for restrictive networks. On polling the
teacher will see the highlight lag behind their own voice — acceptable as a fallback,
not as the default.

### 10. Allow several origins

`WEB_ORIGIN` is a comma-separated list. A deployment may answer to more than one name,
and testing a teacher and a student side by side needs two origins — browsers keep
`localStorage` per origin, and the host token lives there.

### 11. Warn if the host password is still the default

If `HOST_PASSWORD` is still `change-me`, log a warning at startup. Anyone who can reach
the server could otherwise create a room.

### 12. Recovery codes must be checkable before use

There's a route that verifies a recovery code before the teacher acts on it. Without it,
a teacher restoring a room on a new device is told they're teaching, then finds every tap
refused — mid-class, when they can no longer do anything about it.

This was a real bug, found and fixed. Don't reintroduce it.

### 13. Classes must be endable

A room that still works weeks later is one a stranger can wander into, and the teacher
has no way to tell. `room:close` must actually set `is_live = false` and tell everyone.

This was also a real bug — the client listened for `room:closed` but the server never
sent it.

---

## How we'll know it worked

**We already have the test.** `tests/smoke.mjs` checks every promise Phase 1 makes: only
the host can move the room, moves arrive promptly, a late joiner lands on the current
line, the recovery code recovers, a class can be ended.

**Nineteen checks**, all passing against the Node server. (PLAN.md says eleven — that was
the count before the recovery-code and room-close bugs were found; fixing them added the
rest.)

And here's the good part: **that test talks to the server from outside.** It makes web
requests and opens a socket. It never imports any server code, so it does not care that
the server is now Python.

```bash
API_URL=http://localhost:8000 node tests/smoke.mjs
```

**19/19 is the acceptance criterion for this entire port.**

### One honest caveat

The *assertions* carry over untouched. The **setup preamble does not.**

Its first ten lines list texts, take the first one's slug, and read its segments — which
assumed the import pipeline had put a text there. Storing segment ids only removes that
pipeline, so a fresh database has nothing to list.

The replacement is about six lines: register a text from the library, create a room
against it, read back its cached segment ids. Everything from `const teacher = await
connect()` onward — **all nineteen checks** — stays exactly as written.

Change the fixture, never an assertion. The assertions are the only reason the file is
worth keeping.

---
## Track A, in order

### Step 1 — Stand it up empty ✅
FastAPI with a health route, Socket.IO mounted, running in a container.

*Done, verified in the image itself:* `/api/health` answers, `/socket.io/` completes an
Engine.IO handshake advertising `upgrades: ["websocket"]`, the process runs as uid 10001
rather than root, the `HOST_PASSWORD` warning reaches the logs, and `migrations/` ships
inside the image.

The Dockerfile needed a fix to get here. It installed dependencies before copying
`src/`, the usual layer-caching split — but `pyproject.toml` declares a src layout, so
setuptools could not read its own requirements and the build failed with `'src' does not
exist or is not a directory`. Source is now copied before the install. Build takes about
25 seconds.

`wb-classroom-sync:dev` is just a local build name for this check. Under Tutor the image
is named and built by the plugin (Step 5), and never pushed anywhere.

### Step 2 — The web routes
Register a text (search the library by title, **resolve it to one edition and
segmentation**, store that segment id list), create a room, look up a room, verify a
recovery code. Also the library proxy — this service holds the API key, so the reader's
fetches come through here too.

Resolve the edition **once, here**. Never let clients resolve independently: the library
answers a bare text id with its first-listed edition, and thirty students resolving
separately is how a room ends up with ids that do not match.

*Done when:* the smoke test gets as far as creating a room before failing on the socket.

### Step 3 — The live messages
`room:join`, `teach:goto`, `room:close`, `presence`, `room:closed`. Work through the
thirteen points above as a checklist.

*Done when:* **19/19 on the smoke test.**

### Step 4 — Two browsers, by hand
A teacher and a student on two *different* origins — `localhost` and `127.0.0.1`. They
must differ, or they share `localStorage` and you do not have a real teacher and a real
student.

*Done when:* it feels instant, and a reconnect lands on the current line.

### Step 5 — Tutor plugin
**One Python file** in `~/Library/Application Support/tutor-plugins/classroom.py`, then
`tutor plugins enable classroom`. That is the whole deployment mechanism — the old
`infra/native/` Caddyfile and systemd unit become patches inside it.

Two plugins in this install are the working precedents: `studio_tool_service.py` (a
custom service of ours, built from a local checkout) and `tutornotes` (an upstream
service with its own MySQL database). Between them they already demonstrate every piece
the classroom needs. Read both before writing this.

**There is no registry and no image to publish.** Tutor builds it:

```python
@hooks.Filters.IMAGES_BUILD.add()
def _build_image(images, config):
    source_dir = config["CLASSROOM_SOURCE_DIR"]
    if source_dir:                      # laptop: a checkout on this machine
        images.append(("classroom", source_dir, "{{ CLASSROOM_DOCKER_IMAGE }}", ()))
    elif config["CLASSROOM_REPOSITORY"]:  # server: Docker fetches the git URL itself
        ...
```

`tutor images build classroom`. On a server it builds straight from
`https://github.com/WeBuddhist/wb-academy-classroom-sync-service.git#main` — Docker reads
a git URL natively, so nothing is cloned first and a rebuild picks up new commits.

#### The secrets problem solves itself

`CONFIG_UNIQUE` generates a value once, at `tutor config save`, and keeps it in
`config.yml`:

```python
hooks.Filters.CONFIG_UNIQUE.add_items([
    ("CLASSROOM_HOST_PASSWORD", "{{ 32|random_string }}"),
    ("CLASSROOM_MYSQL_PASSWORD", "{{ 8|random_string }}"),
])
```

**So `HOST_PASSWORD` is never `change-me` in a real deployment** — Tutor generates a
32-character one per install. The startup warning stays for people running the service
outside Tutor; inside it, the default is unreachable.

#### The database is created by an init task

`tutornotes` shows the pattern exactly: a `mysql` init task creating the database, the
user and the grant, registered through `CLI_DO_INIT_TASKS` so `tutor local init` runs it.

```
CREATE DATABASE IF NOT EXISTS {{ CLASSROOM_MYSQL_DATABASE }};
CREATE USER IF NOT EXISTS '{{ CLASSROOM_MYSQL_USERNAME }}';
ALTER USER ... IDENTIFIED BY '{{ CLASSROOM_MYSQL_PASSWORD }}';
GRANT ALL ON {{ CLASSROOM_MYSQL_DATABASE }}.* TO ...;
```

A second init task applies `migrations/001_init.sql`, which ships inside the image.
`001_init.sql` is written to be re-runnable, so the task is safe on every `init`.

#### The Caddy route is a path, not a subdomain

`tutormfe` leaves a patch point **inside** the MFE's own site block:

```
import proxy "mfe:8002"
{{ patch("caddyfile-mfe-proxy")|indent(4) }}
```

So the plugin adds:

```
handle_path /classroom-api/* {
    reverse_proxy classroom:8000
}
```

**Same origin as the page, so no CORS and no new subdomain** — Caddy orders `handle_path`
ahead of the catch-all proxy, which is what that patch point is for. (`notes` takes a
subdomain instead; we do not need to, and a second origin would mean CORS and a second
certificate.)

#### The patches, in full

| Hook | What it adds |
|---|---|
| `CONFIG_DEFAULTS` | image name, source dir, repository, database name |
| `CONFIG_UNIQUE` | `HOST_PASSWORD`, MySQL password |
| `IMAGES_BUILD` | builds from a checkout or from the git URL |
| `CLI_DO_INIT_TASKS` | create the database; apply the migration |
| `local-docker-compose-services` | the container itself |
| `local-docker-compose-dev-services` | mounts `src/` in `tutor dev`, so an edit needs only a restart |
| `caddyfile-mfe-proxy` | `/classroom-api/*` on the MFE host |

*Done when:* `tutor local launch` brings it up and `/classroom-api/api/health` answers
through Caddy, on the MFE's own domain.

### Step 6 — Switch over, keep the old one
Point the client at the Python server. **Leave the Node server runnable** for the first
few real classes.

*Delete it only after a few classes have run clean.*

---

# Track B — the reader

A new page in `frontend-app-extension`. Styles in `brand-openedx/paragon/_extension.scss`
as a `.classroom-page` section, alongside `.wishlist-page` and `.course-partner-page`.

## What exists to start from

`apps/web` in the old repo is 1,280 lines, half of it CSS. It already does display, join,
sync and highlight-the-current-line:

| File | Lines | Fate |
|---|---|---|
| `JoinScreen.tsx` | 254 | port roughly as-is |
| `App.tsx` | 126 | port roughly as-is |
| `useRoom.ts` | 122 | port as-is — the socket client |
| `Reader.tsx` | 66 | **the seed of the new reader** |
| `api.ts` | 71 | rewrite: text now comes from the library, not the sync server |
| `styles.css` | 562 | translate to SCSS with brand tokens |
| `fonts.css` | 52 | **delete** — the faces are already in `brand-openedx` |

React versions already line up (`apps/web` uses only `useState`, `useEffect`, `useRef`,
`useCallback`, `StrictMode` — nothing React 19 introduced, so it runs on the MFE's 18).
The only new dependency the MFE needs is **`socket.io-client`**.

## The fonts are already done

`brand-openedx/paragon/_fonts.scss` already declares DM Sans, Cormorant Garamond, Noto
Serif Tibetan and Jomolhari, with the same `unicode-range` split — Latin to DM Sans,
Tibetan to the Tibetan faces.

**But push one improvement back.** The brand loads raw `.ttf`; the classroom loads subset
`.woff2`, and its comment records what that bought:

> Jomolhari from 2.0 MB to 347 KB and Noto from 719 KB to 148 KB. That matters: thirty
> phones pulling fonts over one classroom access point is the worst moment of a session.

Roughly 6× smaller, and it matters most in exactly our situation. Move the woff2 subsets
into `brand-openedx` and repoint `_fonts.scss`. Every MFE gets faster.

This is small, self-contained, and can land before anything else.

## The socket contract — settled

**[`docs/socket-contract.md`](socket-contract.md) is the spec.** A written document, not
code: Python cannot import TypeScript and the client is in another repo, so the shared
import that used to stop the two drifting is gone both ways.

No package. Publishing one would add a release step to every field change, to protect
twelve field names.

Two guards instead, one per side:

| Side | Guard |
|---|---|
| Python server | `tests/smoke.mjs` in CI — a JavaScript client on the real wire. A renamed field fails it. |
| MFE client | The spec's types, copied into the MFE, checked by the compiler. |

The smoke test *is* the contract test, and always was. It was written against the Node
server, it never imports server code, and it speaks the protocol in JavaScript's own
naming. **So the action here is to unskip the CI job**, which Track A Step 3 does anyway.

### One thing it changed: a room pins an edition

Writing the spec down surfaced a bug we would otherwise have shipped. `RoomState.textSlug`
is gone, replaced by `editionId` + `segmentationId` + `title`.

Segment ids belong to a *segmentation*, which belongs to an *edition*. The library
resolves a bare text id by taking **the first edition in the list**
(`textDetails.ts`: `editions?.[0]?.id`). That is fine for one person browsing and wrong
for thirty people who must agree on line 7 — nothing promises that ordering stays put for
the life of a room.

The client also verifies its resolved `segmentationId` against the room's and fails
loudly on a mismatch. Silently highlighting nothing is the worse outcome.

## Track B, in order

### Step 1 — A page that does nothing
New route in the MFE, wrapped in `AuthenticatedPageRoute`. Renders "hello".

### Step 2 — Copy the contract types
From [`docs/socket-contract.md`](socket-contract.md), before porting anything that
imported `@wbac/shared`.

### Step 3 — Join and sync, no text
Port `useRoom.ts`, `api.ts`, `JoinScreen.tsx`, `App.tsx`. Add `socket.io-client`. Show the
current segment **id** as plain text.

*Done when:* a teacher clicks, and the id changes on a student's screen. This proves the
whole chain before any rendering work.

### Step 4 — Display the text
Copy the ~700 lines of fetching and alignment logic. Load the whole text on join. Render
segments, highlight the current one, scroll to it.

*Done when:* a class can follow along.

### Step 5 — Stacked translations
Tibetan above, English below, per segment, where an alignment exists. Hide the lower line
when there is none.

### Step 6 — Styles
`.classroom-page` in `brand-openedx`, translating `styles.css` to brand tokens as you go —
`brand.$spacing-6`, not `24px`.

*Done when:* it looks like part of Academy.

### Step 7 — A real class, end to end
Teacher on one device, students on others, through Academy.

---

# Track C — notes

In `sherab-custom-plugin`, following the `wishlist` pattern: `models.py`, `serializers.py`,
`urls.py`, `apps.py` with `plugin_app`.

## Whole-line notes first

A note on a whole line is simple: you already have a reliable, stable id for every line.

Highlighting *part* of a line means storing character offsets, and here that is worse than
usual — in a stacked view, did they highlight the Tibetan or the English? Different texts,
different lengths. And if the pipeline re-segments, every offset silently slides.

**Ship whole-line highlighting and whole-line notes.** It is genuinely useful — *"this
line matters"* — and costs nothing in fragility. Revisit finer granularity only if someone
asks.

## Worth reading first

Open edX already ships **`edx-notes-api`**, running as its own service in this very Tutor
install. It assumes HTML-anchored ranges in courseware rather than segment ids, so it is
not a drop-in — but read it before building a notes service from scratch.

---

# Decommissioning `wb-academy-classroom`

The old repo and its service are deleted once this is done. Two things to get right.

## Rescue before deletion

Already carried into this repo: `PLAN.md`, this plan, `001_init.sql` (since rewritten for
MySQL), `smoke.mjs`, the socket contract (rewritten as `socket-contract.md`),
`infra/native/`.

Still in the old repo and needed by Track B: `apps/web/` — `useRoom.ts`, `api.ts`,
`JoinScreen.tsx`, `App.tsx`, `Reader.tsx`, `styles.css`, and the **woff2 font subsets**.

**Do not delete the old repo until Track B Step 6 is done**, because that is the last
thing that reads from it.

## Delete the service last, not at cutover

1. Cut over to the Python server and the MFE
2. **Teach several real classes**
3. *Then* delete the service and archive the repo

A classroom is a bad place to discover a missed detail, and the old service is the only
way back. **Archive rather than delete** on GitHub — it costs nothing and keeps the
reasoning readable in six months.

---

# What we are NOT doing

Resist all of these until the three tracks are done:

- Changing the protocol
- Sub-line highlighting or sub-line notes — whole-line only, see Track C
- Merging the sync server into `sherab-custom-plugin`
- Porting any part of `chapterV2` beyond the ~700 lines named above
- "While I'm in here, let me also…"

A port that also changes behaviour has no way of telling a port bug from a design change.

# Decisions, settled

- **The socket contract** lives in [`docs/socket-contract.md`](socket-contract.md),
  guarded by the smoke test on one side and the compiler on the other. See above.

- **The reader reaches the library through this service.** The browser cannot hold the
  API key, so something server-side must attach `X-Application` and `X-API-Key` the way
  WeBuddhist's nginx does. `sherab-custom-plugin` could, but this service already needs
  the key to register a text's segment ids — so routing the reader's fetches through here
  too keeps the key in **one** place instead of two.

  Build it as part of Track A Step 2. It is needed before Track B Step 4.

- **The classroom page requires an Open edX login.** `AuthenticatedPageRoute`, and notes
  then have an identity to hang on with no extra work.

  The cost, recorded so nobody is surprised: a student who walks in with only the
  whiteboard code now meets a sign-in wall first. If that bites in a real class, the
  fallback is an anonymous join with notes prompting for login on first use — but that is
  a change to make on evidence, not in advance.

- **Room creation stays on the shared `HOST_PASSWORD`.** No Open edX role check.

  Considered and rejected on purpose, because requiring login made the obvious objection
  weaker rather than stronger: the page is behind `AuthenticatedPageRoute`, so a leaked
  password now means "someone with an Academy account *and* the password", not "anyone".

  It also guards the cheap thing. The shared password lets you *create* a room; it gives
  no power over an existing one. That is the per-room **host token** — generated at
  creation, shown once, stored only as a sha256 hash. Someone who learns `HOST_PASSWORD`
  can make junk rooms and cannot touch a running class.

  The real cost is revocation: removing one teacher's access means changing it for all of
  them. **Revisit when teachers number in the dozens, or when someone leaves on bad
  terms** — that is the tripwire, not a date.

  Keeping it also leaves the smoke test's tested path alone, which is worth something
  during a rewrite.

- **A room takes whatever edition the library lists first.** No edition picker.

  The consistency problem is already solved: the edition is resolved **once, server-side,
  at registration**, and every student uses the one the room pinned. What is deferred is
  only the teacher's *choice* — if a work has three editions, they get the first and are
  not asked.

  For a work with one edition this is invisible, which is most of them. Handle it when a
  real text makes it matter; Track A Step 2 will show how often that is.

- **Secrets follow the existing pattern.** Checked, and the chain is already right: the
  Tutor folder is not a git repo, the plugin's code reads environment variables rather
  than hardcoding, and the real values live in `config.yml`. `docker-compose.yml` looks
  alarming but is *generated* from that — output, not source.

  So `HOST_PASSWORD`, the database password and the library key go in `config.yml`, reach
  the container as environment variables, and `config.py` reads them. The one rule:
  **never type a secret into this repo's own files**, because this repo *is* in git.
