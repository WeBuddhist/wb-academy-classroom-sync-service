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

Two guards, one per side (the smoke test now takes four logins; see its header):

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

## Getting connected

**A socket is an account.** The connection is accepted only with a valid Academy login:

- A **browser** presents it by cookie: Open edX's two JWT cookies ride the handshake by
  themselves, and the page must be one of ours (the `Origin` is checked, twice: by
  Socket.IO and again by the service). The client must connect with credentials
  (`withCredentials: true`) and to a host the cookies are scoped to, which in dev means
  `http://local.openedx.io:8010`, not `localhost`.
- A **non-browser client** (a test, a script) passes `auth: { token }`.
- Otherwise the connection is refused, with a reason in `connect_error`.

Nobody names themselves. There is no `displayName` and no host token: who you are, and
whether you are the teacher, come from the login and nothing the client says.

## The whole surface

| Message | Direction | Payload fields |
|---|---|---|
| `room:join` | client → server | `code` |
| `teach:goto` | client → server | `segmentId` |
| `session:end` | client → server | *(none)* |
| `teach:goto` | server → client | `segmentId` |
| `presence` | server → client | `count?`, `teacherHere` |
| `session:ended` | server → client | *(none)* |
| `session:started` | server → client | *(none)* |
| `auth:expired` | server → client | *(none)* |
| `member:left` | server → client | `code` |
| `room:closed` | server → client | *(none)* |
| `member:removed` | server → client | `code` |

Every client→server message is acknowledged with `{ ok: true, ... }` or
`{ ok: false, error: string }`.

**Who may do what**

- `room:join` needs the account to be a **member** of the class. The code is needed once,
  over HTTP (`POST /api/rooms/{code}/join`), to become one; after that the account is the
  credential. A removed member is refused here however many times they reconnect.
- `teach:goto` and `session:end` need the account to be the class's **teacher**: the account
  that created it. Checked on the server against the verified login on every message.
  **The teacher-only UI is a convenience, not the security boundary.**
- A student who emits either is told no, and nothing moves.

**What the server sends**

- `presence` counts **people, not tabs**. The **teacher** gets `{count, teacherHere}` on every
  change. A **student** gets `{teacherHere}` only, and only when it changes: they are not shown a
  count, and sending every arrival to everyone is students-squared messages. A student who
  arrives early sees "waiting for the teacher" and can read ahead.
- `session:ended` is *today's* session ending. The class stays: everyone is still a member
  and the position is kept. The **teacher's** client returns to their list; a **student's** stays
  in the class to read and keep notes, and is told the session is over. It is stored on the
  room (`rooms.session_ended`), so a student who joins later, on any worker and after any
  restart, gets `sessionEnded: true` in their join state. The ending teacher's own connection
  is taken out of the class, so a reconnect cannot re-join and start the session again.
- `session:started` is the teacher opening the class afresh (a new `room:join`) after an ended
  session: it clears `sessionEnded` for everyone still in the class.
- `auth:expired` is sent just before the server drops a connection whose login has run out (the
  login is checked once, at connect). The client calls `/api/me` to refresh its cookie and
  reconnects.
- `room:closed` means the class was **archived** (over HTTP). It is gone from everyone's list.
- `member:left` is sent to a student's own tabs when they leave the class (the class stays in
  their list). It is separate from `member:removed` so they are never told a teacher removed them.
- `member:removed` is sent to a member the teacher removed, on every tab they have open, and
  they are dropped from the live class.

**The authoritative snapshot arrives in the ack to `room:join`**, not as an event of its
own. A reconnect re-emits `room:join`, so one path serves both the first join and every
resume. `teach:goto` is the incremental move, applied optimistically.

Rooms are keyed on the room's **id**, not its code, so a teacher changing the code does not
strand anyone already in.

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
- `translationEditionId` — the edition shown beneath the source, or `null`. Pinned by
  the server at room creation for the same reason as `editionId`. The client reads the
  alignment pairs between the two editions itself; only source segment ids ever travel.
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
  /** What the class is called; falls back to the text's title. */
  name: string
  /** Pins the edition. See "Which text a room is about" above. */
  editionId: string
  /** What the room's segment ids belong to. Client verifies; never parses. */
  segmentationId: string
  /** Edition shown beneath the source, or null for a source-only room. */
  translationEditionId: string | null
  title: string
  /** Null before the teacher has selected anything. */
  currentSegmentId: string | null
  /** People here, not tabs. */
  participantCount: number
  teacherHere: boolean
  /** The teacher ended today's session and has not come back. */
  sessionEnded: boolean
  /** What this account is in this class. Decides what the interface offers. */
  role: 'teacher' | 'student'
}

type Ack = { ok: true } | { ok: false; error: string }

interface ClientToServerEvents {
  'room:join': (
    payload: { code: string },
    ack: (r: { ok: true; state: RoomState } | { ok: false; error: string }) => void,
  ) => void
  'teach:goto': (payload: { segmentId: string }, ack: (r: Ack) => void) => void
  'session:end': (payload: {}, ack: (r: Ack) => void) => void
}

interface ServerToClientEvents {
  'teach:goto': (payload: { segmentId: string }) => void
  presence: (payload: { count?: number; teacherHere: boolean }) => void
  'session:ended': () => void
  'session:started': () => void
  'auth:expired': () => void
  'room:closed': () => void
  'member:removed': (payload: { code: string }) => void
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
