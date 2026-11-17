/**
 * Acceptance checks, run against a running server.
 *
 *   API_URL=… HOST_PASSWORD=… AUTH_TOKEN=… AUTH_TOKEN_STUDENT=… AUTH_TOKEN_LATE=… \
 *   AUTH_TOKEN_OUTSIDER=… node tests/smoke.mjs
 *
 * Four accounts, because the socket is now about accounts: a teacher who creates the
 * class, a student and a latecomer who join it, and an outsider who never does. Tokens
 * come from the LMS, or from `scripts/make_test_login.py` when there is none.
 *
 * Creates its own class so the results do not depend on who else is connected, then
 * asserts the properties that matter: only the teacher can move the class, only members
 * can enter it, moves reach students promptly, a late joiner arrives on the current line,
 * and a removed member is dropped and cannot come back.
 */

import { io } from 'socket.io-client'

const URL = process.env.API_URL ?? 'http://localhost:3000'
const HOST_PASSWORD = process.env.HOST_PASSWORD ?? 'change-me'

const TOKENS = {
  teacher: process.env.AUTH_TOKEN,
  student: process.env.AUTH_TOKEN_STUDENT,
  late: process.env.AUTH_TOKEN_LATE,
  outsider: process.env.AUTH_TOKEN_OUTSIDER,
}
const missing = Object.entries(TOKENS).filter(([, t]) => !t).map(([n]) => n)
if (missing.length) {
  console.error(`Missing a login token for: ${missing.join(', ')}.`)
  console.error('Set AUTH_TOKEN, AUTH_TOKEN_STUDENT, AUTH_TOKEN_LATE and AUTH_TOKEN_OUTSIDER.')
  console.error('See scripts/make_test_login.py.')
  process.exit(1)
}

const results = []
const check = (label, pass, detail = '') => results.push({ label, pass, detail })

const api = (who, method, path, body) =>
  fetch(`${URL}${path}`, {
    method,
    headers: { 'content-type': 'application/json', authorization: `JWT ${TOKENS[who]}` },
    body: body ? JSON.stringify(body) : undefined,
  })

const connect = (who) =>
  new Promise((resolve, reject) => {
    const socket = io(URL, { transports: ['websocket'], auth: { token: TOKENS[who] } })
    socket.on('connect', () => resolve(socket))
    socket.on('connect_error', reject)
  })

const join = (socket, code) =>
  new Promise((resolve) => socket.emit('room:join', { code }, resolve))

const goto = (socket, segmentId) =>
  new Promise((resolve) => socket.emit('teach:goto', { segmentId }, resolve))

const endSession = (socket) =>
  new Promise((resolve) => socket.emit('session:end', {}, resolve))

const heard = (socket, event, ms = 2000) =>
  Promise.race([
    new Promise((resolve) => socket.once(event, () => resolve(true))),
    new Promise((resolve) => setTimeout(() => resolve(false), ms)),
  ])

// A class of this test's own, so presence and current position are known exactly.
const search = new URLSearchParams({ title: 'བྱང་ཆུབ', limit: '1' })
const asTeacher = { headers: { authorization: `JWT ${TOKENS.teacher}` } }
const texts = await fetch(`${URL}/api/texts?${search}`, asTeacher).then((r) => r.json())
if (!Array.isArray(texts) || texts.length === 0) {
  console.error('No texts found. Is LIBRARY_BASE_URL/LIBRARY_API_KEY set on the server?')
  process.exit(1)
}
const textId = texts[0].id

const created = await api('teacher', 'POST', '/api/rooms', {
  hostPassword: HOST_PASSWORD,
  textId,
  name: 'Smoke test class',
}).then((r) => r.json())

if (!created.code) {
  console.error(`Could not create a class: ${created.detail ?? created.error ?? 'unknown error'}`)
  console.error('Set HOST_PASSWORD to match the server, and check the login tokens.')
  process.exit(1)
}

// Segment ids come from the library through this server's own proxy -- the same path
// the reader uses, so this covers that too.
const spans = await fetch(
  `${URL}/library/v2/editions/${created.editionId}/segmentation/segments?limit=10&offset=0`,
  asTeacher,
).then((r) => r.json())
const items = spans.items ?? []
if (items.length < 2) {
  console.error('That text has fewer than two segments; nothing to move between.')
  process.exit(1)
}
const [first, second] = [items[0].id, items[Math.min(5, items.length - 1)].id]
const { code } = created

// Membership: the code is needed once, over HTTP, to become a member.
const studentJoined = await api('student', 'POST', `/api/rooms/${code}/join`)
check('a student can join with the code', studentJoined.ok && (await studentJoined.json()).role === 'student')
check('the creator is the teacher', (await api('teacher', 'POST', `/api/rooms/${code}/join`).then((r) => r.json())).role === 'teacher')

const teacher = await connect('teacher')
const student = await connect('student')
await join(teacher, code)
const studentJoin = await join(student, code)

check('a new class starts with no current line', studentJoin.ok && studentJoin.state.currentSegmentId === null)
check('presence counts both people', studentJoin.ok && studentJoin.state.participantCount === 2,
  `${studentJoin.ok ? studentJoin.state.participantCount : '?'} present`)
check('the student is told they are a student', studentJoin.ok && studentJoin.state.role === 'student')

// Entering a class takes membership, not just the code.
const outsider = await connect('outsider')
const outsiderJoin = await join(outsider, code)
check('someone who is not a member cannot enter, even with the code', outsiderJoin.ok === false,
  outsiderJoin.error ?? '')

// Authority. This is the property that actually matters: the teacher-only interface is
// a convenience, and the server is what enforces it.
const studentMove = await goto(student, first)
check('a student cannot move the class', studentMove.ok === false, studentMove.error ?? '')

const outsiderMove = await goto(outsider, first)
check('someone who never joined cannot move the class', outsiderMove.ok === false, outsiderMove.error ?? '')

const badSegment = await goto(teacher, 'does-not-exist')
check('an unknown segment id is rejected', badSegment.ok === false, badSegment.error ?? '')

// The teacher moves the class, and the student hears about it.
const delivered = new Promise((resolve) => {
  const started = performance.now()
  student.once('teach:goto', ({ segmentId }) =>
    resolve({ segmentId, ms: performance.now() - started }),
  )
})
const moved = await goto(teacher, second)
check('the teacher can move the class', moved.ok === true, moved.error ?? '')

const delivery = await delivered
check('the student received the move', delivery.segmentId === second, delivery.segmentId)
check('delivery is well under a second', delivery.ms < 1000, `${delivery.ms.toFixed(1)}ms`)

// A student arriving mid-class must land where the class already is.
await api('late', 'POST', `/api/rooms/${code}/join`)
const latecomer = await connect('late')
const lateJoin = await join(latecomer, code)
check('a late joiner starts on the current line',
  lateJoin.ok && lateJoin.state.currentSegmentId === second,
  lateJoin.ok ? lateJoin.state.currentSegmentId : lateJoin.error)
check('presence includes the late joiner', lateJoin.ok && lateJoin.state.participantCount === 3,
  `${lateJoin.ok ? lateJoin.state.participantCount : '?'} present`)
check('the late joiner is told the teacher is here', lateJoin.ok && lateJoin.state.teacherHere === true)

const stranger = await connect('student')
const badRoom = await join(stranger, 'ZZZZZZ')
check('an unknown class code is rejected', badRoom.ok === false, badRoom.error ?? '')

// --- the teacher on a second device -------------------------------------------------
// No recovery code any more: the account is the credential. Another device is another
// socket logged in as the same person.
const otherDevice = await connect('teacher')
const otherJoin = await join(otherDevice, code)
const otherMove = await goto(otherDevice, first)
check('a teacher on another device can drive the class, with no recovery code',
  otherJoin.ok && otherMove.ok === true, otherMove.error ?? '')

// --- members ----------------------------------------------------------------------------
const members = await api('teacher', 'GET', `/api/rooms/${code}/members`).then((r) => r.json())
check('the teacher can see who is in the class',
  Array.isArray(members) && members.length === 3, `${members.length ?? '?'} members`)
check('a student cannot see the member list', (await api('student', 'GET', `/api/rooms/${code}/members`)).status === 403)

const lateMe = await api('late', 'GET', '/api/me').then((r) => r.json())
const removedLate = heard(latecomer, 'member:removed')
const removal = await api('teacher', 'DELETE', `/api/rooms/${code}/members/${lateMe.id}`)
check('the teacher can remove a member', removal.status === 204, `${removal.status}`)
check('a removed member is told, live', (await removedLate) === true)
const rejoin = await join(latecomer, code)
check('a removed member cannot come back in by reconnecting', rejoin.ok === false, rejoin.error ?? '')

// --- ending the session, then the class --------------------------------------------
const studentEnd = await endSession(student)
check('a student cannot end the session', studentEnd.ok === false, studentEnd.error ?? '')

const sessionHeard = heard(student, 'session:ended')
const teacherEnd = await endSession(teacher)
check('the teacher can end the session', teacherEnd.ok === true, teacherEnd.error ?? '')
check('everyone present is told the session ended', (await sessionHeard) === true)
check('ending a session leaves the class open',
  (await api('student', 'POST', `/api/rooms/${code}/join`)).ok)

const archivedHeard = heard(student, 'room:closed')
const archive = await api('teacher', 'POST', `/api/rooms/${code}/archive`)
check('the teacher can archive the class', archive.status === 204, `${archive.status}`)
check('everyone present is told the class was archived', (await archivedHeard) === true)
check('an archived class accepts no one new',
  (await api('student', 'POST', `/api/rooms/${code}/join`)).status === 410)

for (const { label, pass, detail } of results) {
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`)
}
const failed = results.filter((r) => !r.pass).length
console.log(`\n${results.length - failed}/${results.length} checks passed  (class ${code})`)

for (const socket of [teacher, student, latecomer, stranger, outsider, otherDevice]) {
  socket.disconnect()
}
process.exit(failed === 0 ? 0 : 1)
