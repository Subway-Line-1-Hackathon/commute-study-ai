FROM python:3.13-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY fixtures fixtures

# Railway 내부망은 IPv6를 쓰는 환경이 있어 :: 로 받는다. PORT는 Railway가 넣어준다.
CMD ["sh", "-c", "uvicorn app.main:app --host :: --port ${PORT:-8000}"]
