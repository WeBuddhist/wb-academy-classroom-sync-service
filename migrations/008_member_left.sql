-- A student can leave a class and come back to it without the code.
--
-- Leaving does not delete the membership: it stamps `left_at`. The class stays in the student's
-- list, marked as left, and a press of "Rejoin" clears the stamp. While it is set the student
-- is not counted, is not in the teacher's member list, and cannot enter the live class or add
-- notes (the notes they made are kept and readable). A teacher *removing* someone still deletes
-- the row, which is what makes the code needed again.
alter table room_members
  add column left_at timestamp null;
