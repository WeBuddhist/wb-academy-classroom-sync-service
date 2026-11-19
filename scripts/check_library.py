"""Check this service can actually talk to the library. Run by hand, never in CI.

    LIBRARY_BASE_URL=https://… LIBRARY_APP_NAME=… LIBRARY_API_KEY=… \
      python scripts/check_library.py "spyod jug"

Walks the whole chain a room registration will walk — search, resolve, count segments —
and prints what it found. It reads ids and titles; it never asks for content.

Deliberately not a pytest: CI must not depend on an upstream service being up, and a
test that needs a secret is a test nobody runs.
"""

import asyncio
import os
import sys

from classroom_sync.library import LibraryClient, LibraryError


async def main(title: str) -> int:
    missing = [
        name
        for name in ("LIBRARY_BASE_URL", "LIBRARY_APP_NAME", "LIBRARY_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        print(f"Set {', '.join(missing)} first.")
        return 2

    library = LibraryClient(
        os.environ["LIBRARY_BASE_URL"],
        os.environ["LIBRARY_APP_NAME"],
        os.environ["LIBRARY_API_KEY"],
    )
    try:
        print(f'1. searching for "{title}" …')
        results = await library.search_texts(title, limit=5)
        if not results:
            print("   nothing found — try another title")
            return 1
        for result in results:
            print(f"   {result.id}  {result.language:4}  {result.title}")

        chosen = results[0]
        print(f"\n2. resolving {chosen.id} …")
        context = await library.resolve(chosen.id)
        print(f"   edition      {context.edition_id}")
        print(f"   segmentation {context.segmentation_id}")
        print(f"   text         {context.text_id}")

        print("\n3. reading segment ids …")
        ids = await library.segment_ids(context.edition_id)
        print(f"   {len(ids)} segments")
        if ids:
            print(f"   first {ids[0]}   last {ids[-1]}")
            if len(set(ids)) != len(ids):
                print("   WARNING: duplicate ids — the composite key would reject these")

        print("\nOK. A room registered against this text would store those ids.")
        return 0
    except LibraryError as error:
        print(f"\nFAILED: {error}")
        return 1
    finally:
        await library.aclose()


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "bodhicaryavatara"
    raise SystemExit(asyncio.run(main(query)))
