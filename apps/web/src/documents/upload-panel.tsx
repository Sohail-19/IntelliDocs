import { CloudUpload, FileText, LoaderCircle, X } from 'lucide-react';
import { useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link } from 'react-router-dom';
import type {
  DocumentUploadFields,
  DocumentUploadOptions,
} from '../../../../contracts/core-api.v1.js';
import { useAuth } from '../auth.js';
import { ErrorState } from '../states.js';
import { errorMessage } from './document-state.js';
import { acceptedExtensions, fileSizeLabel, validateFile } from './upload-validation.js';

type SelectedFile = { id: number; file: File };
type FailedFile = SelectedFile & { fields: DocumentUploadFields; error: string };

export function UploadPanel({
  options,
  onBusy,
  onDocumentChange,
}: {
  options: DocumentUploadOptions;
  onBusy: (busy: boolean) => void;
  onDocumentChange: () => void;
}) {
  const { client, sessionId } = useAuth();
  const inputId = useId();
  const nextId = useRef(0);
  const [files, setFiles] = useState<SelectedFile[]>([]);
  const [invalid, setInvalid] = useState<string[]>([]);
  const [failed, setFailed] = useState<FailedFile[]>([]);
  const [storedFailures, setStoredFailures] = useState<{ doc: string; filename: string }[]>([]);
  const [validating, setValidating] = useState(0);
  const [dragOver, setDragOver] = useState(false);
  const [progress, setProgress] = useState<{
    index: number;
    total: number;
    filename: string;
  } | null>(null);
  const [fields, setFields] = useState<DocumentUploadFields>({});
  const busy = progress !== null;

  async function addFiles(input: File[]) {
    if (busy || input.length === 0) return;
    setValidating((value) => value + 1);
    const checked = await Promise.all(
      input.map(async (file) => ({
        id: ++nextId.current,
        file,
        error: await validateFile(file, options.max_upload_bytes),
      })),
    );
    setFiles((existing) => [...existing, ...checked.filter(({ error }) => error === null)]);
    setInvalid((existing) => [
      ...existing,
      ...checked.flatMap(({ error, file }) => (error ? [file.name + ' — ' + error] : [])),
    ]);
    setValidating((value) => value - 1);
  }

  async function upload(batch: SelectedFile[], metadata: DocumentUploadFields) {
    onBusy(true);
    try {
      for (const [index, item] of batch.entries()) {
        if (client.sessionId() !== sessionId) break;
        setProgress({ index: index + 1, total: batch.length, filename: item.file.name });
        try {
          const receipt = await client.uploadDocument(item.file, metadata);
          const failedIds = [
            ...(receipt.index_failed_document_ids ?? []),
            ...(receipt.extraction_failed_document_ids ?? []),
          ];
          if (failedIds.length) {
            const issues = [...new Set(failedIds)].map((doc) => ({
              doc,
              filename: item.file.name,
            }));
            setStoredFailures((existing) => [...existing, ...issues]);
          }
        } catch (error) {
          setFailed((existing) => [
            ...existing,
            {
              ...item,
              fields: metadata,
              error: errorMessage(error, 'Upload could not be completed.'),
            },
          ]);
        } finally {
          onDocumentChange();
        }
      }
    } finally {
      setProgress(null);
      onBusy(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!files.length || busy || validating) return;
    setFiles([]);
    setInvalid([]);
    void upload(files, fields);
  }

  return (
    <section className="document-card upload-card" aria-labelledby="upload-heading">
      <div className="document-card-heading">
        <h2 id="upload-heading">Upload documents</h2>
        <p>Add files and choose metadata for this batch.</p>
      </div>
      <form onSubmit={submit}>
        <div className="upload-grid">
          <div
            className="drop-zone"
            data-active={dragOver}
            onDragOver={(event) => {
              event.preventDefault();
              if (!busy) setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragOver(false);
              void addFiles(Array.from(event.dataTransfer.files));
            }}
          >
            <div className="upload-symbol">
              <CloudUpload size={27} aria-hidden="true" />
            </div>
            <p>Drag and drop files here</p>
            <span className="upload-limits" id={inputId + '-formats'}>
              PDF, PNG, JPG, DOCX, EML or MSG · up to {fileSizeLabel(options.max_upload_bytes)} each
            </span>
            <label className="secondary-button file-picker">
              Browse files
              <input
                className="sr-only"
                type="file"
                multiple
                accept={acceptedExtensions.join(',')}
                aria-describedby={inputId + '-formats'}
                disabled={busy}
                onChange={(event) => {
                  void addFiles(Array.from(event.currentTarget.files ?? []));
                  event.currentTarget.value = '';
                }}
              />
            </label>
          </div>
          <fieldset className="upload-metadata" disabled={busy}>
            <label className="field">
              Department
              <select
                value={fields.department ?? ''}
                onChange={(event) => setFields({ ...fields, department: event.target.value })}
              >
                <option value="">
                  {options.upload_defaults.department ?? 'Select department (optional)'}
                </option>
                {options.departments.map((department) => (
                  <option key={department}>{department}</option>
                ))}
              </select>
            </label>
            <label className="field">
              Document type
              <select
                value={fields.doc_type ?? ''}
                onChange={(event) => setFields({ ...fields, doc_type: event.target.value })}
              >
                <option value="">
                  {options.upload_defaults.doc_type ?? 'Select document type (optional)'}
                </option>
                {options.document_types.map((type) => (
                  <option key={type}>{type}</option>
                ))}
              </select>
            </label>
            <label className="field">
              Document date
              <input
                type="date"
                value={fields.doc_date ?? options.upload_defaults.doc_date ?? ''}
                onChange={(event) => setFields({ ...fields, doc_date: event.target.value })}
              />
            </label>
          </fieldset>
        </div>
        {files.length > 0 && (
          <ul className="selected-files" aria-label="Selected files">
            {files.map(({ id, file }) => (
              <li key={id}>
                <FileText size={16} aria-hidden="true" />
                <span>{file.name}</span>
                <button
                  type="button"
                  className="icon-button"
                  aria-label={'Remove ' + file.name}
                  onClick={() =>
                    setFiles((existing) => existing.filter((fileItem) => fileItem.id !== id))
                  }
                >
                  <X size={16} aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}
        {invalid.length > 0 && (
          <ErrorState
            title="Some files could not be added"
            action={
              <>
                <ul>
                  {invalid.map((message, index) => (
                    <li key={index}>{message}</li>
                  ))}
                </ul>
                <button type="button" className="text-link" onClick={() => setInvalid([])}>
                  Dismiss
                </button>
              </>
            }
          />
        )}
        <div className="document-card-footer">
          <p role="status">
            {progress ? (
              <>
                Processing {progress.index} of {progress.total} · {progress.filename}
              </>
            ) : validating ? (
              'Checking files…'
            ) : files.length ? (
              files.length + ' selected'
            ) : (
              ''
            )}
          </p>
          <button
            className="primary-button"
            type="submit"
            disabled={busy || !!validating || !files.length}
          >
            {busy ? (
              <LoaderCircle size={17} className="motion-safe:animate-spin" aria-hidden="true" />
            ) : (
              <CloudUpload size={17} aria-hidden="true" />
            )}
            Upload{' '}
            {files.length > 1 ? files.length + ' files' : files.length === 1 ? 'file' : 'files'}
          </button>
        </div>
      </form>
      {failed.map((item) => (
        <ErrorState
          key={item.id}
          title={'Upload failed · ' + item.file.name}
          description={item.error}
          action={
            <button
              type="button"
              className="secondary-button"
              disabled={busy}
              onClick={() => {
                setFailed((existing) => existing.filter(({ id }) => id !== item.id));
                void upload([item], item.fields);
              }}
            >
              Retry upload
            </button>
          }
        />
      ))}
      {storedFailures.length > 0 && (
        <ErrorState
          title="Some documents need a processing retry"
          action={
            <>
              <ul>
                {storedFailures.map(({ doc, filename }, index) => (
                  <li key={index}>
                    <Link className="text-link" to={'/documents/' + encodeURIComponent(doc)}>
                      {filename} · view status
                    </Link>
                  </li>
                ))}
              </ul>
              <button type="button" className="text-link" onClick={() => setStoredFailures([])}>
                Dismiss
              </button>
            </>
          }
        />
      )}
    </section>
  );
}
