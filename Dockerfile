FROM python:3.13-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY fixtures fixtures

# --host ""는 IPv4와 IPv6 소켓을 둘 다 연다. Railway 헬스체크는 IPv4로, 내부망은 환경에 따라 IPv6로 들어온다.
# "::"로 열면 파이썬이 IPv6 전용 소켓으로 만들어 IPv4 요청을 받지 못한다. PORT는 Railway가 넣어준다.
CMD ["sh", "-c", "uvicorn app.main:app --host '' --port ${PORT:-8000}"]
