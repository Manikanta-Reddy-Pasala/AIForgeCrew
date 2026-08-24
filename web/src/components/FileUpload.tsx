import { useRef, useState } from 'react';
import { clickable } from '../a11y';

export const MAX_FILE_BYTES = 5 * 1024 * 1024;  // 5MB hard cap per file

export function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

// Reads a File object as base64 (without the data:...;base64, prefix).
// The backend AttachedFile model expects raw base64 only.
export async function readAsBase64(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = '';
  // Chunk to keep String.fromCharCode within safe arg-count limits on
  // larger files (~16MB+ would otherwise blow the stack).
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(
      null, Array.from(bytes.subarray(i, i + CHUNK)) as unknown as number[],
    );
  }
  return btoa(binary);
}

// KISS drag-and-drop dropzone — wraps a native ``<input type="file">``
// so click-to-pick still works alongside drag. The browser sets
// ``dataTransfer.files`` on drop; we forward both surfaces to the
// same onFiles handler.
export function DropZone({ onFiles }: { onFiles: (files: File[]) => void }) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [hover, setHover] = useState(false);
  return (
    <div
      onDragOver={e => { e.preventDefault(); setHover(true); }}
      onDragLeave={() => setHover(false)}
      onDrop={e => {
        e.preventDefault();
        setHover(false);
        const dropped = Array.from(e.dataTransfer?.files || []);
        if (dropped.length) onFiles(dropped);
      }}
      {...clickable(() => inputRef.current?.click())}
      aria-label="Attach files: drag them here, or activate to browse"
      style={{
        border: hover
          ? '2px dashed var(--accent, #4a90e2)'
          : '2px dashed var(--border-1, #ccc)',
        background: hover ? 'var(--bg-2)' : 'var(--bg-1)',
        padding: 16,
        borderRadius: 6,
        textAlign: 'center',
        cursor: 'pointer',
        transition: 'background 120ms, border-color 120ms',
        userSelect: 'none',
      }}
    >
      <div style={{ fontSize: 14, opacity: 0.8 }}>
        {hover
          ? 'Release to attach'
          : 'Drag files here, or click to browse'}
      </div>
      <input
        ref={inputRef}
        type="file"
        multiple
        style={{ display: 'none' }}
        onChange={e => {
          const picked = Array.from(e.target.files || []);
          if (picked.length) onFiles(picked);
          e.target.value = '';
        }}
      />
    </div>
  );
}
