---
title: IntelliDocs Intelligence
emoji: 📄
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 8000
base_path: /health
license: mit
---

# IntelliDocs Intelligence

This is the API for the IntelliDocs demo. `/health` checks the process and
`/ready` checks its configured dependencies. The authenticated Core service
calls the `/v1` endpoints. Use the IntelliDocs web app for document uploads and
search.
