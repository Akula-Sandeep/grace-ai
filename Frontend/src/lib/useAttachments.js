import { useCallback, useRef, useState } from 'react';

export const MAX_ATTACHMENTS = 3;
export const MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024;
export const ATTACHMENT_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];

let nextId = 0;

/**
 * Image attachments for the composer (UI spec §29): validation, read progress,
 * failure + retry, cancel/remove. Files are read locally as data URLs and sent
 * with the message; nothing is uploaded before the user sends.
 */
export default function useAttachments() {
  const [items, setItems] = useState([]);
  const [notice, setNotice] = useState('');
  const readers = useRef(new Map());
  const countRef = useRef(0);

  const read = useCallback((id, file) => {
    const reader = new FileReader();
    readers.current.set(id, reader);
    setItems((current) => current.map((item) => (item.id === id ? { ...item, status: 'reading', progress: 0, error: '' } : item)));
    reader.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const progress = Math.round((event.loaded / event.total) * 100);
      setItems((current) => current.map((item) => (item.id === id ? { ...item, progress } : item)));
    };
    reader.onload = () => {
      readers.current.delete(id);
      setItems((current) => current.map((item) => (
        item.id === id ? { ...item, status: 'ready', progress: 100, dataUrl: String(reader.result || '') } : item
      )));
    };
    reader.onerror = () => {
      readers.current.delete(id);
      setItems((current) => current.map((item) => (
        item.id === id ? { ...item, status: 'error', error: 'Could not read this image.' } : item
      )));
    };
    reader.readAsDataURL(file);
  }, []);

  const add = useCallback((fileList) => {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const problems = [];
    const accepted = [];
    let count = countRef.current;
    for (const file of files) {
      if (!ATTACHMENT_TYPES.includes(file.type)) {
        problems.push(`${file.name || 'That file'} isn’t a supported image (PNG, JPEG, WebP or GIF).`);
        continue;
      }
      if (file.size > MAX_ATTACHMENT_BYTES) {
        problems.push(`${file.name || 'That image'} is larger than 4 MB.`);
        continue;
      }
      if (count >= MAX_ATTACHMENTS) {
        problems.push(`You can attach up to ${MAX_ATTACHMENTS} images.`);
        break;
      }
      count += 1;
      accepted.push({
        id: `attachment-${++nextId}`, file, name: file.name || 'Pasted image', size: file.size,
        status: 'reading', progress: 0, dataUrl: '', error: '',
      });
    }
    countRef.current = count;
    if (accepted.length) setItems((current) => [...current, ...accepted]);
    accepted.forEach((item) => read(item.id, item.file));
    setNotice(problems.join(' '));
  }, [read]);

  const remove = useCallback((id) => {
    readers.current.get(id)?.abort();
    readers.current.delete(id);
    setItems((current) => {
      const next = current.filter((item) => item.id !== id);
      countRef.current = next.length;
      return next;
    });
    setNotice('');
  }, []);

  const retry = useCallback((id) => {
    const item = items.find((candidate) => candidate.id === id);
    if (item) read(id, item.file);
  }, [items, read]);

  const clear = useCallback(() => {
    readers.current.forEach((reader) => reader.abort());
    readers.current.clear();
    countRef.current = 0;
    setItems([]);
    setNotice('');
  }, []);

  return {
    items,
    notice,
    add,
    remove,
    retry,
    clear,
    ready: items.filter((item) => item.status === 'ready').map((item) => item.dataUrl),
    pending: items.some((item) => item.status === 'reading'),
  };
}
