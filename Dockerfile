FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 notifier \
    && useradd --uid 10001 --gid notifier --no-create-home notifier \
    && mkdir /data && chown notifier:notifier /data
COPY notifier/ ./notifier/
COPY start.py ./
USER 10001:10001
ENV STATE_DIR=/data
STOPSIGNAL SIGTERM
CMD ["python", "start.py"]
