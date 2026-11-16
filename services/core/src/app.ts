import express from 'express';
import type { IntelligenceClient } from './intelligence/client.js';
import { searchRouter } from './search.js';
import { uploadRouter } from './upload.js';
import type { UploadDependencies } from './upload.js';

type AppDependencies = UploadDependencies & {
  intelligence: Partial<Pick<IntelligenceClient, 'search'>>;
};

export function createApp(dependencies?: AppDependencies): express.Express {
  const app = express();
  app.use(express.json());
  app.get('/health', (_request, response) => {
    response.status(200).json({ status: 'ok' });
  });
  if (dependencies) {
    app.use(uploadRouter(dependencies));
    const { intelligence } = dependencies;
    if (intelligence.search) app.use(searchRouter(intelligence.search.bind(intelligence)));
  }
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
