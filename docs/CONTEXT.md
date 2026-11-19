# The systems around this service

Written after reading the actual code, not from general Open edX knowledge. Everything
here was checked in this install.

## The five pieces

| Piece | What it is | Our use |
|---|---|---|
| **Tutor** | Runs Open edX in Docker. Config in `config.yml`, generated output in `env/`. | Deploys this service |
| **edx-platform** | The LMS and Studio. Django. | We never edit it |
| **sherab-custom-plugin** | Our Django apps, installed into the LMS | Track C: notes |
| **frontend-app-extension** | Our React MFE, served at `/extension/` | Track B: the reader |
| **brand-openedx** | Paragon theme and SCSS for all MFEs | Track B: styles |

`env/` is **generated**. Editing it by hand is lost on the next `tutor config save`;
everything is changed through `config.yml` or a plugin.

## How a Tutor plugin works

A single `.py` file in `tutor-plugins/`, enabled with `tutor plugins enable <name>`. It
registers hooks: `CONFIG_DEFAULTS` and `CONFIG_UNIQUE` for settings, `IMAGES_BUILD` to
build a container, `CLI_DO_INIT_TASKS` for one-time setup, and `ENV_PATCHES` to inject
text into generated files.

Live examples in this install: `studio_tool_service.py` (our own service, built from a
checkout), `extension_mfe.py` (registers our MFEs through `MFE_APPS`), and the upstream
`tutornotes` (a service with its own MySQL database). Track A Step 5 of the build plan
is written from these.

## How a backend feature is added (the `wishlist` pattern)

`sherab-custom-plugin` is a Python package of Django apps. Open edX finds them through
`setup.py` entry points:

```python
"lms.djangoapp": [
    "wishlist = wishlist.apps:WishlistConfig",
],
```

Each app's `apps.py` declares a `plugin_app` dict saying where its URLs mount and which
settings modules to load. The app then looks like any Django app: `models.py`,
`serializers.py`, `views.py`, `urls.py`, `migrations/`.

Views authenticate like this, every time:

```python
AUTHENTICATION_CLASSES = (JwtAuthentication, SessionAuthentication)

class WishlistListCreateView(APIView):
    authentication_classes = AUTHENTICATION_CLASSES
    permission_classes = (IsAuthenticated,)
```

JWT for MFE calls, session for legacy Mako pages.

**Two things to copy for notes:**

- `Wishlist.course` is a ForeignKey with **`db_constraint=False`**, because the target
  lives in another app's table. Notes will reference segment ids living in a different
  *database* entirely, so they get no ForeignKey at all — the same reasoning, further
  along.
- Migrations are generated from the edx-platform directory, not this one:
  `./manage.py lms makemigrations`. The model docstring says so; it is easy to miss.

Register notes under `lms.djangoapp` only. Studio has no use for them.

## How a frontend page is added

`src/index.tsx` is the whole router:

```tsx
<Route
  path="/wishlist"
  element={<AuthenticatedPageRoute><WishlistPage /></AuthenticatedPageRoute>}
/>
<Route path="/start-campus" element={<StartCampusPage />} />
```

`AuthenticatedPageRoute` redirects anonymous visitors to login. `start-campus` shows what
a public page looks like. **This is the Track B Step 1 template**, and it is two lines.

Confirmed versions: React 18.3.1, react-router-dom 6.30.3, `@tanstack/react-query` v5,
Paragon 23, `@edx/frontend-platform` 8.5.1, built by `fedx-scripts` (webpack), served at
`PUBLIC_PATH=/extension/`.

Backend calls go through frontend-platform, which attaches the JWT and refreshes it:

```ts
const { data } = await getAuthenticatedHttpClient().get(url);
return camelCaseObject(data);
```

## Three findings that affect the build plan

### 1. The camelCase convention does not reach the socket

MFE code calls `camelCaseObject(data)` on HTTP responses **by hand**, as a convention.
`socket.io-client` has no such step and no such convention.

So a Python developer writing `segment_id` would find it *works* over the HTTP routes —
`camelCaseObject` quietly fixes it — and *silently fails* over the socket. The same
mistake, two different outcomes, in one feature.

This makes the contract hazard sharper than
[`socket-contract.md`](socket-contract.md) first described it. The smoke test still
catches it; the point is that partial success will make it look like something else.

### 2. `AuthenticatedPageRoute` guards the page, not the API

It is a React wrapper. It redirects a browser; it does not protect an endpoint. Nothing
stops a direct call to `/classroom-api/*`.

So "joining a room requires login" is currently a property of the UI. Whether that is
enough is a real question we have not answered.

The good news is that it is a choice rather than a blocker: `SESSION_COOKIE_DOMAIN` is
`local.openedx.io`, which browsers send to `apps.local.openedx.io` as well, and our route
lives on the MFE host. So the cookie and JWT **do** reach this service, and it could
validate them if we decide it should.

Worth deciding before Track B Step 3, when the join path is first written.

### 3. The MFE host is where our route lives, and that is what avoids CORS

`MFE_HOST` is `apps.local.openedx.io`; the LMS is `local.openedx.io`. `tutormfe` leaves
`{{ patch("caddyfile-mfe-proxy") }}` inside its own Caddy site block, so
`/classroom-api/*` can hang off the MFE host — same origin as the page.

If it had to go on its own subdomain, we would need CORS, a second certificate, and the
socket's origin checks would need changing too.
