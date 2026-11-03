FROM python:3.12-alpine
WORKDIR /app
COPY shopflow/ /app/shopflow/
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
USER 10001
CMD ["python", "-m", "shopflow.gateway"]
