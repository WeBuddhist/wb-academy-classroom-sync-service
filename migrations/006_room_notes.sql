-- A student's private notes and highlights on a class's text.
--
-- A note is a piece of text inside ONE line, remembered four ways so it survives a
-- change of screen, font and layout:
--   * `segment_id`   which line (the library's own id, as everywhere else)
--   * `layer`        which text on that line: the source, or its translation
--   * start/end      where inside that line's own text it begins and ends, counted from
--                    the start of the line and not of the page. Counted in code points.
--   * quote, prefix, suffix
--                    the selected words and a few either side. If the library ever edits
--                    a line the offsets go stale, and these are how the words are found
--                    again. If they cannot be, the note says "text changed" instead of
--                    pointing at the wrong words.
--
-- Private: every read and write is for the account that wrote it, and nobody else.
--
-- Kept when a member is removed: there is deliberately NO foreign key to `room_members`.
-- A removed member cannot reach their notes (access needs membership), and if they are
-- let back in the notes are there again. They are deleted only with the class itself.
--
-- No foreign key on `segment_id` either, for the reason `room_log` has none: a note is a
-- record of what someone read, and must survive a text being re-registered.
create table if not exists room_notes (
  id           char(36)     collate utf8mb4_bin not null,
  room_id      varchar(255) collate utf8mb4_bin not null,
  user_id      varchar(255) collate utf8mb4_bin not null,
  segment_id   varchar(128) collate utf8mb4_bin not null,
  layer        varchar(16)  not null,
  start_offset int          not null,
  end_offset   int          not null,
  quote        varchar(2000) not null,
  prefix       varchar(200) not null default '',
  suffix       varchar(200) not null default '',
  color        varchar(16)  not null,
  -- NULL is a highlight with no note.
  body         varchar(4000) null,
  created_at   timestamp    not null default current_timestamp,
  updated_at   timestamp    not null default current_timestamp on update current_timestamp,
  primary key (id),
  -- "My notes in this class": the one question this table is asked.
  key room_notes_owner_idx (room_id, user_id, segment_id),
  constraint room_notes_room_fk
    foreign key (room_id) references rooms (id) on delete cascade
) engine=InnoDB default charset=utf8mb4;
