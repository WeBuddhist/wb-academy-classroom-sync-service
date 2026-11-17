-- Who is in a class.
--
-- A member is an Academy account, keyed by the login token's `sub` (opaque and
-- unchanging, so a renamed account is the same member). `username` and `name` are copied
-- in only so a teacher's member list can show someone: a display convenience, refreshed
-- each time the person joins, never used to decide anything.
--
-- Membership is what makes a class one-time to join. The code is needed once; after
-- that the class is in the person's own list, from any device.
--
-- `role` is 'teacher' for the creator and 'student' for everyone else. No co-teachers.
-- Stored here as well as in `rooms.host_user_id` so a student's list can be read from
-- one table.
--
-- Removing a member deletes the row. They can rejoin with the code, which is why a
-- teacher who wants someone gone for good also changes the code.
create table if not exists room_members (
  room_id      varchar(255) collate utf8mb4_bin not null,
  user_id      varchar(255) collate utf8mb4_bin not null,
  username     varchar(255) not null,
  name         varchar(255) not null default '',
  role         varchar(16)  not null default 'student',
  joined_at    timestamp    not null default current_timestamp,
  last_seen_at timestamp    not null default current_timestamp,
  primary key (room_id, user_id),
  -- "Which classes am I in" is the question asked on every visit to the code screen.
  key room_members_user_idx (user_id),
  constraint room_members_room_fk
    foreign key (room_id) references rooms (id) on delete cascade
) engine=InnoDB default charset=utf8mb4;
