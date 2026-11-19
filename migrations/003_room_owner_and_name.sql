-- A class belongs to the account that created it, and has a name.
--
-- `host_user_id` is the creator's Open edX id (the login token's `sub`), the same
-- opaque, unchanging id `room_members` is keyed by. It is the room's teacher. NULL for
-- rooms made before accounts existed: nobody owns those, so nobody can manage them.
-- That is deliberate -- guessing an owner for a room we know nothing about would hand
-- it to the wrong person.
--
-- `name` is what the class is called in a student's list. NULL falls back to the text's
-- title.
--
-- One ALTER, because MySQL 8 runs a single ALTER atomically. Two would leave a half-
-- migrated table if the second failed, and this runner applies each file once.
alter table rooms
  add column name         varchar(255) null,
  add column host_user_id varchar(255) collate utf8mb4_bin null,
  add index rooms_host_user_idx (host_user_id);
