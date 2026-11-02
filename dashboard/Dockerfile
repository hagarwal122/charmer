# Charmer dashboard. Build context is the repo root: it needs plugin/store.py, the shared SQLite schema.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY dashboard/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY plugin/store.py /app/store.py
COPY dashboard/app.py dashboard/state.py dashboard/auth.py /app/
COPY dashboard/templates /app/templates
COPY dashboard/static /app/static
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')"
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
