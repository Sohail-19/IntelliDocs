import worker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { useEffect, useRef, useState } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import type { Citation } from '../../../../contracts/core-api.v1.js';

pdfjs.GlobalWorkerOptions.workerSrc = worker;
const pdfOptions = { isEvalSupported: false, enableXfa: false, useSystemFonts: true };

type Boxes = NonNullable<Citation['boxes']>;

export default function PdfPage({ file, page, boxes }: { file: Blob; page: number; boxes: Boxes }) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(450);
  const [geometry, setGeometry] = useState<{ width: number; height: number }>();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const update = () => {
      if (element.clientWidth > 0) setWidth(element.clientWidth);
    };
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const highlights = geometry
    ? boxes.flatMap(({ bbox }) => {
        const [x0, y0, x1, y1] = bbox;
        if (
          x0 === undefined ||
          y0 === undefined ||
          x1 === undefined ||
          y1 === undefined ||
          ![x0, y0, x1, y1].every(Number.isFinite) ||
          x0 < 0 ||
          y0 < 0 ||
          x1 <= x0 ||
          y1 <= y0 ||
          x0 >= geometry.width ||
          y0 >= geometry.height
        )
          return [];
        return [
          {
            left: (x0 / geometry.width) * 100 + '%',
            top: (y0 / geometry.height) * 100 + '%',
            width: ((Math.min(x1, geometry.width) - x0) / geometry.width) * 100 + '%',
            height: ((Math.min(y1, geometry.height) - y0) / geometry.height) * 100 + '%',
          },
        ];
      })
    : [];

  return (
    <div ref={container} className="citation-pdf" aria-label={'PDF page ' + page}>
      {failed ? (
        <p role="status">The PDF page could not be displayed.</p>
      ) : (
        <Document
          file={file}
          options={pdfOptions}
          onLoadError={() => setFailed(true)}
          loading={<p role="status">Opening PDF…</p>}
        >
          <div className="citation-pdf-page">
            <Page
              pageNumber={page}
              width={width}
              renderAnnotationLayer={false}
              renderTextLayer={false}
              loading={<p role="status">Opening page…</p>}
              onLoadSuccess={(loaded) => setGeometry(loaded.getViewport({ scale: 1 }))}
              onLoadError={() => setFailed(true)}
              onRenderError={() => setFailed(true)}
            />
            {highlights.length > 0 && (
              <div className="citation-highlights" aria-label="Cited block highlights">
                {highlights.map((style, index) => (
                  <span key={index} style={style} />
                ))}
              </div>
            )}
          </div>
        </Document>
      )}
    </div>
  );
}
