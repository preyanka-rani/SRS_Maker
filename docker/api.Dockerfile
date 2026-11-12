FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libmagic1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.in .
RUN pip install --no-cache-dir -r requirements.in
COPY . .
RUN useradd -m app && chown -R app /srv
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
