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

The alphabet excludes `I`, `L`, `O`, `0` and `1` — it gets read off a whiteboard from
the back of a room.

**It does not exclude `S`/`5`**, although this document and the contract both claimed it
did until a test checked. Left as it is during the port; revisit knowingly if a real
class struggles with it.

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

### Step 2 — The web routes ✅
Register a text (search the library by title, **resolve it to one edition and
segmentation**, store that segment id list), create a room, look up a room, verify a
recovery code. Also the library proxy — this service holds the API key, so the reader's
fetches come through here too.

**The library and storage halves are done** — `library.py`, `db.py`, 15 tests. Verified
end to end against the live library and Tutor's own MySQL: search → resolve → 1027
segment ids over 3 pages → stored → re-registration is a no-op → a case-flipped id is
correctly *not* found.

`scripts/check_library.py` and `scripts/check_register.py` repeat those checks by hand.
Neither is a pytest: CI must not depend on an upstream service or a Tutor stack, and a
test needing a secret is a test nobody runs.

Five things running it found, which the code and schema now reflect:

| Found | Consequence |
|---|---|
| `title=` empty → **422**, not "list everything" | The param is omitted when blank; a blank search box would otherwise error |
| Segment ids are 21-char mixed-case nanoids, not `s-0001` | The schema's stated reason for its composite key was wrong — corrected |
| All 25 texts sampled had **one** critical edition | The deferred edition picker is rare, not theoretical |
| MySQL 8.4 dropped `mysql_native_password` | `aiomysql[rsa]` is required, or **connecting fails outright** |
| MySQL 8.4 deprecates `VALUES()` in upserts | Switched to the row-alias form, which works from 8.0.20 |

Registration is **idempotent and does not re-sync**. `rooms.current_segment_id` has a
foreign key onto `segments` with no ON DELETE, so deleting a segment a live room sits on
is refused by the database — a re-sync mid-class would fail halfway. Ids change only when
the pipeline re-segments, which is not something to discover during a lesson; a deliberate
re-sync belongs in its own operation, run when no room is live.

**The room routes are done too** — `rooms.py`, `routes.py`, 25 unit tests, and
`scripts/check_rooms.py` (since replaced by the account-based `tests/smoke.mjs`) passing **15/15** against a server running on Tutor's MySQL and
the live library:

```
POST /api/rooms                    create, 401 on a wrong password
GET  /api/rooms/{code}             lowercase finds it
POST /api/rooms/{code}/verify-host 401 wrong, 200 right
GET  /api/texts?title=             live library search
```

Checklist points 1, 2, 3, 7, 11 and 12 are implemented and covered. Verified in the
database: host tokens are stored as 64-character sha256 hashes and the raw token appears
nowhere.

One more thing a test caught: **the room-code alphabet does not exclude `S`/`5`**,
though this document and the contract both said it did. Corrected in both rather than
changing the alphabet — see point 1.

**The library proxy is done** — `proxy.py`, 33 unit tests, `scripts/check_proxy.py`
passing **16/16** against the live library. The reader's whole data path works with no
key in any request: search → editions → segmentation → spans → real Tibetan content.

It is an **allowlist, not a pass-through**, and that is the important part. Our key can
write: the library has **38 paths accepting POST, PATCH, PUT or DELETE**, among them
`DELETE /v2/texts/{id}`, `PATCH /v2/editions/{id}/content` and `POST /v2/applications`.
A proxy that forwarded whatever it was handed would give every one of those to anyone
who could load the classroom page, authenticated as us. So: GET only — other verbs are
not routed and 405 — and only the thirteen paths the reader actually reads. Everything
else is 403 and never reaches the library.

It is otherwise **transparent**, verified byte-for-byte against a direct call. That
matters more than it sounds: the library answers an unknown text's `/editions` with
`200 []` rather than a 404, and `resolveEditionContext` relies on exactly that to tell a
text id from an edition id. A proxy that tidied it into a 404 would break the copied
resolution logic in a way that would take a long afternoon to find.

**Track A Step 2 is done.**

Resolve the edition **once, here**. Never let clients resolve independently: the library
answers a bare text id with its first-listed edition, and thirty students resolving
separately is how a room ends up with ids that do not match.

*Done when:* the smoke test gets as far as creating a room before failing on the socket.

### Step 3 — The live messages ✅
`room:join`, `teach:goto`, `room:close`, `presence`, `room:closed`, in `events.py`,
ported from the Node server's `socket.ts`.

**19/19 on the smoke test**, three runs in a row, against Tutor's MySQL and the live
library. Delivery measured at **35ms**. All thirteen checklist points are now
implemented.

Only the smoke test's *setup preamble* changed — there is no local import step any
more, so it finds a text in the library and takes segment ids through this server's own
proxy, which means it covers that too. **All 19 assertions are byte-identical**,
verified by diff.

One behaviour was missing and the smoke test caught it: **`verify-host` on a closed room
must answer 410, not 200.** A valid recovery code on an ended class should say the class
ended — telling a returning teacher they are the host of a dead room sends them off to
teach into a room nobody can join. `is_live` is checked *after* the token, so nobody
without a credential can probe which classes have ended.

Verified in the database afterwards: every move has a `room_log` row (5 rooms with a
position, 5 with log rows — the transaction held), and closed rooms have both
`is_live = 0` and `closed_at` set.

The CI job is unskipped. It runs MySQL 8.4 with Tutor's case-insensitive collation, so
the schema's `utf8mb4_bin` columns are exercised rather than accidentally agreeing, and
it is gated on the repository's own credentials so a fork's pull request skips it
instead of failing.

**Needs configuring once:** `LIBRARY_BASE_URL`, `LIBRARY_APP_NAME` and `LIBRARY_API_KEY`
as GitHub repository secrets.

#### What a review of this step found

Three bugs the tests had missed, all reproduced live and fixed, each now with a
regression test:

- **A new text stored its own id as its title**, permanently. `post_room` looked the
  title up with `search_texts(textId)` — a search by *title* using an *id*, which never
  matches, leaving the fallback to store the id. Every existing test missed it because
  they reused texts already registered with correct titles, so the broken branch never
  ran. They were checking the cache, not the code. Now `fetch_text(id)` hits
  `GET /v2/texts/{id}`.
- **A closed room could still be driven.** `teach:goto` checked the room and the token
  but never `is_live`, so a teacher still connected after `room:close` kept moving an
  ended class — broadcasting, writing a position and growing the log, on a room that
  refused newcomers. The Node original had the same gap, so the port was faithful and
  still wrong. Checklist point 13 now actually holds.
- **`room:close` re-broadcast on an already-closed room.** Now answers `ok` without
  emitting again, so a client treating `room:closed` as a one-shot transition sees it
  once.

And one landmine, not yet live: the proxy streamed `aiter_raw()` and forwarded
`content-length` while dropping `content-encoding`. httpx asks for gzip on every
request; the library does not compress *today*. The day it does, or a CDN is put in
front of it, every proxied response would reach the browser as compressed bytes
labelled `application/json`.

Left open deliberately: **the proxy has no authentication**, so anyone who can reach the
service can read the library on our key. Nothing can be written — the allowlist sees to
that — but this is the same question as "does the classroom page's login protect the
API", and it belongs with that decision at Track B Step 3, not patched quietly here.

### Step 4 — Two browsers, by hand
A teacher and a student on two *different* origins — `localhost` and `127.0.0.1`. They
must differ, or they share `localStorage` and you do not have a real teacher and a real
student.

*Done when:* it feels instant, and a reconnect lands on the current line.

### Step 5 — Tutor plugin ✅
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
| `local-docker-compose-dev-services` | mounts `src/` and widens `WEB_ORIGIN` in `tutor dev` |
| `caddyfile-mfe-proxy` | `/classroom-api/*` on the MFE host |

#### `tutor dev` locally, `tutor local` on the server

That is the convention here, and the plugin must serve both. It does, because
**`tutor dev` overlays `tutor local`**: `local-docker-compose-services` lands in
`env/local/docker-compose.yml`, which dev also reads, and
`local-docker-compose-dev-services` adds only the differences on top. Verified —
`studio-tool-service` appears in both files, with the dev one adding nothing but a
volume.

Two things differ in dev, and both need the plugin's dev patch:

- **Mount the source.** As `studio_tool_service.py` does, so an edit needs a container
  restart rather than an image rebuild.
- **Widen `WEB_ORIGIN`.** In dev the extension MFE serves itself on **port 2003**, so
  the classroom page is at `apps.local.openedx.io:2003` while Caddy answers on
  `apps.local.openedx.io` with no port. **Those are different origins**, so the
  same-origin argument above holds in production and not in development. The service
  already reads `WEB_ORIGIN` for both CORS and the Socket.IO origin check; the dev
  patch must add the port form. `extension_mfe.py` already does the equivalent for
  Studio's `CORS_ORIGIN_WHITELIST` — same problem, same shape.

Do not run `tutor local start` on a development machine: Tutor will stop the dev
containers to start the local ones. They share a data volume, so nothing is lost, but
the stack goes down.

*Done.* The plugin is [`infra/tutor/classroom.py`](../infra/tutor/classroom.py) in this
repo, copied to
`~/Library/Application Support/tutor-plugins/classroom.py` and enabled. Verified under
`tutor dev`: **19/19 smoke, 15/15 routes, 16/16 proxy** against the Tutor-managed
container, using Tutor's own generated `HOST_PASSWORD` and database password. Editing
`src/` on the host and running `tutor dev restart classroom` picks the change up with no
rebuild.

Two things only running it revealed:

- **`tutor dev` does not run Caddy.** It is declared in `docker-compose.prod.yml`, which
  only `tutor local` loads. So `/classroom-api/*` does not exist in dev and the browser
  cannot reach the service at all unless its port is published. The dev patch publishes
  `CLASSROOM_DEV_PORT` (8010, clear of the LMS on 8000 and the MFEs on 1995–2004), and
  `CLASSROOM_API_URL` points there in development and at the Caddy path in production.
  This corrects the earlier claim in this document that the same-origin argument held
  everywhere: **it holds under `tutor local` only.**
- **An init task needs a `<service>-job` container**, declared through
  `local-docker-compose-jobs-services`, or `tutor do init` fails with
  `no such service: classroom-job`. Its environment is shared with the service's from
  one definition, so a migration cannot be applied to a different database than the
  service reads.

The schema is applied by `python -m classroom_sync.migrate` rather than a mysql client,
because the image is `python:3.12-slim` and has none. It is safe on every init, which is
what `create table if not exists` throughout buys — the warnings MySQL raises for
already-existing tables are suppressed, since four of them per run reads as a problem
when it is the normal case.

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

### Step 1 — A page that does nothing ✅
Route `/classroom` in the MFE, wrapped in `AuthenticatedPageRoute`.

**Routes live at the root; `/extension/` is only the asset path.** So the page is
`apps.local.openedx.io:2003/classroom`, not `/extension/classroom` — the same shape as
the existing `/wishlist`.

### Step 2 — Copy the contract types ✅
`src/classroom/types.ts`, copied from [`socket-contract.md`](socket-contract.md) with a
header saying what it is: a hand-kept copy of a spec in another language, where a rename
on either side breaks nothing at build time and everything at class time.

### Step 3 — Join and sync, no text ✅
`useRoom.ts`, `api.ts`, `JoinScreen.tsx`, `ClassroomPage.tsx`, plus `socket.io-client`.
Shows the current segment **id** as plain text — deliberately, because that proves the
whole chain before any rendering work exists.

**Verified against the live service** by `useRoom.live.test.tsx`, four tests, skipped
unless `CLASSROOM_API_URL` is set:

```
✓ joins and receives the room state (81 ms)
✓ a teacher move reaches a student (115 ms)
✓ a student without the host token cannot move the room
✓ a wrong room code is rejected rather than left hanging
```

That test is the only thing checking the two hand-kept copies of the contract still
agree, so it earns its keep. Three things were needed to make it run, all recorded in
its docblock:

- `customExportConditions: ["node"]` — without it jsdom resolves socket.io-client's
  browser build and **no connection is attempted at all**, silently.
- A pinned jsdom `url` — the default `http://localhost` has no port, is not in the
  service's `WEB_ORIGIN`, and every request fails CORS before reaching an assertion.
- XHR rather than `fetch` for its setup, since jsdom does not expose Node's `fetch`.

*Done when:* a teacher clicks, and the id changes on a student's screen. This proves the
whole chain before any rendering work.

**Decide here whether this service validates the login.** `AuthenticatedPageRoute` guards
the React route, not the endpoint — nothing stops a direct call to `/classroom-api/*`, so
"joining requires login" is currently a property of the UI. It is a choice rather than a
blocker: the session cookie's domain is `local.openedx.io`, which browsers also send to
`apps.local.openedx.io`, and our route is on that host, so the credentials do arrive.
See [CONTEXT.md](CONTEXT.md).

### Step 4 — Display the text ✅
`text.ts` loads it, `useText.ts` holds it, `Reader.tsx` renders it. The whole text at
once when the class is joined — which is what makes everything else simple: no paging,
no fetching as you scroll, and a student whose network drops keeps reading.

**Far less than the ~700 lines this step budgeted.** The alignment logic is Step 5's;
what Step 4 needs from the reader is two details, and both are kept with their reasons:

- **Join a segment's lines with `\n`, not `''`.** The edition's content carries no line
  breaks of its own — the spans are the only record of where they fall, and they run
  edge to edge. Joining with `''` collapses a four-line verse into one run-on line,
  which looks plausible and is wrong.
- **Slice by code point, not by UTF-16 unit.** The library computes offsets in Python,
  where string indices are code points. That matches JavaScript only while the text
  stays inside the BMP, and nothing guarantees it will.

One thing deliberately *not* copied: the reader slices per line with `Array.from(...)`,
which is correct but O(n) each time — fine for a window of sixty lines, quadratic for a
thousand. Loading a whole text at once asks for the latter, so the content is split into
code points **once**, and each line slices that array.

Verified against the live library (`text.live.test.ts`): every line present, in order,
all distinct, multi-line segments keeping their breaks. `Reader.test.tsx` covers the
rendering with fixtures, in the normal suite.

**A teacher's line is a `<button>`; a student's is a `<p>`.** Not a lint workaround — a
line the teacher can press is a control and a screen reader should say so, while a
student's line is prose and calling it a button would be a lie.

*Done:* a class can follow along. 23 tests pass across the MFE's own suites.

### Step 5 — Stacked translations ✅
Tibetan above, English below, per segment, where an alignment exists. The line beneath is
simply absent where there is none.

How it works, decided with the teacher in mind (they pick the Tibetan text and nothing
else):

- **The server pins the translation when the room is created**, the way it pins the
  source edition, and for the same reason: the library does not promise that "the first
  translation" stays the same one for the life of a class. `LibraryClient.resolve_translation`
  takes the first of the text's `translations` whose first critical edition has an
  alignment to the source, in either direction, and skips any that do not. Stored in
  `room_translations` (migration 002 — a table, not a column, because MySQL has no
  `add column if not exists` and every migration re-runs on `init`).
- **No translation is not an error.** Many texts have none; the room then shows the
  source alone, and a failed lookup at creation does the same rather than refusing to
  teach.
- **The browser reads the pairs.** `text.ts` `loadTranslations` fetches every alignment
  page, forward first then reversed (the library stores a pair in one direction only),
  loads the translation edition's text, and builds `source segment id -> English`. The
  server still handles ids and nothing else, and only a source segment id travels over
  the socket. The allowlist already contained the alignments path.
- **A translation that fails to load does not take the class down.** `useText` falls
  back to the source alone.
- **Deliberately not ported:** WeBuddhist's walk through a shared ancestor text for
  editions with no direct alignment. The server only pins a directly aligned translation,
  so the case cannot arise. If a class ever wants such a pair, that is the place to add it.
- **Not built:** a way for the teacher to choose between several translations. Today it is
  always the first aligned one.

Verified live against the real library through the proxy: the Bodhicaryavatara and its
first translation line up, every mapped key is a real source segment id
(`translations.live.test.ts`). The Bodhicaryavatara's alignment is stored
translation-to-source, which is exactly the case the reverse fallback exists for.

### Step 5b — Reading options and table of contents ✅
A header menu of personal reading preferences, **none of them synced** (the segment id
names the same line however it is displayed): show Source / Translation / both, Prose or
Segmented layout, and Table of Contents Show/Hide. Remembered per browser.

The table of contents is two things, both from `GET /v2/editions/{id}/table-of-contents`
(already in the proxy allowlist): section titles drawn into the text where each begins,
and a **Contents** menu to jump to one. A teacher's jump also moves the class there (a
1,000-line text cannot be taught by scrolling); a student's jump only moves their own
view, so looking ahead drags no one.

- The library gives a section as a character span; the class moves by segment id.
  `outline.ts` bridges them using WeBuddhist's rule (commit 80ed57cd): a segment that
  began before the span but runs into it counts, and an **empty span means a position,
  not a range**, so it anchors to the first segment at or after it.
- Most of the library has no outline. Such a text shows no contents menu and costs no
  segment scan, and a failed read is treated the same, so the contents can never stop a
  class from starting.
- Checked live: the Bodhicaryavatara's chapters all land on real segments, in reading
  order (`translations.live.test.ts`).

## Standing classes (accounts, members, months-long rooms)

**Decided with the product owner.** A class runs for months in one room, with the same
students. Settled:

- **One text per class.** No "change text" yet; saved positions are tied to one text.
- **Members, with a member list.** The teacher can see who is in the class, remove a
  member, and change the class code (a code that stays valid for months is a weak secret).
- **No co-teachers.** The account that created the class is its teacher.
- **Login is required** to join or teach. A person that cannot be identified cannot be
  remembered. The shared host password stays, and only *creates* a class.
- **"End class" splits in two.** *End session* (routine; the room and everyone's
  membership stay, and the position is saved) and *Archive class* (rare; the course is
  over).

### The login check ✅ (step 1)
`src/classroom_sync/auth.py`. Open edX keeps a signed JWT for each logged-in browser in
two cookies (`edx-jwt-cookie-header-payload`, and `edx-jwt-cookie-signature`, which is
HttpOnly so a script can never read it). The service joins them and verifies the
signature against the platform's **public** key. It never calls the LMS and holds nothing
secret.

Things found by checking a token the real LMS issued, not by assuming:

- **Cookie tokens are RS512 and carry no `kid`.** A check pinned to RS256 with a required
  `kid` refused every real login. Algorithms are now pinned to RS512 and RS256 (never
  HMAC or `none`); a token naming an unknown `kid` is refused; a keyless token is tried
  only against the platform's own key.
- **`sub` is the identity**, not the username: opaque and unchanging. The username and
  name only travel along to be shown in a member list.
- **Cookies are only a credential when the request opts in** with `USE-JWT-COOKIE: true`,
  the header Open edX's own frontend library adds. A hostile page can make a browser send
  cookies; it cannot add that header without a CORS preflight we refuse.
- The public key reaches the container through the Tutor plugin, computed from
  `JWT_RSA_PRIVATE_KEY` the same way Tutor does for the LMS. The private key never does.

Verified live: a token minted the way the LMS mints a login cookie is accepted
(`GET /api/me` 200); cookies without the opt-in header, a tampered signature, and an
edited payload are all 401.

**Two things this changes for the browser, still to do.**
1. The cookies are scoped to `local.openedx.io`, so they are never sent to
   `http://localhost:8010`. In dev the API must be reached at
   `http://local.openedx.io:8010`, and `WEB_ORIGIN` must list
   `http://apps.local.openedx.io:2003` (credentials need an explicit origin, not `*`).
   In production the API must sit under the same parent domain as the LMS.
2. Socket.IO cannot add the `USE-JWT-COOKIE` header, and the signature cookie is
   unreadable, so the socket handshake must read the cookies itself, guarded by the
   origin check Socket.IO already does.

### Members and ownership ✅ (step 2, server)
Migrations **003** (`rooms.name`, `rooms.host_user_id`) and **004** (`room_members`).
Because 003 is an `ALTER`, which cannot be written to be re-runnable, `migrate.py` now
**records each applied file in `schema_migrations` and applies it once**. Files 001 and
002 predate that table, are idempotent, and are simply run once more and recorded.

All of these need a login; the account always comes from the verified token, never from
the request:

| Route | Who | What |
|---|---|---|
| `POST /api/rooms` | any login + host password | creates a class; the creator is its teacher and first member, in one transaction; optional `name` |
| `GET /api/me/classes` | any login | the account's classes, most recently visited first; archived ones left out |
| `POST /api/rooms/{code}/join` | any login | records membership (the one step that needs the code); idempotent; role is never changed by joining; 410 if archived |
| `GET /api/rooms/{code}/members` | teacher | member list, teacher first |
| `DELETE /api/rooms/{code}/members/{id}` | teacher | removes a student; the teacher cannot be removed |
| `POST /api/rooms/{code}/leave` | member (student) | the student steps out: stamps `room_members.left_at` (migration 008); the class stays in their list and `POST .../join` rejoins it with no typing; notes kept; the teacher cannot leave |
| `PUT /api/rooms/{code}/dates` | teacher | the first and last day the class is held (`startsOn`, `endsOn`, either may be null); shown, never closes the class (migration 009). Also accepted, optionally, when creating a class |

The library proxy keeps successful answers in memory for `LIBRARY_CACHE_SECONDS` (default 600; 0
turns it off) and shares one in-flight request between everyone asking for the same thing, so a
class of 50 opening the same text costs the library one request, not fifty. Bounded (16 MB an
entry, 96 MB in all, least recently used out first); errors are never kept.

Also, since the review: the library proxy (`/library/...`) and `GET /api/texts` need a login;
`GET /api/rooms/{code}` and `POST .../join` need one too and are rate-limited per account
(30 attempts a minute, `ratelimit.py`) because a class code is what lets someone in; a note's
offsets must match its quote, and the per-student note cap is enforced inside the insert; the
ended-session flag is stored (`007_session_ended.sql`) and a live connection is dropped when its
login expires (`auth:expired`).
| `POST /api/rooms/{code}/change-code` | teacher | new code, old one stops working at once, members unaffected |
| `POST /api/rooms/{code}/archive` | teacher | no one can join; leaves everyone's list |

Decisions worth keeping:
- **A room made before accounts has no owner and nobody can manage it.** Guessing an
  owner would hand it to the wrong person. `is_host` is explicit about this rather than
  leaning on `None == id` being false.
- **403 for "not your class", 404 for "no such class".** Telling them apart leaks only that
  a code exists, which `GET /api/rooms/{code}` already says.
- Members hang off the **room id**, not the code, which is why changing the code does not
  disturb anyone already in.

Verified live with two real LMS accounts (A creates, B joins): create without login 401;
the lists; B refused (403) on members, change-code and archive; remove then rejoin; the
old code 404 after a change while B still sees the class under the new one; archive then
B's join 410 and the class gone from B's list. The smoke test still passes 19/19 (it now
takes `AUTH_TOKEN`; `scripts/make_test_login.py` makes a throwaway login with no LMS, and
CI uses it, with every migration instead of only `001`).

### The socket on accounts ✅ (step 3)
`events.py`, rewritten. A socket is an account:

- **Connecting needs a login**, by cookie from a browser (the `Origin` must be one of ours,
  checked by Socket.IO and again by the service) or `auth: {token}` for a script. Refusals
  carry a reason in `connect_error`.
- **`room:join` needs membership.** The code is entered once over HTTP; after that the
  account is the credential, and a removed member is refused however often they reconnect.
- **The teacher is the room's owner.** `teach:goto` and the new `session:end` are checked
  against the verified login on every message. The host token, the recovery code, the
  `verify-host` route and `room:close` are **gone**; migration 005 makes the old column
  nullable and nothing writes it.
- **Presence counts people, not tabs**, and carries `teacherHere`.
- **The HTTP routes act on live rooms.** Removing a member drops them from the live class
  on every tab (`member:removed`); archiving tells everyone (`room:closed`). Socket rooms
  are keyed by the room **id**, so changing the code strands nobody.

Things found while building it:
- **`tests/test_socket.py` first passed every refusal test with the socket *client* library
  not installed**, because "connecting failed" was all it asserted. Refusal tests now
  assert the server's stated reason, and `aiohttp` is a dev dependency.
- **Socket.IO is not thread-safe.** A test that emitted from its own event loop heard
  nothing; server-side calls must run on the server's loop, as the HTTP routes do in
  production.
- **A test fixture that replaced `api.state.live` and never put it back** made a later
  test talk to a fake. Shared state is restored now.

Verified: 155 tests, including real socket clients against a real server; and the smoke
test, now **29 checks over four accounts** (teacher, student, latecomer, outsider), run
against the live Tutor stack with tokens the real LMS issued, **29/29**.

### The reader ✅ (step 4)
In `frontend-app-extension/src/classroom`, with styles in `brand-openedx`.

- **A login on every call.** `api.ts` uses the platform's authenticated client, opting out
  of the CSRF-token lookup (this service has no such endpoint). The socket connects with
  `withCredentials` after an ordinary request has refreshed the login cookie, since a
  socket cannot refresh it itself. The page was already behind `AuthenticatedPageRoute`.
- **The dev API moved to `http://local.openedx.io:8010`** (was `localhost`), in both the
  reader's fallback and the Tutor plugin. The login cookies are scoped to
  `local.openedx.io`, so a service on `localhost` never sees them.
- **The code screen is "Your classes".** Each class is a card (name, a Teaching/Student
  pill, member count) that opens in one click with no code. Below it, a code field that
  records membership then enters. "Start a class" takes a text, an optional class name and
  the host password, then shows the code for the board. The recovery-code screens, the
  "Take back control" flow and the "Return to your class" link are gone.
- **Inside a class:** the class's own name with the text's title beneath; the teacher sees
  the code, **Manage class** and **End session**; a student sees **Waiting for the teacher**
  while `teacherHere` is false, and may read ahead. The teacher has no follow switch.
- **Manage class** (a modal): the code with **Change the code**, the member list with
  **Remove**, and **Archive this class** behind a confirmation. A changed code is followed
  by the page so a refresh finds the class.
- **Three ways a class stops being live, said plainly:** the session ended (the class is
  kept, so a refresh still finds it), archived, or removed. Archived, removed and refused
  joins forget the remembered class.

Verified: the unit suite (the page, the hook against a stand-in socket, the HTTP client,
the code screen and the manage tools), and live tests against the running service with
tokens the real LMS issued, driving the hook and the code screen as a teacher and a
student: joining, the role, `teacherHere`, a teacher move reaching a student, a student
being refused, a wrong code, ending a session, and joining once then finding the class in
the list. A browser-shaped request (preflight, then the real call with the login cookies
and the opt-in header, from the MFE's origin) is answered correctly, a page on another site
is refused, and a socket handshake with the cookies succeeds.

**Not verified: a real browser.** The browser pane cannot open `openedx.io` hosts, so the
page has not been seen running against a real login cookie. That is the first thing to try
in `tutor dev`. If it fails, check in this order: the cookie is present for
`local.openedx.io` (DevTools, Application), the API is being called at
`http://local.openedx.io:8010`, and the response carries
`access-control-allow-credentials`.

### Notes ✅ (students only, for now)
A student selects words, a small bar offers four colours and "Add note", and the words stay
highlighted from then on. Private, kept for months, and the same on every device.

**Decided with the product owner:** private notes; four colours (yellow, green, blue,
pink); highlight *words*, not whole lines; students only for now (teachers later); teacher-
shared highlights later; export later; notes **kept** when a member is removed or the class
is archived.

**How a note is pinned.** By the line (segment id), the text on it (source or translation),
and where inside *that text* it begins and ends, counted in code points from the start of
the line's own text and not of the page, so it lands on the same words on any screen, font
or layout. The selected words and 24 characters either side are kept too: if the library
ever edits a line the saved position stops matching, and they are how the words are found
again (the best-matching occurrence wins when the words repeat). If they cannot be found
the note is listed as "text changed" rather than drawn on the wrong words.

**Server** (`notes.py`, migration 006, routes under `/api/rooms/{code}/notes`): list,
create, change colour/words, delete.
- **Students only, in one place** (`NOTES_ROLE`), so allowing teachers is one line.
- **Private:** every query is for the verified account *and* the class; a guessed note id
  reaches nothing (a "not found", which is also all anyone else's id ever says).
- **Kept, not lost:** no foreign key to membership, so a removed member's notes stay in the
  database, are unreachable while they are out, and are back if they rejoin. An archived
  class can still be read but not written (410).
- **Nothing malformed is stored:** colour and layer from fixed lists, sane offsets, the
  line must belong to the class's text, length caps on quote/context/words, and a cap on
  how many a student can keep. What a note may change is its colour and words, never where
  it is.

**Reader.**
- `anchors.ts` is plain functions with plain tests: snapping a selection to whole words
  (Tibetan to whole *syllables*, never starting or ending on the tsek, never cutting a stack),
  building an anchor, finding words again, and cutting a line into pieces so overlapping
  highlights both show. Offsets come from the layer element's own text, which highlights
  never change (they add elements, not characters).
- A selection is usable only if it sits inside one line's one text; one that crosses two
  lines, or the Tibetan and its English, is refused for now.
- A bar over the selection on a laptop; **along the bottom on a phone**, where it will not
  sit on the phone's own Copy menu. One press on a colour saves a plain highlight; "Add note"
  opens a dialog for the words. A highlight with words is dotted-underlined. Pressing a
  highlight opens its note, but letting go of a *drag* over one does not.
- **Following the teacher waits** while a student has words selected or a note open, so the
  screen does not move under them.
- **My notes** (a header button): every note in reading order, jump to its line, edit.
- A teacher sees none of it: no highlights, no bar, no button, no fetch. Their tap still
  moves the class and nothing competes with it.

**Verified:** 194 service tests; the live API against the running service with real LMS
accounts (a teacher refused, an outsider refused, bad lines/colours/ranges refused, a second
student seeing nothing and unable to change or delete by id, removal hides and rejoin
restores, archive allows reading but not writing, and the rows are still in MySQL after both);
the reader's unit tests including the anchoring cases that break naive versions; and the
hook against the live server as three accounts. The styles were checked in a preview at
laptop and phone width.

**Not verified:** a real browser and a real finger. Selecting Tibetan with a long-press and
the handles on an actual phone is the riskiest part and needs trying on a device: whether the
bottom bar stays clear of the phone's own menu, and whether a selection survives a teacher's
move (the auto-pause is there to protect it).

### Remaining
5. **Notes later:** teachers' own notes; teacher-shared highlights; export; a highlight that
   spans two lines.
6. **Proxy authentication.** The library proxy is still open to anyone who can reach it;
   with logins in place it can now require one.
7. **A real class, end to end,** on the server (`tutor local`).

### Step 6 — Styles ✅
A `.classroom-page` / `.classroom-reader` section in
`brand-openedx/paragon/_extension.scss`, beside `.wishlist-page`. Brand tokens
throughout — `brand.$spacing-6`, never `24px`.

Three decisions worth keeping:

- **Jomolhari, not the UI face.** The brand sets `$font-family-course-content` aside for
  content rather than chrome, and this is a work being read, not an interface being
  used. `$font-family-brand-ui` (Noto Serif Tibetan) stays for the page around it.
- **`line-height: 1.95`**, far looser than the brand's body. Tibetan stacks vowel signs
  above and below the base letter, and at a Latin line-height the lines collide.
- **The current line is a sand band with a saffron rule in the gutter**, not orange
  text. It reads from the back of a room without the highlight fighting the text.

Looking at it rendered caught a bug no test would have: `width: 100%` with negative
margins **shifts** a box rather than widening it, so the highlight sat twelve pixels
left of the column and stopped twelve pixels short on the right. Measured at
`bleedLeft: 12, bleedRight: -12`; fixed with `width: calc(100% + #{$spacing-3 * 2})` and
re-measured symmetric.

Checked at 900px and at 375px: no overflow either way, the reader clamping to 19px on a
phone, the header stacking. **Tibetan wraps on its own** — browsers break at the tsek
(U+0F0B), verified with a 238-character line falling onto three visual lines.
`overflow-wrap: break-word` is there only for a run with no tsek in it.

*Done:* it looks like part of Academy.

### The teacher had no way in

Steps 1–6 built the student's path and the API a teacher needs, and no screen to reach
it: a room could only be created with `curl`, and a teacher could only become one by
pasting into `localStorage`. The old app's `JoinScreen` had **four** modes and only one
was ported.

The other three are now in: **start a class** (search the library live, pick a text,
host password), **your class is ready** (the room code for the board, the recovery code
shown once), and **take back control** (room code plus recovery code, checked against
the server before it is trusted). Covered by `JoinScreen.live.test.tsx` against the
running service — the whole path, plus a wrong password and a wrong recovery code both
being refused.

The text picker is the one real departure from the original: there is no local
catalogue to list any more, so it is a live library search.

#### The flaky test was a real server bug

Running the client tests together made `useRoom.live` fail intermittently where it
passed alone. The diagnosis took three wrong turns — accumulated sockets, then a
connection leak (measured: there is none, 0 → 8 → 0), then parallelism — before the
server said plainly what was wrong:

```
DIAG code= 9VJ6AE rejected= true error= No room with that code.
```

The room was in the database. **MySQL defaults to REPEATABLE READ, and the pool ran
with `autocommit=False`,** so every plain SELECT implicitly opened a transaction that
nothing committed. The connection went back to the pool holding its snapshot, and the
next read on it could not see anything written since.

In a class that is a student typing a valid code and being told the room does not
exist — intermittently, decided by which pooled connection they happen to get. Nothing
in the test suite or the smoke test would have caught it, because both are short enough
that the pool never cycles.

Fixed by turning autocommit **on**. Atomicity is unaffected: every write path opens its
transaction explicitly with `conn.begin()`. A test now asserts that, structurally,
because a write that forgets `begin()` under autocommit fails silently and partially.
The test that asserted `autocommit is False`, with reasoning that was backwards, now
asserts the opposite and says why.

Looking at the rendered screen caught one thing no test would have. The room code was
set in the display face, **Cormorant Garamond, which uses old-style figures** — so `7`
and `2` dropped below the baseline and read as lowercase letters. On a code chosen
character by character so it cannot be misread from the back of a room, that undoes the
whole point. It is now the UI face with `lining-nums tabular-nums`.

#### What the review of the client found

Six issues, all reproduced, five fixed:

- **A second search kept the first choice.** Searching again after picking a text left
  `textId` set, so Start created a room for a text no longer on screen, with nothing
  highlighted to say so.
- **Four of five structural kinds could never match.** The styles used the old
  classroom app's vocabulary — `title`, `homage`, `outline`, `colophon` — where the
  library returns `front_matter`, `back_matter`, `verse` and `paragraph`. Checked
  across three texts. Forty lines of CSS that looked like a feature and were inert,
  and the reader's own test fixtures were asserting against the invented names too.
  `kinds.test.ts` now reads the stylesheet and fails on either half of the mismatch.
- **Students could not stop following.** `follow` was a literal `true`, so anyone who
  scrolled back to re-read was pulled forward on the teacher's next tap. The ported
  app had this control; it was dropped. Restored as a switch in the header.
- **A missing `CLASSROOM_API_URL` fell back to localhost silently.** In production
  that points every student's browser at their own machine, failing for a reason
  nothing explains. It now throws — except in development, where the fallback is
  genuinely load-bearing, since webpack reports `env.config (ignored)` and the value
  really is undefined under `tutor dev`.
- **`room:state` was specified and never used.** The contract called it the snapshot
  sent on join and reconnect; neither side ever sent or handled it. **Removed from the
  contract and the types** rather than implemented: the `room:join` ack already
  carries the snapshot, a reconnect re-emits `room:join`, and one mechanism is easier
  to keep in step than two. Both documents now say so plainly.
- A `localStorage` read on every render, memoised while the file was open.

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

  For a work with one edition this is invisible. **Checked against the live library:
  all 25 texts sampled had exactly one critical edition**, so this is rare rather than
  theoretical. Handle it when a real text makes it matter.

- **Secrets follow the existing pattern.** Checked, and the chain is already right: the
  Tutor folder is not a git repo, the plugin's code reads environment variables rather
  than hardcoding, and the real values live in `config.yml`. `docker-compose.yml` looks
  alarming but is *generated* from that — output, not source.

  So `HOST_PASSWORD`, the database password and the library key go in `config.yml`, reach
  the container as environment variables, and `config.py` reads them. The one rule:
  **never type a secret into this repo's own files**, because this repo *is* in git.
