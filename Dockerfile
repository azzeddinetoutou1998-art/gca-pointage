FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py templates_html.py ./
COPY static ./static
ENV PORT=8000 DB_PATH=/data/pointage.db
VOLUME /data
EXPOSE 8000
# 1 seul worker + threads : SQLite et la limitation d'essais sont gérés en mémoire
CMD ["sh", "-c", "gunicorn -w 1 --threads 4 -b 0.0.0.0:${PORT} app:app"]
