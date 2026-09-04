FROM node:24-alpine AS dependencies

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

FROM node:24-alpine

ENV NODE_ENV=production \
    PORT=3003

WORKDIR /app

COPY --from=dependencies /app/node_modules ./node_modules
COPY --chown=node:node package.json server.mjs index.html script.js styles.css ./
COPY --chown=node:node surau-logo.png surau-aerial.png ./

USER node

EXPOSE 3003

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3003/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "server.mjs"]
