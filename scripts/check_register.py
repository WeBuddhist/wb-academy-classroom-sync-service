"""End to end: find a text in the library, store its ids, check them back.

    DATABASE_URL=… LIBRARY_BASE_URL=… LIBRARY_APP_NAME=… LIBRARY_API_KEY=… \
      python scripts/check_register.py "སྤྱོད་འཇུག"

Run by hand, against a real library and a real MySQL. Not a pytest, for the same
reason as check_library.py: CI must not depend on either being up.
"""

import asyncio
import os
import sys

from classroom_sync.db import Database
from classroom_sync.library import LibraryClient


async def main(title: str) -> int:
    library = LibraryClient(
        os.environ["LIBRARY_BASE_URL"],
        os.environ["LIBRARY_APP_NAME"],
        os.environ["LIBRARY_API_KEY"],
    )
    database = Database(os.environ["DATABASE_URL"])
    await database.connect()
    try:
        results = await library.search_texts(title, limit=1)
        if not results:
            print(f'nothing matches "{title}"')
            return 1
        text = results[0]
        print(f"1. found   {text.id}  {text.title}")

        context = await library.resolve(text.id)
        print(f"2. pinned  edition {context.edition_id}")
        print(f"           segmentation {context.segmentation_id}")

        ids = await library.segment_ids(context.edition_id)
        print(f"3. library returned {len(ids)} segment ids")

        stored = await database.register_text(
            context, ids, title=text.title, language=text.language
        )
        print(f"4. stored  {stored.segment_count} segments")
        if stored.segment_count != len(ids):
            print("   MISMATCH between what the library gave and what was stored")
            return 1

        again = await database.register_text(
            context, ids, title=text.title, language=text.language
        )
        print(f"5. re-registering is a no-op -> {again.segment_count} segments")

        print(f"6. is '{ids[0]}' part of this text? "
              f"{await database.has_segment(context.segmentation_id, ids[0])}")
        print(f"   is 'not-a-real-id'?           "
              f"{await database.has_segment(context.segmentation_id, 'not-a-real-id')}")

        flipped = ids[0].swapcase()
        if flipped != ids[0]:
            found = await database.has_segment(context.segmentation_id, flipped)
            print(f"   is '{flipped}' (case-flipped)? {found}"
                  f"{'   <- COLLATION BUG' if found else '   <- correct'}")

        print("\nOK.")
        return 0
    finally:
        await library.aclose()
        await database.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "སྤྱོད་འཇུག")))
