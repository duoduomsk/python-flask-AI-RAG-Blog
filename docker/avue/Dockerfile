# 阶段 1：打包编译前端
FROM node:18-alpine AS builder
WORKDIR /app
COPY avue-cli/package*.json ./
RUN npm ci
COPY avue-cli/ ./
RUN npm run build

# 阶段 2：用 Nginx 运行
FROM nginx:alpine
# 复制刚刚编译出来的 dist 文件夹
COPY --from=builder /app/dist /usr/share/nginx/html
# 覆盖 Nginx 配置
COPY docker/avue/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]