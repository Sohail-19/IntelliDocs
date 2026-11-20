import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, CircleAlert, FileText, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { Link, useOutletContext, useParams } from 'react-router-dom';
import type { AuthProfile, DocumentRecord } from '../../../../contracts/core-api.v1.js';
import { useAuth } from '../auth.js';
import { AuthorityBadge } from '../relations/authority.js';
import { EmptyState, ErrorState, LoadingState } from '../states.js';
import {
  StatusBadge,
  documentError,
  documentStatus,
  errorMessage,
  isProcessing,
  useDocumentRetry,
} from './document-state.js';
import { UploadPanel } from './upload-panel.js';
import { ForYouPanel } from './for-you-panel.js';
import './documents.css';

const pollMs = 1500;

function DateValue({ value }: { value: string | null }) {
  if (!value) return <span className="document-muted">—</span>;
  return <time dateTime={value}>{value.slice(0, 10)}</time>;
}

export function DocumentsPage() {
  const { client, sessionId } = useAuth();
  const cache = useQueryClient();
  const [uploading, setUploading] = useState(false);
  const retry = useDocumentRetry();
  const options = useQuery({
    queryKey: ['upload-options', sessionId],
    queryFn: client.uploadOptions,
    retry: false,
  });
  const list = useQuery({
    queryKey: ['documents', sessionId],
    queryFn: client.documents,
    retry: false,
    refetchInterval: (query) =>
      uploading || retry.isPending || query.state.data?.documents.some(isProcessing)
        ? pollMs
        : false,
  });

  return (
    <div className="documents-page">
      <div className="document-page-heading">
        <h1>Documents</h1>
        <p className="page-description">
          Upload documents and track indexing and obligation extraction.
        </p>
      </div>
      {options.isPending && <LoadingState label="Loading upload options…" />}
      {options.isError && (
        <ErrorState
          title="Upload options could not load"
          description={errorMessage(options.error, 'Check your connection.')}
          action={
            <button
              className="secondary-button"
              type="button"
              onClick={() => void options.refetch()}
            >
              Try again
            </button>
          }
        />
      )}
      {options.data && (
        <UploadPanel
          options={options.data}
          onBusy={setUploading}
          onDocumentChange={() => {
            void cache.invalidateQueries({ queryKey: ['documents', sessionId] });
          }}
        />
      )}
      <section className="document-card document-list" aria-labelledby="documents-list-heading">
        <div className="document-card-heading documents-list-heading">
          <div>
            <h2 id="documents-list-heading">Document library</h2>
            <p>Open a document to see its metadata and obligations.</p>
          </div>
          {list.data && (
            <span className="document-count">
              {list.data.documents.length}{' '}
              {list.data.documents.length === 1 ? 'document' : 'documents'}
            </span>
          )}
        </div>
        {list.isPending && <LoadingState label="Loading documents…" />}
        {list.isError && (
          <ErrorState
            title="Documents could not load"
            description={errorMessage(list.error, 'Check your connection.')}
            action={
              <button
                className="secondary-button"
                type="button"
                onClick={() => void list.refetch()}
              >
                Try again
              </button>
            }
          />
        )}
        {list.data?.documents.length === 0 && (
          <EmptyState
            title="No documents yet"
            description="Upload a file to start processing it."
          />
        )}
        {!!list.data?.documents.length && (
          <div className="document-table-wrap">
            <table className="document-table">
              <thead>
                <tr>
                  <th scope="col">Document</th>
                  <th scope="col">Status</th>
                  <th scope="col">Authority</th>
                  <th scope="col">Language</th>
                  <th scope="col">Document date</th>
                </tr>
              </thead>
              <tbody>
                {list.data.documents.map((document) => {
                  const retrying = retry.isPending && retry.variables === document.id;
                  const failed = documentStatus(document) === 'failed';
                  return (
                    <tr key={document.id}>
                      <th scope="row">
                        <div className="document-file">
                          <span className="document-file-icon">
                            <FileText size={18} aria-hidden="true" />
                          </span>
                          <div>
                            <Link to={'/documents/' + encodeURIComponent(document.id)}>
                              {document.filename}
                            </Link>
                            <span className="document-muted">
                              {[document.department, document.doc_type]
                                .filter(Boolean)
                                .join(' · ') || (document.parent_document_id ? 'Attachment' : '')}
                            </span>
                          </div>
                        </div>
                      </th>
                      <td aria-live="polite">
                        <StatusBadge document={document} retrying={retrying} />
                        {!retrying && (
                          <small className="document-muted">
                            {failed
                              ? documentError(document)
                              : isProcessing(document)
                                ? 'Processing…'
                                : 'Ready'}
                          </small>
                        )}
                        {failed && !retrying && (
                          <button
                            className="text-link retry-link"
                            type="button"
                            aria-label={'Retry processing ' + document.filename}
                            disabled={retry.isPending}
                            onClick={() => retry.mutate(document.id)}
                          >
                            <RefreshCw size={14} aria-hidden="true" /> Retry
                          </button>
                        )}
                        {retry.isError && retry.variables === document.id && (
                          <p className="document-row-error" role="alert">
                            {errorMessage(retry.error, 'Retry could not be completed.')}
                          </p>
                        )}
                      </td>
                      <td>
                        {document.authority_status ? (
                          <AuthorityBadge status={document.authority_status} />
                        ) : (
                          '—'
                        )}
                      </td>
                      <td>{document.languages.join(', ') || '—'}</td>
                      <td>
                        <DateValue value={document.doc_date} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function ProcessingSteps({ document, retrying }: { document: DocumentRecord; retrying: boolean }) {
  const status = documentStatus(document);
  const indexStep =
    document.status === 'indexed'
      ? 'Complete'
      : retrying || status === 'parsed'
        ? 'Processing'
        : 'Failed';
  const extractionStep =
    status === 'extracted'
      ? 'Complete'
      : status === 'failed' && document.extraction_status === 'failed' && !retrying
        ? 'Failed'
        : document.status === 'indexed'
          ? 'Processing'
          : 'Waiting';
  return (
    <ol className="processing-steps">
      {(
        [
          ['Parsed', 'Complete'],
          ['Indexed', indexStep],
          ['Obligations extracted', extractionStep],
        ] as const
      ).map(([title, state]) => (
        <li key={title}>
          <span className={'step-icon step-' + state.toLowerCase()}>
            {state === 'Complete' ? (
              <Check size={17} aria-hidden="true" />
            ) : state === 'Failed' ? (
              <CircleAlert size={17} aria-hidden="true" />
            ) : (
              <span className="step-dot" />
            )}
          </span>
          <strong>{title}</strong>
          <span className="document-muted">{state}</span>
        </li>
      ))}
    </ol>
  );
}

export function DocumentDetail() {
  const { id = '' } = useParams();
  const profile = useOutletContext<AuthProfile>();
  const { client, sessionId } = useAuth();
  const retry = useDocumentRetry();
  const documentQuery = useQuery({
    queryKey: ['documents', sessionId, id],
    queryFn: async () => (await client.document(id)).document,
    retry: false,
    refetchInterval: (query) =>
      retry.isPending || (query.state.data && isProcessing(query.state.data)) ? pollMs : false,
  });
  const document = documentQuery.data;
  const canReadObligations = profile.permissions.includes('obligations:read');
  const obligations = useQuery({
    queryKey: ['documents', sessionId, id, 'obligations', document?.extraction_status],
    queryFn: () => client.documentObligations(id),
    enabled: !!document && canReadObligations,
    retry: false,
  });
  const failed = document ? documentStatus(document) === 'failed' : false;

  return (
    <div className="documents-page">
      {profile.permissions.includes('documents:upload') && (
        <Link to="/documents" className="document-back">
          Back to documents
        </Link>
      )}
      {documentQuery.isPending && <LoadingState label="Loading document…" />}
      {documentQuery.isError && (
        <ErrorState
          title="Document could not load"
          description={errorMessage(documentQuery.error, 'Check your connection.')}
          action={
            <button
              className="secondary-button"
              type="button"
              onClick={() => void documentQuery.refetch()}
            >
              Try again
            </button>
          }
        />
      )}
      {document && (
        <>
          <div className="document-page-heading document-detail-heading">
            <div>
              <h1>{document.filename}</h1>
              <p className="page-description">
                Uploaded <DateValue value={document.created_at} />
              </p>
              {document.authority_status && <AuthorityBadge status={document.authority_status} />}
            </div>
            <StatusBadge document={document} retrying={retry.isPending} />
          </div>
          <ForYouPanel
            key={JSON.stringify([
              document.id,
              document.authority_status,
              document.authority_warnings?.map(({ relation_id }) => relation_id),
            ])}
            document={document}
          />
          {failed && !retry.isPending && (
            <ErrorState
              title="Document processing failed"
              description={documentError(document)}
              action={
                profile.permissions.includes('documents:upload') ? (
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => retry.mutate(id)}
                  >
                    Retry processing
                  </button>
                ) : undefined
              }
            />
          )}
          {retry.isError && (
            <ErrorState
              title="Retry failed"
              description={errorMessage(retry.error, 'Retry could not be completed.')}
            />
          )}
          <div className="document-detail-grid">
            <section className="document-card" aria-labelledby="metadata-heading">
              <div className="document-card-heading">
                <h2 id="metadata-heading">Metadata</h2>
              </div>
              <dl className="document-metadata">
                <div>
                  <dt>Department</dt>
                  <dd>{document.department ?? '—'}</dd>
                </div>
                <div>
                  <dt>Document type</dt>
                  <dd>{document.doc_type ?? '—'}</dd>
                </div>
                <div>
                  <dt>Document date</dt>
                  <dd>
                    <DateValue value={document.doc_date} />
                  </dd>
                </div>
                <div>
                  <dt>Language</dt>
                  <dd>{document.languages.join(', ') || '—'}</dd>
                </div>
                <div>
                  <dt>File format</dt>
                  <dd>{document.mime_type ?? '—'}</dd>
                </div>
                <div>
                  <dt>Pages</dt>
                  <dd>{document.page_count ?? '—'}</dd>
                </div>
                {document.parent_document_id && (
                  <div>
                    <dt>Parent document</dt>
                    <dd>
                      <Link
                        className="text-link"
                        to={'/documents/' + encodeURIComponent(document.parent_document_id)}
                      >
                        View parent
                      </Link>
                    </dd>
                  </div>
                )}
              </dl>
            </section>
            <section className="document-card" aria-labelledby="processing-heading">
              <div className="document-card-heading">
                <h2 id="processing-heading">Processing steps</h2>
              </div>
              <ProcessingSteps document={document} retrying={retry.isPending} />
            </section>
          </div>
          {canReadObligations && (
            <section className="document-card" aria-labelledby="extracted-heading">
              <div className="document-card-heading">
                <h2 id="extracted-heading">Extracted obligations</h2>
              </div>
              {obligations.isPending && <LoadingState label="Loading obligations…" />}
              {obligations.isError && (
                <ErrorState
                  title="Obligations could not load"
                  description={errorMessage(obligations.error, 'Check your connection.')}
                  action={
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => void obligations.refetch()}
                    >
                      Try again
                    </button>
                  }
                />
              )}
              {obligations.data?.obligations.length === 0 && (
                <EmptyState
                  title={
                    isProcessing(document) || retry.isPending
                      ? 'Extraction is pending'
                      : 'No obligations to display'
                  }
                />
              )}
              {!!obligations.data?.obligations.length && (
                <ul className="document-obligations">
                  {obligations.data.obligations.map((item) => (
                    <li key={item.id}>
                      <h3>{item.action}</h3>
                      <p className="document-muted">
                        {[
                          item.owner.resolved_role ||
                            item.owner.resolved_department ||
                            item.owner.raw_text,
                          item.deadline.normalized_date || item.deadline.raw_text,
                          item.status.replaceAll('_', ' '),
                        ]
                          .filter(Boolean)
                          .join(' · ')}
                      </p>
                      {item.citations.length > 0 && (
                        <details>
                          <summary>
                            {item.citations.length}{' '}
                            {item.citations.length === 1 ? 'source citation' : 'source citations'}
                          </summary>
                          {item.citations.map((citation, index) => (
                            <blockquote key={index}>
                              <p>{citation.quote}</p>
                              <cite>
                                Page {citation.page_start ?? citation.page}
                                {citation.clause_number
                                  ? ' · Clause ' + citation.clause_number
                                  : ''}
                              </cite>
                            </blockquote>
                          ))}
                        </details>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
