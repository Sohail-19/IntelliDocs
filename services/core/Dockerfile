FROM node:20-alpine AS dependencies

WORKDIR /app
COPY services/core/package.json services/core/package-lock.json ./
RUN apk add --no-cache python3 make g++ && npm ci

FROM dependencies AS build

COPY services/core/ ./
COPY contracts /contracts
RUN npm run build && npm prune --omit=dev

FROM node:20-alpine AS runtime

WORKDIR /app
ENV NODE_ENV=production
COPY services/core/package.json services/core/package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY contracts /contracts
COPY config /config

USER node
EXPOSE 3000
CMD ["node", "dist/server.js"]
