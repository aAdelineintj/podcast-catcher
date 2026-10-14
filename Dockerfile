# syntax=docker/dockerfile:1

############################################################
# 1) 构建 Next.js 前端
############################################################
FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /fe
ENV NEXT_TELEMETRY_DISABLED=1

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY tsconfig.json next-env.d.ts postcss.config.js tailwind.config.ts next.config.ts ./
COPY src ./src
# scripts/ 里有 PocketBay 的构建/启动钩子；本阶段显式调用 build:next，
# 不依赖 scripts/pb-build.sh（那是 Alpine 单服务路径用的），但一并拷入以防被引用。
COPY scripts ./scripts
RUN npm run build:next

############################################################
# 2) 运行镜像：FastAPI + Next.js 同容器
#    PocketBay 是单服务架构，公网入口只指向一个容器，
#    因此这里把两个进程放进同一容器，由 Next.js 反代到本机 FastAPI。
############################################################
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXT_TELEMETRY_DISABLED=1 \
    BACKEND_ORIGIN=http://127.0.0.1:8000 \
    BACKEND_PORT=8000

# node 二进制依赖 libstdc++6；HTTPS 调用腾讯云/百度需要 ca-certificates
RUN apt-get update \
 && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# 复用 node 官方镜像里的 node 运行时
COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-builder /usr/local/lib/node_modules /usr/local/lib/node_modules

WORKDIR /app

# 后端依赖（先装依赖再拷源码，利用层缓存）
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 后端源码 + 本地数据目录兜底
COPY backend ./backend
RUN mkdir -p /app/data/input /app/data/output

# 前端运行所需产物
COPY --from=frontend-builder /fe/.next ./.next
COPY --from=frontend-builder /fe/node_modules ./node_modules
COPY --from=frontend-builder /fe/package.json ./package.json
COPY --from=frontend-builder /fe/next.config.ts ./next.config.ts
COPY --from=frontend-builder /fe/next-env.d.ts ./next-env.d.ts

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 平台注入 PORT；本地默认 3000
EXPOSE 3000

CMD ["/usr/local/bin/docker-entrypoint.sh"]
