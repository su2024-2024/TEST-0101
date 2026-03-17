FROM python:3.11-slim
WORKDIR /app
COPY requirements_azure.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY azure_router.py app.py
ENV PORT=8080
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "8", "--timeout", "120", "app:app"]
