FROM python:3.12-slim
ENV PYTHONPATH=/app:/candidate PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY runner_support/container_main.py /app/container_main.py
COPY runner_support/self_heal /app/self_heal
USER 65534:65534
CMD ["python", "/app/container_main.py"]
