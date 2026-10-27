# 基础镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 先复制依赖文件并安装 (利用缓存，只要 requirements 没变就不重复安装)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制所有代码
COPY . .

# 暴露端口
EXPOSE 5000

# 默认启动 Flask (Worker 和 Beat 会在 docker-compose 里覆盖这个命令)
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "backend.app:app", "--timeout", "300"]