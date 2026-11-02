# Test-only image for the live dashboard e2e (make test-e2e): Microsoft's Playwright base (browsers) + the package.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble@sha256:a7f6cf3ae520c9d670ad956572c13747ed5abdbba5123a01526f873ed1662528
RUN pip install --no-cache-dir --break-system-packages playwright==1.56.0
