FROM python:3.12-alpine

WORKDIR /app

RUN addgroup -g 1001 -S appgroup && \
    adduser -u 1001 -S appuser -G appgroup

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py linkwarden.py selection.py submissions.py scan_export.py scan_jobs.py ./
RUN mkdir -p /app/data && chown -R appuser:appgroup /app

USER appuser

CMD ["python", "bot.py"]
