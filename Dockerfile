FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --system --no-create-home appuser
USER appuser

EXPOSE 8000
# --no-access-log: Tokens stehen in der URL und dürfen nicht im Log landen.
# --proxy-headers: echte Client-IP hinter Nginx Proxy Manager (für Rate-Limits);
# vertraut wird nur FORWARDED_ALLOW_IPS (siehe docker-compose.yml).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]
