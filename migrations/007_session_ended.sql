-- Whether the teacher has ended today's session and not come back.
--
-- Students are not sent away when a session ends: they stay to read and to keep notes, and
-- are told it is over. A student who arrives afterwards has to be told the same, and it must
-- survive a restart of the service and be the same on every worker, so it is stored on the
-- room rather than remembered in a process. Cleared when the teacher comes back.
alter table rooms
  add column session_ended boolean not null default false;
