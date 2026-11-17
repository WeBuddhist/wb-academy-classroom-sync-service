-- The teacher is the account that created the class (`rooms.host_user_id`), so the
-- per-room host token and its recovery code are gone. New rooms are created without one,
-- so the column must allow NULL. Left in place rather than dropped, so this migration
-- cannot lose anything: existing rows keep their old hash, which nothing reads.
alter table rooms
  modify host_token_hash char(64) collate utf8mb4_bin null;
