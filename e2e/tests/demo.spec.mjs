import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const fixture = JSON.parse(readFileSync(new URL('../fixture.json', import.meta.url), 'utf8'));
const email = 'demo-admin@example.test';
const org = 'example';

function fixturePdf(text) {
  const literal = text.replace(/[()\\]/g, '\\$&');
  const content = 'BT /F1 16 Tf 40 650 Td (' + literal + ') Tj ET';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 810 760] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    '<< /Length ' + Buffer.byteLength(content) + ' >>\nstream\n' + content + '\nendstream',
  ];
  let pdf = '%PDF-1.4\n';
  const offsets = objects.map((object, index) => {
    const offset = Buffer.byteLength(pdf);
    pdf += index + 1 + ' 0 obj\n' + object + '\nendobj\n';
    return offset;
  });
  const xref = Buffer.byteLength(pdf);
  pdf += 'xref\n0 6\n0000000000 65535 f \n';
  pdf += offsets.map((offset) => String(offset).padStart(10, '0') + ' 00000 n \n').join('');
  pdf += 'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + xref + '\n%%EOF\n';
  return Buffer.from(pdf);
}

async function upload(page, fixture, text = fixture.quote) {
  await page.getByLabel('Browse files').setInputFiles({
    name: fixture.filename,
    mimeType: 'application/pdf',
    buffer: fixturePdf(text),
  });
  await page.getByLabel('Department').selectOption('finance');
  await page.getByLabel('Document type').selectOption('invoice');
  if (fixture.date) await page.getByLabel('Document date').fill(fixture.date);
  await page.getByRole('button', { name: 'Upload file', exact: true }).click();
  const row = page.getByRole('row').filter({ hasText: fixture.filename });
  await expect(row.getByText('Ready', { exact: true })).toBeVisible({ timeout: 40_000 });
}

test('completes an obligation and flags a task after confirming a circular amendment', async ({
  page,
  request,
}) => {
  const { AUTH_BOOTSTRAP_TOKEN, DEMO_PASSWORD, DEMO_CORE_URL, DEMO_WEB_URL } = process.env;
  if (!AUTH_BOOTSTRAP_TOKEN || !DEMO_PASSWORD || !DEMO_CORE_URL || !DEMO_WEB_URL) {
    throw new Error('Use bash e2e/run-compose.sh to start the isolated demo stack');
  }
  const bootstrap = await request.post(DEMO_CORE_URL + '/api/auth/register', {
    data: {
      org_id: org,
      email,
      password: DEMO_PASSWORD,
      name: 'Demo administrator',
      department: 'finance',
      bootstrap_token: AUTH_BOOTSTRAP_TOKEN,
    },
  });
  expect(bootstrap.status()).toBe(201);

  await page.goto('/login');
  await page.getByLabel('Organization ID').fill(org);
  await page.getByLabel('Email address').fill(email);
  await page.getByLabel('Password', { exact: true }).fill(DEMO_PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.getByRole('link', { name: 'Documents' }).click();
  await upload(page, fixture);

  await page.getByRole('link', { name: 'Search', exact: true }).click();
  await page.getByRole('searchbox', { name: 'Search query' }).fill('reconciliation ledger');
  await page.getByRole('button', { name: 'Search documents' }).click();
  await expect(page.getByText(fixture.quote)).toBeVisible();
  await page.getByRole('button', { name: 'Page 1', exact: true }).click();
  const citation = page.getByRole('dialog', { name: 'Source citation' });
  await expect(citation.locator('canvas')).toBeVisible();
  await expect(citation.getByLabel('Cited block highlights')).toBeVisible();
  await expect(citation.getByText(fixture.quote, { exact: false })).toBeVisible();
  await citation.getByRole('button', { name: 'Close citation' }).click();

  await page.getByRole('link', { name: 'Task feed' }).click();
  await page.getByRole('tab', { name: 'Department queue' }).click();
  const task = page.getByRole('row').filter({ hasText: fixture.action });
  await expect(task.getByText('2099-12-31')).toBeVisible();
  await task.getByRole('button', { name: 'Acknowledge' }).click();
  await task.getByRole('button', { name: 'Start', exact: true }).click();
  await task.getByRole('button', { name: 'Mark done' }).click();
  await expect(task.getByText('Done', { exact: true })).toBeVisible();

  await page.getByRole('link', { name: 'Audit trail' }).click();
  for (const action of [
    'auth.login',
    'document.upload',
    'document.index',
    'obligations.extraction',
    'obligation.route',
  ]) {
    await expect(page.getByRole('rowheader', { name: action, exact: true })).toBeVisible();
  }
  await page.getByLabel('Action', { exact: true }).fill('task.transition');
  await page.getByRole('button', { name: 'Apply filters' }).click();
  const changes = page
    .getByRole('row')
    .filter({ has: page.getByRole('rowheader', { name: 'task.transition', exact: true }) });
  await expect(changes).toHaveCount(3);
  await changes.first().getByText('View changes', { exact: true }).click();
  const diff = changes.first().getByRole('table', { name: 'Changed fields' });
  await expect(diff.getByText('"in_progress"', { exact: true })).toBeVisible();
  await expect(diff.getByText('"done"', { exact: true })).toBeVisible();

  await page.getByRole('link', { name: 'Documents', exact: true }).click();
  await upload(page, fixture.original, '1. ' + fixture.original.quote);
  await upload(page, fixture.amendment, '1. ' + fixture.amendment.quote);
  await page.getByRole('link', { name: 'Relations', exact: true }).click();
  const relation = page.getByRole('article', { name: 'Amends' });
  const newer = relation.getByRole('region', { name: 'Newer document' });
  const older = relation.getByRole('region', { name: 'Older document' });
  await expect(newer.getByText(fixture.amendment.quote)).toBeVisible();
  await expect(older.getByText(fixture.original.quote)).toBeVisible();
  await older.getByRole('button', { name: /Page 1/ }).click();
  await expect(citation.locator('canvas')).toBeVisible();
  await citation.getByRole('button', { name: 'Close citation' }).click();
  await relation.getByRole('button', { name: 'Confirm relation' }).click();
  await expect(page.getByText('Relation confirmed.', { exact: true })).toBeVisible();
  await expect(relation.getByText('confirmed', { exact: true })).toBeVisible();

  await page.getByRole('link', { name: 'Documents', exact: true }).click();
  await page.getByRole('link', { name: fixture.original.filename }).click();
  await expect(page.getByText('Amended', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'View newer document' }).click();
  await expect(page.getByRole('heading', { name: fixture.amendment.filename })).toBeVisible();

  await page.getByRole('link', { name: 'Task feed' }).click();
  await page.getByRole('tab', { name: 'Stale tasks' }).click();
  const stale = page.getByRole('row').filter({ hasText: fixture.original.action });
  await expect(stale.getByText('Stale · review required')).toBeVisible();
  await expect(stale.getByRole('button', { name: 'Acknowledge' })).toHaveCount(0);
  await expect(stale.getByRole('button', { name: 'Keep task' })).toBeVisible();
  await expect(stale.getByRole('button', { name: 'Update task' })).toBeVisible();
  await expect(stale.getByRole('button', { name: 'Close task' })).toBeVisible();
});
