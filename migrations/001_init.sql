-- Texts imported from a TextProvider. A live class reads only from here, never from
-- the provider, so a content-API outage can never interrupt a lesson.
create table if not exists texts (
  id         text primary key,
  slug       text unique not null,
  title_bo   text not null,
  title_en   text,
  source     text not null default 'fixture',
  created_at timestamptz not null default now()
);

-- Segment ids are produced by the content pipeline and adopted as given. They are only
-- unique *within* a text — every text has an s-0001 — so the key is composite.
create table if not exists segments (
  id          text not null,
  text_id     text not null references texts(id) on delete cascade,
  idx         integer not null,
  kind        text not null,
  bo          text not null,
  break_after text not null default 'minor',
  cite        text,
  wylie       text,
  en          text,
  note        text,
  primary key (text_id, id),
  unique (text_id, idx)
);

create table if not exists rooms (
  id                 text primary key,
  code               text unique not null,
  text_id            text not null references texts(id),
  -- sha256 of the host token. The token itself is shown once, at creation, and never
  -- stored, so a database leak cannot be used to take over a room.
  host_token_hash    text not null,
  current_segment_id text,
  is_live            boolean not null default true,
  created_at         timestamptz not null default now(),
  closed_at          timestamptz,
  -- A composite foreign key is satisfied when any column is null, so this permits a
  -- room that has not been moved anywhere yet while still rejecting a segment id that
  -- belongs to a different text.
  foreign key (text_id, current_segment_id) references segments (text_id, id)
);

-- Every move the teacher makes, in order. Cheap to write and it is what a future
-- session replay (or alignment against a class recording) will be built from.
create table if not exists room_log (
  id         bigserial primary key,
  room_id    text not null references rooms(id) on delete cascade,
  segment_id text not null,
  at         timestamptz not null default now()
);

create index if not exists room_log_room_at_idx on room_log (room_id, at);
