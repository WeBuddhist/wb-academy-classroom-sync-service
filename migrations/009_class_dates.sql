-- When a class is held: an optional first and last day.
--
-- Plain dates, no time of day or time zone: a course runs "from 12 March to 30 April", and
-- the same dates read the same everywhere. Both are optional, and either can be set alone.
-- They are shown to people and never close the class by themselves: archiving is still the
-- teacher's act.
alter table rooms
  add column starts_on date null,
  add column ends_on   date null;
