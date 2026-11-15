import { Router } from 'express';
import multer from 'multer';

import type { DocumentRepository } from './documents.js';
import { IntelligenceError } from './intelligence/client.js';
import type { IntelligenceClient, IngestRequest } from './intelligence/client.js';

export interface UploadDependencies {
  intelligence: Pick<IntelligenceClient, 'ingest'>;
  documents: DocumentRepository;
  maxBytes?: number;
}

export function uploadRouter(dependencies: UploadDependencies): Router {
  const router = Router();
  const maxBytes = dependencies.maxBytes ?? Number(process.env.MAX_UPLOAD_BYTES ?? 20971520);
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) throw new Error('Invalid MAX_UPLOAD_BYTES');
  const upload = multer({
    storage: multer.memoryStorage(),
    limits: {
      fileSize: maxBytes,
      files: 1,
      fields: 1,
      fieldSize: 256,
    },
  });
  router.post('/v1/upload', (req, res, next) => {
    upload.single('file')(req, res, (error: unknown) => {
      if (error) {
        res
          .status(
            error instanceof multer.MulterError && error.code === 'LIMIT_FILE_SIZE' ? 413 : 400,
          )
          .json({ error: 'Upload rejected' });
        return;
      }
      if (!req.file) {
        res.status(400).json({ error: 'A file is required' });
        return;
      }
      const fields = req.body as Record<string, unknown>;
      const orgId = fields.org_id;
      if (orgId !== undefined && (typeof orgId !== 'string' || !/^[A-Za-z0-9_-]+$/.test(orgId))) {
        res.status(400).json({ error: 'Invalid org_id' });
        return;
      }
      const request: IngestRequest = {
        file: new Blob([new Uint8Array(req.file.buffer)], { type: req.file.mimetype }),
        ...(typeof orgId === 'string' ? { org_id: orgId } : {}),
      };
      const filename = req.file.originalname;
      void (async () => {
        const result = await dependencies.intelligence.ingest(request, filename);
        await dependencies.documents.save(result);
        res.status(202).json(result);
      })().catch((failure: unknown) => {
        if (failure instanceof IntelligenceError) {
          res
            .status(failure.status >= 400 && failure.status < 500 ? failure.status : 502)
            .json({ error: 'Intelligence ingestion failed' });
        } else next(failure);
      });
    });
  });
  return router;
}
