import { createApp } from './app.js';
import { createDatabasePool } from './database.js';
import { PostgresDocumentRepository } from './documents.js';
import { IntelligenceClient } from './intelligence/client.js';
import { PostgresObligationRepository } from './obligations.js';

const port = Number.parseInt(process.env.CORE_PORT ?? '3000', 10);
const pool = createDatabasePool();

createApp({
  intelligence: new IntelligenceClient(),
  documents: new PostgresDocumentRepository(pool),
  obligations: new PostgresObligationRepository(pool),
}).listen(port, () => {
  console.log(`Core service listening on port ${port}`);
});
