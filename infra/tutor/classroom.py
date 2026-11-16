"""Tutor plugin for the classroom sync service: live text sync for Tibetan classes.

The teacher drives; every student's reader highlights and scrolls to the same line.
The only thing that travels between them is a segment id.

Runs as a container beside the LMS, the way `studio_tool_service.py` and `tutornotes`
do, with its own MySQL database. `tutor images build classroom` builds the image —
from a checkout on this machine if CLASSROOM_SOURCE_DIR is set, otherwise straight from
the repository. Nothing is published to a registry.

This install uses `tutor dev` locally and `tutor local` on the server. Both are served
here: dev overlays local, so the service itself is declared once and the dev patch adds
only what differs — a source mount, and a wider WEB_ORIGIN for the MFE's dev port.

Setup: that repo's docs/BUILD-PLAN.md, Track A Step 5.
"""
import os

from tutor import hooks
from tutormfe.hooks import MFE_APPS

VERSION = "0.1.0"

hooks.Filters.CONFIG_DEFAULTS.add_items([
    ("CLASSROOM_VERSION", VERSION),
    ("CLASSROOM_DOCKER_IMAGE", "classroom-sync:{{ CLASSROOM_VERSION }}"),
    # Where the image is built from. On a laptop: the absolute path of a checkout, whose
    # src/ is also mounted in `tutor dev`. On a server, leave it empty and the image is
    # built from the repository at this branch or tag.
    ("CLASSROOM_SOURCE_DIR", ""),
    ("CLASSROOM_REPOSITORY", "https://github.com/WeBuddhist/wb-academy-classroom-sync-service.git"),
    ("CLASSROOM_REPOSITORY_VERSION", "main"),
    # Its own database beside the LMS and notes ones. Holds segment ids and rooms —
    # never a word of any text.
    ("CLASSROOM_MYSQL_DATABASE", "classroom"),
    ("CLASSROOM_MYSQL_USERNAME", "classroom"),
    # The OpenPecha library. The key lives here and in the service; it never reaches a
    # browser, which is why the reader's library calls are proxied through the service.
    ("CLASSROOM_LIBRARY_BASE_URL", "https://library.webuddhist.com"),
    ("CLASSROOM_LIBRARY_APP_NAME", ""),
    ("CLASSROOM_LIBRARY_API_KEY", ""),
    # The service is one process holding sockets, not a worker pool. It is small.
    ("CLASSROOM_MEMORY_LIMIT", "512m"),
    # `tutor dev` only. Caddy runs under `tutor local`, so in dev the browser reaches
    # this service directly, like the LMS on 8000 and the MFEs on 1995-2004. Chosen
    # clear of both ranges.
    ("CLASSROOM_DEV_PORT", 8010),
])

# Generated once by `tutor config save` and kept in config.yml. This is why
# HOST_PASSWORD is never "change-me" in a real deployment: the service's own warning
# about the default is for people running it outside Tutor.
hooks.Filters.CONFIG_UNIQUE.add_items([
    ("CLASSROOM_HOST_PASSWORD", "{{ 32|random_string }}"),
    ("CLASSROOM_MYSQL_PASSWORD", "{{ 8|random_string }}"),
])


# Tutor's random_string is alphanumeric, so neither password needs percent-encoding in
# this URL. A hand-set password containing @ or / would — the service decodes it.
DATABASE_URL = (
    "mysql://{{ CLASSROOM_MYSQL_USERNAME }}:{{ CLASSROOM_MYSQL_PASSWORD }}"
    "@{{ MYSQL_HOST }}:{{ MYSQL_PORT }}/{{ CLASSROOM_MYSQL_DATABASE }}"
)

# In production the page and the API share an origin, so this is only ever a fallback.
PROD_WEB_ORIGIN = "{% if ENABLE_HTTPS %}https{% else %}http{% endif %}://{{ MFE_HOST }}"


# One definition, used by the service and by the job container that runs its init
# task. Kept in one place so the two cannot drift: a migration applied against a
# different database than the service reads is a bad afternoon.
ENVIRONMENT = """
    DATABASE_URL: "%s"
    HOST_PASSWORD: "{{ CLASSROOM_HOST_PASSWORD }}"
    LIBRARY_BASE_URL: "{{ CLASSROOM_LIBRARY_BASE_URL }}"
    LIBRARY_APP_NAME: "{{ CLASSROOM_LIBRARY_APP_NAME }}"
    LIBRARY_API_KEY: "{{ CLASSROOM_LIBRARY_API_KEY }}"
    WEB_ORIGIN: "%s"
""".rstrip() % (DATABASE_URL, PROD_WEB_ORIGIN)


hooks.Filters.ENV_PATCHES.add_items([
    (
        "local-docker-compose-services",
        """
classroom:
  image: {{ CLASSROOM_DOCKER_IMAGE }}
  environment:%s
  mem_limit: {{ CLASSROOM_MEMORY_LIMIT }}
  restart: unless-stopped
  depends_on: {{ [("mysql", RUN_MYSQL)]|list_if }}
""" % ENVIRONMENT,
    ),
    (
        # The one-off container Tutor runs init tasks in. Without this,
        # `tutor dev do init` fails with "no such service: classroom-job".
        # No restart policy and no memory limit: it runs once and exits.
        "local-docker-compose-jobs-services",
        """
classroom-job:
  image: {{ CLASSROOM_DOCKER_IMAGE }}
  environment:%s
  depends_on: {{ [("mysql", RUN_MYSQL)]|list_if }}
""" % ENVIRONMENT,
    ),
    (
        # `tutor dev` overlays this file on top of the one above, so only the
        # differences belong here. Two of them, and both are load-bearing.
        "local-docker-compose-dev-services",
        """
classroom:
  # **Caddy does not run in `tutor dev`** -- it is declared in
  # docker-compose.prod.yml, which only `tutor local` loads. So the /classroom-api
  # route does not exist here and the browser cannot reach this service at all
  # unless its port is published, the way the LMS and the MFEs publish theirs.
  ports:
    - "{{ CLASSROOM_DEV_PORT }}:8000"
  environment:
    # The extension MFE serves itself on its own port in dev, so the page and this
    # service are on different origins. The same-origin argument that holds in
    # production does not hold here, and both CORS and the Socket.IO origin check
    # would refuse the page without this.
    WEB_ORIGIN: "http://localhost:{{ get_mfe('extension')['port'] }},http://{{ MFE_HOST }}:{{ get_mfe('extension')['port'] }},http://localhost:{{ CLASSROOM_DEV_PORT }},http://{{ MFE_HOST }}"
{% if CLASSROOM_SOURCE_DIR %}
  volumes:
    - "{{ CLASSROOM_SOURCE_DIR }}/src:/app/src:ro"
{% endif %}
""",
    ),
    (
        # tutor-mfe leaves this patch point *inside* the MFE's own site block, so the
        # route hangs off the MFE host rather than taking a subdomain of its own.
        # Caddy orders handle_path ahead of the block's catch-all proxy.
        "caddyfile-mfe-proxy",
        """
handle_path /classroom-api/* {
    reverse_proxy classroom:8000
}
""",
    ),
    (
        # So the page does not have to guess where its API is. In dev it crosses to
        # Caddy's origin; in production it is the same origin as the page.
        "mfe-env-config-runtime-definitions-extension",
        """
// In production Caddy puts this on the MFE's own origin, so it is a path. In dev
// there is no Caddy, so it is the service's published port -- a different origin,
// which is why the dev patch widens WEB_ORIGIN to match.
config.CLASSROOM_API_URL = process.env.NODE_ENV === 'development'
  ? 'http://localhost:{{ CLASSROOM_DEV_PORT }}'
  : '{% if ENABLE_HTTPS %}https{% else %}http{% endif %}://{{ MFE_HOST }}/classroom-api';
""",
    ),
])


# --- one-time setup, run by `tutor dev init` / `tutor local init` -----------------

# Creating the database needs the mysql client, which lives in the mysql container.
MYSQL_INIT = """
mysql -u {{ MYSQL_ROOT_USERNAME }} --password="{{ MYSQL_ROOT_PASSWORD }}" --host "{{ MYSQL_HOST }}" --port {{ MYSQL_PORT }} -e 'CREATE DATABASE IF NOT EXISTS {{ CLASSROOM_MYSQL_DATABASE }};'
mysql -u {{ MYSQL_ROOT_USERNAME }} --password="{{ MYSQL_ROOT_PASSWORD }}" --host "{{ MYSQL_HOST }}" --port {{ MYSQL_PORT }} -e "CREATE USER IF NOT EXISTS '{{ CLASSROOM_MYSQL_USERNAME }}';"
mysql -u {{ MYSQL_ROOT_USERNAME }} --password="{{ MYSQL_ROOT_PASSWORD }}" --host "{{ MYSQL_HOST }}" --port {{ MYSQL_PORT }} -e "ALTER USER '{{ CLASSROOM_MYSQL_USERNAME }}'@'%' IDENTIFIED BY '{{ CLASSROOM_MYSQL_PASSWORD }}';"
mysql -u {{ MYSQL_ROOT_USERNAME }} --password="{{ MYSQL_ROOT_PASSWORD }}" --host "{{ MYSQL_HOST }}" --port {{ MYSQL_PORT }} -e "GRANT ALL ON {{ CLASSROOM_MYSQL_DATABASE }}.* TO '{{ CLASSROOM_MYSQL_USERNAME }}'@'%';"
"""

# The schema lives in the service's own image, which has no mysql client, so it is
# applied from Python. Safe on every init: the migration is `create table if not
# exists` throughout.
CLASSROOM_INIT = """
python -m classroom_sync.migrate
"""

hooks.Filters.CLI_DO_INIT_TASKS.add_items([
    ("mysql", MYSQL_INIT),
    ("classroom", CLASSROOM_INIT),
])


# --- building the image -----------------------------------------------------------

# Tutor builds every image from a folder inside its env folder, so a build from the
# repository is listed with this placeholder folder and _build_from_repository swaps
# it for the repository URL. Docker fetches a git URL itself, so nothing is cloned.
FROM_REPOSITORY = os.path.join("plugins", "classroom", "from-repository")
_repository_url = {"value": ""}


@hooks.Filters.IMAGES_BUILD.add()
def _build_image(images, config):
    source_dir = config["CLASSROOM_SOURCE_DIR"]
    if source_dir:
        images.append(("classroom", source_dir, "{{ CLASSROOM_DOCKER_IMAGE }}", ()))
    elif config["CLASSROOM_REPOSITORY"]:
        _repository_url["value"] = (
            f'{config["CLASSROOM_REPOSITORY"]}#{config["CLASSROOM_REPOSITORY_VERSION"]}'
        )
        images.append(("classroom", FROM_REPOSITORY, "{{ CLASSROOM_DOCKER_IMAGE }}", ()))
    return images


@hooks.Filters.DOCKER_BUILD_COMMAND.add()
def _build_from_repository(command):
    if command and command[-1].endswith(FROM_REPOSITORY) and _repository_url["value"]:
        command = [*command[:-1], _repository_url["value"]]
    return command
