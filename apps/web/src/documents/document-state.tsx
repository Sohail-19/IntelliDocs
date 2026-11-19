import { useMutation, useQueryClient } from '@tanstack/react-query';
import { CircleAlert, Clock, FileCheck2, Layers3 } from 'lucide-react';
import type { DocumentRecord } from '../../../../contracts/core-api.v1.js';
import { ApiError } from '../api.js';
import { useAuth } from '../auth.js';

export function documentStatus(document: DocumentRecord) {
  if (document.status === 'failed' || document.extraction_status === 'failed') return 'failed';
  if (document.extraction_status === 'completed') return 'extracted';
  return document.status;
}

export function isProcessing(document: DocumentRecord) {
  const status = documentStatus(document);
  return status === 'parsed' || status === 'indexed';
}

const statusLabels = {
  parsed: { label: 'Parsed', icon: Clock },
  indexed: { label: 'Indexed', icon: Layers3 },
  extracted: { label: 'Extracted', icon: FileCheck2 },
  failed: { label: 'Failed', icon: CircleAlert },
} as const;

export function StatusBadge({
  document,
  retrying = false,
}: {
  document: DocumentRecord;
  retrying?: boolean;
}) {
  const status = retrying ? 'indexed' : documentStatus(document);
  const { label, icon: Icon } = statusLabels[status];
  return (
    <span className={'document-status document-status-' + status}>
      <Icon size={15} aria-hidden="true" />
      {retrying ? 'Processing' : label}
    </span>
  );
}

export function documentError(document: DocumentRecord): string {
  return document.index_error ?? document.extraction_error ?? 'Processing failed. Try again.';
}

export function errorMessage(error: unknown, fallback: string) {
  return error instanceof ApiError ? error.message : fallback;
}

export function useDocumentRetry() {
  const { client, sessionId } = useAuth();
  const cache = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => client.retryDocument(id),
    onSettled: () => cache.invalidateQueries({ queryKey: ['documents', sessionId] }),
  });
}
