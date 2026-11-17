"""The library proxy, against a running server and the real library. Run by hand.

    API_URL=http://localhost:18000 python scripts/check_proxy.py

The point of the proxy is that the browser never holds the API key, so none of these
requests carries one. The point of its allowlist is that our key can write — so most of
what follows checks what is refused.
"""

import os
import sys

import httpx

URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
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
        # --- the reader's chain, no key in any of these requests -------------
        found = http.get(f"{URL}/library/v2/texts",
                         params={"title": "བྱང་ཆུབ", "limit": 2})
        check("search works without the caller holding a key",
              found.status_code == 200 and found.json().get("items"),
              str(found.status_code))
        text_id = found.json()["items"][0]["id"]

        editions = http.get(f"{URL}/library/v2/texts/{text_id}/editions",
                            params={"edition_type": "critical"})
        check("editions resolve", editions.status_code == 200 and editions.json())
        edition_id = editions.json()[0]["id"]

        seg = http.get(f"{URL}/library/v2/editions/{edition_id}/segmentation")
        check("segmentation resolves", seg.status_code == 200 and seg.json().get("id"))

        segs = http.get(f"{URL}/library/v2/editions/{edition_id}/segmentation/segments",
                        params={"limit": 5, "offset": 0})
        check("segment spans come back", segs.status_code == 200
              and len(segs.json().get("items", [])) > 0)

        content = http.get(f"{URL}/library/v2/editions/{edition_id}/content",
                           params={"span_start": 0, "span_end": 120})
        check("text content comes through", content.status_code == 200
              and len(content.text) > 10, str(content.status_code))

        # --- what must not get through ---------------------------------------
        for verb in ("POST", "PATCH", "PUT", "DELETE"):
            check(f"{verb} is not routed at all",
                  http.request(verb, f"{URL}/library/v2/texts/{text_id}"
                               ).status_code == 405)

        for path in ("v2/applications", "v2/categories",
                     f"v2/editions/{edition_id}/durchens",
                     f"v2/texts/{text_id}/tags/x"):
            check(f"/{path} is refused",
                  http.get(f"{URL}/library/{path}").status_code == 403)

        # --- transparency ------------------------------------------------------
        check("a real 404 is passed through, not flattened to 502",
              http.get(f"{URL}/library/v2/editions/nope123/segmentation"
                       ).status_code == 404)
        check("the library's 200-with-empty-list quirk survives",
              http.get(f"{URL}/library/v2/texts/doesnotexist123/editions"
                       ).json() == [],
              "resolveEditionContext relies on this to tell a text id from an edition id")

        leaked = [k for k in found.headers if k.lower() in ("x-api-key", "x-application")]
        check("no key is echoed to the browser", not leaked, str(leaked))

    print(f"\n{passed}/{passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
