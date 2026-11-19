import { Router } from 'express';
import multer from 'multer';

import { auditContext } from './audit.js';
import type { AuditContext } from './audit.js';
import { principal } from './auth/routes.js';
import type { Guard } from './auth/routes.js';
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

export function uploadLimit(maxBytes = Number(process.env.MAX_UPLOAD_BYTES ?? 20971520)) {
  if (!Number.isSafeInteger(maxBytes + 1) || maxBytes <= 0)
    throw new Error('Invalid MAX_UPLOAD_BYTES');
  return maxBytes;
}

export function uploadRouter(
  dependencies: UploadDependencies,
  guard: Guard,
  extract?: (docId: string, audit: AuditContext) => Promise<ExtractionResult>,
  ownsDocument?: (docId: string, orgId: number) => Promise<boolean>,
): Router {
  const router = Router();
  const maxBytes = uploadLimit(dependencies.maxBytes);
  const upload = multer({
    storage: multer.memoryStorage(),
    limits: {
      fileSize: maxBytes + 1,
      files: 1,
      fields: 4,
      fieldSize: 256,
    },
  });
  async function indexDocument(docId: string, audit: AuditContext): Promise<IndexResult | null> {
    let indexed: IndexResult;
    try {
      indexed = await dependencies.intelligence.index({ doc_id: docId });
      if (indexed.doc_id !== docId) throw new Error('Index response document mismatch');
    } catch {
      await dependencies.documents.setStatus(docId, audit, 'failed', 'Indexing failed');
      return null;
    }
    await dependencies.documents.setStatus(docId, audit, 'indexed');
    return indexed;
  }
  if (ownsDocument) {
    router.post('/api/documents/:id/index', guard('documents:upload'), (req, res, next) => {
      const docId = req.params.id;
      const audit = auditContext(req, res, principal(res));
      void (async () => {
        if (typeof docId !== 'string' || !(await ownsDocument(docId, audit.orgId))) {
          res.status(404).json({ error: 'Document not found' });
          return;
        }
        const indexed = await indexDocument(docId, audit);
        if (!indexed) {
          res.status(502).json({ error: 'Indexing failed' });
          return;
        }
        try {
          res.json({ index_result: indexed, extraction_result: await extract?.(docId, audit) });
        } catch {
          res.status(502).json({ index_result: indexed, error: 'Obligation extraction failed' });
        }
      })().catch(next);
    });
  }
  router.post('/v1/upload', guard('documents:upload'), (req, res, next) => {
    upload.single('file')(req, res, (error: unknown) => {
      if (error) {
        const tooLarge = error instanceof multer.MulterError && error.code === 'LIMIT_FILE_SIZE';
        res.status(tooLarge ? 413 : 400).json({
          error: tooLarge ? 'File exceeds the upload size limit' : 'Upload rejected',
        });
        return;
      }
      if (!req.file) {
        res.status(400).json({ error: 'A file is required' });
        return;
      }
      const fields = req.body as Record<string, unknown>;
      const user = principal(res);
      const audit = auditContext(req, res, user);
      const orgId = fields.org_id;
      if (orgId !== undefined && (typeof orgId !== 'string' || !/^[A-Za-z0-9_-]+$/.test(orgId))) {
        res.status(400).json({ error: 'Invalid org_id' });
        return;
      }
      if (orgId !== undefined && orgId !== user.orgSlug) {
        res.status(403).json({ error: 'Organization does not match authenticated account' });
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
        org_id: user.orgSlug,
        ...(typeof fields.department === 'string' ? { department: fields.department } : {}),
        ...(typeof fields.doc_type === 'string' ? { doc_type: fields.doc_type } : {}),
        ...(docDate !== undefined ? { doc_date: docDate } : {}),
        index: false,
      };
      const filename = req.file.originalname;
      void (async () => {
        const result = await dependencies.intelligence.ingest(request, filename);
        await dependencies.documents.save(result, audit);
        const indexResults: IndexResult[] = [];
        const extractionResults: ExtractionResult[] = [];
        const extractionFailures: string[] = [];
        const indexFailures: string[] = [];
        const documents: ParseResult[] = [result.parse_result];
        for (const parsed of documents) {
          documents.push(...(parsed.attachments ?? []));
          const indexed = await indexDocument(parsed.document.id, audit);
          if (!indexed) {
            indexFailures.push(parsed.document.id);
            continue;
          }
          indexResults.push(indexed);
          if (extract) {
            try {
              extractionResults.push(await extract(parsed.document.id, audit));
            } catch {
              extractionFailures.push(parsed.document.id);
            }
          }
        }
        res.status(indexFailures.length || extractionFailures.length ? 502 : 202).json({
          ...result,
          index_results: indexResults,
          ...(extract ? { extraction_results: extractionResults } : {}),
          ...(extractionFailures.length
            ? {
                extraction_error: 'Some indexed documents failed obligation extraction.',
                extraction_failed_document_ids: extractionFailures,
              }
            : {}),
          ...(indexFailures.length
            ? {
                index_error: 'Some documents failed indexing; parsed documents remain stored.',
                index_failed_document_ids: indexFailures,
              }
            : {}),
        });
      })().catch((failure: unknown) => {
        if (failure instanceof IntelligenceError) {
          const message =
            failure.status === 415
              ? 'The file type or contents are not supported'
              : failure.status === 413
                ? 'The file or its decoded attachments exceed the upload size limit'
                : 'The document could not be parsed';
          res
            .status(failure.status >= 400 && failure.status < 500 ? failure.status : 502)
            .json({ error: message });
        } else next(failure);
      });
    });
  });
  return router;
}
