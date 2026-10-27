FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLOUD_MODE=1 \
    HOST=0.0.0.0 \
    DATA_DIR=/app/data
WORKDIR /app
COPY . /app
EXPOSE 10000
CMD ["python", "server.py"]
