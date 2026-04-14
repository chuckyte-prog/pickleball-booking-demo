FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
ENV PORT=8000
CMD ["/bin/sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
