FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV CATALOG_PATH=/app/services.yaml
EXPOSE 8080

CMD ["python", "app.py"]
