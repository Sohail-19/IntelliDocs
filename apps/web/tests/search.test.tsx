import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import type {
  Answer,
  AuthProfile,
  Citation,
  SearchOptions,
} from '../../../contracts/core-api.v1.js';
import { createClient } from '../src/api.js';
import { App } from '../src/app.js';
import { AuthProvider } from '../src/auth.js';

vi.mock('../src/search/pdf-page.js', () => ({
  default: ({ page, boxes }: { page: number; boxes: NonNullable<Citation['boxes']> }) => (
    <div role="img" aria-label={'PDF page ' + page} data-box-count={boxes.length} />
  ),
}));

const profile: AuthProfile = {
  user_id: 1,
  org_id: 'example',
  roles: ['reader'],
  permissions: ['documents:search', 'documents:answer'],
};
const options: SearchOptions = {
  departments: ['Legal'],
  document_types: ['Circular'],
  languages: ['en', 'hi'],
};
const withheld: Answer = {
  abstained: true,
  abstain_reason: 'The supplied sources do not establish the reporting date.',
  answer_text: '',
  claims: [],
  retrieved_chunk_ids: [],
  model: 'fixture',
  verifier_version: 'v1',
};
const citation: Citation = {
  doc_id: 'source-doc',
  page: 3,
  char_start: 100,
  char_end: 125,
  quote: 'File the report by Friday.',
  clause_number: '4.2',
};

async function show(answer: Answer, pdfStatus = 200, stale = false) {
  const requests: URL[] = [];
  const fetcher = vi.fn<typeof fetch>((input) => {
    if (typeof input !== 'string') throw new Error('Expected an API URL');
    const url = new URL(input);
    requests.push(url);
    switch (url.pathname) {
      case '/api/auth/login':
        return Promise.resolve(Response.json({ access_token: 'a', refresh_token: 'r' }));
      case '/api/auth/me':
        return Promise.resolve(Response.json(profile));
      case '/api/search/options':
        return Promise.resolve(Response.json(options));
      case '/api/answer':
        return Promise.resolve(Response.json(answer));
      case '/api/search':
        return Promise.resolve(
          Response.json({
            results: [
              {
                chunk: { id: 'i', text: 'The report must be filed on Friday.' },
                score: 0.895,
                citations: [citation],
                ...(stale ? { stale, authority_warnings: answer.authority_warnings } : {}),
              },
            ],
          }),
        );
      case '/api/documents/source-doc/source':
        return Promise.resolve(
          pdfStatus === 200
            ? new Response('%PDF-fixture', { headers: { 'content-type': 'application/pdf' } })
            : Response.json({ error: 'No original PDF' }, { status: pdfStatus }),
        );
      default:
        throw new Error(url.pathname);
    }
  });
  const client = createClient('https://core.example.test', fetcher);
  await client.login({ org_id: 'example', email: 'test@example.test', password: 'fixture' });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={['/search']}>
          <App />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  return { user, requests };
}

function answered(source: Citation): Answer {
  return {
    ...withheld,
    abstained: false,
    abstain_reason: '',
    answer_text: 'A model-generated string that is never displayed.',
    claims: [
      { text: '<b>File the report by Friday.</b>', status: 'verified', citations: [source] },
      { text: 'Unverified material', status: 'unverified', citations: [source] },
    ],
  };
}

async function ask(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Ask' }));
  await user.type(screen.getByRole('searchbox', { name: 'Question' }), 'When is the report due?');
  await user.click(screen.getByRole('button', { name: 'Ask documents' }));
}

it('shows a clear abstention and the server reason', async () => {
  const { user, requests } = await show(withheld);
  await screen.findByRole('option', { name: 'hi' });
  await user.selectOptions(
    await screen.findByRole('combobox', { name: 'Document language' }),
    'hi',
  );
  await ask(user);
  expect(await screen.findByRole('heading', { name: 'The answer was withheld' })).toBeVisible();
  expect(screen.getByText(withheld.abstain_reason)).toBeVisible();
  const params = requests.find(({ pathname }) => pathname === '/api/answer')?.searchParams;
  expect(params?.get('language')).toBe('en');
  expect(params?.get('filters[language]')).toBe('hi');
});

it('opens the cited page with only the boxes for that page and renders verified text literally', async () => {
  const source = {
    ...citation,
    page_end: 4,
    boxes: [
      { page: 3, bbox: [10, 10, 50, 30] },
      { page: 4, bbox: [30, 50, 90, 75] },
    ],
  };
  const { user } = await show(answered(source));
  await ask(user);
  expect(await screen.findByText('<b>File the report by Friday.</b>')).toBeVisible();
  expect(screen.queryByText('Unverified material')).not.toBeInTheDocument();
  expect(
    screen.queryByText('A model-generated string that is never displayed.'),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Pages 3–4 · Clause 4.2' }));
  expect(await screen.findByRole('dialog', { name: 'Source citation' })).toBeVisible();
  expect(await screen.findByRole('img', { name: 'PDF page 3' })).toHaveAttribute(
    'data-box-count',
    '1',
  );
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  expect(await screen.findByRole('img', { name: 'PDF page 4' })).toHaveAttribute(
    'data-box-count',
    '1',
  );
});

it.each([200, 415])(
  'shows the verified quote and page when boxes are missing (source status %i)',
  async (status) => {
    const { user } = await show(answered(citation), status);
    await ask(user);
    await user.click(await screen.findByRole('button', { name: 'Page 3 · Clause 4.2' }));
    expect(await screen.findByText('Verified quote · Page 3 · Clause 4.2')).toBeVisible();
    expect(screen.getByText(citation.quote)).toBeVisible();
    if (status === 200) {
      expect(await screen.findByRole('img', { name: 'PDF page 3' })).toHaveAttribute(
        'data-box-count',
        '0',
      );
    } else {
      expect(
        await screen.findByText('An original PDF is unavailable for this source.'),
      ).toBeVisible();
      expect(screen.queryByRole('img', { name: 'PDF page 3' })).not.toBeInTheDocument();
    }
  },
);

it('shows the matching chunk, score, and page with org-configured filters', async () => {
  const { user, requests } = await show(withheld);
  await screen.findByRole('option', { name: 'Legal' });
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Department' }), 'Legal');
  await user.selectOptions(screen.getByRole('combobox', { name: 'Document type' }), 'Circular');
  await user.type(screen.getByRole('searchbox', { name: 'Search query' }), 'report');
  await user.click(screen.getByRole('button', { name: 'Search documents' }));
  expect(await screen.findByText('The report must be filed on Friday.')).toBeVisible();
  expect(screen.getByText('Score 0.895')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Page 3 · Clause 4.2' })).toBeVisible();
  const params = requests.find(({ pathname }) => pathname === '/api/search')?.searchParams;
  expect(params?.get('filters[department]')).toBe('Legal');
  expect(params?.get('filters[doc_type]')).toBe('Circular');
});

it('shows the newer source link in search and alongside an abstained answer', async () => {
  const { user } = await show(
    {
      ...withheld,
      stale_sources: true,
      authority_warnings: [
        {
          doc_id: citation.doc_id,
          newer_doc_id: 'newer-doc',
          relation_id: 'r1',
          status: 'superseded',
          message: 'This document is superseded.',
        },
      ],
    },
    200,
    true,
  );
  await user.type(await screen.findByRole('searchbox', { name: 'Search query' }), 'report');
  await user.click(screen.getByRole('button', { name: 'Search documents' }));
  expect(await screen.findByText('Stale source warning')).toBeVisible();
  expect(screen.getByRole('link', { name: 'View newer document' })).toHaveAttribute(
    'href',
    '/documents/newer-doc',
  );
  await user.click(screen.getByRole('button', { name: 'Ask' }));
  await user.click(screen.getByRole('button', { name: 'Ask documents' }));
  expect(await screen.findByRole('heading', { name: 'The answer was withheld' })).toBeVisible();
  expect(screen.getByText('Stale source warning')).toBeVisible();
  expect(screen.getByRole('link', { name: 'View newer document' })).toHaveAttribute(
    'href',
    '/documents/newer-doc',
  );
});
