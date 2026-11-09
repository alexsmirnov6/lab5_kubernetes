FROM python:3.12.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY data/vacancies.ndjson /data/vacancies.ndjson
ENV PYTHONUNBUFFERED=1
USER 1000:1000
ENTRYPOINT ["python", "src/pairs.py"]
