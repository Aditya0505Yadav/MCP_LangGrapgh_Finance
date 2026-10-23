FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /code

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# API (8000) and MCP server (8001); compose selects the command per service.
EXPOSE 8000 8001

CMD ["python", "-m", "app.main"]
