# 使用官方 Python 3.12 輕量版
FROM python:3.12-slim

# 設定時區與禁用 Python 標準輸出緩衝 (確保 docker logs 即時吐出)
ENV TZ=Asia/Taipei
ENV PYTHONUNBUFFERED=1

# 設定工作目錄
WORKDIR /app

# 先複製並安裝依賴 (利用 Docker 緩存機制加速後續 Build)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製原始碼進入容器的 src 目錄
COPY src/ ./src/

# 指定啟動指令 (直接執行，不透過 shell，確保 SIGTERM 精準攔截)
CMD ["python", "src/main.py"]
