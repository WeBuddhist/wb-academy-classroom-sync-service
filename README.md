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

Read **[docs/PORT-TO-PYTHON.md](docs/PORT-TO-PYTHON.md)** before working on this — it has
the two tracks, the thirteen details that must survive the rewrite, and the open
decisions. **[docs/PLAN.md](docs/PLAN.md)** has the original design and the reasoning
behind each locked decision.

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

A Tutor plugin runs this as a service container beside the LMS, the way
`studio-tool-service` and `notes` already are. The MFE's Caddy forwards one path to it:

```
reverse_proxy /classroom-api/* classroom:8000
```

Same origin as the page, so no CORS and no extra subdomain.

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

**11/11 means the port is done.**

## Open decisions

- **Postgres or MySQL.** The rescued schema is Postgres; Tutor ships MySQL. No database
  driver is pinned yet because of this. See the port plan.
- **Where the socket contract lives.** `docs/socket-contract.ts` is the rescued
  TypeScript definition, kept as reference. Python cannot import it, and the client now
  lives in another repo — so the thing that stopped the two sides drifting no longer
  works. See the port plan, Track B.
