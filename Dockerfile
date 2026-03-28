FROM nikolaik/python-nodejs:python3.11-nodejs20

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Node.js dependencies (framer-api SDK)
COPY package.json .
RUN npm install --omit=dev

# App source
COPY config.py .
COPY main.py .
COPY health.py .
COPY framer_helper.js .
COPY core/ core/
COPY agents/ agents/
COPY bot/ bot/

CMD ["python", "main.py"]
