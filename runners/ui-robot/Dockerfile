# UI robot: the few steps the PRA / Password Safe APIs cannot do (see README.md).
# The Playwright image ships Chromium and its system libraries; pin the tag to the
# playwright version in requirements.txt or the browser build will not match.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY robot ./robot

# Downloads land here; mount a volume if you want to keep them.
ENV ROBOT_DOWNLOAD_DIR=/work
RUN mkdir -p /work
USER pwuser
ENTRYPOINT ["python", "-m", "robot"]
