-- MySQL 8.4 (Tutor's own instance).
--
-- This server stores SEGMENT IDS, NOT TEXT. Nothing here holds a word of any work.
--
-- The reader fetches content from the OpenPecha library directly, so the sync server
-- never serves it and has no reason to keep it. What it keeps is the id list for each
-- text it has seen, which is what lets it answer the only question it actually needs
-- to: "is this segment part of this room's text?" Without that, a bad id would either
-- go unchecked or put the library on the live path of every teacher tap.
--
-- This replaces the original design's import pipeline: a text is registered the first
-- time a room is created against it, by fetching its segment list once.
--
-- Two MySQL-specific points, both load-bearing:
--
-- 1. Identifier columns are `utf8mb4_bin`. Tutor runs MySQL with
--    `--collation-server=utf8mb4_unicode_ci`, which is CASE-INSENSITIVE. Real segment
--    ids are mixed-case nanoids (`RcoqgTCZ3KpivQ9h1fAr9`), so case is the difference
--    between two ids, and these are opaque values owned by the content pipeline —
--    we are not entitled to assume their case is insignificant. Binary collation
--    restores Postgres behaviour. It also affects sort order, which costs nothing:
--    ordering is by `idx`, never by id.
--
-- 2. Indexed columns are `VARCHAR`, because MySQL cannot index `TEXT` without a
--    prefix length.

-- One row per text AS ONE EDITION CUTS IT INTO LINES. Named only well enough to show
-- in a room listing. Deliberately NOT a copy of the text: no body, no translations,
-- no annotations.
--
-- Why the key is a segmentation and not a text. In the library a work has editions,
-- and an edition has a segmentation; segment ids belong to the SEGMENTATION. Two
-- editions of the same work give different ids for the same line. The library resolves
-- a bare text id by taking the first edition it lists -- fine for one reader browsing,
-- wrong for thirty people who must agree on line 7, because nothing promises that
-- ordering holds for the life of a room. So a room pins what the ids actually came
-- from, and the resolution happens once, here, at registration.
create table if not exists segmentations (
  -- The library's segmentation id, adopted exactly as given. Never generated here, so
  -- room logs stay valid if a text is re-registered.
  id         varchar(255) collate utf8mb4_bin not null,
  -- What the client resolves and fetches from. The library's lookup accepts a text or
  -- edition id, not a segmentation id, so the reader needs this one to load anything.
  edition_id varchar(255) collate utf8mb4_bin not null,
  -- The work this is an edition of. Display and search only; never the sync key.
  text_id    varchar(255) collate utf8mb4_bin not null,
  -- Cached from the library at registration so listing a room needs no upstream call.
  -- A display convenience; the reader takes the authoritative title from the library.
  title      varchar(512) not null,
  language   varchar(16),
  -- When the segment ids below were last fetched. A text re-registered after the
  -- pipeline re-segments it will have ids that no longer match older room logs.
  synced_at  timestamp    not null default current_timestamp,
  created_at timestamp    not null default current_timestamp,
  primary key (id),
  -- Not unique: registration resolves one segmentation per edition, but nothing we
  -- have verified promises the library holds only one, and guessing wrong here would
  -- reject a legitimate registration at class time.
  key segmentations_edition_idx (edition_id),
  key segmentations_text_idx (text_id)
) engine=InnoDB default charset=utf8mb4;

-- The id list, in reading order. That is the whole table — there is no content column
-- and there should never be one.
--
-- The key is composite for two reasons, neither of them id collisions. Real ids are
-- 21-character nanoids (`RcoqgTCZ3KpivQ9h1fAr9`), checked against the live library, so
-- they do appear globally unique — but they are opaque values owned by the content
-- pipeline and we are not entitled to depend on that. More concretely, `rooms` guards
-- its current segment with a composite foreign key onto `(segmentation_id, id)`, and
-- that guard needs this key to point at.
create table if not exists segments (
  id              varchar(128) collate utf8mb4_bin not null,
  segmentation_id varchar(255) collate utf8mb4_bin not null,
  -- Display order, from the library. Safe to renumber on a re-sync: nothing external
  -- references it, and ids are what travel over the wire.
  idx             int not null,
  primary key (segmentation_id, id),
  unique key segments_segmentation_idx_key (segmentation_id, idx),
  constraint segments_segmentation_fk
    foreign key (segmentation_id) references segmentations (id) on delete cascade
) engine=InnoDB default charset=utf8mb4;

create table if not exists rooms (
  id                 varchar(255) collate utf8mb4_bin not null,
  -- Codes are generated uppercase and looked up uppercased, so binary collation here
  -- costs nothing and keeps the unique index exact.
  code               varchar(16)  collate utf8mb4_bin not null,
  -- The room's whole text identity. `edition_id` and `title` for RoomState are read by
  -- joining `segmentations` rather than copied here, so there is one source of truth
  -- for what a room is about.
  segmentation_id    varchar(255) collate utf8mb4_bin not null,
  -- sha256 of the host token, as 64 hex characters. The token itself is shown once, at
  -- creation, and never stored, so a database leak cannot be used to take over a room.
  host_token_hash    char(64)     collate utf8mb4_bin not null,
  current_segment_id varchar(128) collate utf8mb4_bin,
  is_live            boolean      not null default true,
  created_at         timestamp    not null default current_timestamp,
  closed_at          timestamp    null default null,
  primary key (id),
  unique key rooms_code_key (code),
  constraint rooms_segmentation_fk
    foreign key (segmentation_id) references segmentations (id),
  -- The guard this whole table exists for. A composite foreign key is satisfied when
  -- any column is null, so this permits a room that has not been moved anywhere yet
  -- while still rejecting a segment id belonging to a different segmentation. InnoDB
  -- behaves as Postgres does here — verified, not assumed.
  constraint rooms_segment_fk
    foreign key (segmentation_id, current_segment_id)
      references segments (segmentation_id, id)
) engine=InnoDB default charset=utf8mb4;

-- Every move the teacher makes, in order. Cheap to write and it is what a future
-- session replay (or alignment against a class recording) will be built from.
create table if not exists room_log (
  id         bigint       not null auto_increment,
  room_id    varchar(255) collate utf8mb4_bin not null,
  -- Intentionally no foreign key: the log is history and must survive a text being
  -- re-registered with different ids. It records what was taught, not what is valid now.
  segment_id varchar(128) collate utf8mb4_bin not null,
  at         timestamp    not null default current_timestamp,
  primary key (id),
  -- Declared inline rather than as a separate statement: MySQL has no
  -- `create index if not exists`, so this is what keeps the migration re-runnable.
  key room_log_room_at_idx (room_id, at),
  constraint room_log_room_fk
    foreign key (room_id) references rooms (id) on delete cascade
) engine=InnoDB default charset=utf8mb4;
