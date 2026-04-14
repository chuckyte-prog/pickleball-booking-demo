FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "-c", "import os,uvicorn; uvicorn.run('api:app', host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))"]
