# The socket contract

The only thing that ever travels between teacher and student is a **segment id**. Never a
DOM range, never a character offset, never a scroll position. A segment id survives
differences in font size, screen width, device and visible text layer, which is what
makes one teacher's click land correctly on thirty different phones.

## Why this file exists, and what guards it

The server is Python and the client is TypeScript in another repo, so no shared import
stops the two drifting. The specific failure that causes is quiet: Python habit writes
`segment_id`, JavaScript expects `segmentId`, **both sides build fine**, and thirty
screens do nothing while the teacher taps.

Two guards, one per side:

| Side | Guard |
|---|---|
| Python server | `tests/smoke.mjs` in CI — a JavaScript client speaking the real wire protocol. A renamed field fails it. |
| MFE client | The types below, copied into the MFE, checked by the compiler. |

Event names (`room:join`, `teach:goto`) look identical in both languages, so nobody is
tempted to translate them. **The risk is entirely in the payload field names below.**

It is worse than it looks, because the mistake half-works. MFE code calls
`camelCaseObject(data)` on HTTP responses by convention, so `segment_id` arriving over
the HTTP routes is quietly corrected. `socket.io-client` has no such step. The same
error therefore passes on one path and fails silently on the other, which reads like a
socket bug rather than a naming bug. See [CONTEXT.md](CONTEXT.md).

## The whole surface

Eight messages, twelve field names.

| Message | Direction | Payload fields |
|---|---|---|
| `room:join` | client → server | `code`, `displayName` |
| `teach:goto` | client → server | `segmentId`, `hostToken` |
| `room:close` | client → server | `hostToken` |
| `room:state` | server → client | *(a `RoomState`)* |
| `teach:goto` | server → client | `segmentId` |
| `presence` | server → client | `count` |
| `room:closed` | server → client | *(no payload)* |

Every client→server message is acknowledged with `{ ok: true, ... }` or
`{ ok: false, error: string }`.

`room:state` is the authoritative snapshot, sent on join and on reconnect; it overrides
local state. `teach:goto` is the incremental move, applied optimistically.

`teach:goto` and `room:close` carry the host token and are rejected server-side unless it
matches the room's stored hash. **The teacher-only UI is a convenience, not the security
boundary.**

## Which text a room is about

A room pins an **edition**, not a text. This is load-bearing and easy to get wrong.

Segment ids belong to a *segmentation*, and a segmentation belongs to an edition. Two
editions of the same work have different segment ids for the same line. The library
resolves a bare text id by taking **the first edition in the list** — fine for one person
browsing, wrong for thirty people who must agree on line 7, because nothing promises that
ordering is stable over the life of a room.

So `RoomState` carries:

- `editionId` — what the client resolves and fetches from. Pinning it is what makes
  everyone's segment ids the same.
- `segmentationId` — what the server's stored ids actually belong to. The client should
  check its resolved segmentation matches and say so loudly if it does not; that mismatch
  means the library moved under a running room, and the alternative is silently
  highlighting nothing.
- `title` — for display only. Never parsed.

## Room codes

```
alphabet  ABCDEFGHJKMNPQRSTUVWXYZ23456789
length    6
```

Excludes **I, L, O, 0 and 1**, so a code read off a whiteboard from the back of a room is
unambiguous.

S and 5 are both present, despite an earlier claim here that they were not — a test
caught it. Left unchanged for now: altering the alphabet during a port would blur the
line between a port bug and a design change.

## The types

Copy these into the MFE. They are not imported by anything; this file is a written spec.

```ts
interface RoomState {
  code: string
  /** Pins the edition. See "Which text a room is about" above. */
  editionId: string
  /** What the room's segment ids belong to. Client verifies; never parses. */
  segmentationId: string
  title: string
  /** Null before the teacher has selected anything. */
  currentSegmentId: string | null
  participantCount: number
}

interface ClientToServerEvents {
  'room:join': (
    payload: { code: string; displayName: string },
    ack: (r: { ok: true; state: RoomState } | { ok: false; error: string }) => void,
  ) => void
  'teach:goto': (
    payload: { segmentId: string; hostToken: string },
    ack: (r: { ok: true } | { ok: false; error: string }) => void,
  ) => void
  'room:close': (
    payload: { hostToken: string },
    ack: (r: { ok: true } | { ok: false; error: string }) => void,
  ) => void
}

interface ServerToClientEvents {
  'room:state': (state: RoomState) => void
  'teach:goto': (payload: { segmentId: string }) => void
  presence: (payload: { count: number }) => void
  'room:closed': () => void
}
```

## What this file used to say

The rescued `socket-contract.ts` also defined `Segment` (with its Tibetan text),
`SegmentedText`, `TextMeta` and a `TextProvider` interface, and described texts being
"imported into Postgres once".

**All of that is gone.** This server stores segment ids and no content; text comes to the
reader from the OpenPecha library directly. Those types were left behind by the design we
replaced, and reading them as instructions would lead someone to implement `getText()` on
the sync server — the exact thing we decided against.

A segment's shape — its kind, its line breaks, its Tibetan — is now the library's
business and the reader's. It is not part of this contract, and it never travels over
this socket.
