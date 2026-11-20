import { Pool } from 'pg';
import { limit, logEvent } from './runtime.js';

export function createDatabasePool(databaseUrl = process.env.DATABASE_URL): Pool {
  if (!databaseUrl) {
    throw new Error('DATABASE_URL is required');
  }
  // Bound acquisition time for readiness and fail disconnected clients predictably.
  const pool = new Pool({
    connectionString: databaseUrl,
    connectionTimeoutMillis: limit('DATABASE_CONNECT_TIMEOUT_MS', 2000, 100, 60000),
    max: limit('DATABASE_POOL_MAX', 10, 1, 50),
  });
  pool.on('error', () => logEvent('database_pool_error'));
  return pool;
}

export async function databaseReady(pool: Pool): Promise<void> {
  const client = await pool.connect();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;
  try {
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        timedOut = true;
        reject(new Error('Postgres readiness timed out'));
      }, 2000);
    });
    await Promise.race([client.query('SELECT 1'), timeout]);
  } finally {
    clearTimeout(timer);
    client.release(timedOut);
  }
}
