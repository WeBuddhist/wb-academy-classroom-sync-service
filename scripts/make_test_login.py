"""Make a throwaway Academy login for tests, with no LMS.

    python scripts/make_test_login.py            # prints KEY=VALUE lines

Generates an RSA key and prints, as environment lines:

  JWT_PUBLIC_JWK_SET, JWT_ISSUER, JWT_AUDIENCE   what the service needs to check logins
  AUTH_TOKEN                                      a token signed with the key

Start the service with the first three and give the smoke test `AUTH_TOKEN`. The token
looks like a real login cookie as far as the service can tell: RS512, no `kid`, which is
what a token the real LMS issued was found to look like.

`--user NAME` signs for someone else, to test two accounts against each other and print
`AUTH_TOKEN_<NAME>`. Used by CI; see .github/workflows/CI.yml.
"""

import argparse
import json
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "http://test.invalid/oauth2"
AUDIENCE = "openedx"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", action="append", default=["teacher"])
    args = parser.parse_args()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "openedx"
    print(f"JWT_PUBLIC_JWK_SET={json.dumps({'keys': [jwk]}, separators=(',', ':'))}")
    print(f"JWT_ISSUER={ISSUER}")
    print(f"JWT_AUDIENCE={AUDIENCE}")

    users = list(dict.fromkeys(args.user))  # keep order, drop repeats
    for index, name in enumerate(users):
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": int(time.time()) + 3600,
                "sub": f"test-{name}",
                "preferred_username": name,
                "name": name.title(),
            },
            key,
            algorithm="RS512",
        )
        print(f"AUTH_TOKEN_{name.upper()}={token}")
        if index == 0:
            print(f"AUTH_TOKEN={token}")


if __name__ == "__main__":
    main()
