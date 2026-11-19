# wb-academy-classroom — build plan

## Context

A teacher and students sit together in one physical classroom studying dense Tibetan
philosophical texts. Tibetan prose has no spaces and runs as continuous syllables, so
students constantly lose their place and the teacher loses time re-orienting the room.

This app puts the text on every student's device and lets the teacher drive it: when the
teacher selects a line, every student's screen highlights and scrolls to that same line.
Rooms are entered with a short code the teacher writes on the whiteboard.

Three constraints shape the design:

1. **Everyone shares one venue uplink.** When it drops, the whole class loses the app at
   once. The client must keep working — readable, scrollable — with only sync paused.
2. **Texts are served by the WeBuddhist content API**, which does not exist yet. The app
   is written against a provider interface, not against any particular source.
3. **No text data lives in this repo.** The corpus is unpublished work in progress. No
   source files, no segmented output, no fixtures are committed.

## Scope boundary

This repo is the **application**: the classroom client and its realtime server.

Text processing — segmenting raw Tibetan into addressable lines — is a content-pipeline
concern and lives in its own repo. The application consumes already-segmented text and
never parses Tibetan itself.

## Decisions (locked)

| # | Decision | Why |
|---|---|---|
| 1 | Sync a **segment id**, never DOM ranges or character offsets | Survives differing font size, screen width, and text layer across devices. Payload is a few bytes. |
| 2 | All text access behind a **`TextProvider` interface** | API-backed in production, fixture-backed in development, without touching room or sync code. |
| 3 | Texts are **imported into Postgres**; a live class never reads upstream | A content API outage mid-lesson must not stop a class. |
| 4 | Teacher authority enforced **server-side** via host token | Hiding buttons in the UI is not authority. |
| 5 | **Offline-first client** — full text cached on join | Venue uplink failure degrades to "highlight stops", not "blank screen". |
| 6 | **No student accounts** — code + display name | Everyone is in the room together; social accountability covers the abuse case. |
| 7 | Single shared **host password** creates rooms | A small number of trusted teachers. No user table. |
| 8 | **No text data committed**, ever | The corpus is unpublished. `/texts/` is gitignored. |

## Stack

- **Client** — Vite + React + TypeScript (static build; no SSR to self-host)
- **Server** — Fastify + Socket.IO + TypeScript, single process
- **DB** — Postgres 16
- **Proxy/TLS** — Caddy (automatic HTTPS; proxies WebSockets with no header configuration)
- **Packaging** — none; installed directly and run under systemd
- **Host** — one EC2 `t4g.small` in `ap-south-1` (Mumbai)

Rejected: Next.js (SSR adds self-hosting complexity this app gains nothing from);
Supabase/Ably (we own the socket process, so no realtime vendor is needed); Redis
(irrelevant until more than one server process, which is far off).

## Repo layout

```
wb-academy-classroom/
├── apps/
│   ├── web/                 # Vite + React client
│   └── server/              # Fastify + Socket.IO + Postgres
├── packages/
│   └── shared/              # segment/room types, socket event contracts
├── texts/                   # LOCAL ONLY, gitignored — dev fixtures
├── infra/native/            # install/deploy scripts, systemd unit, Caddy config
└── docs/
```

`packages/shared` is imported by both client and server so the socket contract cannot drift.

## Data model

```sql
create table texts (
  id         text primary key,
  slug       text unique not null,
  title_bo   text not null,
  title_en   text,
  source     text not null default 'api',
  created_at timestamptz not null default now()
);

create table segments (
  -- Segment ids come from the pipeline and are unique only *within* a text — every
  -- text has an s-0001 — so the key is composite, not `id` alone.
  id      text not null,
  text_id text not null references texts(id) on delete cascade,
  idx     integer not null,          -- display order
  kind    text not null,             -- title|homage|outline|prose|verse|colophon
  bo      text not null,
  cite    text,
  wylie   text,                      -- layers stay null until they exist
  en      text,
  note    text,
  primary key (text_id, id),
  unique (text_id, idx)
);

create table rooms (
  id                 text primary key,
  code               text unique not null,   -- 6 chars, ambiguity-free alphabet
  text_id            text not null references texts(id),
  host_token_hash    text not null,
  current_segment_id text references segments(id),
  is_live            boolean not null default true,
  created_at         timestamptz not null default now(),
  closed_at          timestamptz
);

create table room_log (                      -- replay timeline; cheap to write, valuable later
  id         bigserial primary key,
  room_id    text not null references rooms(id) on delete cascade,
  segment_id text not null references segments(id),
  at         timestamptz not null default now()
);
create index on room_log (room_id, at);
```

`id` is the only thing ever exposed over the wire or stored in a log. Segment ids are
**owned by the content pipeline** and adopted as-is; this app never generates them, so
room logs stay valid across re-imports. Layer columns are nullable, so translations can
land later with zero schema or protocol change.

Room code alphabet excludes ambiguous glyphs: `ABCDEFGHJKMNPQRSTUVWXYZ23456789`.

## Text provider boundary

```ts
interface TextProvider {
  listTexts(): Promise<TextMeta[]>
  getText(slug: string): Promise<SegmentedText>
}
```

- `ApiTextProvider` — the WeBuddhist content API. Production.
- `FixtureTextProvider` — reads JSON from the gitignored `texts/` directory. Development
  only, so work can proceed before the API exists without committing any corpus.

An `import` command runs the provider and upserts into Postgres. Rooms, sync and the
client read only from Postgres, so swapping providers touches nothing else. The API's
shape is unknown, which is exactly why this adapter stays thin.

## Socket protocol (`packages/shared`)

| Direction | Event | Payload |
|---|---|---|
| client → server | `room:join` | `{ code, displayName }` |
| server → client | `room:state` | `{ textSlug, currentSegmentId, participantCount }` |
| host → server | `teach:goto` | `{ segmentId, hostToken }` |
| server → clients | `teach:goto` | `{ segmentId }` |
| server → clients | `presence` | `{ count }` |

`teach:goto` is validated against `host_token_hash`, written to `rooms` and `room_log`,
then broadcast. Clients apply the broadcast optimistically for responsiveness and treat
`room:state` as truth on join or reconnect. Reconnect is just `room:join` again.

The host token is returned once at room creation and kept in the teacher's `localStorage`.
A recovery code is shown at creation so a cleared browser doesn't lock a teacher out of
their own room.

## Phases

### Phase 0 — Scaffold ✅
npm workspaces, TypeScript, `packages/shared` carrying the types and socket contract.

### Phase 1 — Core sync ✅
Server: room create (shared password), join, `teach:goto`, presence, Postgres schema and
migrations, `FixtureTextProvider` plus the import command. Client: join screen, reader
view, teacher control view.

*Verified:* `npm run smoke -w @wbac/server` creates a room and asserts all of it — 11/11,
with delivery to a student measured at 1.6ms. Confirmed in two browser windows on
separate origins (`localhost` and `127.0.0.1`), which is what it takes to have genuinely
separate `localStorage` and therefore a real teacher and a real student.

Two gaps found after the first pass and closed:
- The recovery code was displayed but could not be redeemed — there was no way to enter
  one. A teacher who cleared their browser lost the room permanently.
- Nothing ever ended a class. `is_live` and `closed_at` existed and the client listened
  for `room:closed`, but the server never emitted it, so codes stayed live forever.

Two things changed from the plan as written:
- `segments` is keyed on `(text_id, id)`, since pipeline ids repeat across texts.
- `WEB_ORIGIN` accepts a comma-separated list. A deployment may answer to more than one
  name, and side-by-side testing needs two origins.

### Phase 2 — Classroom hardening
Service worker + IndexedDB caching of the full text; follow-mode with a "Jump to teacher"
pill that breaks on manual scroll; reconnect and resume; Wake Lock; self-hosted Tibetan
webfont with long-lived immutable caching; mobile-first layout, `lang="bo"`, generous
line-height, no justification.

*Done when:* with the network killed mid-session the text stays readable and scrollable,
and on restore the client snaps back to the teacher's current line unprompted.

### Phase 3 — Deploy
Direct deployment onto one EC2 `t4g.small` in `ap-south-1`: Node, PostgreSQL and Caddy
installed normally, the app under systemd, Caddy terminating TLS and proxying the API.
Scripts in `infra/native/`, runbook in `infra/deploy.md`.

Containers were considered and a full setup written, then removed rather than left to
rot unused: with one server and one operator, the layer between a symptom and its cause
is paid continuously while the consistency benefits do not yet apply. Worth revisiting
if a second operator, a staging environment, or a move off this instance appears.

Session Manager was planned for shell access instead of an open SSH port, but the
available IAM credentials do not permit it, so SSH is restricted to a single source IP
instead.

*Done when:* reachable over HTTPS on a real domain and a phone on the classroom WiFi can
join a room and follow along.

### Deferred
`ApiTextProvider` (blocked on the API existing); layer toggles for Wylie/English/
commentary; sa bcad outline navigation; session replay from `room_log`; sub-line phrase
highlighting; alignment with the audio pipeline.

Phases 1–2 are built so each of these is additive — none requires changing the sync
protocol or the segment id contract.

## Verification

- `npm test` — unit tests
- `npm run typecheck`
- Manual two-window test for phase 1 acceptance
- Phase 2 checked with devtools offline mode and a real phone on the same LAN
- Phase 3 checked by joining a room from a phone over mobile data against the deployed host

## Open questions

- **Content API shape is unknown.** `ApiTextProvider` is specified but not built. The
  `Segment` shape in `packages/shared` is provisional and should be reconciled with the
  API's response once it is defined.
- **Segment id format** is whatever the pipeline produces; this app treats ids as opaque.
- **Layer display is deferred** until translations exist. Schema and sync already support it.
