FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY scenarios ./scenarios
COPY apps ./apps
RUN pip install --no-cache-dir -e '.[all]'
ENV PYTHONPATH=/app/src
CMD ["python", "scripts/production_smoke.py"]
