FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py .
COPY main.py .
COPY health.py .
COPY core/ core/
COPY agents/ agents/
COPY bot/ bot/

CMD ["python", "main.py"]
