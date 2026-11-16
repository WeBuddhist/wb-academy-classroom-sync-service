# wb-academy-classroom-sync-service

Realtime sync server for live Tibetan text classes. The teacher drives; every student's
reader highlights and scrolls to the same line.

Students join a room with a short code the teacher writes on the whiteboard — no
accounts. Because Tibetan prose runs as unbroken syllables with no word spaces, losing
your place is the default experience in these classes; this removes that.

## The one rule

The only thing that travels between teacher and student is a **segment id**. Never a DOM
range, never a character offset, never a scroll position. A segment id means the same
line on a phone, a tablet and a projector, at any font size, in any text layer — which is
what makes one teacher's click land correctly on thirty different screens.

Everything else follows from it: late joiners just ask for the current id, reconnects are
idempotent, and translation layers can be added later without touching the protocol.

## Status

**Being ported.** This service replaces the Node server in `wb-academy-classroom`, which
is retired once the port is proven. The client moves separately into
`frontend-app-extension`.

Read **[docs/BUILD-PLAN.md](docs/BUILD-PLAN.md)** before working on this — it has the
three tracks, the thirteen details that must survive the rewrite, and the open decisions.
**[docs/PLAN.md](docs/PLAN.md)** has the original design; where the two disagree the build
plan is current, and it says where and why.

Done so far: the service stands up and answers `/api/health`. Rooms, texts and the socket
events are next.

## Scope

This repo is **the sync server only**.

- **The client** is a page in `frontend-app-extension`; its styles live in
  `brand-openedx`.
- **Text processing** — segmenting Tibetan into addressable lines — is a content-pipeline
  concern and lives elsewhere.
- **Text data.** No corpus, no source files, no segmented output is ever committed here.
  In production the service receives texts from the WeBuddhist content API.

## Running it

```bash
pip install -e ".[dev]"
uvicorn classroom_sync.app:app --reload --port 8000
```

```bash
curl localhost:8000/api/health
```

Tests:

```bash
pytest
```

## How it reaches Open edX

One Python file in `tutor-plugins/` runs this as a service container beside the LMS, the
way `studio_tool_service.py` and `tutornotes` already do. Tutor builds the image itself —
there is no registry and nothing to publish.

`tutormfe` leaves a patch point inside the MFE's own Caddy site block, so the route hangs
off the MFE host:

```
handle_path /classroom-api/* {
    reverse_proxy classroom:8000
}
```

Same origin as the page, so no CORS and no extra subdomain. Tutor also generates
`HOST_PASSWORD` and the database password per install via `CONFIG_UNIQUE`, and creates
the database in an init task. See the build plan, Track A Step 5.

## This server stores segment ids, not text

**No work's content is ever stored here.** The reader fetches text from the OpenPecha
library directly, so this server never serves it and has no reason to keep it.

What it keeps is each text's **id list**, so it can answer the only question it needs to:
*is this segment part of this room's text?* Without that, a bad id would either go
unchecked or put the library on the live path of every teacher tap.

A text is registered the first time a room is created against it, by fetching its segment
list once. This replaces the original design's import pipeline — there is no longer a step
between "find a text" and "teach it".

The trade: if the library is down **and** a student joins **after** that, the latecomer
cannot load the text. Everyone already in the room is unaffected — their text is in their
browser, and sync carries only ids. Judged worth it against maintaining an import
pipeline and a copy of the corpus.

## Database

**MySQL 8.4** — Tutor's own instance, in a database of its own beside the LMS and notes
ones. `migrations/001_init.sql` is the whole schema: four tables, none holding content.

Two notes, both explained in the file:

- Identifier columns are `utf8mb4_bin`. Tutor runs MySQL with
  `--collation-server=utf8mb4_unicode_ci`, which is **case-insensitive**, and segment ids
  are opaque values from the content pipeline — we cannot assume their case is
  insignificant.
- Indexed columns are `VARCHAR`, not `TEXT`, because MySQL cannot index `TEXT` without a
  prefix length.
- The key is a **segmentation**, not a text. Segment ids belong to a segmentation, which
  belongs to an edition; two editions of one work give different ids for the same line.
  A room pins what its ids actually came from, resolved once at registration, so thirty
  students cannot resolve thirty different editions.

## Layout

```
src/classroom_sync/   the service
migrations/           database schema
tests/                pytest, plus smoke.mjs (see below)
infra/native/         deploy scripts from the Node server, to become Tutor plugin patches
docs/                 the port plan, the original design, the socket contract
```

## `tests/smoke.mjs` — read this before touching it

This is the acceptance test for the port, written against the Node server and **kept
unchanged on purpose**. It talks to a running server over HTTP and a socket and never
imports server code, so it tests this implementation exactly as it tested the old one.

**Do not rewrite it in Python.** A rewritten test no longer proves the behaviour is the
same, which is the only thing it is there for.

```bash
API_URL=http://localhost:8000 node tests/smoke.mjs
```

**19/19 means the port is done.**

## The socket contract

**[docs/socket-contract.md](docs/socket-contract.md)** is the spec — a written document,
not code. Python cannot import TypeScript and the client lives in another repo, so the
shared import that used to keep the two sides honest is gone both ways.

What replaces it: `tests/smoke.mjs` guards the server, and the spec's types copied into
the MFE guard the client. The hazard is specific and quiet — Python writes `segment_id`,
JavaScript expects `segmentId`, both sides build fine, and thirty screens do nothing.

Open decisions are tracked in the build plan.
