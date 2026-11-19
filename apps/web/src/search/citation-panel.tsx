import { useQuery } from '@tanstack/react-query';
import { BookOpen, X } from 'lucide-react';
import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { Citation } from '../../../../contracts/core-api.v1.js';
import { ApiError } from '../api.js';
import { useAuth } from '../auth.js';

const PdfPage = lazy(() => import('./pdf-page.js'));

export function CitationChips({
  citations,
  open,
}: {
  citations: Citation[];
  open: (citation: Citation) => void;
}) {
  return (
    <ul className="citation-chips" aria-label="Citations">
      {citations.map((citation, index) => (
        <li key={citation.doc_id + ':' + citation.char_start + ':' + index}>
          <button className="citation-chip" type="button" onClick={() => open(citation)}>
            <BookOpen size={15} aria-hidden="true" /> {citationLabel(citation)}
          </button>
        </li>
      ))}
    </ul>
  );
}

export function citationLabel(citation: Citation): string {
  const first = citation.page_start ?? citation.page;
  const last = citation.page_end ?? first;
  const page = last > first ? 'Pages ' + first + '–' + last : 'Page ' + first;
  return citation.clause_number ? page + ' · Clause ' + citation.clause_number : page;
}

export function CitationPanel({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const first = citation.page_start ?? citation.page;
  const last = Math.max(first, citation.page_end ?? first);
  const [page, setPage] = useState(first);
  const dialog = useRef<HTMLDialogElement>(null);
  const { client, sessionId } = useAuth();
  const source = useQuery({
    queryKey: ['citation-pdf', sessionId, citation.doc_id],
    queryFn: () => client.sourcePdf(citation.doc_id),
    retry: false,
    gcTime: 0,
  });

  useEffect(() => {
    const element = dialog.current;
    const opener = document.activeElement;
    if (typeof element?.showModal === 'function') element.showModal();
    else element?.setAttribute('open', '');
    return () => {
      element?.close?.();
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, []);

  const unavailable =
    source.error instanceof ApiError &&
    (source.error.status === 404 || source.error.status === 415);

  return (
    <dialog
      className="citation-panel"
      ref={dialog}
      aria-labelledby="citation-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <header>
        <div>
          <h2 id="citation-title">Source citation</h2>
          <p>{citationLabel(citation)}</p>
        </div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close citation">
          <X size={21} aria-hidden="true" />
        </button>
      </header>
      <div className="citation-panel-body">
        {last > first && (
          <div className="citation-page-controls">
            <button
              className="secondary-button"
              type="button"
              disabled={page === first}
              onClick={() => setPage(page - 1)}
            >
              Previous page
            </button>
            <span aria-live="polite">Page {page}</span>
            <button
              className="secondary-button"
              type="button"
              disabled={page === last}
              onClick={() => setPage(page + 1)}
            >
              Next page
            </button>
          </div>
        )}
        {source.isPending && <p role="status">Loading source PDF…</p>}
        {source.isError && (
          <p role="status">
            {unavailable
              ? 'An original PDF is unavailable for this source.'
              : 'The source PDF could not be loaded.'}
          </p>
        )}
        {source.data && (
          <Suspense fallback={<p role="status">Opening PDF…</p>}>
            <PdfPage
              key={page}
              file={source.data}
              page={page}
              boxes={(citation.boxes ?? []).filter((box) => box.page === page)}
            />
          </Suspense>
        )}
        <figure className="citation-quote">
          <figcaption>Verified quote · {citationLabel(citation)}</figcaption>
          <blockquote>{citation.quote}</blockquote>
        </figure>
      </div>
    </dialog>
  );
}
