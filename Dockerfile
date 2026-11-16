# Tutor runs this as a service container alongside the LMS. It is never pip-installed
# by anything else, which is why there is no wheel build here — the source is copied in
# and run in place.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

COPY src/ ./src/
COPY migrations/ ./migrations/

# Not root. The service needs nothing on disk beyond what it was built with.
RUN useradd --create-home --uid 10001 classroom
USER classroom

EXPOSE 8000

# One worker, deliberately. Socket.IO keeps connections in process memory, so two
# workers would put a teacher and their students in different rooms with no shared
# state between them. Scaling past one process needs Redis, which the plan defers
# until there is a reason for it.
CMD ["uvicorn", "classroom_sync.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
