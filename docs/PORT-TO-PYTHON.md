# Porting the classroom app

There are **two ports**, and they do not depend on each other:

| Track | From | To |
|---|---|---|
| **A — the server** | Node (Fastify + Socket.IO) | Python (FastAPI + python-socketio) |
| **B — the frontend** | `apps/web` (standalone Vite app) | `frontend-app-extension` (the MFE), styles in `brand-openedx` |

They're independent because **the protocol doesn't change**. The Python server talks to
the old frontend; the new MFE talks to the old server. Either track can go first, and
they can run in parallel by different people.

Most of this document is Track A, because it's the bigger one. Track B is near the
bottom.

## Why we're doing this

**The server**, because notes, highlights and anything tied to a student's identity will
be written in Python, inside the Open edX plugin. Running two backend languages for one
product is a cost we pay forever.

**The frontend**, because students reach this through WeBuddhist Academy. It has to live
in the Open edX MFE, not as a separate site they're sent off to.

## What this is NOT

**This is not a redesign.** Everything already decided stays decided:

- Only a segment id travels between teacher and student. Never scroll positions.
- The teacher's authority is checked on the server, not hidden in the UI.
- No student accounts. A room code and a nickname.
- Rooms read from our own database, never from an upstream API mid-class.

We are changing the language. Nothing else.

## The one thing that makes this safe

There is a Python library called **`python-socketio`** that speaks the exact same
protocol as the Node server does today.

That means **no frontend code changes because of the server port.** Whichever frontend
is pointed at it — today's `apps/web`, or tomorrow's MFE — connects the same way, sends
the same messages, gets the same replies. It cannot tell the difference.

(The frontend *is* moving, but for its own reasons, on its own schedule. That's Track B.)

So if we get this wrong, we find out immediately, and we can switch back by pointing the
frontend at the old server.

---

# Track A — the server

## How big is this really?

The whole server is **698 lines**, including comments. Here is all of it:

| File | Lines | What it does |
|---|---|---|
| `index.ts` | 167 | The web routes — list texts, create a room, check a recovery code |
| `rooms.ts` | 130 | Room codes, host tokens, moving the room, ending a class |
| `import.ts` | 106 | Loading a text into the database |
| `socket.ts` | 109 | The five live messages |
| `migrate.ts` | 56 | Setting up the database tables |
| `providers/fixture.ts` | 59 | Reading sample texts from disk during development |
| `db.ts`, `config.ts` | 71 | Database connection and settings |

This is a small server. A careful rewrite is a few days, not a few weeks.

## What we'll build it with

| Part | Now | After |
|---|---|---|
| Web routes | Fastify | **FastAPI** |
| Live messages | Socket.IO | **python-socketio** |
| Database | Postgres (`pg`) | **Postgres** (`asyncpg` or SQLAlchemy) |
| Running it | systemd | systemd (unchanged) |
| Proxy | Caddy | Caddy (unchanged) |

Same database. Same server. Same Caddy config. Only the program in the middle changes.

### Why not Django?

We considered putting this in `sherab-custom-plugin` alongside the notes work. We can't.
Open edX runs under uWSGI, which cannot hold connections open — and live sync needs
connections held open all lesson. Confirmed in our own Tutor config:

```
env/build/openedx/settings/uwsgi.ini
  wsgi-file = $(SERVICE_VARIANT)/wsgi.py
```

So this stays a **separate program**, like Open edX's own notes service already is. It
just becomes a Python one.

---

## The five live messages

This is the whole protocol. Everything else is ordinary web requests.

| Who sends it | Message | What it carries |
|---|---|---|
| anyone → server | `room:join` | room code, nickname |
| server → that person | `room:state` | which text, which line we're on, how many present |
| teacher → server | `teach:goto` | segment id, host token |
| server → everyone | `teach:goto` | segment id |
| server → everyone | `presence` | how many people are in the room |
| teacher → server | `room:close` | host token |
| server → everyone | `room:closed` | — |

---

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

**We already have the test.** `apps/server/scripts/smoke.mjs` checks every promise Phase 1
makes: only the host can move the room, moves arrive promptly, a late joiner lands on the
current line. Eleven checks, all passing against the Node server.

And here's the good part: **that test talks to the server from outside.** It makes web
requests and opens a socket. It never imports any server code.

So it tests the Python server **completely unchanged**:

```bash
API_URL=http://localhost:8000 npm run smoke -w @wbac/server
```

That is the acceptance criterion for this entire port. **11/11 against the Python server,
and we're done.** No new test to write, no argument about whether we're finished.

---

## Track A, in order

### Step 1 — Stand it up empty
New Python service. FastAPI with one health route. Connects to the same database. Runs
under systemd behind the same Caddy config.

*Done when:* `/api/health` answers.

### Step 2 — The web routes
List texts, fetch one text, create a room, look up a room, verify a recovery code. No
live messages yet.

*Done when:* the smoke test gets as far as creating a room before failing on the socket.

### Step 3 — The live messages
`room:join`, `teach:goto`, `room:close`, `presence`, `room:closed`. Work through the
13 points above as a checklist.

*Done when:* **11/11 on the smoke test.**

### Step 4 — Two browsers, by hand
Open a teacher and a student on two different origins (`localhost` and `127.0.0.1` — they
must be different, or they share `localStorage` and you don't have a real teacher and a
real student). Click lines. Watch them land.

*Done when:* it feels instant, and a reconnect lands on the current line.

### Step 5 — Import and migrations
Port the import command and the database migrations.

*Done when:* a fresh database can be set up and a text loaded with Python alone.

### Step 6 — Switch over, keep the old one
Point the frontend at the Python server. **Leave the Node server installed and runnable**
for the first few real classes. If something goes wrong in a classroom, we change one
URL and we're back.

*Delete the Node server only after a few classes have run clean.*

---

---

# Track B — the frontend

The classroom client moves out of `apps/web` and into **`frontend-app-extension`**, the
Open edX MFE, as a new page alongside `/wishlist` and `/start-campus`.

## How big is this one?

Smaller than the server. **1,280 lines, and half of that is CSS:**

| File | Lines | What it is |
|---|---|---|
| `styles.css` | 562 | Nearly half the total — becomes SCSS in `brand-openedx` |
| `JoinScreen.tsx` | 254 | Enter a code, pick a name |
| `App.tsx` | 126 | Holds it together |
| `useRoom.ts` | 122 | The socket connection and room state |
| `api.ts` | 71 | Fetching texts |
| `Reader.tsx` | 66 | The reader — temporary, see below |
| `fonts.css` | 52 | Tibetan webfont — **delete, already in `brand-openedx`** |

About **650 lines of actual React.**

## The good news

**The versions already line up.** This is the thing I expected to be a problem and isn't:

| | `apps/web` | the MFE |
|---|---|---|
| React | 19 | 18 |

That looks like a mismatch, but the code only uses `useState`, `useEffect`, `useRef`,
`useCallback` and `StrictMode`. **Nothing React 19 introduced.** It runs on 18 unchanged.

And the dependency list is almost nothing: `react`, `react-dom`, `socket.io-client`.
That's it. No router, no state library, no UI kit to reconcile.

**So the MFE needs exactly one new dependency: `socket.io-client`.**

## The three real pieces of work

### 1. The contract has to find a new home

`packages/shared` holds the socket contract, and the repo's own README says why:

> imported by both client and server so the socket contract cannot drift

**Both halves of that break.** The server becomes Python and can't import TypeScript. The
client moves to another repo and can't reach into this one.

So the shared package stops doing its job. Options, roughly in order of effort:

- **Copy the types into the MFE, keep this as the source of truth.** Simple, but drift is
  now possible and nothing catches it.
- **Publish `@wbac/shared` to npm**, MFE installs it. Keeps one source of truth, adds a
  release step.
- **Make the contract a written spec** (this doc already half is), with types on each side
  matching it by hand.

**Decide this before Track B starts.** It's the only part of the move with a real
architectural consequence — everything else is copying files.

### 2. Styling — it goes in `brand-openedx`, not the MFE

Page styles for this MFE live in **`brand-openedx/paragon/_extension.scss`**, not beside
the components. That file already holds `.wishlist-page` and `.course-partner-page`; the
classroom becomes the third, namespaced the same way.

So this isn't "copy 562 lines of CSS across." It's **translating them to brand tokens**:

```scss
// not this
padding: 24px;
color: #3d2b1f;

// this
@use "./variables" as brand;
padding: brand.$spacing-6;
color: brand.$text-color;
```

Slower than a copy, but it's what makes the classroom look like Academy rather than like
a different site inside it. Everything is already there to use —
`brand.$font-family-brand-ui`, `brand.$accent`, `brand.$minimum-touch-target`,
`brand.$focus-ring-width`.

### 2b. The fonts are already done

**`apps/web/src/fonts.css` can be deleted.** `brand-openedx/paragon/_fonts.scss` already
declares the same four faces, with the same `unicode-range` split:

| | Used for |
|---|---|
| DM Sans | Latin UI text (no unicode-range, so it covers everything Latin) |
| Cormorant Garamond | display headings |
| Noto Serif Tibetan | Tibetan interface text |
| Jomolhari | the texts themselves |

This is the same system — the classroom's own comment says the `size-adjust: 115%` on
Jomolhari was "taken from the brand's own declaration."

**But push one improvement back into the brand package.** The brand loads raw `.ttf`:

```
./fonts/Jomolhari/Jomolhari-Regular.ttf
./fonts/Noto_Serif_Tibetan/static/NotoSerifTibetan-Regular.ttf
```

The classroom loads subset `.woff2`, and its comment records what that bought:

> Jomolhari from 2.0 MB to 347 KB and Noto from 719 KB to 148 KB. That matters: thirty
> phones pulling fonts over one classroom access point is the worst moment of a session.

**That's roughly 6× smaller, and it matters most in exactly our situation.** Move the
woff2 subsets into `brand-openedx` and point `_fonts.scss` at them. Every MFE gets
faster, not just ours.

### 3. Which reader?

`Reader.tsx` is 66 lines. It works, but it's a placeholder — no translations, no
commentary, no search.

The plan is for students to eventually get the **WeBuddhist reader** instead. That's a
bigger job with its own blockers (the Tailwind spike, the `/library` proxy in
`sherab-custom-plugin`).

**Don't wait for it.** Move the simple reader across first and get the classroom working
inside Academy end to end. Swap in the WeBuddhist reader as a separate, later step — it
highlights by segment id exactly the same way, so nothing else has to change.

## Track B, in order

### Step 1 — A page that does nothing
New route in the MFE. Renders "hello". Decide whether it needs `AuthenticatedPageRoute`
(Open edX login) or should stay open like `/start-campus` — students currently need no
account, so probably open.

*Done when:* the route loads in the MFE.

### Step 2 — Settle the contract question
Pick one of the three options above. Do this before copying code that imports
`@wbac/shared`.

### Step 3 — Copy the pieces
`useRoom.ts`, `api.ts`, `JoinScreen.tsx`, `Reader.tsx`, `App.tsx`. Add
`socket.io-client`. Point the API URL at the sync server via MFE config.

*Done when:* it compiles.

### Step 4 — Styles, in the brand package
Add a `.classroom-page` section to `brand-openedx/paragon/_extension.scss`, translating
`styles.css` to brand tokens as you go. Delete `fonts.css` — the faces are already
declared there.

Separately, move the woff2 font subsets into `brand-openedx` and repoint `_fonts.scss`.

*Done when:* it looks like part of Academy, and Tibetan renders in Jomolhari.

### Step 5 — A real class, end to end
Teacher on one device, students on others, through Academy. Same two-origin rule as
Track A Step 4.

*Done when:* a class can be taught entirely inside Academy.

### Step 6 — Later, separately
Swap `Reader.tsx` for the WeBuddhist reader. Blocked on the Tailwind spike and the
library proxy. **Not part of this move.**

---

## What we're NOT doing in this port

Resist all of these until the port is done and proven:

- Changing the protocol
- Adding notes or highlights
- Adding student accounts
- Merging this into `sherab-custom-plugin`
- "While I'm in here, let me also…"

A port that also changes behaviour has no way of telling a port bug from a design change.
Get to 11/11 first, with identical behaviour. Then build on it.

---

# Decommissioning this repo

**This repo and its service get deleted once the move is done.** That is the goal, but it
changes two things.

## 1. The Python server needs a home — decide this first

Track A says "rewrite the server in Python." It doesn't say *where the code lives*, because
until now the answer was obvious: here. It isn't any more.

Two reasonable answers:

- **A new repo** (`wb-classroom-sync` or similar). Cleanest. The service is separately
  deployed, so it has a separate lifecycle, and a separate repo matches that honestly.
- **Inside `sherab-custom-plugin`.** Tempting — one Python home for everything. But every
  other thing in there is a Django app loaded *into* the LMS process, and this is a
  standalone service deployed on its own. Mixing two deploy targets in one repo needs
  clear signposting or someone will eventually try to load it as a plugin.

**Nothing in Track A can start until this is settled**, since it's where the first file goes.

## 2. Seven things must be rescued before deletion

The repo is only four commits old, so git history isn't the loss. These are:

| What | Where it should end up |
|---|---|
| **`apps/server/scripts/smoke.mjs`** | With the Python server. **This is the acceptance test for the whole port.** |
| `docs/PLAN.md` | With the Python server. The locked decisions and *why* — the only record. |
| `docs/PORT-TO-PYTHON.md` | This file. See below. |
| `packages/shared/src/index.ts` | Wherever the contract lands. Its comments explain the reasoning, not just the shapes. |
| `apps/server/migrations/001_init.sql` | With the Python server. |
| `README.md` | Fold "the one rule" into the new repo's README. |
| `infra/native/` | Caddyfile, systemd unit and deploy scripts. The shape carries over; the commands change. |

**This document is in the repo it describes.** Move it out early — before Track A's first
commit, not at the end — or the plan disappears partway through following it.

## 3. Delete the service last, not at cutover

Track A Step 6 and Track B Step 5 both say to keep the old thing running. That still holds,
and it matters more now that deletion is permanent:

1. Cut over to the Python server and the MFE
2. **Teach several real classes**
3. *Then* delete the service and archive the repo

A classroom is a bad place to discover a missed detail, and the old service is the whole
fallback. Deleting it the same week as the cutover removes the only way back.

Consider **archiving** rather than deleting the repo on GitHub — it costs nothing and
keeps the reasoning readable if a question comes up in six months.

## Open questions

- **Which database library?** `asyncpg` is closer to what's there now (plain SQL).
  SQLAlchemy is more familiar to Django developers and would make sharing models with the
  notes work easier later. Worth a short decision before Step 2.
- **Do we move it into Tutor as a container?** It can stay on its own EC2 box, or become
  a Tutor service next to the notes one. The second is tidier operationally. Doesn't
  block anything — decide after Step 6.
- **Where does `ApiTextProvider` fit?** Reading texts from the OpenPecha library is
  separate work. Keep it out of this port; do it next, in Python, once the port is done.
- **Where does the socket contract live** once the server is Python and the client is in
  another repo? See Track B, piece 1. This one blocks Track B and should be settled early.
  Note that **this repo is being deleted**, so "leave it in `packages/shared`" is not one
  of the options — publishing from a repo that is about to disappear doesn't work either.
  Realistically: it moves to the MFE, or to the new sync-server repo, and Python follows it.
- **Does the classroom page need an Open edX login?** Students currently join with a code
  and a nickname, no account. Keeping that means an open route. It also decides where
  notes can hang later.
