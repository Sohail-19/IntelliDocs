import { createApp } from './app.js';
import { createDatabasePool } from './database.js';
import { PostgresDocumentRepository } from './documents.js';
import { IntelligenceClient } from './intelligence/client.js';

const port = Number.parseInt(process.env.CORE_PORT ?? '3000', 10);

createApp({
  intelligence: new IntelligenceClient(),
  documents: new PostgresDocumentRepository(createDatabasePool()),
}).listen(port, () => {
  console.log(`Core service listening on port ${port}`);
});
