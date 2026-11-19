import { Router } from 'express';
import multer from 'multer';

import type { DocumentRepository } from './documents.js';
import { IntelligenceError } from './intelligence/client.js';
import type {
  IntelligenceClient,
  ExtractionResult,
  IngestRequest,
  IndexResult,
  ParseResult,
} from './intelligence/client.js';

export interface UploadDependencies {
  intelligence: Pick<IntelligenceClient, 'ingest' | 'index'>;
  documents: DocumentRepository;
  maxBytes?: number;
}

export function uploadRouter(
  dependencies: UploadDependencies,
  extract?: (docId: string) => Promise<ExtractionResult>,
): Router {
  const router = Router();
  const maxBytes = dependencies.maxBytes ?? Number(process.env.MAX_UPLOAD_BYTES ?? 20971520);
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) throw new Error('Invalid MAX_UPLOAD_BYTES');
  const upload = multer({
    storage: multer.memoryStorage(),
    limits: {
      fileSize: maxBytes,
      files: 1,
      fields: 4,
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
      for (const name of ['department', 'doc_type', 'doc_date']) {
        const value = fields[name];
        if (value !== undefined && (typeof value !== 'string' || !value.trim())) {
          res.status(400).json({ error: `Invalid ${name}` });
          return;
        }
      }
      const docDate = fields.doc_date as string | undefined;
      if (
        docDate !== undefined &&
        (!/^\d{4}-\d{2}-\d{2}$/.test(docDate) ||
          Number.isNaN(Date.parse(docDate)) ||
          new Date(docDate).toISOString().slice(0, 10) !== docDate)
      ) {
        res.status(400).json({ error: 'Invalid doc_date' });
        return;
      }
      const request: IngestRequest = {
        file: new Blob([new Uint8Array(req.file.buffer)], { type: req.file.mimetype }),
        ...(typeof orgId === 'string' ? { org_id: orgId } : {}),
        ...(typeof fields.department === 'string' ? { department: fields.department } : {}),
        ...(typeof fields.doc_type === 'string' ? { doc_type: fields.doc_type } : {}),
        ...(docDate !== undefined ? { doc_date: docDate } : {}),
        index: false,
      };
      const filename = req.file.originalname;
      void (async () => {
        const result = await dependencies.intelligence.ingest(request, filename);
        await dependencies.documents.save(result);
        const indexResults: IndexResult[] = [];
        const extractionResults: ExtractionResult[] = [];
        const extractionFailures: string[] = [];
        let failed = false;
        const documents: ParseResult[] = [result.parse_result];
        for (const parsed of documents) {
          documents.push(...(parsed.attachments ?? []));
          try {
            const indexed = await dependencies.intelligence.index({ doc_id: parsed.document.id });
            if (indexed.doc_id !== parsed.document.id)
              throw new Error('Index response document mismatch');
            await dependencies.documents.setStatus(parsed.document.id, 'indexed');
            indexResults.push(indexed);
          } catch {
            failed = true;
            await dependencies.documents.setStatus(parsed.document.id, 'failed', 'Indexing failed');
            continue;
          }
          if (extract) {
            try {
              extractionResults.push(await extract(parsed.document.id));
            } catch {
              extractionFailures.push(parsed.document.id);
            }
          }
        }
        res.status(failed || extractionFailures.length ? 502 : 202).json({
          ...result,
          index_results: indexResults,
          ...(extract ? { extraction_results: extractionResults } : {}),
          ...(extractionFailures.length
            ? {
                extraction_error: 'Some indexed documents failed obligation extraction.',
                extraction_failed_document_ids: extractionFailures,
              }
            : {}),
          ...(failed
            ? { index_error: 'Some documents failed indexing; parsed documents remain stored.' }
            : {}),
        });
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
