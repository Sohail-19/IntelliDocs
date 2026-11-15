import express from 'express';
import { uploadRouter } from './upload.js';
import type { UploadDependencies } from './upload.js';

export function createApp(dependencies?: UploadDependencies): express.Express {
  const app = express();
  app.use(express.json());
  app.get('/health', (_request, response) => {
    response.status(200).json({ status: 'ok' });
  });
  if (dependencies) app.use(uploadRouter(dependencies));
  app.use(
    (
      _error: unknown,
      _request: express.Request,
      response: express.Response,
      _next: express.NextFunction,
    ) => {
      void _next;
      response.status(500).json({ error: 'Document persistence failed' });
    },
  );
  return app;
}
