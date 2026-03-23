FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY agent_voyage.py .
COPY veille_lundi.py .
COPY email_rapport.py .
COPY analytics.py .
COPY search_console.py .
CMD ["python", "bot.py"]
