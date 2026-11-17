-- Which translation a room shows beneath the source, if any.
--
-- A separate table rather than a column on `rooms`: MySQL has no `add column if not
-- exists`, and this service re-runs every migration on each `init`. `create table if
-- not exists` is the idempotent form, so that is what keeps this file re-runnable.
--
-- It pins an EDITION, resolved once when the room is created, for the same reason the
-- room pins its source edition: the library does not promise that "the first
-- translation" stays the same one for the life of a class, and thirty students must
-- all be shown the same English. A room with no row here shows the source alone.
--
-- Stores an id, never a word of the translation.
create table if not exists room_translations (
  room_id    varchar(255) collate utf8mb4_bin not null,
  edition_id varchar(255) collate utf8mb4_bin not null,
  primary key (room_id),
  constraint room_translations_room_fk
    foreign key (room_id) references rooms (id) on delete cascade
) engine=InnoDB default charset=utf8mb4;
