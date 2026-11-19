import { createRequire } from 'node:module';

import { PGlite } from '@electric-sql/pglite';
import type { MigrationBuilder } from 'node-pg-migrate';
import type { Pool } from 'pg';

const require = createRequire(import.meta.url);
const { default: Builder } = require('node-pg-migrate/dist/migrationBuilder') as {
  default: new (
    database: object,
    shorthands: object,
    decamelize: boolean,
    logger: Console,
  ) => MigrationBuilder & { getSql(): string };
};

export function createTestDatabase() {
  const db = new PGlite();
  const connection = {
    async query(sql: string, values?: unknown[]) {
      const result = await db.query(sql, values);
      return { rows: result.rows, rowCount: result.affectedRows ?? result.rows.length };
    },
    release() {},
  };
  const pool = { ...connection, connect: () => Promise.resolve(connection) } as unknown as Pool;
  async function apply(migrate: (builder: MigrationBuilder) => void): Promise<void> {
    const builder = new Builder({}, {}, false, console);
    migrate(builder);
    await db.exec(builder.getSql());
  }
  return { db, pool, apply };
}
