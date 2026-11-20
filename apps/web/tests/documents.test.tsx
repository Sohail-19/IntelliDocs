import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import type {
  AuthProfile,
  AuthTokens,
  DocumentRecord,
  DocumentSummary,
  DocumentUploadOptions,
} from '../../../contracts/core-api.v1.js';
import { createClient } from '../src/api.js';
import { App } from '../src/app.js';
import { AuthProvider } from '../src/auth.js';

const access: AuthTokens = {
  access_token: 'preview',
  refresh_token: 'refresh',
  token_type: 'Bearer',
  expires_in: 60,
};
const profile: AuthProfile = {
  user_id: 1,
  org_id: 'example',
  roles: ['preview-author'],
  permissions: ['documents:upload', 'obligations:read'],
};
const options: DocumentUploadOptions = {
  departments: ['Legal', 'Operations'],
  document_types: ['Circular', 'Agreement'],
  max_upload_bytes: 32,
  upload_defaults: {},
};
const ready: DocumentRecord = {
  id: 'doc-one',
  filename: 'contract.pdf',
  department: 'Legal',
  doc_type: 'Agreement',
  doc_date: '2026-11-19',
  mime_type: 'application/pdf',
  languages: ['en', 'hi'],
  page_count: 3,
  parent_document_id: null,
  status: 'indexed',
  extraction_status: 'completed',
  index_error: null,
  extraction_error: null,
  created_at: '2026-11-20T00:00:00Z',
  indexed_at: '2026-11-20T00:00:12Z',
};

function json(body: unknown, status = 200) {
  return Promise.resolve(Response.json(body, { status }));
}

async function show(
  path: string,
  handler: (path: string, init?: RequestInit) => Promise<Response>,
  authProfile = profile,
) {
  const fetcher = vi.fn<typeof fetch>((url, init) => {
    const parsed = new URL(typeof url === 'string' ? url : '');
    const endpoint = parsed.pathname;
    if (endpoint === '/api/auth/login') return json(access);
    if (endpoint === '/api/auth/me') return json(authProfile);
    if (endpoint === '/api/documents/upload-options') return json(options);
    return handler(endpoint + parsed.search, init);
  });
  const client = createClient('https://core.example.test', fetcher);
  await client.login({ org_id: 'example', email: 'fixture@example.test', password: 'fixture' });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return fetcher;
}

it('checks dropped files against the accepted extension, signature, and server size limit', async () => {
  const fetcher = await show('/documents', () => json({ documents: [] }));
  const drop = await screen.findByText('Drag and drop files here');
  fireEvent.drop(drop, {
    dataTransfer: {
      files: [
        new File(['x'], 'notes.exe'),
        new File(['%PDF-' + 'x'.repeat(40)], 'large.pdf'),
        new File(['not a PDF'], 'wrong.pdf'),
      ],
    },
  });
  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent('notes.exe — Unsupported file type');
  expect(alert).toHaveTextContent('large.pdf — Exceeds the 32 B file limit');
  expect(alert).toHaveTextContent('wrong.pdf — File contents do not match');
  expect(screen.getByRole('button', { name: 'Upload files' })).toBeDisabled();
  expect(
    fetcher.mock.calls.some(([url]) => typeof url === 'string' && url.endsWith('/v1/upload')),
  ).toBe(false);
});

it('uploads multiple files with configured metadata and exposes saved failures without re-uploading', async () => {
  let pending = Promise.resolve();
  let finish = () => {};
  pending = new Promise<void>((resolve) => {
    finish = resolve;
  });
  const uploads: FormData[] = [];
  let documents: DocumentRecord[] = [];
  await show('/documents', async (path, init) => {
    if (path === '/api/documents') return json({ documents });
    if (path !== '/v1/upload' || !(init?.body instanceof FormData)) throw new Error(path);
    expect(new Headers(init.headers).get('Content-Type')).toBeNull();
    uploads.push(init.body);
    if (uploads.length === 1) {
      await pending;
      documents = [ready];
      return json({ document_id: ready.id }, 202);
    }
    documents = [
      ...documents,
      {
        ...ready,
        id: 'doc-two',
        filename: 'memo.pdf',
        status: 'failed',
        index_error: 'Indexing failed',
      },
    ];
    return json({ document_id: 'doc-two', index_failed_document_ids: ['doc-two'] }, 502);
  });
  const user = userEvent.setup();
  await user.selectOptions(await screen.findByLabelText('Department'), 'Operations');
  await user.selectOptions(screen.getByLabelText('Document type'), 'Circular');
  await user.upload(screen.getByLabelText('Browse files'), [
    new File(['%PDF-x'], 'contract.pdf'),
    new File(['%PDF-y'], 'memo.pdf'),
  ]);
  await user.click(await screen.findByRole('button', { name: 'Upload 2 files' }));
  expect(await screen.findByText('Processing 1 of 2 · contract.pdf')).toBeInTheDocument();
  finish();
  const row = await screen.findByRole('row', { name: /memo.pdf/ });
  expect(within(row).getByText('Failed')).toBeVisible();
  expect(within(row).getByRole('button', { name: 'Retry processing memo.pdf' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Retry upload' })).not.toBeInTheDocument();
  expect(uploads.map((form) => [form.get('department'), form.get('doc_type')])).toEqual([
    ['Operations', 'Circular'],
    ['Operations', 'Circular'],
  ]);
});

it('polls parsed and indexed documents until extraction is ready', async () => {
  let reads = 0;
  await show('/documents', (path) => {
    if (path !== '/api/documents') throw new Error(path);
    reads++;
    return json({
      documents: [
        reads === 1
          ? { ...ready, status: 'parsed', extraction_status: 'pending' }
          : reads === 2
            ? { ...ready, extraction_status: 'pending' }
            : ready,
      ],
    });
  });
  expect(await screen.findByText('Parsed', { exact: true })).toBeVisible();
  expect(await screen.findByText('Indexed', { exact: true }, { timeout: 3000 })).toBeVisible();
  expect(await screen.findByText('Extracted', { exact: true }, { timeout: 3000 })).toBeVisible();
  expect(screen.getByText('Ready')).toBeVisible();
  expect(screen.getByText('en, hi')).toBeVisible();
  expect(screen.getByText('2026-11-19')).toBeVisible();
}, 8000);

it('shows a processing error on the detail page and reloads obligations after retry', async () => {
  let completed = false;
  const fetcher = await show('/documents/doc-one', (path) => {
    switch (path) {
      case '/api/documents/doc-one':
        return json({
          document: completed
            ? ready
            : { ...ready, extraction_status: 'failed', extraction_error: 'Extraction unavailable' },
        });
      case '/api/documents/doc-one/index':
        completed = true;
        return json({});
      case '/api/documents/doc-one/summary':
        return json({ summary: null, authority_warnings: [] });
      case '/api/documents/doc-one/obligations':
        return json({
          extraction_status: completed ? 'completed' : 'failed',
          extraction_error: completed ? null : 'Extraction unavailable',
          obligations: completed
            ? [
                {
                  id: 'obl-one',
                  action: 'File the quarterly report',
                  status: 'verified',
                  owner: { raw_text: 'Legal', resolved_department: 'Legal', resolved_role: null },
                  deadline: { raw_text: '30 November', normalized_date: '2026-11-30' },
                  citations: [{ page: 2, clause_number: '4', quote: 'File the quarterly report.' }],
                },
              ]
            : [],
        });
      default:
        throw new Error(path);
    }
  });
  expect(await screen.findByRole('heading', { name: 'contract.pdf' })).toBeVisible();
  expect(screen.getByRole('alert')).toHaveTextContent('Extraction unavailable');
  expect(screen.getByRole('heading', { name: 'Processing steps' })).toBeVisible();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Retry processing' }));
  expect(await screen.findByRole('heading', { name: 'File the quarterly report' })).toBeVisible();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledWith(
    'https://core.example.test/api/documents/doc-one/index',
    expect.objectContaining({ method: 'POST' }),
  );
});

const source = {
  doc_id: 'doc-one',
  page: 2,
  char_start: 0,
  char_end: 15,
  quote: 'File a report.',
  clause_number: '4',
};
const summary: NonNullable<DocumentSummary['summary']> = {
  doc_id: 'doc-one',
  audience: { department: 'Legal' },
  language: 'en',
  points: [
    {
      text: 'File a report — 2026-11-30',
      obligation_id: 'obl-one',
      due_date: '2026-11-30',
      citations: [source],
    },
    { text: 'Reports must include expenses', citations: [source] },
  ],
  not_relevant: false,
  relevance_reason: '',
  generated_by: 'example',
  verifier_version: 'example',
};
const reader = { ...profile, permissions: ['documents:upload'] };

it('loads the reader summary automatically with obligations, source chips, and the newer document link', async () => {
  let finish: (response: Response) => void = () => {};
  const pending = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  await show(
    '/documents/doc-one',
    (path) => {
      if (path === '/api/documents/doc-one') return json({ document: ready });
      if (path === '/api/documents/doc-one/summary') return pending;
      if (path === '/api/documents/doc-one/source') return json({ error: 'No PDF' }, 415);
      throw new Error(path);
    },
    reader,
  );
  const panel = within(await screen.findByRole('region', { name: 'For you' }));
  expect(panel.getByRole('status')).toHaveTextContent('Loading summary');
  finish(
    Response.json({
      summary,
      authority_warnings: [
        {
          doc_id: 'doc-one',
          newer_doc_id: 'newer',
          relation_id: 'rel',
          status: 'partially_amended',
          message: 'Newer document amends the original.',
        },
      ],
    }),
  );
  expect(await panel.findByText('Reports must include expenses')).toBeVisible();
  expect(panel.getByRole('heading', { name: 'Your obligations' })).toBeVisible();
  expect(panel.getByText('2026-11-30')).toHaveAttribute('datetime', '2026-11-30');
  expect(panel.getByRole('link', { name: 'View newer document' })).toHaveAttribute(
    'href',
    '/documents/newer',
  );
  expect(panel.queryByRole('button', { name: 'Preview summary' })).not.toBeInTheDocument();
  await userEvent.setup().click(panel.getAllByRole('button', { name: 'Page 2 · Clause 4' })[0]!);
  const dialog = within(await screen.findByRole('dialog', { name: 'Source citation' }));
  expect(dialog.getByText('File a report.')).toBeVisible();
});

it('shows an error and retries to an empty state', async () => {
  let reads = 0;
  await show(
    '/documents/doc-one',
    (path) => {
      if (path === '/api/documents/doc-one') return json({ document: ready });
      if (path !== '/api/documents/doc-one/summary') throw new Error(path);
      if (++reads === 1) return json({ error: 'Summary timeout' }, 502);
      return json({ summary: null, authority_warnings: [] });
    },
    reader,
  );
  const panel = within(await screen.findByRole('region', { name: 'For you' }));
  expect(await panel.findByRole('alert')).toHaveTextContent('Summary timeout');
  await userEvent.setup().click(panel.getByRole('button', { name: 'Try again' }));
  expect(await panel.findByRole('heading', { name: 'No summary available' })).toBeVisible();
});

it('lets a manager deliberately preview another audience and return to their own summary', async () => {
  const fetcher = await show(
    '/documents/doc-one',
    (path) => {
      if (path === '/api/documents/doc-one') return json({ document: ready });
      if (path === '/api/documents/doc-one/summary')
        return json({
          summary,
          authority_warnings: [],
          preview_options: {
            departments: ['Legal', 'Operations'],
            roles: [{ name: 'operator', department: 'Operations' }],
          },
        });
      if (path === '/api/documents/doc-one/summary?department=Operations&role=operator')
        return json({
          summary: {
            ...summary,
            audience: { department: 'Operations', role: 'operator' },
            points: [],
            not_relevant: true,
            relevance_reason: 'This document sets no operating procedures.',
          },
          authority_warnings: [],
        });
      throw new Error(path);
    },
    { ...reader, permissions: [...reader.permissions, 'documents:summary-preview'] },
  );
  const user = userEvent.setup();
  await user.selectOptions(await screen.findByLabelText('Preview department'), 'Operations');
  await user.selectOptions(screen.getByLabelText('Preview role'), 'operator');
  expect(
    fetcher.mock.calls.some(([url]) => typeof url === 'string' && url.includes('?department=')),
  ).toBe(false);
  await user.click(screen.getByRole('button', { name: 'Preview summary' }));
  expect(await screen.findByRole('heading', { name: 'Not relevant to your role' })).toBeVisible();
  expect(screen.getByText('This document sets no operating procedures.')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Back to my summary' }));
  expect(screen.getByText('Reports must include expenses')).toBeVisible();
});
