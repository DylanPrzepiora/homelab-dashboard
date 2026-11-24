FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER 1000:1000
EXPOSE 8443
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD ["python", "-c", "import ssl,urllib.request as u; c=ssl._create_unverified_context(); u.urlopen('https://127.0.0.1:8443/healthz', context=c, timeout=4)"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8443", "--ssl-keyfile", "/certs/key.pem", "--ssl-certfile", "/certs/cert.pem", "--no-server-header", "--no-access-log"]
