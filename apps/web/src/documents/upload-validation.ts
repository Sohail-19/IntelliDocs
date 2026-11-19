import formats from '../../../../contracts/document-upload-formats.v1.json';

export const acceptedExtensions = Object.keys(formats);

function readPrefix(file: File, length: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (!(reader.result instanceof ArrayBuffer)) {
        reject(new Error('File could not be read'));
        return;
      }
      const bytes = new Uint8Array(reader.result);
      resolve(Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join(''));
    };
    reader.onerror = () => reject(new Error('File could not be read', { cause: reader.error }));
    reader.readAsArrayBuffer(file.slice(0, length));
  });
}

export function fileSizeLabel(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 ** 2)
    return (bytes / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 }) + ' KiB';
  return (bytes / 1024 ** 2).toLocaleString(undefined, { maximumFractionDigits: 1 }) + ' MiB';
}

export async function validateFile(file: File, maxBytes: number): Promise<string | null> {
  const name = file.name.split(/[\\/]/).at(-1) ?? '';
  const point = name.lastIndexOf('.');
  const extension =
    point > 0 && /[^.]/.test(name.slice(0, point)) ? name.slice(point).toLowerCase() : '';
  if (!Object.hasOwn(formats, extension)) return 'Unsupported file type.';
  if (file.size > maxBytes) return 'Exceeds the ' + fileSizeLabel(maxBytes) + ' file limit.';
  if (file.size === 0) return 'The file is empty.';
  const signature = formats[extension as keyof typeof formats].signature;
  if (signature) {
    try {
      if ((await readPrefix(file, signature.length / 2)) !== signature) {
        return 'File contents do not match the extension.';
      }
    } catch {
      return 'The file could not be read.';
    }
  }
  return null;
}
