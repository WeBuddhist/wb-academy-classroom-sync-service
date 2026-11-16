"""The HTTP routes, against a running server. Run by hand.

    API_URL=http://localhost:18000 HOST_PASSWORD=… python scripts/check_rooms.py

Talks over HTTP only and never imports server code, like tests/smoke.mjs — so it tests
whatever is actually running. Needs a real library and a real MySQL behind it, which is
why it is not a pytest.
"""

import os
import sys
import urllib.parse

import httpx

URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
PASSWORD = os.environ.get("HOST_PASSWORD", "change-me")

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok    {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def main() -> int:
    with httpx.Client(timeout=60) as http:
        # Let httpx encode the query. Hand-written percent-encoding of Tibetan is how
        # you end up searching for a word one code point different from the one you
        # meant, and blaming the server.
        found = http.get(f"{URL}/api/texts", params={"title": "བྱང་ཆུབ", "limit": 3})
        texts = found.json() if found.status_code == 200 else []
        check("searching the library returns texts", len(texts) > 0, str(found.status_code))
        if not texts:
            return 1
        check("an empty search lists rather than erroring",
              http.get(f"{URL}/api/texts", params={"limit": 2}).status_code == 200)

        text_id = texts[0]["id"]

        check("a wrong host password is refused",
              http.post(f"{URL}/api/rooms",
                        json={"hostPassword": "not-it", "textId": text_id}
                        ).status_code == 401)

        created = http.post(f"{URL}/api/rooms",
                            json={"hostPassword": PASSWORD, "textId": text_id})
        check("a room is created", created.status_code == 201, created.text[:120])
        if created.status_code != 201:
            return 1
        room = created.json()
        code, token = room["code"], room["hostToken"]
        check("the room pins an edition and a segmentation",
              bool(room["editionId"]) and bool(room["segmentationId"]))
        check("the room knows how many segments it has", room["segmentCount"] > 0,
              str(room["segmentCount"]))

        looked_up = http.get(f"{URL}/api/rooms/{code.lower()}")
        check("a student typing lowercase finds the room",
              looked_up.status_code == 200, str(looked_up.status_code))
        state = looked_up.json() if looked_up.status_code == 200 else {}
        check("a new room has no current line", state.get("currentSegmentId") is None)
        check("a new room is live", state.get("isLive") is True)

        check("an unknown room code is a 404",
              http.get(f"{URL}/api/rooms/ZZZZZZ").status_code == 404)

        check("a wrong recovery code is refused",
              http.post(f"{URL}/api/rooms/{code}/verify-host",
                        json={"hostToken": "not-the-real-token"}).status_code == 401)
        check("an empty recovery code is refused",
              http.post(f"{URL}/api/rooms/{code}/verify-host",
                        json={"hostToken": ""}).status_code == 401)
        check("the real recovery code is accepted",
              http.post(f"{URL}/api/rooms/{code}/verify-host",
                        json={"hostToken": token}).status_code == 200)

        second = http.post(f"{URL}/api/rooms",
                           json={"hostPassword": PASSWORD, "textId": text_id}).json()
        check("two rooms on one text get different codes", second["code"] != code)
        check("two rooms on one text get different host tokens",
              second["hostToken"] != token)

    print(f"\n{passed}/{passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
