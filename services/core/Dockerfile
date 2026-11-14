FROM node:20-alpine AS dependencies

WORKDIR /app
COPY services/core/package.json services/core/package-lock.json ./
RUN npm ci

FROM dependencies AS build

COPY services/core/ ./
RUN npm run build

FROM node:20-alpine AS runtime

WORKDIR /app
ENV NODE_ENV=production
COPY services/core/package.json services/core/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
COPY contracts /contracts
COPY config /config

USER node
EXPOSE 3000
CMD ["node", "dist/server.js"]
