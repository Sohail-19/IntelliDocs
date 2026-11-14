import { createApp } from './app.js';

const port = Number.parseInt(process.env.CORE_PORT ?? '3000', 10);

createApp().listen(port, () => {
  console.log(`Core service listening on port ${port}`);
});
