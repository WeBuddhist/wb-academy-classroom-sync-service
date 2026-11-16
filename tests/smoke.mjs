/**
 * Phase 1 acceptance checks, run against a running dev server.
 *
 *   npm run smoke -w @wbac/server
 *
 * Creates its own room so the results do not depend on who else is connected, then
 * asserts the properties phase 1 is supposed to guarantee: only the host can move the
 * room, moves reach students promptly, and a late joiner arrives on the current line.
 */

import { io } from 'socket.io-client'

const URL = process.env.API_URL ?? 'http://localhost:3000'
const HOST_PASSWORD = process.env.HOST_PASSWORD ?? 'change-me'

const results = []
const check = (label, pass, detail = '') => results.push({ label, pass, detail })

const connect = () =>
  new Promise((resolve, reject) => {
    const socket = io(URL, { transports: ['websocket'] })
    socket.on('connect', () => resolve(socket))
    socket.on('connect_error', reject)
  })

const join = (socket, code, displayName) =>
  new Promise((resolve) => socket.emit('room:join', { code, displayName }, resolve))

const goto = (socket, segmentId, hostToken) =>
  new Promise((resolve) => socket.emit('teach:goto', { segmentId, hostToken }, resolve))

// A room of this test's own, so presence and current position are known exactly.
const texts = await fetch(`${URL}/api/texts`).then((r) => r.json())
if (texts.length === 0) {
  console.error('No texts imported. Run: npm run import -w @wbac/server -- --all')
  process.exit(1)
}
const textSlug = texts[0].slug
const { segments } = await fetch(`${URL}/api/texts/${textSlug}`).then((r) => r.json())
const [first, second] = [segments[0].id, segments[Math.min(5, segments.length - 1)].id]

const created = await fetch(`${URL}/api/rooms`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ hostPassword: HOST_PASSWORD, textSlug }),
}).then((r) => r.json())

if (!created.code) {
  console.error(`Could not create a room: ${created.error ?? 'unknown error'}`)
  console.error('Set HOST_PASSWORD to match the server.')
  process.exit(1)
}
const { code, hostToken } = created

const teacher = await connect()
const student = await connect()
await join(teacher, code, 'Teacher')
const studentJoin = await join(student, code, 'Student')

check('a new room starts with no current line', studentJoin.ok && studentJoin.state.currentSegmentId === null)
check('presence counts both clients', studentJoin.ok && studentJoin.state.participantCount === 2,
  `${studentJoin.ok ? studentJoin.state.participantCount : '?'} present`)

// Authority. This is the property that actually matters: the teacher-only interface is
// a convenience, and the server is what enforces it.
const forged = await goto(student, first, 'not-the-real-token')
check('a student with a forged token is rejected', forged.ok === false, forged.error ?? '')

const noToken = await goto(student, first, '')
check('a student with no token is rejected', noToken.ok === false, noToken.error ?? '')

const badSegment = await goto(teacher, 'does-not-exist', hostToken)
check('an unknown segment id is rejected', badSegment.ok === false, badSegment.error ?? '')

// The teacher moves the room, and the student hears about it.
const delivered = new Promise((resolve) => {
  const started = performance.now()
  student.once('teach:goto', ({ segmentId }) =>
    resolve({ segmentId, ms: performance.now() - started }),
  )
})
const moved = await goto(teacher, second, hostToken)
check('the host token is accepted', moved.ok === true, moved.error ?? '')

const delivery = await delivered
check('the student received the move', delivery.segmentId === second, delivery.segmentId)
check('delivery is well under a second', delivery.ms < 1000, `${delivery.ms.toFixed(1)}ms`)

// A student arriving mid-class must land where the class already is.
const latecomer = await connect()
const lateJoin = await join(latecomer, code, 'Latecomer')
check('a late joiner starts on the current line',
  lateJoin.ok && lateJoin.state.currentSegmentId === second,
  lateJoin.ok ? lateJoin.state.currentSegmentId : lateJoin.error)
check('presence includes the late joiner', lateJoin.ok && lateJoin.state.participantCount === 3,
  `${lateJoin.ok ? lateJoin.state.participantCount : '?'} present`)

const stranger = await connect()
const badRoom = await join(stranger, 'ZZZZZZ', 'Stranger')
check('an unknown room code is rejected', badRoom.ok === false, badRoom.error ?? '')

// --- the recovery code actually recovers ----------------------------------
// A teacher whose browser forgot the room, or who moved to another device, has only
// this code. If it does not work the class is unrecoverable.
const verifyHost = (token) =>
  fetch(`${URL}/api/rooms/${code}/verify-host`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ hostToken: token }),
  })

check('a wrong recovery code is refused', (await verifyHost('not-the-real-token')).status === 401)
check('the real recovery code is accepted', (await verifyHost(hostToken)).ok)

const restored = await connect()
await join(restored, code, 'Teacher on another device')
const restoredMove = await goto(restored, first, hostToken)
check('a teacher restored from the recovery code can drive the room',
  restoredMove.ok === true, restoredMove.error ?? '')

// --- ending the class -----------------------------------------------------
const close = (socket, token) =>
  new Promise((resolve) => socket.emit('room:close', { hostToken: token }, resolve))

const studentClose = await close(student, 'not-the-real-token')
check('a student cannot end the class', studentClose.ok === false, studentClose.error ?? '')

const notified = Promise.race([
  new Promise((resolve) => student.once('room:closed', () => resolve(true))),
  new Promise((resolve) => setTimeout(() => resolve(false), 2000)),
])
const hostClose = await close(teacher, hostToken)
check('the host can end the class', hostClose.ok === true, hostClose.error ?? '')
check('everyone present is told the class ended', (await notified) === true)

const afterwards = await connect()
const lateToAClosedRoom = await join(afterwards, code, 'Too late')
check('a closed room accepts no one new', lateToAClosedRoom.ok === false,
  lateToAClosedRoom.error ?? '')
check('a closed room reports itself as ended to a returning host',
  (await verifyHost(hostToken)).status === 410)

for (const { label, pass, detail } of results) {
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`)
}
const failed = results.filter((r) => !r.pass).length
console.log(`\n${results.length - failed}/${results.length} checks passed  (room ${code})`)

for (const socket of [teacher, student, latecomer, stranger, restored, afterwards]) {
  socket.disconnect()
}
process.exit(failed === 0 ? 0 : 1)
